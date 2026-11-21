"""Phase 2 tests: 8 Byzantine modes, verifiable evidence, slow primary, calibration, quarantine/replace, and budget."""

from __future__ import annotations

import asyncio
from dataclasses import replace as dc_replace

import pytest

from byzheist import referee
from byzheist.pbft.cluster import Cluster
from byzheist.pbft.evidence import verify


FAST_KWARGS = {
    "latency_range_ms": (1.5, 5.0),
    "view_change_timeout_ms": 250.0,
    "client_timeout_ms": 180.0,
}


async def _wait_until(predicate, timeout_s: float = 3.0, poll_s: float = 0.015) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("Condition did not become true within timeout")
        await asyncio.sleep(poll_s)


@pytest.mark.asyncio
async def test_all_eight_modes_f1_under_load_for_5s_conserve_money_and_agree() -> None:
    scenarios = [
        ("silent", "S1", {}),
        ("equivocate", "S0", {}),
        ("wrong_digest", "S2", {}),
        ("slow", "S0", {"delay_ms": 180.0}),
        ("drop_votes", "S3", {"drop_prob": 0.7}),
        ("lie_reply", "S1", {}),
        ("forge_request", "S0", {}),
        ("frame", "S2", {"target": "S0"}),
    ]

    async def _run_mode(idx: int, mode: str, node: str, params: dict) -> None:
        cluster = Cluster(
            seed=1000 + idx,
            latency_range_ms=(2.0, 6.0),
            view_change_timeout_ms=300.0,
            client_timeout_ms=450.0,
        )
        await cluster.start()
        try:
            res = cluster.budget.set_fault(node, mode, params=params, actor="red")
            assert res.ok, f"Failed to set {mode} on {node}: {res.reason}"

            load_task = cluster.start_load_generator(rate_hz=8.0)
            await asyncio.sleep(5.0)
            load_task.cancel()
            await asyncio.gather(load_task, return_exceptions=True)
            await asyncio.sleep(0.15)

            honest_ids = [r for r in cluster.replica_ids if r != node]
            verdict = referee.check(cluster, honest_ids=honest_ids)
            assert verdict["total_ok"] is True, f"Mode {mode} violated money conservation: {verdict}"
            assert verdict["agreement_ok"] is True, f"Mode {mode} broke honest agreement: {verdict}"
            assert max(verdict["last_exec_seq_per_replica"][r] for r in honest_ids) > 0, (
                f"Mode {mode} made zero progress over 5s"
            )
        finally:
            await cluster.stop()

    await asyncio.gather(
        *(_run_mode(i, m, n, p) for i, (m, n, p) in enumerate(scenarios))
    )


@pytest.mark.asyncio
async def test_equivocate_wrong_digest_and_forge_request_produce_verifiable_evidence() -> None:
    # 1. Equivocation by primary S0
    c_equiv = Cluster(seed=2001, **FAST_KWARGS)
    await c_equiv.start()
    try:
        assert c_equiv.budget.set_fault("S0", "equivocate")
        req = c_equiv.client.create_request("A0", "A1", 10)
        c_equiv.network.send(c_equiv.client.client_id, "S0", req)
        await _wait_until(
            lambda: any(e.kind == "equivocation" for e in c_equiv.verified_evidence())
        )
        evs = [e for e in c_equiv.verified_evidence() if e.kind == "equivocation"]
        assert evs and evs[0].accused == "S0"
        assert verify(evs[0]) is True

        # Tampering invalidates verification
        bad_payload = dict(evs[0].payload)
        bad_pp1 = dict(bad_payload["pre_prepare_1"])
        bad_pp1["signature"] = "00" * 64
        bad_payload["pre_prepare_1"] = bad_pp1
        assert verify(dc_replace(evs[0], payload=bad_payload)) is False
    finally:
        await c_equiv.stop()

    # 2. Wrong digest by backup S2
    c_wd = Cluster(seed=2002, **FAST_KWARGS)
    await c_wd.start()
    try:
        assert c_wd.budget.set_fault("S2", "wrong_digest")
        await c_wd.submit("A0", "A1", 10, timeout_s=2.0)
        await _wait_until(
            lambda: any(e.kind == "bad_digest" for e in c_wd.verified_evidence())
        )
        evs = [e for e in c_wd.verified_evidence() if e.kind == "bad_digest"]
        assert evs and evs[0].accused == "S2"
        assert verify(evs[0]) is True
    finally:
        await c_wd.stop()

    # 3. Forged request by primary S0
    c_forge = Cluster(seed=2003, **FAST_KWARGS)
    await c_forge.start()
    try:
        assert c_forge.budget.set_fault("S0", "forge_request")
        await _wait_until(
            lambda: any(e.kind == "forged_request" for e in c_forge.verified_evidence())
        )
        evs = [e for e in c_forge.verified_evidence() if e.kind == "forged_request"]
        assert evs and evs[0].accused == "S0"
        assert verify(evs[0]) is True
    finally:
        await c_forge.stop()

    # 4. Frame mode corrupts only reported observations, never signed evidence
    c_frame = Cluster(seed=2004, **FAST_KWARGS)
    await c_frame.start()
    try:
        assert c_frame.budget.set_fault("S3", "frame", params={"target": "S0"})
        await c_frame.submit("A0", "A1", 5, timeout_s=2.0)
        await asyncio.sleep(0.05)
        assert c_frame.verified_evidence() == []
        framed_obs = c_frame.replicas["S3"].get_reported_observations()["S0"]
        honest_obs = c_frame.replicas["S1"].get_reported_observations()["S0"]
        assert framed_obs["missing_votes"] >= honest_obs["missing_votes"] + 20
        assert framed_obs["mismatches"] >= honest_obs["mismatches"] + 10
    finally:
        await c_frame.stop()


