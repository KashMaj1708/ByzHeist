"""Tests for Red eviction on replace, approve_pending error safety, and WS replace_event banner fields."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
import pytest

from byzheist import telemetry
from byzheist.agents.blue import BlueWatchdog
from byzheist.agents.gemini import FakeGemini
from byzheist.agents.red import RedAttacker
from byzheist.pbft.cluster import Cluster
from byzheist.server import create_app


@pytest.mark.asyncio
async def test_replaced_primary_evicts_red_stays_honest_and_recovers_throughput() -> None:
    cluster = Cluster(
        seed=701,
        latency_range_ms=(1.5, 4.0),
        view_change_timeout_ms=350.0,
        client_timeout_ms=600.0,
    )
    await cluster.start()
    fake = FakeGemini()
    blue_sb = cluster.budget.blue_interface(telemetry_fn=lambda: telemetry.snapshot(cluster))
    blue = BlueWatchdog(sandbox=blue_sb, gemini=fake, interval_sec=1.0)
    red_sb = cluster.budget.red_interface(actor="red")
    red = RedAttacker(
        sandbox=red_sb,
        gemini=fake,
        status_provider=lambda: {"primary": "S0"},
        png_provider=lambda: blue.latest_png,
        adapt_interval_sec=0.12,
    )
    await red.start_adaptive_loop()
    try:
        # 1. Red plans `slow` on S0 via FakeGemini; enqueue an aggressive Hydrogen tweak in case
        #    adapt_step ever ran after eviction (it must NOT re-infect S0!)
        fake.enqueue_reason(
            {
                "heard": "make primary S0 slow",
                "node": "S0",
                "mode": "slow",
                "delay_ms": 220.0,
                "narrative": "Throttle primary S0.",
            }
        )
        fake.enqueue_reason(
            {
                "action": "tweak_params",
                "node": "S0",
                "mode": "slow",
                "delay_ms": 240.0,
                "rationale": "Should never execute after eviction.",
            }
        )
        plan_res = await red.plan("make primary S0 slow", apply=True)
        assert plan_res["applied"] is True
        assert red.active_node == "S0"

        # Measure slow commit time and trigger Blue pending quarantine card
        for _ in range(3):
            await cluster.submit("A0", "A1", 1, timeout_s=2.0)
        await blue.step()
        assert blue.pending_quarantine is not None
        assert blue.pending_quarantine["suspect"] == "S0"

        # 2. Human approves pending card -> S0 replaced & Red immediately evicted from S0
        approved = blue.approve_pending()
        assert approved is not None and approved["ok"] is True
        assert blue.pending_quarantine is None
        assert red.active_node is None
        assert red.active_mode == "honest"
        assert red.last_eviction is not None and red.last_eviction["node"] == "S0"
        assert any(ev.get("event") == "red_evicted" for ev in red.history)

        # 3. Advance well past one adapt interval -> S0 stays honest and throughput recovers!
        await asyncio.sleep(0.30)
        assert red.active_node is None
        assert not cluster._faults["S0"].is_byzantine

        t0 = asyncio.get_running_loop().time()
        for _ in range(8):
            await cluster.submit("A1", "A2", 1, timeout_s=2.0)
        elapsed = asyncio.get_running_loop().time() - t0
        recovered_tps = 8.0 / max(elapsed, 0.01)
        assert recovered_tps > 8.0, f"Expected fast recovered throughput, got {recovered_tps:.2f} tx/s"
        snap_after = telemetry.snapshot(cluster)
        assert snap_after["replicas"]["S0"]["median_outbound_latency_ms"] < 60.0
    finally:
        await red.stop_adaptive_loop()
        await cluster.stop()


@pytest.mark.asyncio
async def test_approve_pending_keeps_card_on_replace_failure() -> None:
    cluster = Cluster(seed=702, latency_range_ms=(1.5, 4.0), view_change_timeout_ms=300.0, client_timeout_ms=400.0)
    await cluster.start()
    fake = FakeGemini()
    blue_sb = cluster.budget.blue_interface(telemetry_fn=lambda: telemetry.snapshot(cluster))
    blue = BlueWatchdog(sandbox=blue_sb, gemini=fake)
    try:
        blue.pending_quarantine = {
            "suspect": "S99",  # Invalid replica id forces replace/quarantine ValueError
            "attack_type": "slow",
            "source": "gemini-decision-1",
            "rationale": "Test error handling",
        }
        res = blue.approve_pending()
        assert res is not None and res["ok"] is False
        assert "ValueError" in res["error"]
        # pending_quarantine must NOT be cleared when quarantine/replace fails
        assert blue.pending_quarantine is not None
    finally:
        await cluster.stop()


def test_websocket_state_after_replace_contains_replace_event_banner_fields() -> None:
    app = create_app(
        use_fake_gemini=True,
        load_rate_hz=4.0,
        blue_interval_sec=0.2,
        red_adapt_interval_sec=30.0,
        access_code="",
    )
    with TestClient(app) as client:
        # Trigger a provable fault (`forge_request` on S0) so Blue auto-quarantines + replaces S0
        r = client.post(
            "/red/manual",
            json={"node": "S0", "mode": "forge_request"},
            headers={"X-ByzHeist-Sid": "banner-test"},
        )
        assert r.status_code == 200 and r.json()["applied"] is True

        import time

        rep_ev = None
        deadline = time.time() + 4.0
        while time.time() < deadline:
            with client.websocket_connect("/ws?sid=viewer-phone") as ws:
                st = ws.receive_json()
                if st.get("replace_event"):
                    rep_ev = st["replace_event"]
                    break
            time.sleep(0.15)

        assert rep_ev is not None, "Expected replace_event in WebSocket state"
        assert rep_ev["node"] == "S0"
        assert isinstance(rep_ev["seq"], int)
        assert rep_ev["source"] in ("proof", "human approved")
        assert st["nodes"]["S0"]["new_key_badge"] is True
        assert st["red"]["last_eviction"]["node"] == "S0"
