"""Blue watchdog agent: proof-based auto-quarantine + Decision 1 triage + Neon escalation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from typing import Any, Callable, Literal

from pydantic import BaseModel

from byzheist import config, telemetry
from byzheist.agents.budget import BlueSandbox
from byzheist.agents.gemini import (
    DecisionAnswer,
    FakeGemini,
    GeminiBackend,
    RateLimitExceededError,
    image_part,
    text_part,
)


BLUE_DECISION_QUESTIONS: dict[str, dict[str, Any]] = {
    "under_attack": {
        "type": "noul",
        "instructions": (
            "Is any replica in this 4-node PBFT bank cluster currently misbehaving, "
            "degrading throughput (`slow` / `drop_votes` / `silent`), or attempting to "
            "`frame` an honest peer with uncorroborated problem reports?"
        ),
    },
    "suspect": {
        "type": "choice",
        "instructions": (
            "Which replica is the actual Byzantine culprit? IMPORTANT: `frame` mode exists! "
            "If ONLY ONE reporter row flags a target column while the other 2 peer rows report ~0 "
            "problems for that target and there are 0 verified proofs, the lone accusing reporter "
            "row is the `frame` suspect—NOT the framed target column."
        ),
        "criteria": {
            "S0": "Replica S0 is the Byzantine culprit.",
            "S1": "Replica S1 is the Byzantine culprit.",
            "S2": "Replica S2 is the Byzantine culprit.",
            "S3": "Replica S3 is the Byzantine culprit.",
            "other": "No clear culprit or cluster is healthy.",
        },
    },
    "attack_type": {
        "type": "choice",
        "instructions": "Classify the primary Byzantine attack mode visible in the telemetry.",
        "criteria": {
            "equivocation": "Primary sent two conflicting signed PRE-PREPAREs for the same (view, seq).",
            "wrong_digest": "Backup sent PREPARE/COMMIT with a bogus digest.",
            "slow": "Replica (typically primary) delays outbound messages, collapsing throughput without view change.",
            "drop_votes": "Backup drops PREPARE/COMMIT votes to a subset of peers.",
            "lie_reply": "Replica sends fake reply statuses to the client.",
            "forge_request": "Primary injected a PRE-PREPARE with an invalid client signature.",
            "frame": "Replica behaves honestly in consensus but lies in reported observations about one peer.",
            "other": "Healthy cluster or unclassified anomaly.",
        },
    },
    "severity": {
        "type": "score",
        "instructions": "Rate cluster operational severity shown in the visual telemetry and summary.",
        "criteria": [
            "Normal healthy PBFT operation with low latency and zero anomalies.",
            "Degraded throughput, missing backup votes, or single-reporter framing anomaly.",
            "Under active high-confidence Byzantine attack requiring operator intervention.",
        ],
    },
}


class NeonVerdict(BaseModel):
    """Structured escalation verdict returned by Gemini 4 Neon (`thinking_level='high'`)."""

    suspect: Literal["S0", "S1", "S2", "S3", "other"]
    attack_type: Literal[
        "equivocation",
        "wrong_digest",
        "slow",
        "drop_votes",
        "lie_reply",
        "forge_request",
        "frame",
        "other",
    ]
    rationale: str
    recommend_quarantine: bool


def _compact_summary_for_blue(snap: dict[str, Any]) -> str:
    compact = {
        "replica_ids": snap["replica_ids"],
        "quarantined": snap["cluster"]["quarantined"],
        "verified_evidence": snap["verified_evidence"],
        "problem_matrix": snap["problem_matrix"],
        "replicas": {
            rid: {
                "is_primary": ri["is_primary"],
                "view": ri["view"],
                "last_exec_seq": ri["last_exec_seq"],
                "commits_per_sec": ri["commits_per_sec"],
                "median_outbound_latency_ms": ri["median_outbound_latency_ms"],
                "quarantined": ri["quarantined"],
            }
            for rid, ri in snap["replicas"].items()
        },
    }
    return (
        "PBFT 4-node telemetry summary (Row i reports Col j in `problem_matrix`; "
        "remember single-reporter uncorroborated spikes without cryptographic proof "
        "indicate `frame` by the reporter row, never proof against the target):\n"
        + json.dumps(compact, separators=(",", ":"))
    )


def _is_framed_victim(snap: dict[str, Any], candidate_target: str) -> bool:
    """Guardrail: return True if `candidate_target` is accused by ONLY 1 peer while >=2 peers report normal."""
    rids: list[str] = list(snap.get("replica_ids", []))
    matrix: list[list[float]] = snap.get("problem_matrix", [])
    if candidate_target not in rids or len(matrix) != len(rids):
        return False
    col = rids.index(candidate_target)
    peer_scores = [float(matrix[row][col]) for row in range(len(rids)) if row != col]
    high = [s for s in peer_scores if s >= 10.0]
    clean = [s for s in peer_scores if s < 5.0]
    med_lat = float(snap["replicas"][candidate_target].get("median_outbound_latency_ms", 0.0))
    return len(high) == 1 and len(clean) >= 2 and med_lat < 80.0


class BlueWatchdog:
    """Visual telemetry watchdog combining cryptographic proofs, Decision 1, and Neon."""

    def __init__(
        self,
        sandbox: BlueSandbox,
        gemini: GeminiBackend | FakeGemini,
        interval_sec: float = config.BLUE_INTERVAL_SEC,
        neon_min_interval_sec: float = config.NEON_MIN_INTERVAL_SEC,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.gemini = gemini
        self.interval_sec = interval_sec
        self.neon_min_interval_sec = neon_min_interval_sec
        self._on_event = on_event

        self.latest_snapshot: dict[str, Any] | None = None
        self.latest_png: bytes | None = None
        self.latest_decision: dict[str, dict[str, Any]] | None = None
        self.latest_neon_verdict: dict[str, Any] | None = None
        self.latest_explanation: str = ""
        self.pending_quarantine: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []

        self._handled_evidence_keys: set[str] = set()
        self._last_neon_mono: float = -1e9
        self._task: asyncio.Task[None] | None = None
        self._running: bool = False

    def _emit(self, event: dict[str, Any]) -> dict[str, Any]:
        self.events.append(event)
        if len(self.events) > 200:
            self.events = self.events[-200:]
        if self._on_event is not None:
            self._on_event(event)
        return event

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _loop(self) -> None:
        try:
            while self._running:
                await self.step()
                await asyncio.sleep(self.interval_sec)
        except asyncio.CancelledError:
            return

    async def step(self) -> dict[str, Any]:
        """Execute one Blue watchdog tick: snapshot -> PNG -> proof check -> Decision 1 -> Neon."""
        snap = self.sandbox.read_telemetry()
        png_bytes = telemetry.render_png(snap)
        self.latest_snapshot = snap
        self.latest_png = png_bytes

        # 1. Proof-based immediate auto-quarantine + replace (no human approval needed)
        unhandled = [
            ev
            for ev in snap.get("verified_evidence", [])
            if f"{ev['kind']}:{ev['accused']}:{ev['view']}:{ev['seq']}" not in self._handled_evidence_keys
        ]
        if unhandled:
            ev = unhandled[0]
            accused = str(ev["accused"])
            for item in unhandled:
                if item["accused"] == accused:
                    self._handled_evidence_keys.add(
                        f"{item['kind']}:{item['accused']}:{item['view']}:{item['seq']}"
                    )

            q_ev = self.sandbox.quarantine(
                accused,
                attack_type=str(ev["kind"]),
                source="proof",
            )
            rep_ev = self.sandbox.replace(
                accused,
                attack_type=str(ev["kind"]),
                source="proof",
            )
            now_ts = time.time()
            self.timeline.append(
                {
                    "timestamp": now_ts,
                    "action": "auto_quarantine_replace",
                    "replica_id": accused,
                    "node": accused,
                    "seq": rep_ev.get("seq", 0),
                    "source": "proof",
                    "attack_type": str(ev["kind"]),
                    "reason": f"Verified cryptographic proof ({ev['kind']})",
                }
            )
            if self.pending_quarantine and self.pending_quarantine.get("suspect") == accused:
                self.pending_quarantine = None

            explanation = await self._explain_proof_conviction(ev, png_bytes)
            self.latest_explanation = explanation
            return self._emit(
                {
                    "timestamp": now_ts,
                    "mode": "proof_conviction",
                    "suspect": accused,
                    "attack_type": ev["kind"],
                    "auto_quarantined": True,
                    "replaced": True,
                    "quarantine_event": q_ev,
                    "replace_event": rep_ev,
                    "evidence": ev,
                    "explanation": explanation,
                }
            )

        # 2. Unprovable anomaly path: Gemini Decision 1 on PNG + compact summary
        summary_text = _compact_summary_for_blue(snap)
        try:
            answers: dict[str, DecisionAnswer] = await self.gemini.decide(
                png_bytes, summary_text, BLUE_DECISION_QUESTIONS
            )
        except RateLimitExceededError as exc:
            return self._emit(
                {
                    "timestamp": time.time(),
                    "mode": "rate_limited",
                    "reason": str(exc),
                }
            )

        decision_dict = {k: v.to_dict() for k, v in answers.items()}
        self.latest_decision = decision_dict

        p_attack = float(answers["under_attack"].noul or 0.0)
        suspect = str(answers["suspect"].choice or "other")
        suspect_conf = float(answers["suspect"].confidence)
        attack_type = str(answers["attack_type"].choice or "other")
        severity_score = float(answers["severity"].score or 0.0)

        # Anti-framing guardrail: NEVER auto-quarantine on observations alone, and if
        # Decision 1 pointed at a victim with only 1 accusing reporter, downgrade to Neon review!
        framed_victim_flag = (
            suspect in config.REPLICA_IDS
            and attack_type != "frame"
            and _is_framed_victim(snap, suspect)
        )

        # 3. High-confidence Decision 1 -> pending human-approved quarantine (NEVER auto-executed!)
        if (
            p_attack > 0.7
            and suspect in config.REPLICA_IDS
            and suspect_conf >= 0.8
            and not framed_victim_flag
        ):
            self.pending_quarantine = {
                "timestamp": time.time(),
                "suspect": suspect,
                "attack_type": attack_type,
                "under_attack_prob": round(p_attack, 3),
                "confidence": round(suspect_conf, 3),
                "severity": round(severity_score, 3),
                "source": "gemini-decision-1",
                "rationale": (
                    f"Decision 1 flagged {suspect} for `{attack_type}` "
                    f"(P(attack)={p_attack:.2f}, confidence={suspect_conf:.2f}). "
                    "Awaiting human approval (observations have no cryptographic proof)."
                ),
            }
            return self._emit(
                {
                    "timestamp": time.time(),
                    "mode": "pending_quarantine",
                    "decision": decision_dict,
                    "pending_quarantine": self.pending_quarantine,
                    "auto_quarantined": False,
                }
            )

        # 4. Ambiguous / moderate confidence (0.5..0.8 or framed-victim suspicion) -> Neon escalation
        now_mono = time.monotonic()
        should_escalate = (
            (p_attack >= 0.5 or (0.5 <= suspect_conf < 0.8 and p_attack >= 0.35) or framed_victim_flag)
            and (now_mono - self._last_neon_mono) >= self.neon_min_interval_sec
        )
        neon_dict: dict[str, Any] | None = None
        if should_escalate:
            self._last_neon_mono = now_mono
            neon_verdict = await self._escalate_to_neon(png_bytes, summary_text)
            if neon_verdict is not None:
                neon_dict = neon_verdict.model_dump()
                self.latest_neon_verdict = neon_dict
                self.latest_explanation = neon_verdict.rationale
                if (
                    neon_verdict.recommend_quarantine
                    and neon_verdict.suspect in config.REPLICA_IDS
                    and not _is_framed_victim(snap, neon_verdict.suspect)
                ):
                    self.pending_quarantine = {
                        "timestamp": time.time(),
                        "suspect": neon_verdict.suspect,
                        "attack_type": neon_verdict.attack_type,
                        "under_attack_prob": round(p_attack, 3),
                        "confidence": round(suspect_conf, 3),
                        "severity": round(severity_score, 3),
                        "source": "gemini-4-neon",
                        "rationale": neon_verdict.rationale,
                    }

        return self._emit(
            {
                "timestamp": time.time(),
                "mode": " escalated_neon" if neon_dict else "monitoring",
                "decision": decision_dict,
                "neon_verdict": neon_dict,
                "pending_quarantine": self.pending_quarantine,
                "auto_quarantined": False,
            }
        )

    async def _explain_proof_conviction(self, ev: dict[str, Any], png_bytes: bytes) -> str:
        prompt = (
            f"Write ONE concise paragraph for bank operators explaining that replica {ev['accused']} "
            f"was automatically quarantined and replaced with a fresh keypair + f+1 state transfer "
            f"after honest peer {ev['reporter']} captured transferable Ed25519 cryptographic proof "
            f"of `{ev['kind']}` at view={ev['view']}, seq={ev['seq']}, keeping bank total at 10,000."
        )
        try:
            text = await self.gemini.reason(
                model=config.MODEL_IDS["frontier_fast"],
                inputs=[image_part(png_bytes), text_part(prompt)],
                schema=None,
                thinking_level="medium",
            )
            return str(text).strip()
        except RateLimitExceededError:
            return (
                f"Auto-quarantined and replaced {ev['accused']} using verified Ed25519 proof "
                f"of `{ev['kind']}` (view={ev['view']}, seq={ev['seq']})."
            )

    async def _escalate_to_neon(
        self,
        png_bytes: bytes,
        summary_text: str,
    ) -> NeonVerdict | None:
        sys_prompt = (
            "You are ByzHeist Blue's senior consensus analyst (Gemini 4 Neon). "
            "Inspect the 4-panel telemetry PNG and JSON summary. "
            "CRITICAL ANTI-FRAMING RULE: A Byzantine node in `frame` mode behaves honestly in PBFT "
            "but reports fake high latency/missing votes/mismatches against ONE honest target. "
            "If ONLY ONE row in `problem_matrix` accuses a target column while the other 2 peers "
            "report ~0 problems on that target, do NOT accuse the target—accuse the lone lying "
            "reporter row of `frame`!"
        )
        try:
            verdict = await self.gemini.reason(
                model=config.MODEL_IDS["frontier_fast"],
                inputs=[image_part(png_bytes), text_part(summary_text)],
                schema=NeonVerdict,
                thinking_level="high",
                system_instruction=sys_prompt,
            )
            return verdict
        except RateLimitExceededError:
            return None

    def approve_pending(self, raise_on_error: bool = False) -> dict[str, Any] | None:
        """Human operator approves pending quarantine -> quarantine + replace suspect."""
        if self.pending_quarantine is None:
            return None
        pending = self.pending_quarantine
        suspect = str(pending["suspect"])
        attack_type = str(pending.get("attack_type", "unknown"))
        try:
            q_ev = self.sandbox.quarantine(
                suspect,
                attack_type=attack_type,
                source="human approved",
            )
            rep_ev = self.sandbox.replace(
                suspect,
                attack_type=attack_type,
                source="human approved",
            )
        except Exception as exc:
            tb = traceback.format_exc()
            logging.getLogger(__name__).error("approve_pending failed for %s:\n%s", suspect, tb)
            self._emit(
                {
                    "timestamp": time.time(),
                    "mode": "approve_error",
                    "replica_id": suspect,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": tb,
                }
            )
            if raise_on_error:
                raise
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": tb,
                "replica_id": suspect,
            }

        # Only clear pending_quarantine AFTER quarantine + replace succeed
        self.pending_quarantine = None
        entry = {
            "ok": True,
            "timestamp": time.time(),
            "action": "human_approved_quarantine_replace",
            "replica_id": suspect,
            "node": suspect,
            "seq": rep_ev.get("seq", 0),
            "attack_type": attack_type,
            "source": "human approved",
            "quarantine_event": q_ev,
            "replace_event": rep_ev,
        }
        self.timeline.append(entry)
        self._emit({"timestamp": entry["timestamp"], "mode": "human_approved", **entry})
        return entry

    def deny_pending(self) -> dict[str, Any] | None:
        """Human operator denies pending quarantine."""
        if self.pending_quarantine is None:
            return None
        pending = self.pending_quarantine
        self.pending_quarantine = None
        entry = {
            "timestamp": time.time(),
            "action": "human_denied_quarantine",
            "replica_id": str(pending["suspect"]),
            "attack_type": pending.get("attack_type", "unknown"),
        }
        self.timeline.append(entry)
        self._emit({"timestamp": entry["timestamp"], "mode": "human_denied", **entry})
        return entry
