"""Cryptographically verifiable Byzantine evidence and per-peer observation tracking."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import statistics
import time
from typing import Any

from nacl.signing import VerifyKey

from byzheist import config
from byzheist.pbft.messages import PrePrepare, Prepare


# Global append-only registry of legitimate cluster VerifyKeys so `verify(evidence)` works
# standalone without requiring a Cluster reference, while still rejecting rogue keys.
_REPLICA_KEY_REGISTRY: dict[str, set[bytes]] = {}
_CLIENT_KEY_REGISTRY: dict[str, set[bytes]] = {}


def register_replica_key(replica_id: str, verify_key: VerifyKey) -> None:
    _REPLICA_KEY_REGISTRY.setdefault(replica_id, set()).add(bytes(verify_key))


def register_client_key(client_id: str, verify_key: VerifyKey) -> None:
    _CLIENT_KEY_REGISTRY.setdefault(client_id, set()).add(bytes(verify_key))


def _resolve_replica_vk(replica_id: str, pubkey_hex: str) -> VerifyKey | None:
    try:
        raw = bytes.fromhex(pubkey_hex)
    except ValueError:
        return None
    if raw not in _REPLICA_KEY_REGISTRY.get(replica_id, set()):
        return None
    return VerifyKey(raw)


def _primary_for_view(view: int) -> str:
    return config.REPLICA_IDS[view % len(config.REPLICA_IDS)]


@dataclass(frozen=True)
class Evidence:
    """Transferable cryptographic proof of Byzantine misbehaviour by `accused`."""

    kind: str  # "equivocation" | "bad_digest" | "forged_request"
    accused: str
    reporter: str
    view: int
    seq: int
    timestamp: float
    accused_pubkey_hex: str
    primary_pubkey_hex: str
    payload: dict[str, Any] = field(default_factory=dict)

    def evidence_id(self) -> str:
        return f"{self.kind}:{self.accused}:{self.view}:{self.seq}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "accused": self.accused,
            "reporter": self.reporter,
            "view": self.view,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "accused_pubkey_hex": self.accused_pubkey_hex,
            "primary_pubkey_hex": self.primary_pubkey_hex,
            "payload": self.payload,
        }


def make_equivocation_evidence(
    reporter: str,
    pp1: PrePrepare,
    pp2: PrePrepare,
    primary_vk: VerifyKey,
) -> Evidence:
    pk_hex = bytes(primary_vk).hex()
    return Evidence(
        kind="equivocation",
        accused=pp1.signer,
        reporter=reporter,
        view=pp1.view,
        seq=pp1.seq,
        timestamp=time.time(),
        accused_pubkey_hex=pk_hex,
        primary_pubkey_hex=pk_hex,
        payload={
            "pre_prepare_1": pp1.to_dict(),
            "pre_prepare_2": pp2.to_dict(),
        },
    )


def make_bad_digest_evidence(
    reporter: str,
    prepare: Prepare,
    accused_vk: VerifyKey,
    primary_vk: VerifyKey,
) -> Evidence:
    return Evidence(
        kind="bad_digest",
        accused=prepare.signer,
        reporter=reporter,
        view=prepare.view,
        seq=prepare.seq,
        timestamp=time.time(),
        accused_pubkey_hex=bytes(accused_vk).hex(),
        primary_pubkey_hex=bytes(primary_vk).hex(),
        payload={
            "prepare": prepare.to_dict(),
        },
    )


def make_forged_request_evidence(
    reporter: str,
    pre_prepare: PrePrepare,
    primary_vk: VerifyKey,
    client_pubkey_hex: str | None,
) -> Evidence:
    pk_hex = bytes(primary_vk).hex()
    return Evidence(
        kind="forged_request",
        accused=pre_prepare.signer,
        reporter=reporter,
        view=pre_prepare.view,
        seq=pre_prepare.seq,
        timestamp=time.time(),
        accused_pubkey_hex=pk_hex,
        primary_pubkey_hex=pk_hex,
        payload={
            "pre_prepare": pre_prepare.to_dict(),
            "client_pubkey_hex": client_pubkey_hex or "",
        },
    )


def verify(evidence: Evidence) -> bool:
    """Independently verify attached Ed25519 signatures for transferable evidence."""
    if not isinstance(evidence, Evidence):
        return False

    accused_vk = _resolve_replica_vk(evidence.accused, evidence.accused_pubkey_hex)
    if accused_vk is None:
        return False

    if evidence.kind == "equivocation":
        try:
            pp1 = PrePrepare.from_dict(evidence.payload["pre_prepare_1"])
            pp2 = PrePrepare.from_dict(evidence.payload["pre_prepare_2"])
        except (KeyError, TypeError, ValueError):
            return False

        expected_primary = _primary_for_view(pp1.view)
        if evidence.accused != expected_primary:
            return False
        if pp1.signer != evidence.accused or pp2.signer != evidence.accused:
            return False
        if pp1.view != pp2.view or pp1.seq != pp2.seq:
            return False
        if pp1.digest == pp2.digest:
            return False
        return pp1.verify(accused_vk) and pp2.verify(accused_vk)

    if evidence.kind == "bad_digest":
        try:
            prep = Prepare.from_dict(evidence.payload["prepare"])
        except (KeyError, TypeError, ValueError):
            return False

        pp = prep.pre_prepare
        expected_primary = _primary_for_view(prep.view)
        primary_vk = _resolve_replica_vk(expected_primary, evidence.primary_pubkey_hex)
        if primary_vk is None:
            return False
        if prep.signer != evidence.accused or prep.signer == expected_primary:
            return False
        if pp.signer != expected_primary or pp.view != prep.view or pp.seq != prep.seq:
            return False
        if not prep.verify(accused_vk) or not pp.verify(primary_vk):
            return False
        return prep.digest != pp.digest

    if evidence.kind == "forged_request":
        try:
            pp = PrePrepare.from_dict(evidence.payload["pre_prepare"])
        except (KeyError, TypeError, ValueError):
            return False

        expected_primary = _primary_for_view(pp.view)
        if evidence.accused != expected_primary or pp.signer != expected_primary:
            return False
        if not pp.verify(accused_vk):
            return False
        req = pp.request
        if req.noop:
            return False

        registered_client_keys = _CLIENT_KEY_REGISTRY.get(req.client_id, set())
        if not registered_client_keys:
            return True
        # Request is forged if NONE of the registered keys for client_id validate it
        return not any(req.verify(VerifyKey(raw)) for raw in registered_client_keys)

    return False


class PeerObservations:
    """Unprovable local observations per peer (latency, missing votes, mismatches)."""

    def __init__(self, replica_ids: tuple[str, ...] = config.REPLICA_IDS) -> None:
        self.replica_ids = replica_ids
        self._latencies_ms: dict[str, deque[tuple[float, float]]] = {
            rid: deque(maxlen=200) for rid in replica_ids
        }
        self.missing_votes: dict[str, int] = {rid: 0 for rid in replica_ids}
        self.mismatches: dict[str, int] = {rid: 0 for rid in replica_ids}

    def record_latency(self, peer_id: str, latency_ms: float) -> None:
        if peer_id in self._latencies_ms and latency_ms >= 0.0:
            self._latencies_ms[peer_id].append((time.perf_counter(), latency_ms))

    def record_missing_vote(self, peer_id: str, count: int = 1) -> None:
        if peer_id in self.missing_votes:
            self.missing_votes[peer_id] += count

    def record_mismatch(self, peer_id: str, count: int = 1) -> None:
        if peer_id in self.mismatches:
            self.mismatches[peer_id] += count

    def snapshot(self, self_id: str, window_sec: float = 8.0) -> dict[str, dict[str, float | int]]:
        now = time.perf_counter()
        out: dict[str, dict[str, float | int]] = {}
        for rid in self.replica_ids:
            if rid == self_id:
                out[rid] = {
                    "avg_latency_ms": 0.0,
                    "median_latency_ms": 0.0,
                    "missing_votes": 0,
                    "mismatches": 0,
                }
                continue
            raw_entries = list(self._latencies_ms[rid])
            recent = [lat for ts, lat in raw_entries if (now - ts) <= window_sec][-16:]
            if not recent and raw_entries:
                recent = [lat for _, lat in raw_entries[-5:]]
            avg_lat = float(statistics.fmean(recent)) if recent else 0.0
            med_lat = float(statistics.median(recent)) if recent else 0.0
            out[rid] = {
                "avg_latency_ms": round(avg_lat, 2),
                "median_latency_ms": round(med_lat, 2),
                "missing_votes": self.missing_votes[rid],
                "mismatches": self.mismatches[rid],
            }
        return out