@pytest.mark.asyncio
async def test_slow_primary_drops_throughput_over_50_percent_without_view_change() -> None:
    timing_kwargs = {
        "latency_range_ms": (2.0, 5.0),
        "view_change_timeout_ms": 500.0,
        "client_timeout_ms": 750.0,
    }

    # Baseline honest throughput over 2.2s
    honest_cluster = Cluster(seed=3001, **timing_kwargs)
    await honest_cluster.start()
    try:
        t_load = honest_cluster.start_load_generator(rate_hz=20.0)
        await asyncio.sleep(2.2)
        t_load.cancel()
        await asyncio.gather(t_load, return_exceptions=True)
        honest_commits = max(r.last_exec_seq for r in honest_cluster.replicas.values())
    finally:
        await honest_cluster.stop()

    # Slow primary (default delay = 0.8 * 500 = 400 ms) over 2.2s
    slow_cluster = Cluster(seed=3001, **timing_kwargs)
    await slow_cluster.start()
    try:
        assert slow_cluster.budget.set_fault("S0", "slow")
        t_load = slow_cluster.start_load_generator(rate_hz=20.0)
        await asyncio.sleep(2.2)
        t_load.cancel()
        await asyncio.gather(t_load, return_exceptions=True)

        slow_commits = max(r.last_exec_seq for r in slow_cluster.replicas.values())
        assert slow_commits > 0
        assert slow_commits < 0.5 * honest_commits, (
            f"Expected >50% throughput drop, got slow={slow_commits} vs honest={honest_commits}"
        )
        for rid, replica in slow_cluster.replicas.items():
            assert replica.view == 0, f"Unexpected view change on {rid}: view={replica.view}"
            assert not replica.in_view_change
    finally:
        await slow_cluster.stop()


@pytest.mark.asyncio
async def test_calibration_two_colluding_byzantine_replicas_break_agreement() -> None:
    cluster = Cluster(
        seed=4001,
        calibration=True,
        latency_range_ms=(1.0, 3.0),
        view_change_timeout_ms=1000.0,
        client_timeout_ms=1000.0,
    )
    await cluster.start()
    try:
        assert cluster.budget.set_fault("S0", "equivocate")
        assert cluster.budget.set_fault("S3", "collude")

        # Send one valid transfer request to primary S0
        req = cluster.client.create_request("A0", "A1", 20)
        cluster.network.send(cluster.client.client_id, "S0", req)

        # Honest S1 executes real transfer (A0->A1); honest S2 executes forged transfer (A0->A99)
        await _wait_until(
            lambda: cluster.replicas["S1"].last_exec_seq >= 1
            and cluster.replicas["S2"].last_exec_seq >= 1
        )

        verdict = referee.check(cluster, honest_ids=["S1", "S2"])
        assert verdict["agreement_ok"] is False
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_quarantine_then_replace_restores_four_healthy_agreeing_replicas() -> None:
    cluster = Cluster(seed=5001, **FAST_KWARGS)
    await cluster.start()
    try:
        assert cluster.budget.set_fault("S2", "silent")
        for _ in range(10):
            await cluster.submit("A0", "A1", 1, timeout_s=2.0)

        old_vk_bytes = bytes(cluster.peer_keys["S2"])
        cluster.quarantine("S2")
        assert "S2" in cluster.quarantined

        for _ in range(10):
            await cluster.submit("A1", "A2", 1, timeout_s=2.0)

        await _wait_until(
            lambda: all(cluster.replicas[r].last_exec_seq == 20 for r in ("S0", "S1", "S3"))
        )
        assert cluster.replicas["S2"].last_exec_seq < 20

        # Replace S2: rotates key, transfers state at seq=20 from f+1 peers, un-quarantines
        cluster.replace("S2")
        assert "S2" not in cluster.quarantined
        assert bytes(cluster.peer_keys["S2"]) != old_vk_bytes
        assert cluster.replicas["S2"].last_exec_seq == 20

        # Commit 10 more transfers and confirm all 4 replicas execute & agree
        for _ in range(10):
            await cluster.submit("A2", "A3", 1, timeout_s=2.0)

        await _wait_until(
            lambda: all(r.last_exec_seq == 30 for r in cluster.replicas.values())
        )
        assert len(set(cluster.state_hashes().values())) == 1
        verdict = referee.check(cluster)
        assert verdict["total_ok"] is True
        assert verdict["agreement_ok"] is True
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_budget_refuses_second_byzantine_node_and_logs_anti_cheat() -> None:
    cluster = Cluster(seed=6001, **FAST_KWARGS)
    await cluster.start()
    try:
        red = cluster.budget.red_interface(actor="red")
        blue = cluster.budget.blue_interface(telemetry_fn=lambda: {"ok": True})

        r1 = red.set_fault("S1", "wrong_digest")
        assert r1.ok is True
        assert len(cluster.budget.anti_cheat_log) == 0

        # Second distinct Byzantine node without calibration -> refused & logged
        r2 = red.set_fault("S2", "slow")
        assert not r2
        assert len(cluster.budget.anti_cheat_log) == 1
        assert cluster.budget.anti_cheat_log[0]["actor"] == "red"
        assert "budget exceeded" in cluster.budget.anti_cheat_log[0]["reason"].lower()

        # Forbidden capability calls by Red and Blue are also refused & logged
        r3 = red.write_ledger("A99", 99999)
        assert not r3
        r4 = blue.get_fault_config("S1")
        assert not r4
        assert len(cluster.budget.anti_cheat_log) == 3
        assert {e["actor"] for e in cluster.budget.anti_cheat_log} == {"red", "blue"}
    finally:
        await cluster.stop()
