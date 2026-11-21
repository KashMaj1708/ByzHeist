"""Phase 5 tests: FastAPI server, HTML UI, WebSocket state stream, manual budget log, tunnel auth, and Red seat lock."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from byzheist.server import create_app


def test_server_starts_with_fake_gemini_serves_html_ws_and_logs_budget_violation() -> None:
    app = create_app(
        use_fake_gemini=True,
        load_rate_hz=2.0,
        blue_interval_sec=2.0,
        red_adapt_interval_sec=30.0,
        access_code="",
    )
    with TestClient(app) as client:
        # 1. `/` returns Arena HTML
        resp = client.get("/")
        assert resp.status_code == 200
        assert "ByzHeist Arena" in resp.text
        assert "Bank total: 10,000" in resp.text

        # 2. `/telemetry.png` returns valid PNG bytes
        png_resp = client.get("/telemetry.png")
        assert png_resp.status_code == 200
        assert png_resp.headers["content-type"] == "image/png"
        assert png_resp.content.startswith(b"\x89PNG\r\n\x1a\n")

        # 3. WebSocket `/ws` sends state snapshot immediately
        with client.websocket_connect("/ws?sid=test-browser-1") as ws:
            state = ws.receive_json()
            assert state["bank_total"] == 10_000
            assert state["total_ok"] is True
            assert state["agreement_ok"] is True
            assert set(state["nodes"].keys()) == {"S0", "S1", "S2", "S3"}

        # 4. `POST /red/manual` sets first fault, then refuses second Byzantine node & logs in anti_cheat_log
        headers = {"X-ByzHeist-Sid": "test-browser-1"}
        r1 = client.post(
            "/red/manual",
            json={"node": "S1", "mode": "wrong_digest"},
            headers=headers,
        )
        assert r1.status_code == 200
        assert r1.json()["applied"] is True

        r2 = client.post(
            "/red/manual",
            json={"node": "S2", "mode": "slow"},
            headers=headers,
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["applied"] is False
        assert len(body2["anti_cheat_log"]) >= 1
        assert "budget exceeded" in body2["anti_cheat_log"][-1]["reason"].lower()


def test_tunnel_access_code_and_shared_red_seat_lock() -> None:
    app = create_app(
        use_fake_gemini=True,
        load_rate_hz=0.0,
        blue_interval_sec=10.0,
        red_adapt_interval_sec=30.0,
        access_code="nyc-sigma-5",
    )
    with TestClient(app) as client:
        # 1. Direct localhost (no Cf-Connecting-IP / X-Forwarded-For) bypasses access code
        local_ok = client.get("/")
        assert local_ok.status_code == 200
        assert "Hold to Speak Attack" in local_ok.text

        # 2. Simulated cloudflared tunnel request (`Cf-Connecting-IP` present) is blocked without code
        tunnel_headers = {"Cf-Connecting-IP": "198.51.100.42"}
        unauth_html = client.get("/", headers=tunnel_headers)
        assert unauth_html.status_code == 401
        assert 'name="code"' in unauth_html.text

        unauth_api = client.post(
            "/red/surprise",
            headers={**tunnel_headers, "X-ByzHeist-Sid": "judge-1"},
        )
        assert unauth_api.status_code == 401

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers=tunnel_headers) as ws:
                ws.receive_json()

        # 3. Opening `/?code=nyc-sigma-5` sets httponly auth cookie and unlocks endpoints + WS
        login_resp = client.get("/?code=nyc-sigma-5", headers=tunnel_headers)
        assert login_resp.status_code == 200
        assert "byzheist_auth" in login_resp.cookies

        # 4. Shared Red seat lock: judge-1 claims Red seat; judge-2 gets 409 on Red but CAN call Blue
        j1_resp = client.post(
            "/red/text",
            json={"text": "make the primary slow"},
            headers={**tunnel_headers, "X-ByzHeist-Sid": "judge-1"},
        )
        assert j1_resp.status_code == 200
        assert j1_resp.json()["plan"]["heard"] != ""

        j2_red = client.post(
            "/red/surprise",
            headers={**tunnel_headers, "X-ByzHeist-Sid": "judge-2"},
        )
        assert j2_red.status_code == 409
        assert "seat taken" in j2_red.json()["detail"].lower()

        j2_blue = client.post(
            "/blue/deny",
            headers={**tunnel_headers, "X-ByzHeist-Sid": "judge-2"},
        )
        assert j2_blue.status_code == 200
