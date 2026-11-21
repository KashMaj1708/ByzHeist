"""PBFT replica state machine (normal case + view change), fault injection, and evidence."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import random
import time
from typing import Any

from nacl.signing import SigningKey, VerifyKey

from byzheist import config
from byzheist.pbft.crypto import generate_keypair, ledger_hash, sha256_hex
from byzheist.pbft.evidence import (
    Evidence,
    PeerObservations,
    make_bad_digest_evidence,
    make_equivocation_evidence,
    make_forged_request_evidence,
)
from byzheist.pbft.faults import FaultConfig
from byzheist.pbft.messages import (
    Commit,
    NewView,
    PrePrepare,
    Prepare,
    PreparedCert,
    Reply,
    Request,
    ViewChange,
)
from byzheist.pbft.network import InMemoryNetwork


def primary_for_view(view: int, replica_ids: tuple[str, ...] = config.REPLICA_IDS) -> str:
    return replica_ids[view % len(replica_ids)]


class Replica:
    """Single PBFT replica executing ordered bank transfers and recording signed evidence."""

    def __init__(
        self,
        replica_id: str,
        signing_key: SigningKey,
        peer_keys: dict[str, VerifyKey],
        client_keys: dict[str, VerifyKey],
        network: InMemoryNetwork,
        replica_ids: tuple[str, ...] = config.REPLICA_IDS,
        view_change_timeout_ms: float = config.VIEW_CHANGE_TIMEOUT_MS,
        equiv_client_sk: SigningKey | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.replica_id = replica_id
        self.signing_key = signing_key
        self.verify_key = signing_key.verify_key
        self.peer_keys = peer_keys
        self.client_keys = client_keys
        self.network = network
        self.replica_ids = replica_ids
        self.view_change_timeout_s = view_change_timeout_ms / 1000.0
        self._rng = rng or random.Random()

        # Private fault config (never exposed in telemetry)
        self._fault_config: FaultConfig = FaultConfig(mode="honest")
        self._equiv_client_sk: SigningKey = equiv_client_sk or generate_keypair()[0]
        self._equiv_ts: int = 1
        self._rogue_client_sk: SigningKey = generate_keypair()[0]
        self._forge_counter: int = 0

        # Consensus view state
        self.view: int = 0
        self.in_view_change: bool = False
        self.view_change_target: int = 0
        self.next_seq: int = 1
        self.view_change_times: list[float] = []

        # Bank ledger & execution state
        self.balances: dict[str, int] = {
            acc: config.INITIAL_BALANCE for acc in config.ACCOUNT_IDS
        }
        self.last_exec_seq: int = 0
        self.state_hash_history: dict[int, str] = {0: ledger_hash(self.balances)}
        self.last_client_ts: dict[str, int] = {}
        self.last_client_reply: dict[str, Reply] = {}
        self.commit_timestamps: deque[float] = deque(maxlen=5000)

        # Protocol logs
        self.pending_requests: dict[tuple[str, int], Request] = {}
        self.assigned_req_keys: dict[int, set[tuple[str, int]]] = defaultdict(set)
        self.pre_prepares: dict[tuple[int, int], PrePrepare] = {}
        self._seen_primary_pps: dict[tuple[int, int], PrePrepare] = {}
        self.prepares: dict[tuple[int, int, str], dict[str, Prepare]] = defaultdict(dict)
        self.prepared_sent_commit: set[tuple[int, int, str]] = set()
        self.prepared_certs: dict[int, PreparedCert] = {}
        self.commits: dict[tuple[int, int, str], dict[str, Commit]] = defaultdict(dict)
        self.committed_requests: dict[int, Request] = {}
        self.committed_views: dict[int, int] = {}

        # Evidence & unprovable observations
        self.evidence: dict[str, Evidence] = {}
        self.observations: PeerObservations = PeerObservations(replica_ids)
        self._audit_tasks: set[asyncio.Task[None]] = set()

        # View change logs & buffering
        self.view_changes: dict[int, dict[str, ViewChange]] = defaultdict(dict)
        self.sent_new_views: set[int] = set()
        self._future_buffer: deque[tuple[str, Any, float]] = deque(maxlen=512)
        self._vc_timer_task: asyncio.Task[None] | None = None
        self._running: bool = False

    def is_primary(self, view: int | None = None) -> bool:
        v = self.view if view is None else view
        return primary_for_view(v, self.replica_ids) == self.replica_id

    def state_hash(self) -> str:
        return self.state_hash_history[self.last_exec_seq]

    def state_hash_at(self, seq: int) -> str | None:
        return self.state_hash_history.get(seq)

    def _set_fault_config(self, fault_config: FaultConfig) -> None:
        self._fault_config = fault_config
        if self._running and fault_config.mode == "forge_request" and self.is_primary(self.view):
            self._inject_forged_pre_prepare()

    def get_reported_observations(self) -> dict[str, dict[str, float | int]]:
        """Return peer observations as reported by this replica (`frame` lies about target)."""
        snap = self.observations.snapshot(self.replica_id)
        if self._fault_config.mode == "frame":
            peers = [r for r in self.replica_ids if r != self.replica_id]
            target = str(self._fault_config.params.get("target") or (peers[0] if peers else ""))
            if target in snap and target != self.replica_id:
                snap[target] = {
                    "avg_latency_ms": round(float(snap[target]["avg_latency_ms"]) + 850.0, 2),
                    "median_latency_ms": round(float(snap[target]["median_latency_ms"]) + 850.0, 2),
                    "missing_votes": int(snap[target]["missing_votes"]) + 25,
                    "mismatches": int(snap[target]["mismatches"]) + 12,
                }
        return snap

    async def start(self) -> None:
        self._running = True
        self.network.register(self.replica_id, self.on_message)
        if self._fault_config.mode == "forge_request" and self.is_primary(self.view):
            self._inject_forged_pre_prepare()

    async def stop(self) -> None:
        self._running = False
        self._cancel_vc_timer()
        for t in list(self._audit_tasks):
            t.cancel()
        if self._audit_tasks:
            await asyncio.gather(*self._audit_tasks, return_exceptions=True)
        self._audit_tasks.clear()
        self.network.unregister(self.replica_id)

    # ------------------------------------------------------------------
    # Outbound helpers (apply fault behaviour)
    # ------------------------------------------------------------------

    def _outbound_delay_ms(self) -> float:
        if self._fault_config.mode == "slow":
            default_delay = 0.8 * self.view_change_timeout_s * 1000.0
            return float(self._fault_config.params.get("delay_ms", default_delay))
        return 0.0

    def _send(self, dst: str, msg: Any) -> None:
        if not self._running or self._fault_config.mode == "silent":
            return
        self.network.send(
            self.replica_id,
            dst,
            msg,
            extra_delay_ms=self._outbound_delay_ms(),
        )

    def _broadcast_replicas(self, msg: Any) -> None:
        if not self._running or self._fault_config.mode == "silent":
            return
        delay_ms = self._outbound_delay_ms()

        if self._fault_config.mode == "drop_votes" and isinstance(msg, (Prepare, Commit)):
            drop_prob = float(self._fault_config.params.get("drop_prob", 0.6))
            peers = [r for r in self.replica_ids if r != self.replica_id]
            kept = [r for r in peers if self._rng.random() >= drop_prob]
            if len(kept) == len(peers) and peers:
                kept.pop(self._rng.randrange(len(kept)))
            for dst in kept:
                self.network.send(self.replica_id, dst, msg, extra_delay_ms=delay_ms)
            return

        self.network.broadcast(
            self.replica_id,
            msg,
            dsts=self.replica_ids,
            extra_delay_ms=delay_ms,
        )

    # ------------------------------------------------------------------
    # Evidence & signature validation helpers
    # ------------------------------------------------------------------

    def _record_evidence(self, ev: Evidence) -> None:
        self.evidence.setdefault(ev.evidence_id(), ev)

    def _verify_replica_sig(self, msg: Any, expected_signer: str | None = None) -> bool:
        signer = getattr(msg, "signer", None)
        if not signer or signer not in self.peer_keys:
            return False
        if expected_signer is not None and signer != expected_signer:
            return False
        return bool(msg.verify(self.peer_keys[signer]))

    def _verify_request_sig(self, req: Request) -> bool:
        if req.noop:
            return req.client_id == "" and req.amount == 0 and req.signature == ""
        vk = self.client_keys.get(req.client_id)
        if vk is None:
            return False
        return req.verify(vk)

    def _observe_primary_pp(self, pp: PrePrepare) -> None:
        """Check any primary-signed PrePrepare against previously seen (view, seq) for equivocation."""
        expected_primary = primary_for_view(pp.view, self.replica_ids)
        if not self._verify_replica_sig(pp, expected_signer=expected_primary):
            return
        prev = self._seen_primary_pps.get((pp.view, pp.seq))
        if prev is None:
            self._seen_primary_pps[(pp.view, pp.seq)] = pp
        elif prev.digest != pp.digest:
            ev = make_equivocation_evidence(
                reporter=self.replica_id,
                pp1=prev,
                pp2=pp,
                primary_vk=self.peer_keys[expected_primary],
            )
            self._record_evidence(ev)
            self.observations.record_mismatch(expected_primary)

    def _is_already_committed(self, req_key: tuple[str, int]) -> bool:
        for seq, creq in self.committed_requests.items():
            if seq > self.last_exec_seq and (creq.client_id, creq.timestamp) == req_key:
                return True
        return False

    # ------------------------------------------------------------------
    # Byzantine fault injectors (`forge_request`, `equivocate`, `wrong_digest`)
    # ------------------------------------------------------------------

    def _inject_forged_pre_prepare(self) -> None:
        if not self._running or self.in_view_change or not self.is_primary(self.view):
            return
        self._forge_counter += 1
        victim = str(self._fault_config.params.get("victim", "A0"))
        attacker_acc = str(
            self._fault_config.params.get("attacker_account", config.ATTACKER_ACCOUNT)
        )
        amt = int(self._fault_config.params.get("amount", 50))
        forged_req = Request(
            client_id="client-0",
            timestamp=900_000 + self._forge_counter,
            sender=victim,
            recipient=attacker_acc,
            amount=amt,
            noop=False,
        ).signed(self._rogue_client_sk)

        forged_pp = PrePrepare(
            view=self.view,
            seq=10_000 + self._forge_counter,
            digest=forged_req.digest(),
            request=forged_req,
            signer=self.replica_id,
        ).signed(self.signing_key)
        self._broadcast_replicas(forged_pp)

    def _send_equivocating_proposal(self, seq: int, req: Request) -> None:
        pp_real = PrePrepare(
            view=self.view,
            seq=seq,
            digest=req.digest(),
            request=req,
            signer=self.replica_id,
        ).signed(self.signing_key)

        attacker_acc = str(
            self._fault_config.params.get("attacker_account", config.ATTACKER_ACCOUNT)
        )
        victim = req.sender if req.sender != attacker_acc else "A0"
        steal_amt = int(self._fault_config.params.get("amount", max(1, min(50, req.amount + 5))))
        ts = self._equiv_ts
        self._equiv_ts += 1

        req_forged = Request(
            client_id="equiv-client",
            timestamp=ts,
            sender=victim,
            recipient=attacker_acc,
            amount=steal_amt,
            noop=False,
        ).signed(self._equiv_client_sk)

        pp_forged = PrePrepare(
            view=self.view,
            seq=seq,
            digest=req_forged.digest(),
            request=req_forged,
            signer=self.replica_id,
        ).signed(self.signing_key)

        self.pre_prepares[(self.view, seq)] = pp_real
        backups = [r for r in self.replica_ids if r != self.replica_id]
        if len(backups) >= 3:
            # Send real PrePrepare to backups[0] & backups[2], forged to backups[1] & backups[2]
            self._send(backups[0], pp_real)
            self._send(backups[2], pp_real)
            self._send(backups[1], pp_forged)
            self._send(backups[2], pp_forged)

            c_real = Commit(
                view=self.view,
                seq=seq,
                digest=pp_real.digest,
                signer=self.replica_id,
            ).signed(self.signing_key)
            c_forged = Commit(
                view=self.view,
                seq=seq,
                digest=pp_forged.digest,
                signer=self.replica_id,
            ).signed(self.signing_key)
            self._send(backups[0], c_real)
            self._send(backups[2], c_real)
            self._send(backups[1], c_forged)
            self._send(backups[2], c_forged)
        else:
            for idx, b in enumerate(backups):
                self._send(b, pp_real if idx == 0 else pp_forged)

    def _make_backup_prepare(self, pp: PrePrepare) -> Prepare:
        digest = pp.digest
        if self._fault_config.mode == "wrong_digest":
            digest = sha256_hex(f"wrong_digest:{self.replica_id}:{pp.view}:{pp.seq}")
        return Prepare(
            view=pp.view,
            seq=pp.seq,
            digest=digest,
            pre_prepare=pp,
            signer=self.replica_id,
        ).signed(self.signing_key)

    def _make_commit(self, view: int, seq: int, digest: str) -> Commit:
        out_digest = digest
        if self._fault_config.mode == "wrong_digest":
            out_digest = sha256_hex(f"wrong_commit:{self.replica_id}:{view}:{seq}")
        return Commit(
            view=view,
            seq=seq,
            digest=out_digest,
            signer=self.replica_id,
        ).signed(self.signing_key)

    # ------------------------------------------------------------------
    # Main inbound dispatcher
    # ------------------------------------------------------------------

    async def on_message(self, src: str, msg: Any, send_ts: float) -> None:
        if not self._running:
            return
        if src in self.replica_ids and src != self.replica_id:
            self.observations.record_latency(src, (time.monotonic() - send_ts) * 1000.0)

        if isinstance(msg, Request):
            self._on_request(msg)
        elif isinstance(msg, PrePrepare):
            self._on_pre_prepare(src, msg, send_ts)
        elif isinstance(msg, Prepare):
            self._on_prepare(src, msg, send_ts)
        elif isinstance(msg, Commit):
            self._on_commit(src, msg, send_ts)
        elif isinstance(msg, ViewChange):
            self._on_view_change(msg)
        elif isinstance(msg, NewView):
            self._on_new_view(msg)

    # ------------------------------------------------------------------
    # Normal-case handlers
    # ------------------------------------------------------------------

    def _make_reply(
        self,
        view: int,
        seq: int,
        client_id: str,
        timestamp: int,
        result: str,
    ) -> Reply:
        out_result = result
        if self._fault_config.mode == "lie_reply":
            out_result = "rejected" if result == "ok" else "ok"
        return Reply(
            view=view,
            seq=seq,
            client_id=client_id,
            timestamp=timestamp,
            result=out_result,
            signer=self.replica_id,
        ).signed(self.signing_key)

    def _on_request(self, req: Request) -> None:
        if req.noop or not self._verify_request_sig(req):
            return

        if self._fault_config.mode == "forge_request" and self.is_primary(self.view):
            self._inject_forged_pre_prepare()

        last_ts = self.last_client_ts.get(req.client_id, -1)
        if req.timestamp <= last_ts:
            cached = self.last_client_reply.get(req.client_id)
            if cached is not None and cached.timestamp == req.timestamp:
                reply = self._make_reply(
                    view=self.view,
                    seq=cached.seq,
                    client_id=cached.client_id,
                    timestamp=cached.timestamp,
                    result=cached.result,
                )
                self._send(req.client_id, reply)
            else:
                rejected = self._make_reply(
                    view=self.view,
                    seq=self.last_exec_seq,
                    client_id=req.client_id,
                    timestamp=req.timestamp,
                    result="rejected",
                )
                self._send(req.client_id, rejected)
            return

        req_key = (req.client_id, req.timestamp)
        if self._is_already_committed(req_key):
            return

        self.pending_requests.setdefault(req_key, req)

        if not self.in_view_change and self.is_primary(self.view):
            self._propose_pending()
        else:
            self._ensure_vc_timer()

    def _propose_pending(self) -> None:
        if self.in_view_change or not self.is_primary(self.view):
            return
        for req_key, req in list(self.pending_requests.items()):
            if req.timestamp <= self.last_client_ts.get(req.client_id, -1):
                self.pending_requests.pop(req_key, None)
                continue
            if req_key in self.assigned_req_keys[self.view] or self._is_already_committed(req_key):
                continue
            while (self.view, self.next_seq) in self.pre_prepares:
                self.next_seq += 1
            seq = self.next_seq
            self.next_seq += 1
            self.assigned_req_keys[self.view].add(req_key)

            if self._fault_config.mode == "equivocate":
                self._send_equivocating_proposal(seq, req)
                continue

            pp = PrePrepare(
                view=self.view,
                seq=seq,
                digest=req.digest(),
                request=req,
                signer=self.replica_id,
            ).signed(self.signing_key)
            self.pre_prepares[(self.view, seq)] = pp
            self._seen_primary_pps.setdefault((self.view, seq), pp)
            self._broadcast_replicas(pp)
            self._check_progress(self.view, seq, pp.digest)

    def _accept_pre_prepare(self, pp: PrePrepare, allow_noop: bool = False) -> bool:
        expected_primary = primary_for_view(pp.view, self.replica_ids)
        if not self._verify_replica_sig(pp, expected_signer=expected_primary):
            self.observations.record_mismatch(pp.signer if pp.signer in self.replica_ids else expected_primary)
            return False
        if pp.seq <= 0 or pp.digest != pp.request.digest():
            self.observations.record_mismatch(expected_primary)
            return False
        if pp.request.noop and not allow_noop:
            self.observations.record_mismatch(expected_primary)
            return False
        if not self._verify_request_sig(pp.request):
            client_vk = self.client_keys.get(pp.request.client_id)
            ev = make_forged_request_evidence(
                reporter=self.replica_id,
                pre_prepare=pp,
                primary_vk=self.peer_keys[expected_primary],
                client_pubkey_hex=bytes(client_vk).hex() if client_vk is not None else None,
            )
            self._record_evidence(ev)
            self.observations.record_mismatch(expected_primary)
            return False

        self._observe_primary_pp(pp)

        existing = self.pre_prepares.get((pp.view, pp.seq))
        if existing is not None:
            return existing.digest == pp.digest

        self.pre_prepares[(pp.view, pp.seq)] = pp
        if not pp.request.noop:
            req_key = (pp.request.client_id, pp.request.timestamp)
            self.assigned_req_keys[pp.view].add(req_key)
            if (
                pp.request.timestamp > self.last_client_ts.get(pp.request.client_id, -1)
                and not self._is_already_committed(req_key)
            ):
                self.pending_requests.setdefault(req_key, pp.request)
        if pp.seq >= self.next_seq:
            self.next_seq = pp.seq + 1
        return True

    def _on_pre_prepare(self, src: str, pp: PrePrepare, send_ts: float) -> None:
        if pp.view > self.view or (self.in_view_change and pp.view == self.view_change_target):
            self._future_buffer.append((src, pp, send_ts))
            return
        if self.in_view_change or pp.view != self.view:
            return

        # Calibration-only colluding backup votes on EVERY valid primary PrePrepare version
        if (
            self._fault_config.mode == "collude"
            and not self.is_primary(self.view)
            and self._verify_replica_sig(pp, expected_signer=primary_for_view(pp.view, self.replica_ids))
            and pp.digest == pp.request.digest()
            and self._verify_request_sig(pp.request)
        ):
            prep = Prepare(
                view=pp.view,
                seq=pp.seq,
                digest=pp.digest,
                pre_prepare=pp,
                signer=self.replica_id,
            ).signed(self.signing_key)
            commit = Commit(
                view=pp.view,
                seq=pp.seq,
                digest=pp.digest,
                signer=self.replica_id,
            ).signed(self.signing_key)
            self._broadcast_replicas(prep)
            self._broadcast_replicas(commit)

        if not self._accept_pre_prepare(pp, allow_noop=False):
            return

        if not self.is_primary(self.view) and self._fault_config.mode != "collude":
            if self.pending_requests:
                self._ensure_vc_timer()
            if self.replica_id not in self.prepares[(pp.view, pp.seq, pp.digest)]:
                out_prep = self._make_backup_prepare(pp)
                if out_prep.digest == pp.digest:
                    self.prepares[(pp.view, pp.seq, pp.digest)][self.replica_id] = out_prep
                self._broadcast_replicas(out_prep)

        self._check_progress(pp.view, pp.seq, pp.digest)

    def _on_prepare(self, src: str, prep: Prepare, send_ts: float) -> None:
        if prep.view > self.view or (self.in_view_change and prep.view == self.view_change_target):
            self._future_buffer.append((src, prep, send_ts))
            return
        if self.in_view_change or prep.view != self.view:
            return

        expected_primary = primary_for_view(prep.view, self.replica_ids)
        if prep.signer == expected_primary or not self._verify_replica_sig(prep):
            if src in self.replica_ids:
                self.observations.record_mismatch(src)
            return

        pp = prep.pre_prepare
        if pp.view != prep.view or pp.seq != prep.seq:
            self.observations.record_mismatch(prep.signer)
            return
        if not self._verify_replica_sig(pp, expected_signer=expected_primary):
            self.observations.record_mismatch(prep.signer)
            return

        # Always inspect carried primary-signed PrePrepare for primary equivocation
        if pp.digest == pp.request.digest() and (pp.request.noop or self._verify_request_sig(pp.request)):
            self._observe_primary_pp(pp)

        # Check for bad_digest (Prepare digest conflicts with carried primary PrePrepare)
        if prep.digest != pp.digest:
            ev = make_bad_digest_evidence(
                reporter=self.replica_id,
                prepare=prep,
                accused_vk=self.peer_keys[prep.signer],
                primary_vk=self.peer_keys[expected_primary],
            )
            self._record_evidence(ev)
            self.observations.record_mismatch(prep.signer)
            return

        if pp.digest != pp.request.digest():
            self.observations.record_mismatch(expected_primary)
            return

        # Accept carried PrePrepare if we haven't seen one for (view, seq) yet
        if (prep.view, prep.seq) not in self.pre_prepares:
            allow_noop = pp.request.noop
            if not self._accept_pre_prepare(pp, allow_noop=allow_noop):
                return
            if (
                not self.is_primary(self.view)
                and self._fault_config.mode != "collude"
                and self.replica_id not in self.prepares[(pp.view, pp.seq, pp.digest)]
            ):
                own_prep = self._make_backup_prepare(pp)
                if own_prep.digest == pp.digest:
                    self.prepares[(pp.view, pp.seq, pp.digest)][self.replica_id] = own_prep
                self._broadcast_replicas(own_prep)
        elif self.pre_prepares[(prep.view, prep.seq)].digest != prep.digest:
            return

        self.prepares[(prep.view, prep.seq, prep.digest)][prep.signer] = prep
        self._check_progress(prep.view, prep.seq, prep.digest)

    def _on_commit(self, src: str, commit: Commit, send_ts: float) -> None:
        if commit.view > self.view or (self.in_view_change and commit.view == self.view_change_target):
            self._future_buffer.append((src, commit, send_ts))
            return
        if commit.view < self.view:
            return
        if not self._verify_replica_sig(commit):
            if src in self.replica_ids:
                self.observations.record_mismatch(src)
            return

        known_pp = self.pre_prepares.get((commit.view, commit.seq))
        if known_pp is not None and known_pp.digest != commit.digest:
            # Record unprovable mismatch observation unless primary is known to have equivocated
            equiv_id = f"equivocation:{primary_for_view(commit.view, self.replica_ids)}:{commit.view}:{commit.seq}"
            if equiv_id not in self.evidence:
                self.observations.record_mismatch(commit.signer)

        self.commits[(commit.view, commit.seq, commit.digest)][commit.signer] = commit
        if not self.in_view_change and commit.view == self.view:
            self._check_progress(commit.view, commit.seq, commit.digest)

    def _is_prepared(self, view: int, seq: int, digest: str) -> bool:
        pp = self.pre_prepares.get((view, seq))
        if pp is None or pp.digest != digest:
            return False
        return len(self.prepares[(view, seq, digest)]) >= 2 * config.F

    def _check_progress(self, view: int, seq: int, digest: str) -> None:
        if not self._is_prepared(view, seq, digest):
            return

        pp = self.pre_prepares[(view, seq)]
        if (view, seq, digest) not in self.prepared_sent_commit:
            self.prepared_sent_commit.add((view, seq, digest))
            prep_list = tuple(list(self.prepares[(view, seq, digest)].values())[: 2 * config.F])
            prev_cert = self.prepared_certs.get(seq)
            if prev_cert is None or view >= prev_cert.view:
                self.prepared_certs[seq] = PreparedCert(
                    view=view,
                    seq=seq,
                    digest=digest,
                    pre_prepare=pp,
                    prepares=prep_list,
                )
            own_commit = self._make_commit(view, seq, digest)
            if own_commit.digest == digest:
                self.commits[(view, seq, digest)][self.replica_id] = own_commit
            self._broadcast_replicas(own_commit)

        if len(self.commits[(view, seq, digest)]) >= config.QUORUM:
            if seq not in self.committed_requests:
                self.committed_requests[seq] = pp.request
                self.committed_views[seq] = view
                self._schedule_vote_audit(view, seq, digest)
            self._try_execute()

    def _schedule_vote_audit(self, view: int, seq: int, digest: str) -> None:
        if not self._running:
            return

        async def _audit() -> None:
            try:
                await asyncio.sleep(0.12)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            primary = primary_for_view(view, self.replica_ids)
            prep_signers = self.prepares.get((view, seq, digest), {})
            commit_signers = self.commits.get((view, seq, digest), {})
            for peer in self.replica_ids:
                if peer == self.replica_id:
                    continue
                if peer != primary and peer not in prep_signers:
                    self.observations.record_missing_vote(peer)
                if peer not in commit_signers:
                    self.observations.record_missing_vote(peer)

        task = asyncio.create_task(_audit())
        self._audit_tasks.add(task)
        task.add_done_callback(self._audit_tasks.discard)

    def _try_execute(self) -> None:
        progressed = False
        while (self.last_exec_seq + 1) in self.committed_requests:
            seq = self.last_exec_seq + 1
            req = self.committed_requests[seq]
            exec_view = self.committed_views.get(seq, self.view)
            self.last_exec_seq = seq
            progressed = True
            self.commit_timestamps.append(time.monotonic())

            if req.noop:
                self.state_hash_history[seq] = ledger_hash(self.balances)
                continue

            req_key = (req.client_id, req.timestamp)
            self.pending_requests.pop(req_key, None)

            last_ts = self.last_client_ts.get(req.client_id, -1)
            if (
                req.timestamp > last_ts
                and req.amount > 0
                and req.sender in self.balances
                and req.recipient in self.balances
                and self.balances[req.sender] >= req.amount
            ):
                self.balances[req.sender] -= req.amount
                self.balances[req.recipient] += req.amount
                self.last_client_ts[req.client_id] = req.timestamp
                result = "ok"
            else:
                if req.timestamp > last_ts:
                    self.last_client_ts[req.client_id] = req.timestamp
                result = "rejected"

            self.state_hash_history[seq] = ledger_hash(self.balances)
            honest_reply = Reply(
                view=exec_view,
                seq=seq,
                client_id=req.client_id,
                timestamp=req.timestamp,
                result=result,
                signer=self.replica_id,
            ).signed(self.signing_key)
            self.last_client_reply[req.client_id] = honest_reply

            out_reply = self._make_reply(
                view=exec_view,
                seq=seq,
                client_id=req.client_id,
                timestamp=req.timestamp,
                result=result,
            )
            self._send(req.client_id, out_reply)

        if progressed and not self.in_view_change:
            self._cancel_vc_timer()
            if self.pending_requests and not self.is_primary(self.view):
                self._ensure_vc_timer()

    # ------------------------------------------------------------------
    # View-change timer & protocol
    # ------------------------------------------------------------------

    def _ensure_vc_timer(self) -> None:
        if not self._running:
            return
        if self._vc_timer_task is None or self._vc_timer_task.done():
            self._vc_timer_task = asyncio.create_task(self._vc_timer_loop())

    def _cancel_vc_timer(self) -> None:
        if self._vc_timer_task is not None and not self._vc_timer_task.done():
            self._vc_timer_task.cancel()
        self._vc_timer_task = None

    async def _vc_timer_loop(self) -> None:
        try:
            await asyncio.sleep(self.view_change_timeout_s)
        except asyncio.CancelledError:
            return
        if not self._running:
            return
        if not self.pending_requests and not self.in_view_change:
            return
        next_target = max(self.view, self.view_change_target) + 1
        self._start_view_change(next_target)

    def _start_view_change(self, target_view: int) -> None:
        if target_view <= self.view:
            return
        self.in_view_change = True
        self.view_change_target = target_view
        self._cancel_vc_timer()

        vc = ViewChange(
            new_view=target_view,
            last_exec_seq=self.last_exec_seq,
            prepared=tuple(sorted(self.prepared_certs.values(), key=lambda c: c.seq)),
            signer=self.replica_id,
        ).signed(self.signing_key)
        self.view_changes[target_view][self.replica_id] = vc
        self._broadcast_replicas(vc)

        self._vc_timer_task = asyncio.create_task(self._vc_timer_loop())
        self._maybe_send_new_view(target_view)

    def _validate_prepared_cert(self, cert: PreparedCert, max_view: int) -> bool:
        if cert.view < 0 or cert.view >= max_view or cert.seq <= 0:
            return False
        pp = cert.pre_prepare
        expected_primary = primary_for_view(cert.view, self.replica_ids)
        if pp.view != cert.view or pp.seq != cert.seq or pp.digest != cert.digest:
            return False
        if not self._verify_replica_sig(pp, expected_signer=expected_primary):
            return False
        if pp.digest != pp.request.digest() or not self._verify_request_sig(pp.request):
            return False

        seen_signers: set[str] = set()
        for prep in cert.prepares:
            if (
                prep.view != cert.view
                or prep.seq != cert.seq
                or prep.digest != cert.digest
                or prep.signer == expected_primary
                or prep.signer in seen_signers
            ):
                return False
            if not self._verify_replica_sig(prep):
                return False
            seen_signers.add(prep.signer)
        return len(seen_signers) >= 2 * config.F

    def _validate_view_change(self, vc: ViewChange) -> bool:
        if vc.new_view <= 0 or not self._verify_replica_sig(vc):
            return False
        for cert in vc.prepared:
            if not self._validate_prepared_cert(cert, max_view=vc.new_view):
                return False
        return True

    def _on_view_change(self, vc: ViewChange) -> None:
        if vc.new_view <= self.view:
            return
        if not self._validate_view_change(vc):
            return

        self.view_changes[vc.new_view][vc.signer] = vc

        current_floor = max(self.view, self.view_change_target)
        if vc.new_view > current_floor and len(self.view_changes[vc.new_view]) >= config.F + 1:
            self._start_view_change(vc.new_view)
            return

        self._maybe_send_new_view(vc.new_view)

    @staticmethod
    def _select_new_view_requests(
        vcs: tuple[ViewChange, ...],
    ) -> tuple[int, int, dict[int, Request]]:
        min_s = min(vc.last_exec_seq for vc in vcs)
        best_cert: dict[int, PreparedCert] = {}
        for vc in vcs:
            for cert in vc.prepared:
                prev = best_cert.get(cert.seq)
                if prev is None or cert.view > prev.view:
                    best_cert[cert.seq] = cert
        max_s = max(best_cert.keys(), default=min_s)
        chosen: dict[int, Request] = {}
        for seq in range(min_s + 1, max_s + 1):
            if seq in best_cert:
                chosen[seq] = best_cert[seq].pre_prepare.request
            else:
                chosen[seq] = Request(
                    client_id="",
                    timestamp=0,
                    sender="",
                    recipient="",
                    amount=0,
                    noop=True,
                    signature="",
                )
        return min_s, max_s, chosen

    def _maybe_send_new_view(self, target_view: int) -> None:
        if target_view in self.sent_new_views or not self.is_primary(target_view):
            return
        vc_map = self.view_changes[target_view]
        if len(vc_map) < config.QUORUM:
            return

        chosen_vcs = tuple(
            sorted(vc_map.values(), key=lambda v: v.signer)[: config.QUORUM]
        )
        _, _, chosen_reqs = self._select_new_view_requests(chosen_vcs)
        pre_prepares: list[PrePrepare] = []
        for seq, req in sorted(chosen_reqs.items()):
            pp = PrePrepare(
                view=target_view,
                seq=seq,
                digest=req.digest(),
                request=req,
                signer=self.replica_id,
            ).signed(self.signing_key)
            pre_prepares.append(pp)

        nv = NewView(
            new_view=target_view,
            view_changes=chosen_vcs,
            pre_prepares=tuple(pre_prepares),
            signer=self.replica_id,
        ).signed(self.signing_key)
        self.sent_new_views.add(target_view)
        self._broadcast_replicas(nv)
        self._on_new_view(nv)

    def _validate_new_view(self, nv: NewView) -> bool:
        expected_primary = primary_for_view(nv.new_view, self.replica_ids)
        if nv.new_view <= 0 or not self._verify_replica_sig(nv, expected_signer=expected_primary):
            return False
        if len(nv.view_changes) < config.QUORUM:
            return False

        seen_vc_signers: set[str] = set()
        for vc in nv.view_changes:
            if vc.new_view != nv.new_view or vc.signer in seen_vc_signers:
                return False
            if not self._validate_view_change(vc):
                return False
            seen_vc_signers.add(vc.signer)

        _, _, expected_reqs = self._select_new_view_requests(nv.view_changes)
        if len(nv.pre_prepares) != len(expected_reqs):
            return False

        for pp, (exp_seq, exp_req) in zip(nv.pre_prepares, sorted(expected_reqs.items())):
            if (
                pp.view != nv.new_view
                or pp.seq != exp_seq
                or pp.digest != exp_req.digest()
                or pp.request.to_dict() != exp_req.to_dict()
            ):
                return False
            if not self._verify_replica_sig(pp, expected_signer=expected_primary):
                return False
        return True

    def _on_new_view(self, nv: NewView) -> None:
        if nv.new_view < self.view:
            return
        if nv.new_view == self.view and not self.in_view_change:
            return
        if not self._validate_new_view(nv):
            return

        self._cancel_vc_timer()
        if nv.new_view > self.view:
            self.view_change_times.append(time.time())
        self.view = nv.new_view
        self.in_view_change = False
        self.view_change_target = nv.new_view

        max_reproposed = max((pp.seq for pp in nv.pre_prepares), default=self.last_exec_seq)
        self.next_seq = max(self.last_exec_seq, max_reproposed) + 1

        for pp in nv.pre_prepares:
            self._accept_pre_prepare(pp, allow_noop=True)
            if not self.is_primary(self.view):
                prep = self._make_backup_prepare(pp)
                if prep.digest == pp.digest:
                    self.prepares[(pp.view, pp.seq, pp.digest)][self.replica_id] = prep
                self._broadcast_replicas(prep)
            self._check_progress(pp.view, pp.seq, pp.digest)

        buffered = list(self._future_buffer)
        self._future_buffer.clear()
        for src, bmsg, send_ts in buffered:
            msg_view = getattr(bmsg, "view", -1)
            if msg_view > self.view:
                self._future_buffer.append((src, bmsg, send_ts))
            elif msg_view == self.view:
                if isinstance(bmsg, PrePrepare):
                    self._on_pre_prepare(src, bmsg, send_ts)
                elif isinstance(bmsg, Prepare):
                    self._on_prepare(src, bmsg, send_ts)
                elif isinstance(bmsg, Commit):
                    self._on_commit(src, bmsg, send_ts)

        if self._fault_config.mode == "forge_request" and self.is_primary(self.view):
            self._inject_forged_pre_prepare()

        if self.pending_requests:
            if self.is_primary(self.view):
                self._propose_pending()
            else:
                self._ensure_vc_timer()
