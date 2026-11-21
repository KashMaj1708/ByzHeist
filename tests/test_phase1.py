"""Phase 1 tests: PBFT normal case, crashed backup, view change, replay, and bad signatures."""

from __future__ import annotations

import asyncio
import random

import pytest

from byzheist import config, referee
from byzheist.pbft.cluster import Cluster
from byzheist.pbft.crypto import generate_keypair
from byzheist.pbft.messages import Request


FAST_CLUSTER_KWARGS = {
    "latency_range_ms": (1.0, 4.0),
    "view_change_timeout_ms": 150.0,
    "client_timeout_ms": 100.0,
}


async def _wait_until(predicate, timeout_s: float = 2.0, poll_s: float = 0.01) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("Condition did not become true before timeout")
        await asyncio.sleep(poll_s)


@pytest.mark.asyncio
async def test_50_random_transfers_commit_and_agree() -> None:
    rng = random.Random(101)
    cluster = Cluster(seed=101, **FAST_CLUSTER_KWARGS)
    await cluster.start()
    try:
        for _ in range(50):
            src_acc, dst_acc = rng.sample(config.ACCOUNT_IDS, 2)
            amt = rng.randint(1, 20)
            reply = await cluster.submit(src_acc, dst_acc, amt, timeout_s=2.0)
            assert reply.result in ("ok", "rejected")

        # Wait for all 4 replicas to finish delivering trailing Commit messages
        await _wait_until(
            lambda: all(r.last_exec_seq == 50 for r in cluster.replicas.values())
        )

        hashes = cluster.state_hashes()
        assert len(set(hashes.values())) == 1

        verdict = referee.check(cluster)
        assert verdict["total_ok"] is True
        assert verdict["agreement_ok"] is True
        assert all(tot == 10_000 for tot in verdict["totals_per_replica"].values())
        assert len(cluster.network.event_log) > 0
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_crashed_backup_50_transfers_still_commit() -> None:
    rng = random.Random(202)
    cluster = Cluster(seed=202, **FAST_CLUSTER_KWARGS)
    await cluster.start()
    try:
        cluster.network.drop_node("S3")
        live_ids = ["S0", "S1", "S2"]

        for _ in range(50):
            src_acc, dst_acc = rng.sample(config.ACCOUNT_IDS, 2)
            amt = rng.randint(1, 20)
            await cluster.submit(src_acc, dst_acc, amt, timeout_s=2.0)

        await _wait_until(
            lambda: all(cluster.replicas[rid].last_exec_seq == 50 for rid in live_ids)
        )

        assert cluster.replicas["S3"].last_exec_seq == 0
        live_hashes = {cluster.state_hashes()[rid] for rid in live_ids}
        assert len(live_hashes) == 1

        verdict = referee.check(cluster, honest_ids=live_ids)
        assert verdict["total_ok"] is True
        assert verdict["agreement_ok"] is True
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_crashed_primary_triggers_view_change_and_keeps_committing() -> None:
    rng = random.Random(303)
    cluster = Cluster(seed=303, **FAST_CLUSTER_KWARGS)
    await cluster.start()
    try:
        # Commit 10 transfers in view 0 under primary S0
        for _ in range(10):
            src_acc, dst_acc = rng.sample(config.ACCOUNT_IDS, 2)
            await cluster.submit(src_acc, dst_acc, rng.randint(1, 15), timeout_s=2.0)

        # Crash primary S0: backups S1, S2, S3 must view-change to view >= 1 (primary S1)
        cluster.network.drop_node("S0")
        live_ids = ["S1", "S2", "S3"]

        for _ in range(20):
            src_acc, dst_acc = rng.sample(config.ACCOUNT_IDS, 2)
            await cluster.submit(src_acc, dst_acc, rng.randint(1, 15), timeout_s=3.0)

        await _wait_until(
            lambda: all(cluster.replicas[rid].last_exec_seq >= 30 for rid in live_ids)
        )

        for rid in live_ids:
            assert cluster.replicas[rid].view >= 1

        live_hashes = {cluster.state_hashes()[rid] for rid in live_ids}
        assert len(live_hashes) == 1

        verdict = referee.check(cluster, honest_ids=live_ids)
        assert verdict["total_ok"] is True
        assert verdict["agreement_ok"] is True
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_replay_old_signed_request_does_not_execute_twice() -> None:
    cluster = Cluster(seed=404, **FAST_CLUSTER_KWARGS)
    await cluster.start()
    try:
        req1 = cluster.client.create_request("A0", "A1", 25)
        rep1 = await cluster.client.submit_request(req1, timeout_s=2.0)
        assert rep1.result == "ok"

        await _wait_until(
            lambda: all(r.last_exec_seq == 1 for r in cluster.replicas.values())
        )
        assert cluster.balances("S0")["A0"] == 75
        assert cluster.balances("S0")["A1"] == 125

        # Replay exact same signed request immediately (timestamp == last_client_ts)
        rep_dup = await cluster.client.submit_request(
            req1, timeout_s=2.0, broadcast_first=True
        )
        assert rep_dup.result == "ok"
        await asyncio.sleep(0.05)
        for rid in cluster.replica_ids:
            assert cluster.balances(rid)["A0"] == 75
            assert cluster.balances(rid)["A1"] == 125
            assert cluster.replicas[rid].last_exec_seq == 1

        # Advance client timestamp with a second valid transfer, then replay stale req1
        rep2 = await cluster.submit("A0", "A2", 10, timeout_s=2.0)
        assert rep2.result == "ok"
        await _wait_until(
            lambda: all(r.last_exec_seq == 2 for r in cluster.replicas.values())
        )

        rep_stale = await cluster.client.submit_request(
            req1, timeout_s=2.0, broadcast_first=True
        )
        assert rep_stale.result == "rejected"
        await asyncio.sleep(0.05)
        for rid in cluster.replica_ids:
            assert cluster.balances(rid)["A0"] == 65
            assert cluster.balances(rid)["A1"] == 125
            assert cluster.balances(rid)["A2"] == 110
            assert cluster.replicas[rid].last_exec_seq == 2

        verdict = referee.check(cluster)
        assert verdict["total_ok"] is True
        assert verdict["agreement_ok"] is True
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_bad_client_signature_is_rejected() -> None:
    cluster = Cluster(seed=505, **FAST_CLUSTER_KWARGS)
    await cluster.start()
    try:
        rogue_sk, _ = generate_keypair()
        forged_req = Request(
            client_id=cluster.client.client_id,
            timestamp=1,
            sender="A0",
            recipient="A99",
            amount=50,
            noop=False,
        ).signed(rogue_sk)

        with pytest.raises(TimeoutError):
            await cluster.client.submit_request(
                forged_req, timeout_s=0.35, broadcast_first=True
            )

        for rid in cluster.replica_ids:
            assert cluster.replicas[rid].last_exec_seq == 0
            assert cluster.balances(rid)["A0"] == 100
            assert cluster.balances(rid)["A99"] == 100

        verdict = referee.check(cluster)
        assert verdict["total_ok"] is True
        assert verdict["agreement_ok"] is True
    finally:
        await cluster.stop()


@pytest.mark.asyncio
async def test_insufficient_balance_orders_rejected_noop_and_conserves_money() -> None:
    cluster = Cluster(seed=606, **FAST_CLUSTER_KWARGS)
    await cluster.start()
    try:
        reply = await cluster.submit("A0", "A1", 500, timeout_s=2.0)
        assert reply.result == "rejected"
        await _wait_until(
            lambda: all(r.last_exec_seq == 1 for r in cluster.replicas.values())
        )
        assert cluster.balances("S0")["A0"] == 100
        assert cluster.balances("S0")["A1"] == 100
        assert referee.check(cluster)["total_ok"] is True
    finally:
        await cluster.stop()
