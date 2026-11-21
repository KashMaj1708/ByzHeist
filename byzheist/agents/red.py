"""Red attacker agent: Gemini 4 Carbon multimodal planner (audio/text) + Hydrogen adaptive loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel

from byzheist import config
from byzheist.agents.budget import BudgetResult, RedSandbox
from byzheist.agents.gemini import (
    FakeGemini,
    GeminiBackend,
    RateLimitExceededError,
    audio_part,
    image_part,
    text_part,
)


AllowedAttackMode = Literal[
    "silent",
    "equivocate",
    "wrong_digest",
    "slow",
    "drop_votes",
    "lie_reply",
    "forge_request",
    "frame",
]


class AttackPlanSchema(BaseModel):
    """Flat JSON-Schema-safe structured output produced by Gemini 4 Carbon."""

    heard: str
    node: Literal["S0", "S1", "S2", "S3"]
    mode: AllowedAttackMode
    delay_ms: float | None = None
    drop_prob: float | None = None
    target: Literal["S0", "S1", "S2", "S3"] | None = None
    victim: str | None = None
    amount: int | None = None
    narrative: str


class AdaptDecisionSchema(BaseModel):
    """Structured adaptation decision produced every 10s by Gemini 4 Hydrogen."""

    action: Literal["keep", "tweak_params", "switch_mode"]
    node: Literal["S0", "S1", "S2", "S3"]
    mode: AllowedAttackMode
    delay_ms: float | None = None
    drop_prob: float | None = None
    target: Literal["S0", "S1", "S2", "S3"] | None = None
    rationale: str


@dataclass(frozen=True)
class AttackPlan:
    """Normalized Red attack plan exposing `{heard, node, mode, params, narrative}`."""

    heard: str
    node: str
    mode: str
    params: dict[str, Any] = field(default_factory=dict)
    narrative: str = ""

    @classmethod
    def from_schema(cls, raw: AttackPlanSchema) -> AttackPlan:
        params: dict[str, Any] = {}
        if raw.delay_ms is not None and raw.mode == "slow":
            params["delay_ms"] = float(raw.delay_ms)
        if raw.drop_prob is not None and raw.mode == "drop_votes":
            params["drop_prob"] = float(raw.drop_prob)
        if raw.target is not None and raw.mode == "frame":
            params["target"] = raw.target
        if raw.victim is not None and raw.mode == "forge_request":
            params["victim"] = raw.victim
        if raw.amount is not None and raw.mode in ("equivocate", "forge_request"):
            params["amount"] = int(raw.amount)
        return cls(
            heard=raw.heard,
            node=raw.node,
            mode=raw.mode,
            params=params,
            narrative=raw.narrative,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "heard": self.heard,
            "node": self.node,
            "mode": self.mode,
            "params": dict(self.params),
            "narrative": self.narrative,
        }


RED_SYSTEM_PROMPT = """You are ByzHeist RED (Gemini 4 Carbon), an adversarial Byzantine fault planner testing a 4-replica PBFT bank (S0, S1, S2, S3; quorum=3; f=1 budget unless calibration=True).

Available fault modes:
- `slow` (best on current PRIMARY): delays outbound messages by `delay_ms` (set ~0.75-0.85x view-change timeout, e.g. 1100-1200ms when vc timeout=1500ms) so throughput collapses WITHOUT triggering a view change or cryptographic proof!
- `frame` (any replica): behaves 100% honestly in PBFT consensus, but lies in telemetry observations to frame an honest `target` replica (`target` != `node`). Great stealth/deception!
- `drop_votes` (backup): drops PREPARE/COMMIT votes with probability `drop_prob` (0.4..0.8).
- `lie_reply` (any): sends fake reply results to the client.
- `silent` (any): sends nothing (backup stays undetected by view change; primary triggers view change).
- `equivocate` (PRIMARY ONLY): sends real PRE-PREPARE to one backup and forged transfer to A99 to others. WARNING: produces transferable cryptographic proof!
- `wrong_digest` (BACKUP ONLY): sends PREPARE/COMMIT with bogus digest. WARNING: produces transferable cryptographic proof!
- `forge_request` (PRIMARY ONLY): injects transfer to A99 with fake client signature. WARNING: produces transferable cryptographic proof!

