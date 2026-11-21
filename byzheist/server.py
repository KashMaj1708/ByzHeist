"""FastAPI server + ~4 Hz WebSocket stream + tunnel access-code guard + 60s Red seat lock."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from byzheist import config, referee, telemetry
from byzheist.agents.blue import BlueWatchdog
from byzheist.agents.gemini import FakeGemini, GeminiBackend, has_api_key
from byzheist.agents.red import RedAttacker
from byzheist.agents.tts import ArenaCommentator
from byzheist.pbft.cluster import Cluster


load_dotenv(config.PROJECT_ROOT / ".env")

WEB_DIR: Path = Path(__file__).resolve().parent / "web"
AUTH_COOKIE_NAME = "byzheist_auth"
SID_COOKIE_NAME = "byzheist_sid"
RED_SEAT_TTL_SEC = 60.0


def _expected_auth_token(access_code: str) -> str:
    return hashlib.sha256(f"byzheist-arena:{access_code}".encode("utf-8")).hexdigest()


def _is_direct_localhost(headers: Any, client_host: str) -> bool:
    """Return True iff request is from local loopback WITHOUT tunnel forwarding headers."""
    has_cf = bool(headers.get("cf-connecting-ip"))
    has_xff = bool(headers.get("x-forwarded-for"))
    if has_cf or has_xff:
        return False
    return client_host in {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_authorized(headers: Any, cookies: dict[str, str], client_host: str, access_code: str) -> bool:
    if not access_code:
        return True
    if _is_direct_localhost(headers, client_host):
        return True
    hdr_code = (headers.get("x-arena-access-code") or "").strip()
    if hdr_code and hmac.compare_digest(hdr_code, access_code):
        return True
    token = cookies.get(AUTH_COOKIE_NAME, "")
    if not token:
        return False
    return hmac.compare_digest(token, _expected_auth_token(access_code))


def _login_html(invalid: bool = False) -> str:
    err_html = (
        '<p style="color:#f87171;margin:0 0 12px;font-size:14px;">Invalid access code.</p>'
        if invalid
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ByzHeist Arena — Access Code</title>
  <style>
    body {{
      margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: #0b1120; color: #f8fafc; font-family: system-ui, -apple-system, sans-serif;
    }}
    .card {{
      background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 28px 32px;
      width: 100%; max-width: 360px; box-shadow: 0 20px 40px rgba(0,0,0,0.45);
    }}
    h1 {{ margin: 0 0 8px; font-size: 20px; }}
    p {{ color: #94a3b8; font-size: 13px; margin: 0 0 16px; }}
    input {{
      width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px;
      border: 1px solid #475569; background: #0f172a; color: #f8fafc; font-size: 15px;
      margin-bottom: 12px;
    }}
    button {{
      width: 100%; padding: 10px 14px; border-radius: 8px; border: none; cursor: pointer;
      background: #ef4444; color: white; font-weight: 600; font-size: 14px;
    }}
  </style>
</head>
<body>
  <form class="card" method="GET" action="/">
    <h1>ByzHeist Arena</h1>
    <p>Enter the judge access code to join the live PBFT arena.</p>
    {err_html}
    <input type="password" name="code" placeholder="Access code" autofocus required />
    <button type="submit">Unlock Arena</button>
  </form>
</body>
</html>"""


