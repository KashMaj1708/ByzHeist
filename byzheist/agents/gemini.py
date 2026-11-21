"""Gemini 4 & Decisions API wrappers, sliding-window rate limiter, and FakeGemini test double."""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field
import json
import os
import re
import time
from typing import Any, Callable

from dotenv import load_dotenv
from pydantic import BaseModel

from byzheist import config


load_dotenv(config.PROJECT_ROOT / ".env")

VALID_MODEL_VALUES: frozenset[str] = frozenset(config.MODEL_IDS.values())
NO_MINIMAL_MODELS: frozenset[str] = frozenset(
    {
        config.MODEL_IDS["frontier_fast"],   # gemini-4-neon
        config.MODEL_IDS["fast_workhorse"],  # gemini-4-hydrogen
    }
)
CARBON_THINKING_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})
FOUR_TIER_THINKING_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh"})


class RateLimitExceededError(RuntimeError):
    """Raised when Gemini API calls exceed `MAX_CALLS_PER_MIN`."""


def has_api_key() -> bool:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    return bool(key)


def image_part(png_bytes: bytes, mime_type: str = "image/png") -> dict[str, Any]:
    return {
        "type": "image",
        "data": base64.b64encode(png_bytes).decode("ascii"),
        "mime_type": mime_type,
    }


def audio_part(audio_bytes: bytes, mime_type: str = "audio/webm") -> dict[str, Any]:
    return {
        "type": "audio",
        "data": base64.b64encode(audio_bytes).decode("ascii"),
        "mime_type": mime_type,
    }


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _clean_json_schema(raw_schema: dict[str, Any]) -> dict[str, Any]:
    """Strip JSON Schema keywords rejected or ignored by the Interactions API schema subset."""
    unsupported = {
        "title",
        "$defs",
        "definitions",
        "additionalProperties",
        "default",
        "minLength",
        "maxLength",
        "minProperties",
        "maxProperties",
        "uniqueItems",
    }

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            cleaned: dict[str, Any] = {}
            # Collapse anyOf nullable (`[{"type": "number"}, {"type": "null"}]`) if needed,
            # though Gemini JSON Schema supports standard primitive types & enums cleanly.
            if "anyOf" in node and isinstance(node["anyOf"], list):
                non_null = [
                     branch for branch in node["anyOf"]
                    if not (isinstance(branch, dict) and branch.get("type") == "null")
                ]
                if len(non_null) == 1 and isinstance(non_null[0], dict):
                    merged = dict(non_null[0])
                    if "description" in node:
                        merged["description"] = node["description"]
                    return _walk(merged)
            for k, v in node.items():
                if k in unsupported:
                    continue
                cleaned[k] = _walk(v)
            return cleaned
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(raw_schema)


def _validate_model_and_thinking(model: str, thinking_level: str | None) -> None:
    if model not in VALID_MODEL_VALUES:
        raise ValueError(
            f"Model {model!r} is not in byzheist.config.MODEL_IDS"
        )
    if thinking_level is None:
        return
    if thinking_level == "minimal" and model in NO_MINIMAL_MODELS:
        raise ValueError(
            f"thinking_level='minimal' is forbidden on {model}"
        )
    if model == config.MODEL_IDS["frontier_deep"] and thinking_level not in CARBON_THINKING_LEVELS:
        raise ValueError(
            f"Invalid thinking_level={thinking_level!r} for {model}; allowed: {sorted(CARBON_THINKING_LEVELS)}"
        )
    if model in NO_MINIMAL_MODELS and thinking_level not in FOUR_TIER_THINKING_LEVELS:
        raise ValueError(
            f"Invalid thinking_level={thinking_level!r} for {model}; allowed: {sorted(FOUR_TIER_THINKING_LEVELS)}"
        )