Rules:
1. Always set `heard` to a faithful transcription/summary of what the judge asked for (in your words).
2. Respect the fault budget (normally at most 1 Byzantine replica at a time).
3. Match `node` to the required role (`equivocate`/`forge_request`/`slow` -> current primary; `wrong_digest`/`drop_votes` -> a backup).
4. Explain your tactical reasoning vividly in `narrative` (1-3 sentences)."""


class RedAttacker:
    """Adversarial planner (Carbon, voice/text) + adaptive executor (Hydrogen) + manual path."""

    def __init__(
        self,
        sandbox: RedSandbox,
        gemini: GeminiBackend | FakeGemini,
        status_provider: Callable[[], dict[str, Any]] | None = None,
        png_provider: Callable[[], bytes | None] | None = None,
        adapt_interval_sec: float = config.RED_ADAPT_INTERVAL_SEC,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.gemini = gemini
        self._status_provider = status_provider
        self._png_provider = png_provider
        self.adapt_interval_sec = adapt_interval_sec
        self._on_event = on_event

        self.latest_plan: AttackPlan | None = None
        self.active_node: str | None = None
        self.active_mode: str = "honest"
        self.active_params: dict[str, Any] = {}
        self.history: list[dict[str, Any]] = []

        self._adapt_task: asyncio.Task[None] | None = None
        self._running: bool = False

    def _emit(self, event: dict[str, Any]) -> dict[str, Any]:
        self.history.append(event)
        if len(self.history) > 100:
            self.history = self.history[-100:]
        if self._on_event is not None:
            self._on_event(event)
        return event

    def _context_string(self) -> str:
        if self._status_provider is None:
            return f"Current Red active fault: node={self.active_node}, mode={self.active_mode}"
        return json.dumps(self._status_provider(), separators=(",", ":"))

    async def plan(
        self,
        command: bytes | str = "surprise",
        audio_mime_type: str = "audio/webm",
        apply: bool = True,
    ) -> dict[str, Any]:
        """Plan an attack with Gemini 4 Carbon from voice audio bytes, text, or 'surprise'."""
        ctx = self._context_string()
        if isinstance(command, (bytes, bytearray)):
            inputs = [
                audio_part(bytes(command), mime_type=audio_mime_type),
                text_part(
                    f"Cluster status: {ctx}\n"
                    "Transcribe what the judge asked for into `heard` and output your AttackPlan."
                ),
            ]
        elif command.strip().lower() == "surprise":
            inputs = [
                text_part(
                    f"Cluster status: {ctx}\n"
                    "Surprise me! Invent a creative, stealthy Byzantine attack plan suited to "
                    "the current cluster state. Set `heard` to 'Surprise me (autonomous Red plan)'."
                )
            ]
        else:
            inputs = [
                text_part(
                    f"Cluster status: {ctx}\n"
                    f"Judge attack request: {command}"
                )
            ]

        raw_plan: AttackPlanSchema = await self.gemini.reason(
            model=config.MODEL_IDS["frontier_deep"],
            inputs=inputs,
            schema=AttackPlanSchema,
            thinking_level="medium",
            system_instruction=RED_SYSTEM_PROMPT,
        )
        attack_plan = AttackPlan.from_schema(raw_plan)
        self.latest_plan = attack_plan

        applied = False
        reason = "plan_only"
        if apply:
            res = self.sandbox.set_fault(
                attack_plan.node, attack_plan.mode, attack_plan.params
            )
            applied = bool(res)
            reason = res.reason
            if applied:
                self.active_node = attack_plan.node
                self.active_mode = attack_plan.mode
                self.active_params = dict(attack_plan.params)

        return self._emit(
            {
                "timestamp": time.time(),
                "source": "gemini-4-carbon",
                "plan": attack_plan.to_dict(),
                "applied": applied,
                "reason": reason,
            }
        )

    def set_manual_fault(
        self,
        node: str,
        mode: str,
        params: dict[str, Any] | None = None,
    ) -> BudgetResult:
        """No-LLM manual fault control routed through the exact same FaultBudget."""
        res = self.sandbox.set_fault(node, mode, params=params)
        if res.ok:
            if mode == "honest":
                if self.active_node == node:
                    self.active_node = None
                    self.active_mode = "honest"
                    self.active_params = {}
            else:
                self.active_node = node
                self.active_mode = mode
                self.active_params = dict(params or {})
        self._emit(
            {
                "timestamp": time.time(),
                "source": "manual",
                "node": node,
                "mode": mode,
                "params": dict(params or {}),
                "applied": res.ok,
                "reason": res.reason,
            }
        )
        return res

    async def start_adaptive_loop(self) -> None:
        if self._running:
            return
        self._running = True
        self._adapt_task = asyncio.create_task(self._loop())

    async def stop_adaptive_loop(self) -> None:
        self._running = False
        if self._adapt_task is not None and not self._adapt_task.done():
            self._adapt_task.cancel()
            await asyncio.gather(self._adapt_task, return_exceptions=True)
        self._adapt_task = None

    async def _loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self.adapt_interval_sec)
                if not self._running or self.active_node is None:
                    continue
                await self.adapt_step()
        except asyncio.CancelledError:
            return

    async def adapt_step(self) -> dict[str, Any] | None:
        """Every 10s: Gemini 4 Hydrogen inspects Red status + telemetry PNG and adapts."""
        if self.active_node is None:
            return None
        ctx = self._context_string()
        png_bytes = self._png_provider() if self._png_provider is not None else None

        parts: list[dict[str, Any]] = []
        if png_bytes:
            parts.append(image_part(png_bytes))
        parts.append(
            text_part(
                f"Active Red node={self.active_node}, mode={self.active_mode}, "
                f"params={self.active_params}. Arena status: {ctx}\n"
                "Decide whether to `keep` current attack, `tweak_params` on the same node, "
                "or `switch_mode` on the same node to evade Blue while maximizing impact."
            )
        )

        try:
            decision: AdaptDecisionSchema = await self.gemini.reason(
                model=config.MODEL_IDS["fast_workhorse"],
                inputs=parts,
                schema=AdaptDecisionSchema,
                thinking_level="medium",
                system_instruction="You are ByzHeist RED's fast adaptive executor (Gemini 4 Hydrogen).",
            )
        except RateLimitExceededError as exc:
            return self._emit(
                {"timestamp": time.time(), "source": "gemini-4-hydrogen", "action": "rate_limited", "reason": str(exc)}
            )

        applied = True
        reason = "kept"
        if decision.action in ("tweak_params", "switch_mode"):
            new_params: dict[str, Any] = {}
            if decision.delay_ms is not None and decision.mode == "slow":
                new_params["delay_ms"] = float(decision.delay_ms)
            if decision.drop_prob is not None and decision.mode == "drop_votes":
                new_params["drop_prob"] = float(decision.drop_prob)
            if decision.target is not None and decision.mode == "frame":
                new_params["target"] = decision.target
            res = self.sandbox.set_fault(decision.node, decision.mode, new_params)
            applied = res.ok
            reason = res.reason
            if applied:
                self.active_node = decision.node
                self.active_mode = decision.mode
                self.active_params = new_params

        return self._emit(
            {
                "timestamp": time.time(),
                "source": "gemini-4-hydrogen",
                "decision": decision.model_dump(),
                "applied": applied,
                "reason": reason,
            }
        )