class ArenaState:
    """Manages one live PBFT round (Cluster + Load + BlueWatchdog + RedAttacker + Seat Lock)."""

    def __init__(
        self,
        use_fake_gemini: bool | None = None,
        load_rate_hz: float = 4.0,
        blue_interval_sec: float = config.BLUE_INTERVAL_SEC,
        red_adapt_interval_sec: float = config.RED_ADAPT_INTERVAL_SEC,
        access_code: str | None = None,
    ) -> None:
        if use_fake_gemini is None:
            use_fake_gemini = not has_api_key()
        self.use_fake_gemini = use_fake_gemini
        self.load_rate_hz = load_rate_hz
        self.blue_interval_sec = blue_interval_sec
        self.red_adapt_interval_sec = red_adapt_interval_sec
        self.access_code = (
            os.environ.get("ARENA_ACCESS_CODE", "").strip()
            if access_code is None
            else access_code.strip()
        )

        self.gemini: GeminiBackend | FakeGemini = (
            FakeGemini() if self.use_fake_gemini else GeminiBackend()
        )
        self.commentator = ArenaCommentator(
            counter=self.gemini.counter,
            use_fake=self.use_fake_gemini,
            min_interval_sec=8.0,
        )
        self._referee_broken_announced: bool = False

        self.round_number: int = 0
        self.revealed: bool = False
        self.cluster: Cluster | None = None
        self.blue: BlueWatchdog | None = None
        self.red: RedAttacker | None = None
        self._latest_png: bytes = b""
        self._event_cursor: int = 0

        # Shared Red seat lock (60s inactivity timeout)
        self.red_seat_holder: str | None = None
        self.red_seat_expires_mono: float = 0.0

        # Stretch S1: Antigravity Managed Agent campaign status + playbook
        playbook_file = config.OUT_DIR / "red_playbook.md"
        initial_pb = playbook_file.read_text(encoding="utf-8") if playbook_file.is_file() else ""
        self.campaign_state: dict[str, Any] = {
            "active": False,
            "round": 3 if initial_pb else 0,
            "total_rounds": 3,
            "status": "complete" if initial_pb else "idle",
            "playbook": initial_pb,
        }
        self._baseline_tps: float = max(0.5, load_rate_hz)

        self._ws_clients: dict[WebSocket, str] = {}
        self._broadcast_task: asyncio.Task[None] | None = None
        self._running: bool = False
        self._reset_lock = asyncio.Lock()

    async def start(self) -> None:
        self._running = True
        await self._init_round(calibration=False)
        self._broadcast_task = asyncio.create_task(self._ws_push_loop())

    async def stop(self) -> None:
        self._running = False
        if self._broadcast_task is not None and not self._broadcast_task.done():
            self._broadcast_task.cancel()
            await asyncio.gather(self._broadcast_task, return_exceptions=True)
        self._broadcast_task = None
        await self._teardown_round()

    async def _teardown_round(self) -> None:
        if self.red is not None:
            await self.red.stop_adaptive_loop()
        if self.blue is not None:
            await self.blue.stop()
        if self.cluster is not None:
            await self.cluster.stop()

    async def _init_round(self, calibration: bool = False) -> None:
        self.round_number += 1
        self.revealed = False
        self._referee_broken_announced = False
        cluster = Cluster(seed=1000 + self.round_number, calibration=calibration)
        await cluster.start()

        def _on_blue_event(ev: dict[str, Any]) -> None:
            mode = ev.get("mode", "")
            if mode == "proof_conviction":
                suspect = ev.get("suspect")
                atk = ev.get("attack_type")
                rep_ev = ev.get("replace_event") or {}
                seq = rep_ev.get("seq", 0)

                async def _announce_pair() -> None:
                    await self.commentator.announce(
                        "blue_verdict",
                        f"Replica {suspect} quarantined on cryptographic proof of {atk}!",
                        force=True,
                    )
                    await self.commentator.announce(
                        "replace_done",
                        f"Replica {suspect} replaced with fresh key and state transferred at sequence {seq}.",
                        force=True,
                    )

                asyncio.create_task(_announce_pair())
            elif mode == "pending_quarantine":
                pq = ev.get("pending_quarantine") or {}
                line = f"Blue watchdog flagged {pq.get('suspect')} for {pq.get('attack_type')}, awaiting human approval."
                asyncio.create_task(self.commentator.announce("blue_verdict", line))

        blue_sb = cluster.budget.blue_interface(telemetry_fn=lambda: telemetry.snapshot(cluster))
        blue = BlueWatchdog(
            sandbox=blue_sb,
            gemini=self.gemini,
            interval_sec=self.blue_interval_sec,
            on_event=_on_blue_event,
        )

        def _red_status() -> dict[str, Any]:
            snap = blue.latest_snapshot or telemetry.snapshot(cluster)
            primary = next(
                (rid for rid, ri in snap["replicas"].items() if ri["is_primary"]),
                "S0",
            )
            return {
                "primary": primary,
                "calibration": cluster.budget.calibration,
                "max_byzantine": cluster.budget.max_byzantine,
                "active_byzantine_count": len(cluster.budget.active_byzantine_nodes()),
                "blue_pending_quarantine": blue.pending_quarantine,
                "blue_latest_decision": blue.latest_decision,
            }

        red_sb = cluster.budget.red_interface(actor="red")
        red = RedAttacker(
            sandbox=red_sb,
            gemini=self.gemini,
            status_provider=_red_status,
            png_provider=lambda: self._latest_png or None,
            adapt_interval_sec=self.red_adapt_interval_sec,
        )

        # Render initial PNG synchronously before starting loops so /telemetry.png is immediately ready
        init_snap = telemetry.snapshot(cluster)
        self._latest_png = telemetry.render_png(init_snap)
        blue.latest_snapshot = init_snap
        blue.latest_png = self._latest_png

        self.cluster = cluster
        self.blue = blue
        self.red = red

        if self.load_rate_hz > 0:
            cluster.start_load_generator(rate_hz=self.load_rate_hz)
        await blue.start()
        await red.start_adaptive_loop()

    async def reset_round(self, calibration: bool = False) -> None:
        async with self._reset_lock:
            await self._teardown_round()
            self.red_seat_holder = None
            self.red_seat_expires_mono = 0.0
            await self._init_round(calibration=calibration)

    # ------------------------------------------------------------------
    # Red seat lock (60s inactivity TTL)
    # ------------------------------------------------------------------

    def claim_red_seat(self, client_sid: str) -> tuple[bool, float]:
        now = time.monotonic()
        if (
            self.red_seat_holder is None
            or now >= self.red_seat_expires_mono
            or self.red_seat_holder == client_sid
        ):
            self.red_seat_holder = client_sid
            self.red_seat_expires_mono = now + RED_SEAT_TTL_SEC
            return True, RED_SEAT_TTL_SEC
        return False, max(0.0, self.red_seat_expires_mono - now)

    def seat_status_for(self, client_sid: str) -> dict[str, Any]:
        now = time.monotonic()
        active = self.red_seat_holder is not None and now < self.red_seat_expires_mono
        if not active:
            return {"taken": False, "is_yours": False, "remaining_sec": 0.0}
        return {
            "taken": True,
            "is_yours": self.red_seat_holder == client_sid,
            "remaining_sec": round(self.red_seat_expires_mono - now, 1),
        }

    # ------------------------------------------------------------------
    # State snapshot pushed to UI at ~4 Hz
    # ------------------------------------------------------------------

    def build_ui_state(self, client_sid: str = "") -> dict[str, Any]:
        assert self.cluster is not None and self.blue is not None and self.red is not None
        cluster = self.cluster
        blue = self.blue
        red = self.red

        snap = telemetry.snapshot(cluster)
        if blue.latest_png:
            self._latest_png = blue.latest_png

        honest_ids = cluster._honest_ids()
        ref = referee.check(cluster, honest_ids=honest_ids)
        if (not ref["total_ok"] or not ref["agreement_ok"]) and not self._referee_broken_announced:
            self._referee_broken_announced = True
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self.commentator.announce(
                        "referee_break",
                        "Alert! Colluding Byzantine attack broke honest replica agreement!",
                    )
                )
            except RuntimeError:
                pass
        # Display conserved total across honest replicas (or minimum if broken)
        honest_totals = [ref["totals_per_replica"][r] for r in honest_ids] or [10_000]
        bank_total = honest_totals[0] if len(set(honest_totals)) == 1 else min(honest_totals)

        now_mono = time.monotonic()
        replacing_set = {
            ev["replica_id"]
            for ev in cluster.replace_events
            if (now_mono - ev["mono_t"]) <= 3.0
        }
        new_key_set = {
            ev["replica_id"]
            for ev in cluster.replace_events
            if (now_mono - ev["mono_t"]) <= 10.0
        }
        suspected_id: str | None = None
        if blue.pending_quarantine:
            suspected_id = blue.pending_quarantine.get("suspect")
        elif blue.latest_decision:
            s_ans = blue.latest_decision.get("suspect", {})
            u_ans = blue.latest_decision.get("under_attack", {})
            if float(u_ans.get("noul", 0.0)) >= 0.5 and s_ans.get("choice") in config.REPLICA_IDS:
                suspected_id = s_ans.get("choice")

        nodes_ui: dict[str, Any] = {}
        for rid in cluster.replica_ids:
            ri = snap["replicas"][rid]
            if rid in cluster.quarantined:
                visual_state = "quarantined"
            elif rid in replacing_set:
                visual_state = "replacing"
            elif self.revealed and cluster._faults[rid].is_byzantine:
                visual_state = "byzantine_revealed"
            elif rid == suspected_id:
                visual_state = "suspected"
            else:
                visual_state = "honest"

            node_entry: dict[str, Any] = {
                "id": rid,
                "visual_state": visual_state,
                "is_primary": ri["is_primary"],
                "view": ri["view"],
                "last_exec_seq": ri["last_exec_seq"],
                "commits_per_sec": ri["commits_per_sec"],
                "median_outbound_latency_ms": ri["median_outbound_latency_ms"],
                "quarantined": ri["quarantined"],
                "new_key_badge": rid in new_key_set,
            }
            if self.revealed:
                node_entry["revealed_mode"] = cluster._faults[rid].mode
            nodes_ui[rid] = node_entry

        recent_bus = list(cluster.network.event_log)[-28:]
        bus_msgs = [
            {
                "t": round(ev["t"], 3),
                "src": ev["src"],
                "dst": ev["dst"],
                "type": ev["type"],
                "seq": ev["seq"],
                "dropped": ev["dropped"],
            }
            for ev in recent_bus
            if ev["src"] != ev["dst"]
        ]

        clean_q_events = [
            {
                "id": ev.get("id", idx + 1),
                "timestamp": ev["timestamp"],
                "node": ev.get("node", ev["replica_id"]),
                "replica_id": ev["replica_id"],
                "attack_type": ev.get("attack_type", "unknown"),
                "source": ev.get("source", "manual"),
            }
            for idx, ev in enumerate(cluster.quarantine_events[-10:])
        ]
        clean_rep_events = [
            {
                "id": ev.get("id", idx + 1),
                "timestamp": ev["timestamp"],
                "node": ev.get("node", ev["replica_id"]),
                "replica_id": ev["replica_id"],
                "seq": ev.get("seq", ev.get("transferred_seq", 0)),
                "transferred_seq": ev.get("transferred_seq", 0),
                "attack_type": ev.get("attack_type", "unknown"),
                "source": ev.get("source", "manual"),
            }
            for idx, ev in enumerate(cluster.replace_events[-10:])
        ]

        return {
            "round": self.round_number,
            "revealed": self.revealed,
            "fake_gemini": self.use_fake_gemini,
            "calibration": cluster.budget.calibration,
            "api_calls_last_min": self.gemini.counter.calls_in_last_minute(),
            "api_calls_max_per_min": self.gemini.counter.max_calls_per_min,
            "bank_total": bank_total,
            "total_ok": ref["total_ok"],
            "agreement_ok": ref["agreement_ok"],
            "nodes": nodes_ui,
            "bus_messages": bus_msgs,
            "throughput_timeline": snap["cluster"]["throughput_timeline"],
            "verified_evidence": snap["verified_evidence"],
            "quarantine_events": clean_q_events,
            "replace_events": clean_rep_events,
            "quarantine_event": clean_q_events[-1] if clean_q_events else None,
            "replace_event": clean_rep_events[-1] if clean_rep_events else None,
            "blue": {
                "decision": blue.latest_decision,
                "neon_verdict": blue.latest_neon_verdict,
                "explanation": blue.latest_explanation,
                "pending_quarantine": blue.pending_quarantine,
                "timeline": blue.timeline[-15:],
            },
            "red": {
                "seat": self.seat_status_for(client_sid),
                "latest_plan": red.latest_plan.to_dict() if red.latest_plan else None,
                "active_node": red.active_node if self.seat_status_for(client_sid)["is_yours"] or self.revealed else None,
                "active_mode": red.active_mode if self.seat_status_for(client_sid)["is_yours"] or self.revealed else None,
                "last_eviction": red.last_eviction,
                "history": red.history[-8:] if self.seat_status_for(client_sid)["is_yours"] or self.revealed else [],
                "campaign": self.campaign_state,
            },
            "anti_cheat_log": cluster.budget.anti_cheat_log[-20:],
            "commentary": {
                "enabled": self.commentator.enabled,
                "latest_clip": self.commentator.latest_clip,
                "clips": list(self.commentator.history)[-5:],
            },
        }

    async def _ws_push_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(0.25)
                if not self._ws_clients or self.cluster is None:
                    continue
                dead: list[WebSocket] = []
                for ws, sid in list(self._ws_clients.items()):
                    try:
                        payload = self.build_ui_state(client_sid=sid)
                        await ws.send_text(json.dumps(payload))
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    self._ws_clients.pop(ws, None)
        except asyncio.CancelledError:
            return


