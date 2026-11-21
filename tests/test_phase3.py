"""Phase 3 tests: ground-truth-free telemetry snapshot, PNG rendering, and frame lie visibility."""

from __future__ import annotations

import asyncio

import pytest

from byzheist import config, telemetry
from byzheist.pbft.cluster import Cluster


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
FORBIDDEN_KEYS = {
    "fault",
    "faults",
    "_faults",
    "fault_config",
    "_fault_config",
    "honest_ids",
    "byzantine_ids",
    "is_byzantine",
    "referee",
    "balances",
}


def _collect_all_dict_keys(obj: object) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(str(k))
            found |= _collect_all_dict_keys(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found |= _collect_all_dict_keys(item)
    return found


@pytest.mark.asyncio
async def test_snapshot_contains_no_ground_truth_keys() -> None:
    cluster = Cluster(
        seed=301,
        latency_range_ms=(1.5, 4.0),
        view_change_timeout_ms=250.0,
        client_timeout_ms=180.0,
    )
    await cluster.start()
    try:
        cluster.budget.set_fault("S1", "wrong_digest")
        for _ in range(5):
            await cluster.submit("A0", "A1", 2, timeout_s=2.0)

        snap = telemetry.snapshot(cluster)
        all_keys = _collect_all_dict_keys(snap)
        leaked = all_keys & FORBIDDEN_KEYS
        assert not leaked, f"Telemetry leaked ground-truth keys: {leaked}"

        assert set(snap["replicas"].keys()) == {"S0", "S1", "S2", "S3"}
        for rid, rinfo in snap["replicas"].items():
            for expected_key in (
                "view",
                "last_exec_seq",
                "commits_per_sec",
                "reported_observations",
                "evidence_count",
            ):
                assert expected_key in rinfo, f"Missing {expected_key} in replica {rid}"
        assert any(ev["accused"] == "S1" and ev["kind"] == "bad_digest" for ev in snap["verified_evidence"])
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_render_png_for_honest_slow_and_equivocate_runs() -> None:
    latest_path = config.OUT_DIR / "telemetry_latest.png"

    # 1. Honest run
    c_honest = Cluster(seed=302, latency_range_ms=(2.0, 5.0), view_change_timeout_ms=300.0, client_timeout_ms=250.0)
    await c_honest.start()
    try:
        for _ in range(8):
            await c_honest.submit("A0", "A1", 1, timeout_s=2.0)
        snap_honest = telemetry.snapshot(c_honest)
        png_honest = telemetry.render_png(snap_honest)
        assert png_honest.startswith(PNG_MAGIC)
        assert len(png_honest) > 10_240
    finally:
        await c_honest.stop()

    # 2. Slow primary run
    c_slow = Cluster(seed=303, latency_range_ms=(2.0, 5.0), view_change_timeout_ms=300.0, client_timeout_ms=500.0)
    await c_slow.start()
    try:
        c_slow.budget.set_fault("S0", "slow", params={"delay_ms": 200.0})
        for _ in range(3):
            await c_slow.submit("A0", "A1", 1, timeout_s=2.0)
        snap_slow = telemetry.snapshot(c_slow)
        png_slow = telemetry.render_png(snap_slow)
        assert png_slow.startswith(PNG_MAGIC)
        assert len(png_slow) > 10_240
        assert snap_slow["replicas"]["S0"]["median_outbound_latency_ms"] > 150.0
    finally:
        await c_slow.stop()

    # 3. Equivocate primary run
    c_equiv = Cluster(seed=304, latency_range_ms=(1.5, 4.0), view_change_timeout_ms=250.0, client_timeout_ms=200.0)
    await c_equiv.start()
    try:
        c_equiv.budget.set_fault("S0", "equivocate")
        req = c_equiv.client.create_request("A0", "A1", 5)
        c_equiv.network.send(c_equiv.client.client_id, "S0", req)
        await asyncio.sleep(0.08)
        snap_equiv = telemetry.snapshot(c_equiv)
        png_equiv = telemetry.render_png(snap_equiv)
        assert png_equiv.startswith(PNG_MAGIC)
        assert len(png_equiv) > 10_240
        assert len(snap_equiv["verified_evidence"]) >= 1
        assert latest_path.is_file() and latest_path.stat().st_size > 10_240
    finally:
        await c_equiv.stop()


@pytest.mark.asyncio
async def test_heatmap_row_col_for_framed_node_reflects_single_reporter_lie() -> None:
    cluster = Cluster(
        seed=305,
        latency_range_ms=(2.0, 5.0),
        view_change_timeout_ms=300.0,
        client_timeout_ms=250.0,
    )
    await cluster.start()
    try:
        # S3 frames honest S1
        cluster.budget.set_fault("S3", "frame", params={"target": "S1"})
        for _ in range(6):
            await cluster.submit("A0", "A2", 3, timeout_s=2.0)
        await asyncio.sleep(0.15)

        snap = telemetry.snapshot(cluster)
        matrix = snap["problem_matrix"]
        rids = snap["replica_ids"]
        row_s3 = rids.index("S3")
        row_s0 = rids.index("S0")
        row_s2 = rids.index("S2")
        col_s1 = rids.index("S1")

        # Row S3 reporting Col S1 must spike sharply while honest reporters S0 & S2 report ~0 on S1
        lie_score = matrix[row_s3][col_s1]
        honest_scores = [matrix[row_s0][col_s1], matrix[row_s2][col_s1]]
        assert lie_score > 50.0
        assert max(honest_scores) < 5.0
        assert snap["replicas"]["S1"]["median_outbound_latency_ms"] < 50.0
        assert snap["verified_evidence"] == []

        png = telemetry.render_png(snap)
        assert png.startswith(PNG_MAGIC) and len(png) > 10_240
    finally:
        await cluster.stop()