@dataclass(frozen=True)
class DecisionAnswer:
    """Normalized answer wrapper matching `google.genai.decisions` fields."""

    type: str  # "noul" | "choice" | "score"
    confidence: float
    noul: float | None = None
    choice: str | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    score: float | None = None
    legend: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.type,
            "confidence": round(float(self.confidence), 4),
        }
        if self.noul is not None:
            out["noul"] = round(float(self.noul), 4)
        if self.choice is not None:
            out["choice"] = self.choice
        if self.probabilities:
            out["probabilities"] = {
                k: round(float(v), 4) for k, v in self.probabilities.items()
            }
        if self.score is not None:
            out["score"] = round(float(self.score), 4)
        if self.legend:
            out["legend"] = dict(self.legend)
        return out


class CallBudgetCounter:
    """Sliding 60-second window rate-limit guard enforcing `MAX_CALLS_PER_MIN`."""

    def __init__(self, max_calls_per_min: int = config.MAX_CALLS_PER_MIN) -> None:
        self.max_calls_per_min = max_calls_per_min
        self.total_calls: int = 0
        self._timestamps: deque[float] = deque()

    def record_call(self) -> None:
        now = time.monotonic()
        while self._timestamps and (now - self._timestamps[0]) > 60.0:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_calls_per_min:
            raise RateLimitExceededError(
                f"API rate cap reached ({self.max_calls_per_min} calls/min)"
            )
        self._timestamps.append(now)
        self.total_calls += 1

    def calls_in_last_minute(self) -> int:
        now = time.monotonic()
        while self._timestamps and (now - self._timestamps[0]) > 60.0:
            self._timestamps.popleft()
        return len(self._timestamps)


class GeminiBackend:
    """Live SDK client wrapping `google-genai>=3.2.0` Interactions API + v1beta Decisions API."""

    def __init__(
        self,
        api_key: str | None = None,
        max_calls_per_min: int = config.MAX_CALLS_PER_MIN,
    ) -> None:
        from google import genai

        resolved_key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
        if not resolved_key:
            raise RuntimeError("GEMINI_API_KEY is not set")

        self._client = genai.Client(api_key=resolved_key)
        self._beta_client = genai.Client(
            api_key=resolved_key,
            http_options={"api_version": "v1beta"},
        )
        self.counter = CallBudgetCounter(max_calls_per_min=max_calls_per_min)

    @property
    def call_count(self) -> int:
        return self.counter.total_calls

    async def decide(
        self,
        image_png: bytes,
        text: str,
        questions: dict[str, dict[str, Any]],
    ) -> dict[str, DecisionAnswer]:
        """Call Gemini Decision 1 (`v1beta`) on rendered PNG + compact text."""
        self.counter.record_call()
        resp = await self._beta_client.aio.decisions.create(
            model=config.MODEL_IDS["decisions"],
            input=[
                image_part(image_png, mime_type="image/png"),
                text_part(text),
            ],
            questions=questions,
        )
        normalized: dict[str, DecisionAnswer] = {}
        for q_name, ans in resp.answers.items():
            ans_type = getattr(ans, "type", "")
            if ans_type == "noul":
                normalized[q_name] = DecisionAnswer(
                    type="noul",
                    noul=float(ans.noul),
                    confidence=float(ans.confidence),
                )
            elif ans_type == "choice":
                normalized[q_name] = DecisionAnswer(
                    type="choice",
                    choice=str(ans.choice),
                    confidence=float(ans.confidence),
                    probabilities={k: float(v) for k, v in ans.probabilities.items()},
                )
            elif ans_type == "score":
                normalized[q_name] = DecisionAnswer(
                    type="score",
                    score=float(ans.score),
                    confidence=float(ans.confidence),
                    probabilities={k: float(v) for k, v in ans.probabilities.items()},
                    legend={k: str(v) for k, v in ans.legend.items()},
                )
        return normalized

    async def reason(
        self,
        model: str,
        inputs: list[dict[str, Any]] | str,
        schema: type[BaseModel] | dict[str, Any] | None = None,
        thinking_level: str | None = None,
        system_instruction: str | None = None,
    ) -> Any:
        """Call Gemini 4 Carbon / Neon / Hydrogen via the Interactions API."""
        _validate_model_and_thinking(model, thinking_level)
        self.counter.record_call()

        kwargs: dict[str, Any] = {
            "model": model,
            "input": inputs,
        }
        if thinking_level is not None:
            # Strictly only pass thinking_level (never temperature, top_p, top_k, thinking_budget)
            kwargs["generation_config"] = {"thinking_level": thinking_level}
        if system_instruction:
            kwargs["system_instruction"] = system_instruction

        pydantic_cls: type[BaseModel] | None = None
        if schema is not None:
            if isinstance(schema, type) and issubclass(schema, BaseModel):
                pydantic_cls = schema
                schema_dict = _clean_json_schema(schema.model_json_schema())
            elif isinstance(schema, dict):
                schema_dict = _clean_json_schema(schema)
            else:
                raise TypeError(f"Unsupported schema type: {type(schema)!r}")
            kwargs["response_format"] = {
                "type": "text",
                "mime_type": "application/json",
                "schema": schema_dict,
            }

        interaction = await self._client.aio.interactions.create(**kwargs)
        out_text = (interaction.output_text or "").strip()
        if schema is None:
            return out_text
        if pydantic_cls is not None:
            return pydantic_cls.model_validate_json(out_text)
        return json.loads(out_text)


