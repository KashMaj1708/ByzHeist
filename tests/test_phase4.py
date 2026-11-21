"""Phase 4 tests: Blue watchdog, Red planner/adapter, rate/model guards, and live smoke test."""

from __future__ import annotations

import asyncio

import pytest

from byzheist import config, referee, telemetry
from byzheist.agents.blue import BLUE_DECISION_QUESTIONS, BlueWatchdog, NeonVerdict
from byzheist.agents.gemini import (
    DecisionAnswer,
    FakeGemini,
    GeminiBackend,
    RateLimitExceededError,
    has_api_key,
    image_part,
    text_part,
)
from byzheist.agents.red import RedAttacker
from byzheist.pbft.cluster import Cluster


FAST_KWARGS = {
    "latency_range_ms": (1.5, 4.5),
    "view_change_timeout_ms": 250.0,
    "client_timeout_ms": 200.0,
}


async def _wait_until(predicate, timeout_s: float = 5.0, poll_s: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("Condition did not become true before timeout")
        await asyncio.sleep(poll_s)


@pytest.mark.asyncio
async def test_proof_based_auto_quarantine_of_equivocating_primary_within_10s() -> None:
    cluster = Cluster(seed=401, **FAST_KWARGS)
    await cluster.start()
    fake = FakeGemini()
    blue_sb = cluster.budget.blue_interface(telemetry_fn=lambda: telemetry.snapshot(cluster))
    blue = BlueWatchdog(sandbox=blue_sb, gemini=fake, interval_sec=0.15)
    try:
        old_s0_vk = bytes(cluster.peer_keys["S0"])
        assert cluster.budget.set_fault("S0", "equivocate")

        req = cluster.client.create_request("A0", "A1", 10)
        cluster.network.send(cluster.client.client_id, "S0", req)

        await blue.start()
        await _wait_until(
            lambda: len(cluster.replace_events) >= 1
            and cluster.replace_events[0]["replica_id"] == "S0",
            timeout_s=5.0,
        )

        # S0 was convicted by cryptographic proof, quarantined, and replaced with a fresh key
        assert bytes(cluster.peer_keys["S0"]) != old_s0_vk
        assert "S0" not in cluster.quarantined
        assert blue.latest_explanation != ""
        assert any(ev.get("mode") == "proof_conviction" for ev in blue.events)

        # Cluster continues committing cleanly across all 4 replicas
        rep = await cluster.submit("A2", "A3", 5, timeout_s=2.5)
        assert rep.result == "ok"
        verdict = referee.check(cluster)
        assert verdict["total_ok"] is True
        assert verdict["agreement_ok"] is True
    finally:
        await blue.stop()
        await cluster.stop()


@pytest.mark.asyncio
async def test_decision_path_creates_pending_not_executed_quarantine_for_slow() -> None:
    cluster = Cluster(
        seed=402,
        latency_range_ms=(2.0, 5.0),
        view_change_timeout_ms=350.0,
        client_timeout_ms=600.0,
    )
    await cluster.start()
    fake = FakeGemini()
    blue_sb = cluster.budget.blue_interface(telemetry_fn=lambda: telemetry.snapshot(cluster))
    blue = BlueWatchdog(sandbox=blue_sb, gemini=fake, interval_sec=1.0)
    try:
        assert cluster.budget.set_fault("S0", "slow", params={"delay_ms": 200.0})
        for _ in range(3):
            await cluster.submit("A0", "A1", 2, timeout_s=2.0)

        verdict = await blue.step()
        assert verdict["auto_quarantined"] is False
        assert cluster.quarantined == set()
        assert cluster.replace_events == []
        assert blue.pending_quarantine is not None
        assert blue.pending_quarantine["suspect"] == "S0"
        assert blue.pending_quarantine["attack_type"] == "slow"

        # Human operator approves pending quarantine -> quarantine + replace executes
        approved = blue.approve_pending()
        assert approved is not None and approved["replica_id"] == "S0"
        assert blue.pending_quarantine is None
        assert len(cluster.replace_events) == 1
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_frame_alone_never_causes_auto_quarantine() -> None:
    cluster = Cluster(seed=403, **FAST_KWARGS)
    await cluster.start()
    fake = FakeGemini()
    blue_sb = cluster.budget.blue_interface(telemetry_fn=lambda: telemetry.snapshot(cluster))
    blue = BlueWatchdog(
        sandbox=blue_sb,
        gemini=fake,
        interval_sec=0.5,
        neon_min_interval_sec=0.0,
    )
    try:
        # S3 lies in reported observations to frame honest S1
        assert cluster.budget.set_fault("S3", "frame", params={"target": "S1"})
        for _ in range(5):
            await cluster.submit("A0", "A1", 3, timeout_s=2.0)
        await asyncio.sleep(0.15)

        # Tick 1: Default FakeGemini detects single-reporter frame -> escalates to Neon, no auto-quarantine
        v1 = await blue.step()
        assert v1["auto_quarantined"] is False
        assert cluster.quarantined == set()
        assert cluster.quarantine_events == []
        assert cluster.replace_events == []

        # Tick 2: Even if Decision 1 mistakenly returned 0.95 confidence accusing framed victim S1,
        # Blue's anti-framing guardrail refuses to auto-quarantine or blindly queue honest S1!
        fake.enqueue_decision(
            {
                "under_attack": DecisionAnswer(type="noul", noul=0.95, confidence=0.95),
                "suspect": DecisionAnswer(
                    type="choice",
                    choice="S1",
                    confidence=0.95,
                    probabilities={"S0": 0.01, "S1": 0.95, "S2": 0.01, "S3": 0.01, "other": 0.02},
                ),
                "attack_type": DecisionAnswer(
                    type="choice",
                    choice="slow",
                    confidence=0.90,
                    probabilities={"slow": 0.9, "other": 0.1},
                ),
                "severity": DecisionAnswer(type="score", score=1.9, confidence=0.9),
            }
        )
        blue.pending_quarantine = None
        v2 = await blue.step()
        assert v2["auto_quarantined"] is False
        assert cluster.quarantined == set()
        assert cluster.quarantine_events == []
        if blue.pending_quarantine is not None:
            assert blue.pending_quarantine["suspect"] != "S1"
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_red_voice_plan_adapt_and_budget_violation_refused() -> None:
    cluster = Cluster(seed=404, **FAST_KWARGS)
    await cluster.start()
    fake = FakeGemini()
    red_sb = cluster.budget.red_interface(actor="red")
    snap = telemetry.snapshot(cluster)
    png_bytes = telemetry.render_png(snap)
    red = RedAttacker(
        sandbox=red_sb,
        gemini=fake,
        status_provider=lambda: {"primary": "S0"},
        png_provider=lambda: png_bytes,
    )
    try:
        # 1. Multimodal audio input to Carbon -> sets first fault on S0 and populates `heard`
        res1 = await red.plan(b"RIFF\x24\x00\x00\x00WAVEfmt ", audio_mime_type="audio/wav")
        assert res1["applied"] is True
        assert res1["plan"]["heard"] != ""
        assert res1["plan"]["node"] == "S0"

        # 2. Hydrogen 10s adaptive step inspects telemetry PNG + status
        adapt_res = await red.adapt_step()
        assert adapt_res is not None
        assert adapt_res["source"] == "gemini-4-hydrogen"

        # 3. Second Red plan targeting S2 while S0 is already Byzantine -> refused & logged!
        fake.enqueue_reason(
            {
                "heard": "Also attack S2 with wrong_digest",
                "node": "S2",
                "mode": "wrong_digest",
                "narrative": "Attempting illegal second Byzantine node.",
            }
        )
        res2 = await red.plan("Also attack S2 with wrong_digest")
        assert res2["applied"] is False
        assert len(cluster.budget.anti_cheat_log) == 1
        assert "budget exceeded" in cluster.budget.anti_cheat_log[0]["reason"].lower()
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_gemini_sdk_constraints_and_rate_cap() -> None:
    fake = FakeGemini(max_calls_per_min=2)

    # Neon & Hydrogen forbid `minimal` thinking level
    with pytest.raises(ValueError, match="minimal"):
        await fake.reason(
            model=config.MODEL_IDS["frontier_fast"],
            inputs="test",
            thinking_level="minimal",
        )
    with pytest.raises(ValueError, match="minimal"):
        await fake.reason(
            model=config.MODEL_IDS["fast_workhorse"],
            inputs="test",
            thinking_level="minimal",
        )

    # Unverified model strings outside config.MODEL_IDS are rejected
    with pytest.raises(ValueError, match="MODEL_IDS"):
        await fake.reason(model="gemini-1.5-pro", inputs="test")

    # Rate cap enforced
    await fake.reason(model=config.MODEL_IDS["frontier_fast"], inputs="call 1", thinking_level="low")
    await fake.reason(model=config.MODEL_IDS["frontier_fast"], inputs="call 2", thinking_level="low")
    with pytest.raises(RateLimitExceededError):
        await fake.reason(model=config.MODEL_IDS["frontier_fast"], inputs="call 3", thinking_level="low")


@pytest.mark.live
@pytest.mark.skipif(not has_api_key(), reason="GEMINI_API_KEY is not set")
@pytest.mark.asyncio
async def test_live_smoke_decision1_and_neon() -> None:
    cluster = Cluster(seed=499, **FAST_KWARGS)
    await cluster.start()
    try:
        await cluster.submit("A0", "A1", 5, timeout_s=2.0)
        snap = telemetry.snapshot(cluster)
        png_bytes = telemetry.render_png(snap)

        backend = GeminiBackend()
        answers = await backend.decide(
            image_png=png_bytes,
            text="Healthy 4-node PBFT bank cluster check.",
            questions=BLUE_DECISION_QUESTIONS,
        )
        assert "under_attack" in answers and 0.0 <= (answers["under_attack"].noul or 0.0) <= 1.0
        assert answers["suspect"].choice in ("S0", "S1", "S2", "S3", "other")

        verdict: NeonVerdict = await backend.reason(
            model=config.MODEL_IDS["frontier_fast"],
            inputs=[image_part(png_bytes), text_part("Inspect healthy cluster telemetry.")],
            schema=NeonVerdict,
            thinking_level="high",
        )
        assert verdict.suspect in ("S0", "S1", "S2", "S3", "other")
        assert isinstance(verdict.rationale, str) and len(verdict.rationale) > 0
    finally:
        await cluster.stop()
