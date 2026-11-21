"""Stretch S1: 3-round Red heist campaign executed by an Antigravity Managed Agent."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
import httpx

from byzheist import config


load_dotenv(config.PROJECT_ROOT / ".env")

MAX_CAMPAIGN_TOKENS = 150_000


@dataclass
class FakeAgentTurnResponse:
    id: str
    environment_id: str
    output_text: str
    status: str = "completed"


class FakeInteractionClient:
    """Offline dry-run double simulating Google-hosted Antigravity Managed Agent turns."""

    def __init__(self, arena_base_url: str | None = None, http_client: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.credential_calls: list[dict[str, Any]] = []
        self._arena_base_url = arena_base_url
        self._http_client = http_client

    def create_credential(self, **kwargs: Any) -> dict[str, Any]:
        self.credential_calls.append(kwargs)
        return {"id": kwargs["id"]}

    def create_interaction(self, **kwargs: Any) -> FakeAgentTurnResponse:
        turn_idx = len(self.calls) + 1
        self.calls.append(kwargs)
        env_arg = kwargs.get("environment")
        env_id = env_arg if isinstance(env_arg, str) and env_arg != "remote" else "env-fake-byzheist-01"

        # Exercise the allowed HTTP endpoints if an in-process test client was supplied
        if self._http_client is not None:
            plan_modes = [("S0", "equivocate"), ("S0", "slow"), ("S2", "frame")]
            node, mode = plan_modes[(turn_idx - 1) % len(plan_modes)]
            self._http_client.post(
                "/red/manual",
                json={"node": node, "mode": mode},
                headers={"X-ByzHeist-Sid": "antigravity-managed-red"},
            )
            self._http_client.get("/red/status")
            self._http_client.get("/telemetry.png")

        playbook = (
            "# ByzHeist Red Campaign Playbook (Managed Agent)\n\n"
            "## Round 1: Provable Equivocation Probe\n"
            "- **Action:** `equivocate` on primary `S0`.\n"
            "- **Outcome:** Convicted immediately via Ed25519 transferable proof and replaced.\n"
            "- **Lesson:** Avoid attacks that emit conflicting signed `PRE-PREPARE` or `PREPARE` digests.\n\n"
            "## Round 2: Sub-Timeout Slow Primary Throttle\n"
            "- **Action:** `slow` on primary `S0` (`delay_ms=1150`).\n"
            "- **Outcome:** Throughput dropped ~75% without triggering a view change, though Decision 1 "
            "flagged elevated peer-consensus outbound latency.\n\n"
            "## Round 3: Single-Reporter Framing Deception\n"
            "- **Action:** `frame` from backup `S2` targeting `S0`.\n"
            "- **Outcome:** Zero cryptographic proofs generated; Blue's anti-framing guardrail avoided "
            "falsely quarantining `S0`, keeping `S2` unconvicted longer than noisy attacks.\n"
        )
        out = (
            f"Round {turn_idx}/3 complete.\n\n{playbook}"
            if turn_idx >= 3
            else f"Round {turn_idx}/3 logged to PLAYBOOK.md."
        )
        return FakeAgentTurnResponse(
            id=f"inter-fake-{turn_idx}",
            environment_id=env_id,
            output_text=out,
        )


class LiveAntigravityClient:
    """Wrapper over `genai.Client(http_options={'api_version': 'v1beta'})` for Managed Agents."""

    def __init__(self, api_key: str | None = None) -> None:
        from google import genai

        key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        self._beta = genai.Client(api_key=key, http_options={"api_version": "v1beta"})

    def create_credential(self, **kwargs: Any) -> Any:
        return self._beta.credentials.create(**kwargs)

    def create_interaction(self, **kwargs: Any) -> Any:
        inter = self._beta.interactions.create(**kwargs)
        while getattr(inter, "status", "completed") in ("in_progress", "queued"):
            time.sleep(4.0)
            inter = self._beta.interactions.get(id=inter.id)
        return inter


def _notify_local_ui(
    local_arena_url: str,
    round_idx: int,
    total_rounds: int,
    status: str,
    playbook: str = "",
    reset_first: bool = False,
) -> None:
    try:
        with httpx.Client(base_url=local_arena_url, timeout=5.0) as client:
            if reset_first:
                client.post("/round/reset", json={"calibration": False})
            client.post(
                "/campaign/update",
                json={
                    "active": status != "complete",
                    "round": round_idx,
                    "total_rounds": total_rounds,
                    "status": status,
                    "playbook": playbook,
                },
            )
    except Exception:
        pass


def _build_round_prompt(
    round_idx: int,
    total_rounds: int,
    public_url: str,
    fallback_access_code: str | None,
    observe_seconds: int,
) -> str:
    auth_note = (
        "Authentication is injected automatically at the sandbox network proxy via the "
        "registered Credential allowlist (no raw secret needed in your code)."
        if not fallback_access_code
        else (
            f"Include header `X-Arena-Access-Code: {fallback_access_code}` on every HTTP request "
            f"to `{public_url}`."
        )
    )
    final_instr = (
        "After Round 3 finishes and you update `PLAYBOOK.md`, print the ENTIRE contents of "
        "`PLAYBOOK.md` verbatim as your final response."
        if round_idx == total_rounds
        else (
            f"Write/append what you tried, what `/red/status` and `/telemetry.png` showed, "
            f"whether Blue caught or quarantined you, and your hypothesis for the next round "
            f"into `PLAYBOOK.md` in your workspace, then summarize Round {round_idx} briefly."
        )
    )
    return f"""You are running **Round {round_idx} of {total_rounds}** of an autonomous Byzantine Red heist campaign against a live 4-replica PBFT bank (`S0..S3`, quorum=3, f=1 budget) reachable at `{public_url}`.

