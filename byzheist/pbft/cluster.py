"""PBFT 4-replica cluster manager, fault quarantine/replacement, and load generator."""

from __future__ import annotations

import asyncio
from collections import Counter
import random
import time
from typing import Any

from nacl.signing import SigningKey, VerifyKey

from byzheist import config
from byzheist.agents.budget import FaultBudget
from byzheist.pbft.client import PBFTClient
from byzheist.pbft.crypto import generate_keypair
from byzheist.pbft.evidence import (
    Evidence,
    register_client_key,
    register_replica_key,
    verify as verify_evidence,
)
from byzheist.pbft.faults import FaultConfig
from byzheist.pbft.messages import Reply
from byzheist.pbft.network import InMemoryNetwork
from byzheist.pbft.replica import Replica


class Cluster:
    """Builds and runs n=4 PBFT replicas over an in-memory network with quarantine & replace."""

    def __init__(
        self,
        latency_range_ms: tuple[float, float] = config.LATENCY_RANGE_MS,
        view_change_timeout_ms: float = config.VIEW_CHANGE_TIMEOUT_MS,
        client_timeout_ms: float = config.CLIENT_TIMEOUT_MS,
        seed: int | None = None,
        calibration: bool = False,
    ) -> None:
        self._rng = random.Random(seed)
        self.latency_range_ms = latency_range_ms
        self.view_change_timeout_ms = view_change_timeout_ms
        self.client_timeout_ms = client_timeout_ms

        self.network = InMemoryNetwork(
            latency_range_ms=latency_range_ms,
            rng=random.Random(self._rng.randint(0, 2**31 - 1)),
        )

        self.replica_ids: tuple[str, ...] = config.REPLICA_IDS
        self._signing_keys: dict[str, SigningKey] = {}
        self.peer_keys: dict[str, VerifyKey] = {}
        for rid in self.replica_ids:
            sk, vk = generate_keypair()
            self._signing_keys[rid] = sk
            self.peer_keys[rid] = vk
            register_replica_key(rid, vk)

        self.client_keys: dict[str, VerifyKey] = {}
        self.client = PBFTClient(
            client_id="client-0",
            network=self.network,
            replica_keys=self.peer_keys,
            replica_ids=self.replica_ids,
            client_timeout_ms=client_timeout_ms,
        )
        self.client_keys[self.client.client_id] = self.client.verify_key
        register_client_key(self.client.client_id, self.client.verify_key)

        # Secondary client identity used when a Byzantine primary crafts a valid-format
        # transfer to the attacker account during `equivocate`.
        self._equiv_client_sk, equiv_vk = generate_keypair()
        self.client_keys["equiv-client"] = equiv_vk
        register_client_key("equiv-client", equiv_vk)

        self._faults: dict[str, FaultConfig] = {
            rid: FaultConfig(mode="honest") for rid in self.replica_ids
        }
        self.quarantined: set[str] = set()
        self.quarantine_events: list[dict[str, Any]] = []
        self.replace_events: list[dict[str, Any]] = []

        self.replicas: dict[str, Replica] = {
            rid: Replica(
                replica_id=rid,
                signing_key=self._signing_keys[rid],
                peer_keys=self.peer_keys,
                client_keys=self.client_keys,
                network=self.network,
                replica_ids=self.replica_ids,
                view_change_timeout_ms=view_change_timeout_ms,
                equiv_client_sk=self._equiv_client_sk,
                rng=random.Random(self._rng.randint(0, 2**31 - 1)),
            )
            for rid in self.replica_ids
        }

        self.budget = FaultBudget(self, calibration=calibration)
        self._load_tasks: set[asyncio.Task[Any]] = set()
        self._running: bool = False

    # ------------------------------------------------------------------
    # Internal ground-truth fault helpers (used ONLY by FaultBudget & Eval)
    # ------------------------------------------------------------------

    def _active_byzantine_ids(self) -> set[str]:
        return {rid for rid, fc in self._faults.items() if fc.is_byzantine}

    def _honest_ids(self) -> list[str]:
        return [rid for rid in self.replica_ids if not self._faults[rid].is_byzantine]

    def _apply_fault(self, replica_id: str, fault_config: FaultConfig) -> None:
        self._faults[replica_id] = fault_config
        self.replicas[replica_id]._set_fault_config(fault_config)

    # ------------------------------------------------------------------
    # Quarantine, Replace, and Verified Evidence
    # ------------------------------------------------------------------

    def verified_evidence(self) -> list[Evidence]:
        """Collect deduplicated cryptographically verified evidence across non-quarantined replicas."""
        by_id: dict[str, Evidence] = {}
        for rid, replica in self.replicas.items():
            if rid in self.quarantined:
                continue
            for ev_id, ev in replica.evidence.items():
                if ev_id not in by_id and verify_evidence(ev):
                    by_id[ev_id] = ev
        return sorted(by_id.values(), key=lambda e: e.timestamp)

    def quarantine(self, replica_id: str) -> None:
        """Isolate `replica_id`: all other replicas and client ignore its messages."""
        if replica_id not in self.replica_ids:
            raise ValueError(f"Unknown replica_id {replica_id!r}")
        if replica_id in self.quarantined:
            return
        self.quarantined.add(replica_id)
        for rid in self.replica_ids:
            if rid != replica_id:
                self.network.block_sender(rid, replica_id)
                self.network.block_sender(replica_id, rid)
        self.network.block_sender(self.client.client_id, replica_id)
        self.network.block_sender(replica_id, self.client.client_id)
        self.quarantine_events.append(
            {"timestamp": time.time(), "mono_t": time.monotonic(), "replica_id": replica_id}
        )

    def replace(self, replica_id: str) -> None:
        """Reset `replica_id` to honest with a fresh keypair and f+1 verified state transfer."""
        if replica_id not in self.replica_ids:
            raise ValueError(f"Unknown replica_id {replica_id!r}")

        healthy_peers = [
            self.replicas[rid]
            for rid in self.replica_ids
            if rid != replica_id and rid not in self.quarantined
        ]
        if len(healthy_peers) < config.CLIENT_QUORUM:
            raise RuntimeError(
                f"Cannot replace {replica_id}: fewer than f+1={config.CLIENT_QUORUM} healthy peers"
            )

        # Find highest sequence number backed by >= f+1 peers with matching state hashes,
        # choosing a donor replica whose current ledger corresponds to that checkpoint or prefix.
        candidate_seqs = sorted({p.last_exec_seq for p in healthy_peers}, reverse=True)
        donor: Replica | None = None
        agreed_seq: int = 0

        for seq in candidate_seqs:
            hash_counts = Counter(
                p.state_hash_at(seq)
                for p in healthy_peers
                if p.state_hash_at(seq) is not None
            )
            matching_hashes = [h for h, cnt in hash_counts.items() if cnt >= config.CLIENT_QUORUM]
            if not matching_hashes:
                continue
            target_hash = matching_hashes[0]
            exact_donors = [
                p
                for p in healthy_peers
                if p.last_exec_seq == seq and p.state_hash() == target_hash
            ]
            if exact_donors:
                donor = exact_donors[0]
                agreed_seq = seq
                break

        if donor is None:
            # Fallback to common prefix min(last_exec_seq)
            min_peer = min(healthy_peers, key=lambda p: p.last_exec_seq)
            target_seq = min_peer.last_exec_seq
            target_hash = min_peer.state_hash()
            agreeing = [
                p for p in healthy_peers if p.state_hash_at(target_seq) == target_hash
            ]
            if len(agreeing) < config.CLIENT_QUORUM:
                raise RuntimeError("State transfer failed: fewer than f+1 peers agree on prefix")
            donor = min_peer
            agreed_seq = target_seq

        # Stop old replica timers cleanly
        old_replica = self.replicas[replica_id]
        old_replica._running = False
        old_replica._cancel_vc_timer()
        for t in list(old_replica._audit_tasks):
            t.cancel()
        old_replica._audit_tasks.clear()

        # Rotate Ed25519 keypair and broadcast/re-register VerifyKey
        new_sk, new_vk = generate_keypair()
        self._signing_keys[replica_id] = new_sk
        self.peer_keys[replica_id] = new_vk
        register_replica_key(replica_id, new_vk)

        # Build fresh honest replica and transfer verified state + in-flight slots
        fresh = Replica(
            replica_id=replica_id,
            signing_key=new_sk,
            peer_keys=self.peer_keys,
            client_keys=self.client_keys,
            network=self.network,
            replica_ids=self.replica_ids,
            view_change_timeout_ms=self.view_change_timeout_ms,
            equiv_client_sk=self._equiv_client_sk,
            rng=random.Random(self._rng.randint(0, 2**31 - 1)),
        )
        fresh.view = max(p.view for p in healthy_peers)
        fresh.view_change_target = fresh.view
        fresh.last_exec_seq = agreed_seq
        fresh.balances = dict(donor.balances)
        fresh.state_hash_history = {
            s: h for s, h in donor.state_hash_history.items() if s <= agreed_seq
        }
        fresh.last_client_ts = dict(donor.last_client_ts)

        # Copy committed / in-flight consensus entries above agreed_seq so no slot gap occurs
        max_peer = max(healthy_peers, key=lambda p: p.last_exec_seq)
        for seq, req in max_peer.committed_requests.items():
            if seq > agreed_seq:
                fresh.committed_requests[seq] = req
                fresh.committed_views[seq] = max_peer.committed_views.get(seq, fresh.view)
        for (v, seq), pp in max_peer.pre_prepares.items():
            if v == fresh.view and seq > agreed_seq:
                fresh.pre_prepares[(v, seq)] = pp
        fresh.next_seq = max(
            max((p.next_seq for p in healthy_peers), default=agreed_seq + 1),
            agreed_seq + 1,
        )

        self._faults[replica_id] = FaultConfig(mode="honest")
        self.replicas[replica_id] = fresh

        if self._running:
            fresh._running = True
            self.network.register(replica_id, fresh.on_message)
            fresh._try_execute()

        # Un-quarantine and restore network links
        self.quarantined.discard(replica_id)
        self.network.restore_node(replica_id)
        for rid in self.replica_ids:
            self.network.unblock_sender(rid, replica_id)
            self.network.unblock_sender(replica_id, rid)
        self.network.unblock_sender(self.client.client_id, replica_id)
        self.network.unblock_sender(replica_id, self.client.client_id)

        self.replace_events.append(
            {
                "timestamp": time.time(),
                "mono_t": time.monotonic(),
                "replica_id": replica_id,
                "transferred_seq": fresh.last_exec_seq,
            }
        )

    # ------------------------------------------------------------------
    # Lifecycle & load generation
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        for replica in self.replicas.values():
            await replica.start()

    async def stop(self) -> None:
        self._running = False
        load_tasks = list(self._load_tasks)
        for task in load_tasks:
            task.cancel()
        if load_tasks:
            await asyncio.gather(*load_tasks, return_exceptions=True)
        self._load_tasks.clear()

        for replica in self.replicas.values():
            await replica.stop()
        self.client.close()
        await self.network.close()

    async def submit(
        self,
        sender: str,
        recipient: str,
        amount: int,
        timeout_s: float | None = None,
    ) -> Reply:
        return await self.client.submit(
            sender=sender,
            recipient=recipient,
            amount=amount,
            timeout_s=timeout_s,
        )

    def start_load_generator(self, rate_hz: float = 5.0) -> asyncio.Task[None]:
        task = asyncio.create_task(self.load_generator(rate_hz=rate_hz))
        self._load_tasks.add(task)
        task.add_done_callback(self._load_tasks.discard)
        return task

    async def load_generator(self, rate_hz: float = 5.0) -> None:
        """Submit random transfers across A0..A99 paced at rate_hz (serialized per client)."""
        interval = 1.0 / max(rate_hz, 0.1)
        loop = asyncio.get_running_loop()
        try:
            while self._running:
                t0 = loop.time()
                src_acc, dst_acc = self._rng.sample(config.ACCOUNT_IDS, 2)
                amt = self._rng.randint(1, 15)
                try:
                    await self.submit(src_acc, dst_acc, amt)
                except TimeoutError:
                    pass
                elapsed = loop.time() - t0
                sleep_s = interval - elapsed
                if sleep_s > 0 and self._running:
                    await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            return

    def state_hashes(self) -> dict[str, str]:
        return {rid: replica.state_hash() for rid, replica in self.replicas.items()}

    def balances(self, replica_id: str) -> dict[str, int]:
        return dict(self.replicas[replica_id].balances)