def _resolve_client_sid(request: Request) -> str:
    hdr = request.headers.get("x-byzheist-sid", "").strip()
    if hdr:
        return hdr
    cookie_sid = request.cookies.get(SID_COOKIE_NAME, "").strip()
    if cookie_sid:
        return cookie_sid
    return f"client-{request.client.host if request.client else 'local'}"


def create_app(
    use_fake_gemini: bool | None = None,
    load_rate_hz: float = 4.0,
    blue_interval_sec: float = config.BLUE_INTERVAL_SEC,
    red_adapt_interval_sec: float = config.RED_ADAPT_INTERVAL_SEC,
    access_code: str | None = None,
) -> FastAPI:
    arena = ArenaState(
        use_fake_gemini=use_fake_gemini,
        load_rate_hz=load_rate_hz,
        blue_interval_sec=blue_interval_sec,
        red_adapt_interval_sec=red_adapt_interval_sec,
        access_code=access_code,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await arena.start()
        try:
            yield
        finally:
            await arena.stop()

    app = FastAPI(title="ByzHeist Arena", lifespan=lifespan)
    app.state.arena = arena

    def _require_auth(request: Request) -> None:
        host = request.client.host if request.client else ""
        if not _is_authorized(request.headers, request.cookies, host, arena.access_code):
            raise HTTPException(status_code=401, detail="Arena access code required")

    def _require_red_seat(request: Request) -> str:
        sid = _resolve_client_sid(request)
        ok, rem = arena.claim_red_seat(sid)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=f"Red seat taken ({rem:.0f}s of inactivity remaining)",
            )
        return sid

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, code: str | None = None) -> Response:
        host = request.client.host if request.client else ""
        set_auth_cookie = False

        if arena.access_code and not _is_direct_localhost(request.headers, host):
            if code is not None:
                if hmac.compare_digest(code.strip(), arena.access_code):
                    set_auth_cookie = True
                else:
                    return HTMLResponse(_login_html(invalid=True), status_code=401)
            elif not _is_authorized(request.headers, request.cookies, host, arena.access_code):
                return HTMLResponse(_login_html(invalid=False), status_code=401)

        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        resp = HTMLResponse(html, status_code=200)
        if SID_COOKIE_NAME not in request.cookies:
            resp.set_cookie(
                key=SID_COOKIE_NAME,
                value=secrets.token_hex(12),
                httponly=False,
                samesite="lax",
            )
        if set_auth_cookie:
            resp.set_cookie(
                key=AUTH_COOKIE_NAME,
                value=_expected_auth_token(arena.access_code),
                httponly=True,
                samesite="lax",
            )
        return resp

    @app.get("/telemetry.png")
    async def get_telemetry_png(request: Request) -> Response:
        _require_auth(request)
        if not arena._latest_png and arena.cluster is not None:
            snap = telemetry.snapshot(arena.cluster)
            arena._latest_png = telemetry.render_png(snap)
        return Response(
            content=arena._latest_png,
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        host = ws.client.host if ws.client else ""
        if not _is_authorized(ws.headers, ws.cookies, host, arena.access_code):
            await ws.close(code=1008)
            return
        sid = (
            ws.query_params.get("sid", "").strip()
            or ws.cookies.get(SID_COOKIE_NAME, "").strip()
            or f"ws-{secrets.token_hex(6)}"
        )
        await ws.accept()
        arena._ws_clients[ws] = sid
        try:
            await ws.send_text(json.dumps(arena.build_ui_state(client_sid=sid)))
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            arena._ws_clients.pop(ws, None)

    @app.get("/red/status")
    async def red_status(request: Request) -> JSONResponse:
        _require_auth(request)
        assert arena.cluster is not None and arena.blue is not None and arena.red is not None
        snap = arena.blue.latest_snapshot or telemetry.snapshot(arena.cluster)
        primary = next(
            (rid for rid, ri in snap["replicas"].items() if ri["is_primary"]),
            "S0",
        )
        active_node = arena.red.active_node
        blue_suspect: str | None = None
        if arena.blue.pending_quarantine:
            blue_suspect = arena.blue.pending_quarantine.get("suspect")
        elif arena.blue.latest_decision:
            blue_suspect = arena.blue.latest_decision.get("suspect", {}).get("choice")

        was_replaced = bool(
            active_node
            and any(ev["replica_id"] == active_node for ev in arena.cluster.replace_events)
        )
        flagged_by_blue = bool(
            active_node
            and (
                blue_suspect == active_node
                or active_node in arena.cluster.quarantined
                or was_replaced
            )
        )

        honest_cps = [
            float(ri["commits_per_sec"])
            for rid, ri in snap["replicas"].items()
            if not ri["quarantined"]
        ]
        recent_cps = round(sum(honest_cps) / max(1, len(honest_cps)), 2)
        base_cps = max(0.5, arena._baseline_tps)
        impact_pct = round(max(0.0, min(100.0, (1.0 - (recent_cps / base_cps)) * 100.0)), 1)

        return JSONResponse(
            {
                "round": arena.round_number,
                "primary": primary,
                "active_node": active_node,
                "active_mode": "replaced_to_honest" if was_replaced else arena.red.active_mode,
                "active_params": arena.red.active_params,
                "flagged_by_blue": flagged_by_blue,
                "was_quarantined_or_replaced": was_replaced or (active_node in arena.cluster.quarantined if active_node else False),
                "blue_suspect": blue_suspect,
                "pending_quarantine": arena.blue.pending_quarantine is not None,
                "quarantined_nodes": sorted(arena.cluster.quarantined),
                "throughput_recent_cps": recent_cps,
                "throughput_impact_pct": impact_pct,
            }
        )

    @app.post("/campaign/update")
    async def campaign_update(request: Request) -> JSONResponse:
        _require_auth(request)
        body = await request.json()
        arena.campaign_state = {
            "active": bool(body.get("active", False)),
            "round": int(body.get("round", 0)),
            "total_rounds": int(body.get("total_rounds", 3)),
            "status": str(body.get("status", "idle")),
            "playbook": str(body.get("playbook") or arena.campaign_state.get("playbook", "")),
        }
        return JSONResponse({"ok": True, "campaign": arena.campaign_state})

    @app.post("/red/voice")
    async def red_voice(request: Request) -> JSONResponse:
        _require_auth(request)
        _require_red_seat(request)
        assert arena.red is not None
        content_type = request.headers.get("content-type", "audio/webm").split(";")[0].strip()
        audio_bytes = await request.body()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Empty audio payload")
        res = await arena.red.plan(
            audio_bytes,
            audio_mime_type=content_type or "audio/webm",
            apply=True,
        )
        if res.get("applied") and res.get("plan"):
            p = res["plan"]
            await arena.commentator.announce(
                "red_plan_applied",
                f"Red voice attack deployed {p['mode']} on replica {p['node']}.",
            )
        return JSONResponse(res)

    @app.post("/red/text")
    async def red_text(request: Request) -> JSONResponse:
        _require_auth(request)
        _require_red_seat(request)
        assert arena.red is not None
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="Missing `text` field")
        res = await arena.red.plan(text, apply=True)
        if res.get("applied") and res.get("plan"):
            p = res["plan"]
            await arena.commentator.announce(
                "red_plan_applied",
                f"Red attacker deployed {p['mode']} on replica {p['node']}.",
            )
        return JSONResponse(res)

    @app.post("/red/surprise")
    async def red_surprise(request: Request) -> JSONResponse:
        _require_auth(request)
        _require_red_seat(request)
        assert arena.red is not None
        res = await arena.red.plan("surprise", apply=True)
        if res.get("applied") and res.get("plan"):
            p = res["plan"]
            await arena.commentator.announce(
                "red_plan_applied",
                f"Red surprise plan deployed {p['mode']} on replica {p['node']}.",
            )
        return JSONResponse(res)

    @app.post("/red/manual")
    async def red_manual(request: Request) -> JSONResponse:
        _require_auth(request)
        _require_red_seat(request)
        assert arena.red is not None and arena.cluster is not None
        body = await request.json()
        if "calibration" in body and body["calibration"] is not None:
            arena.cluster.budget.set_calibration(bool(body["calibration"]))
        node = str(body.get("node", ""))
        mode = str(body.get("mode", ""))
        params = body.get("params") or {}
        if not node or not mode:
            return JSONResponse(
                {
                    "ok": True,
                    "calibration": arena.cluster.budget.calibration,
                    "anti_cheat_log": arena.cluster.budget.anti_cheat_log[-20:],
                }
            )
        res = arena.red.set_manual_fault(node=node, mode=mode, params=params)
        if res.ok and mode != "honest":
            await arena.commentator.announce(
                "red_plan_applied",
                f"Manual override set {mode} fault on replica {node}.",
            )
        return JSONResponse(
            {
                "ok": res.ok,
                "applied": res.ok,
                "reason": res.reason,
                "node": node,
                "mode": mode,
                "calibration": arena.cluster.budget.calibration,
                "anti_cheat_log": arena.cluster.budget.anti_cheat_log[-20:],
            }
        )

    @app.post("/blue/approve")
    async def blue_approve(request: Request) -> JSONResponse:
        _require_auth(request)
        assert arena.blue is not None
        approved = arena.blue.approve_pending()
        if approved is None:
            return JSONResponse({"ok": False, "approved": None})
        if not approved.get("ok", True):
            return JSONResponse(
                {
                    "ok": False,
                    "error": approved.get("error", "Quarantine/replace failed"),
                    "detail": approved.get("error", "Quarantine/replace failed"),
                    "traceback": approved.get("traceback", ""),
                },
                status_code=500,
            )
        node = approved["node"]
        atk = approved.get("attack_type", "unknown")
        seq = approved.get("seq", 0)
        await arena.commentator.announce(
            "quarantine_approved",
            f"Quarantine approved! Replica {node} isolated for {atk}.",
            force=True,
        )
        await arena.commentator.announce(
            "replace_done",
            f"Replica {node} replaced with fresh key and state transferred at sequence {seq}.",
            force=True,
        )
        return JSONResponse({"ok": True, "approved": approved})

    @app.post("/commentary/toggle")
    async def commentary_toggle(request: Request) -> JSONResponse:
        _require_auth(request)
        body = await request.json()
        enabled = bool(body.get("enabled", False))
        arena.commentator.set_enabled(enabled)
        return JSONResponse({"ok": True, "enabled": arena.commentator.enabled})

    @app.post("/blue/deny")
    async def blue_deny(request: Request) -> JSONResponse:
        _require_auth(request)
        assert arena.blue is not None
        denied = arena.blue.deny_pending()
        return JSONResponse({"ok": denied is not None, "denied": denied})

    @app.post("/round/reveal")
    async def round_reveal(request: Request) -> JSONResponse:
        _require_auth(request)
        arena.revealed = True
        return JSONResponse({"ok": True, "revealed": True})

    @app.post("/round/reset")
    async def round_reset(request: Request) -> JSONResponse:
        _require_auth(request)
        calibration = False
        try:
            body = await request.json()
            if isinstance(body, dict):
                calibration = bool(body.get("calibration", False))
        except Exception:
            pass
        await arena.reset_round(calibration=calibration)
        return JSONResponse({"ok": True, "round": arena.round_number, "calibration": calibration})

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run("byzheist.server:app", host="127.0.0.1", port=8000)