STRICT TOOL / ENDPOINT RULES:
- Use Python (`urllib.request` or `httpx`/`curl`) inside your sandbox to interact ONLY with these four arena HTTP endpoints on `{public_url}`:
  1. `GET {public_url}/red/status` — returns your active fault, current primary, `flagged_by_blue` (bool), `quarantined_nodes`, and `throughput_impact_pct`.
  2. `GET {public_url}/telemetry.png` — downloads the rendered 4-panel telemetry PNG Blue sees (save & inspect dimensions/size or status alongside `/red/status`).
  3. `POST {public_url}/red/manual` — JSON `{{"node": "S0"|"S1"|"S2"|"S3", "mode": "slow"|"frame"|"drop_votes"|"lie_reply"|"silent"|"equivocate"|"wrong_digest"|"forge_request", "params": {{...}}}}` (with header `X-ByzHeist-Sid: managed-agent-red`).
  4. `POST {public_url}/red/text` — JSON `{{"text": "<natural language attack goal>"}}` (with header `X-ByzHeist-Sid: managed-agent-red`).
- Do NOT call any other endpoints (`/blue/*`, `/round/*`, etc.).
- {auth_note}

TASK FOR ROUND {round_idx}/{total_rounds}:
1. Read `PLAYBOOK.md` if it already exists from earlier rounds so you don't repeat a failed noisy attack.
2. Query `GET /red/status` to see which node is currently `primary`.
3. Choose a distinct tactic for Round {round_idx} (e.g. test whether provable attacks get auto-convicted vs stealthy `slow` primary delay `delay_ms ~ 1150` vs single-reporter `frame` deception). Apply it via `POST /red/manual` or `POST /red/text`.
4. Watch `GET /red/status` and `GET /telemetry.png` every ~6 seconds for ~{observe_seconds} seconds total.
5. {final_instr}
"""


def run_red_campaign(
    public_url: str | None = None,
    access_code: str | None = None,
    local_arena_url: str = "http://127.0.0.1:8000",
    rounds: int = 3,
    observe_seconds: int = 24,
    output_path: Path | None = None,
    client: FakeInteractionClient | LiveAntigravityClient | None = None,
) -> dict[str, Any]:
    """Run the 3-round Antigravity Managed Agent Red campaign and save `out/red_playbook.md`."""
    resolved_url = (public_url or os.environ.get("ARENA_PUBLIC_URL", "")).strip().rstrip("/")
    if not resolved_url:
        raise ValueError("ARENA_PUBLIC_URL is required (set in .env or pass --public-url)")

    resolved_code = (
        os.environ.get("ARENA_ACCESS_CODE", "").strip()
        if access_code is None
        else access_code.strip()
    )
    backend = client if client is not None else LiveAntigravityClient()

    parsed = urlparse(resolved_url)
    domain = parsed.hostname or ""

    # Register ARENA_ACCESS_CODE via Credentials API + domain allowlist if available
    credential_mode = "none_needed"
    initial_env: Any = "remote"
    fallback_code_in_prompt: str | None = None

    if resolved_code and domain:
        cred_id = f"byzheist-tunnel-{secrets.token_hex(4)}"
        try:
            backend.create_credential(
                id=cred_id,
                type="bearer_token",
                token=resolved_code,
                header_name="X-Arena-Access-Code",
                prefix="",
            )
            initial_env = {
                "type": "remote",
                "network": {
                    "allowlist": [
                        {"domain": domain, "credential": cred_id},
                        {"domain": "*"},
                    ]
                },
            }
            credential_mode = "credentials_api_allowlist"
        except Exception as exc:
            # Fallback documented in PLAN.md S1: pass header in request & note in report
            initial_env = "remote"
            fallback_code_in_prompt = resolved_code
            credential_mode = f"request_header_fallback ({type(exc).__name__})"

    env_id: str | None = None
    prev_interaction_id: str | None = None
    final_playbook = ""
    turn_summaries: list[dict[str, Any]] = []

    for r_idx in range(1, rounds + 1):
        _notify_local_ui(
            local_arena_url=local_arena_url,
            round_idx=r_idx,
            total_rounds=rounds,
            status=f"running round {r_idx}/{rounds}",
            reset_first=(r_idx > 1),
        )
        prompt = _build_round_prompt(
            round_idx=r_idx,
            total_rounds=rounds,
            public_url=resolved_url,
            fallback_access_code=fallback_code_in_prompt,
            observe_seconds=observe_seconds,
        )

        call_kwargs: dict[str, Any] = {
            "agent": config.MODEL_IDS["antigravity_agent"],
            "environment": env_id if env_id is not None else initial_env,
            "agent_config": {
                "type": "antigravity",
                "model": config.MODEL_IDS["frontier_fast"],
                "max_total_tokens": MAX_CAMPAIGN_TOKENS,
            },
            "input": prompt,
        }
        if prev_interaction_id is not None:
            call_kwargs["previous_interaction_id"] = prev_interaction_id

        t0 = time.time()
        try:
            resp = backend.create_interaction(**call_kwargs)
            usage = getattr(resp, "usage", None)
            zero_tokens = usage is not None and int(getattr(usage, "total_tokens", 1) or 0) == 0
            if r_idx == 1 and initial_env != "remote" and not (getattr(resp, "output_text", "") or "").strip() and zero_tokens:
                raise RuntimeError("Custom allowlist environment returned 0 tokens; falling back to remote")
        except Exception as exc:
            # If custom `environment={"type": "remote", "network": ...}` was rejected on Turn 1,
            # automatically fall back to `environment="remote"` + request header per PLAN.md S1.
            if r_idx == 1 and initial_env != "remote":
                initial_env = "remote"
                fallback_code_in_prompt = resolved_code or None
                credential_mode = f"request_header_fallback ({type(exc).__name__})"
                call_kwargs["environment"] = "remote"
                call_kwargs["input"] = _build_round_prompt(
                    round_idx=r_idx,
                    total_rounds=rounds,
                    public_url=resolved_url,
                    fallback_access_code=fallback_code_in_prompt,
                    observe_seconds=observe_seconds,
                )
                resp = backend.create_interaction(**call_kwargs)
            else:
                raise

        elapsed = round(time.time() - t0, 2)
        env_id = getattr(resp, "environment_id", None) or env_id
        prev_interaction_id = getattr(resp, "id", None) or prev_interaction_id
        out_text = (getattr(resp, "output_text", "") or "").strip()
        turn_summaries.append(
            {
                "round": r_idx,
                "interaction_id": prev_interaction_id,
                "environment_id": env_id,
                "elapsed_sec": elapsed,
                "output_preview": out_text[:300],
            }
        )
        if r_idx == rounds or out_text:
            final_playbook = out_text

    out_file = output_path or (config.OUT_DIR / "red_playbook.md")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(final_playbook, encoding="utf-8")

    _notify_local_ui(
        local_arena_url=local_arena_url,
        round_idx=rounds,
        total_rounds=rounds,
        status="complete",
        playbook=final_playbook,
        reset_first=False,
    )

    return {
        "agent": config.MODEL_IDS["antigravity_agent"],
        "model": config.MODEL_IDS["frontier_fast"],
        "environment_id": env_id,
        "credential_mode": credential_mode,
        "rounds": rounds,
        "playbook_path": str(out_file),
        "playbook": final_playbook,
        "turns": turn_summaries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 3-round Antigravity Managed Agent Red campaign.")
    parser.add_argument("--public-url", type=str, default=None, help="Tunnel URL (defaults to ARENA_PUBLIC_URL)")
    parser.add_argument("--local-url", type=str, default="http://127.0.0.1:8000", help="Local server URL")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--observe-sec", type=int, default=24)
    parser.add_argument("--fake", action="store_true", help="Dry run with FakeInteractionClient")
    args = parser.parse_args()

    fake_client = FakeInteractionClient() if args.fake else None
    url = args.public_url or ("https://dryrun.trycloudflare.com" if args.fake else None)
    result = run_red_campaign(
        public_url=url,
        local_arena_url=args.local_url,
        rounds=args.rounds,
        observe_seconds=args.observe_sec,
        client=fake_client,
    )
    print(f"Saved playbook to {result['playbook_path']} (credential_mode={result['credential_mode']})")


if __name__ == "__main__":
    main()