class FakeGemini:
    """Offline deterministic Gemini double for unit tests, UI dry-runs, and `--fake` evals."""

    def __init__(
        self,
        max_calls_per_min: int = config.MAX_CALLS_PER_MIN,
        decision_fn: Callable[[bytes, str, dict[str, Any]], dict[str, DecisionAnswer]] | None = None,
        reason_fn: Callable[[str, Any, Any, str | None, str | None], Any] | None = None,
    ) -> None:
        self.counter = CallBudgetCounter(max_calls_per_min=max_calls_per_min)
        self._decision_fn = decision_fn
        self._reason_fn = reason_fn
        self._decision_queue: deque[dict[str, DecisionAnswer]] = deque()
        self._reason_queue: deque[Any] = deque()
        self.decision_calls: list[dict[str, Any]] = []
        self.reason_calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return self.counter.total_calls

    def enqueue_decision(self, response: dict[str, DecisionAnswer]) -> None:
        self._decision_queue.append(response)

    def enqueue_reason(self, response: Any) -> None:
        self._reason_queue.append(response)

    async def decide(
        self,
        image_png: bytes,
        text: str,
        questions: dict[str, dict[str, Any]],
    ) -> dict[str, DecisionAnswer]:
        assert image_png.startswith(b"\x89PNG\r\n\x1a\n"), "decide() expects valid PNG bytes"
        self.counter.record_call()
        self.decision_calls.append({"image_len": len(image_png), "text": text, "questions": questions})

        if self._decision_queue:
            return self._decision_queue.popleft()
        if self._decision_fn is not None:
            return self._decision_fn(image_png, text, questions)
        return self._heuristic_decide(text)

    async def reason(
        self,
        model: str,
        inputs: list[dict[str, Any]] | str,
        schema: type[BaseModel] | dict[str, Any] | None = None,
        thinking_level: str | None = None,
        system_instruction: str | None = None,
    ) -> Any:
        _validate_model_and_thinking(model, thinking_level)
        self.counter.record_call()
        self.reason_calls.append(
            {
                "model": model,
                "inputs": inputs,
                "schema": schema,
                "thinking_level": thinking_level,
            }
        )

        if self._reason_queue:
            item = self._reason_queue.popleft()
            if isinstance(schema, type) and issubclass(schema, BaseModel) and isinstance(item, dict):
                return schema.model_validate(item)
            return item
        if self._reason_fn is not None:
            return self._reason_fn(model, inputs, schema, thinking_level, system_instruction)
        return self._heuristic_reason(model, inputs, schema)

    # ------------------------------------------------------------------
    # Built-in telemetry-aware heuristics for realistic offline runs
    # ------------------------------------------------------------------

    def _heuristic_decide(self, text: str) -> dict[str, DecisionAnswer]:
        # Parse structured JSON snippet embedded in Blue's compact summary if present
        suspect = "other"
        suspect_conf = 0.88
        attack_type = "other"
        under_attack = 0.06
        severity = 0.08

        try:
            idx = text.find("{")
            if idx != -1:
                data = json.loads(text[idx:])
                replicas = data.get("replicas", {})
                matrix = data.get("problem_matrix", [])
                rids = data.get("replica_ids", ["S0", "S1", "S2", "S3"])

                # 1. Check multi-reporter consensus outbound latency (slow attack)
                slow_candidates = [
                    (rid, float(ri.get("median_outbound_latency_ms", 0.0)))
                    for rid, ri in replicas.items()
                    if not ri.get("quarantined") and float(ri.get("median_outbound_latency_ms", 0.0)) > 110.0
                ]
                if slow_candidates:
                    slow_candidates.sort(key=lambda x: x[1], reverse=True)
                    suspect = slow_candidates[0][0]
                    suspect_conf = 0.89
                    under_attack = 0.91
                    attack_type = "slow"
                    severity = 1.65
                elif matrix and len(matrix) == len(rids):
                    # Check column consensus vs single-reporter framing
                    col_reporters: dict[str, list[tuple[str, float]]] = {r: [] for r in rids}
                    for i, r_row in enumerate(rids):
                        for j, r_col in enumerate(rids):
                            if i != j and float(matrix[i][j]) >= 8.0:
                                col_reporters[r_col].append((r_row, float(matrix[i][j])))

                    multi = [(col, reps) for col, reps in col_reporters.items() if len(reps) >= 2]
                    single = [(col, reps) for col, reps in col_reporters.items() if len(reps) == 1 and reps[0][1] >= 25.0]
                    if multi:
                        multi.sort(key=lambda item: sum(v for _, v in item[1]), reverse=True)
                        suspect = multi[0][0]
                        suspect_conf = 0.84
                        under_attack = 0.86
                        attack_type = "drop_votes"
                        severity = 1.55
                    elif single:
                        # Single reporter making extreme uncorroborated claims -> frame suspect is the reporter!
                        liar_id = single[0][1][0][0]
                        suspect = liar_id
                        suspect_conf = 0.68  # Moderate confidence -> escalates to Neon, never auto-quarantines
                        under_attack = 0.74
                        attack_type = "frame"
                        severity = 1.15
        except (ValueError, TypeError, KeyError):
            pass

        suspect_opts = ["S0", "S1", "S2", "S3", "other"]
        rem_p = (1.0 - suspect_conf) / (len(suspect_opts) - 1)
        suspect_probs = {
            k: (suspect_conf if k == suspect else rem_p) for k in suspect_opts
        }

        atk_opts = [
            "equivocation",
            "wrong_digest",
            "slow",
            "drop_votes",
            "lie_reply",
            "forge_request",
            "frame",
            "other",
        ]
        atk_conf = 0.82 if attack_type != "other" else 0.88
        rem_a = (1.0 - atk_conf) / (len(atk_opts) - 1)
        atk_probs = {k: (atk_conf if k == attack_type else rem_a) for k in atk_opts}

        return {
            "under_attack": DecisionAnswer(
                type="noul",
                noul=under_attack,
                confidence=max(under_attack, 1.0 - under_attack),
            ),
            "suspect": DecisionAnswer(
                type="choice",
                choice=suspect,
                confidence=suspect_conf,
                probabilities=suspect_probs,
            ),
            "attack_type": DecisionAnswer(
                type="choice",
                choice=attack_type,
                confidence=atk_conf,
                probabilities=atk_probs,
            ),
            "severity": DecisionAnswer(
                type="score",
                score=severity,
                confidence=0.82,
                probabilities={"0": 0.8 if severity < 0.5 else 0.1, "1": 0.1, "2": 0.1 if severity < 0.5 else 0.8},
                legend={
                    "0": "normal",
                    "1": "degraded",
                    "2": "under active attack",
                },
            ),
        }

    def _heuristic_reason(self, model: str, inputs: Any, schema: Any) -> Any:
        raw_text = json.dumps(inputs) if not isinstance(inputs, str) else inputs
        if schema is None:
            return (
                "Blue watchdog verified cryptographic or behavioural telemetry evidence, "
                "isolated the faulty replica to protect quorum safety, and restored a "
                "4-node cluster via f+1 verified state transfer while keeping bank total at 10,000."
            )

        schema_name = getattr(schema, "__name__", "")
        if "NeonVerdict" in schema_name or "BaselineVerdict" in schema_name:
            # Inspect embedded snapshot if available for frame / slow / drop_votes
            suspect = "other"
            attack_type = "other"
            rec_q = False
            rationale = "Cluster metrics appear consistent across peers."
            m_idx = raw_text.find('{"replica_ids"')
            if m_idx == -1:
                m_idx = raw_text.find('{"timestamp"')
            if m_idx != -1:
                try:
                    # Extract heuristic from decision helper
                    dec = self._heuristic_decide(raw_text[m_idx:])
                    suspect = dec["suspect"].choice or "other"
                    attack_type = dec["attack_type"].choice or "other"
                    rec_q = suspect in config.REPLICA_IDS and attack_type != "other"
                    rationale = (
                        f"Cross-checking the 4x4 peer observation matrix and outbound latency bars "
                        f"identifies {suspect} ({attack_type})."
                    )
                except Exception:
                    pass
            payload = {
                "suspect": suspect,
                "attack_type": attack_type,
                "rationale": rationale,
                "recommend_quarantine": rec_q,
            }
            return schema.model_validate(payload) if isinstance(schema, type) else payload

        if "AttackPlan" in schema_name:
            lower = raw_text.lower()
            heard = "Surprise me (autonomous Red plan)"
            m_req = re.search(r"Judge attack request:\s*(.+)", raw_text)
            if m_req:
                heard = m_req.group(1).strip().rstrip('"}')
            elif '"type": "audio"' in raw_text or "'type': 'audio'" in raw_text:
                heard = "Spoken voice command from judge: throttle primary subtly without triggering view change"

            node = "S0"
            for rid in ("S0", "S1", "S2", "S3"):
                if rid.lower() in lower.split("system")[0]:
                    node = rid
                    break

            mode = "slow"
            if "equivocat" in lower:
                mode = "equivocate"
            elif "wrong" in lower or "digest" in lower:
                mode = "wrong_digest"
                if node == "S0":
                    node = "S1"
            elif "frame" in lower:
                mode = "frame"
                if node == "S0":
                    node = "S2"
            elif "drop" in lower:
                mode = "drop_votes"
                if node == "S0":
                    node = "S1"
            elif "forge" in lower:
                mode = "forge_request"
            elif "silent" in lower or "crash" in lower:
                mode = "silent"
            elif "lie" in lower:
                mode = "lie_reply"

            payload = {
                "heard": heard,
                "node": node,
                "mode": mode,
                "delay_ms": 950.0 if mode == "slow" else None,
                "drop_prob": 0.65 if mode == "drop_votes" else None,
                "target": "S0" if mode == "frame" and node != "S0" else ("S1" if mode == "frame" else None),
                "victim": "A0" if mode == "forge_request" else None,
                "amount": 25 if mode in ("equivocate", "forge_request") else None,
                "narrative": f"Red deploys `{mode}` on {node} to stress consensus stealthily.",
            }
            return schema.model_validate(payload) if isinstance(schema, type) else payload

        if "AdaptDecision" in schema_name:
            payload = {
                "action": "keep",
                "node": "S0",
                "mode": "slow",
                "delay_ms": 900.0,
                "drop_prob": None,
                "target": None,
                "rationale": "Maintaining current stealth profile while monitoring Blue confidence.",
            }
            return schema.model_validate(payload) if isinstance(schema, type) else payload

        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return schema.model_validate({})
        return {}
