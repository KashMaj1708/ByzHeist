"""Stretch S2 tests: default-off commentary toggle, 8s rate cooldown, call-cap counting, and live smoke."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient
import pytest

from byzheist.agents.gemini import CallBudgetCounter, has_api_key
from byzheist.agents.tts import ArenaCommentator
from byzheist.server import create_app


@pytest.mark.asyncio
async def test_commentator_default_off_cooldown_and_call_cap() -> None:
    counter = CallBudgetCounter(max_calls_per_min=3)
    tts = ArenaCommentator(counter=counter, use_fake=True, min_interval_sec=8.0)

    # Default off -> emits nothing and consumes 0 API calls
    assert tts.enabled is False
    assert await tts.announce("red_plan_applied", "Red deploys slow on S0.") is None
    assert counter.total_calls == 0

    # Enable commentary -> first key event emits WAV clip and increments call cap counter
    tts.set_enabled(True)
    clip1 = await tts.announce("red_plan_applied", "Red deploys slow on S0.")
    assert clip1 is not None
    assert clip1["id"] == 1
    assert counter.total_calls == 1
    wav_bytes = base64.b64decode(clip1["audio_b64"])
    assert wav_bytes.startswith(b"RIFF") and b"WAVE" in wav_bytes[:16]

    # Immediate second call within 8s cooldown is suppressed
    clip2 = await tts.announce("blue_verdict", "Blue flags S0!")
    assert clip2 is None
    assert counter.total_calls == 1

    # Unknown event type is ignored even after cooldown reset
    tts._last_clip_mono -= 10.0
    assert await tts.announce("minor_tick", "Routine tick") is None


def test_server_commentary_toggle_endpoint() -> None:
    app = create_app(use_fake_gemini=True, load_rate_hz=0.0, blue_interval_sec=10.0)
    with TestClient(app) as client:
        with client.websocket_connect("/ws?sid=tts-browser") as ws:
            st0 = ws.receive_json()
            assert st0["commentary"]["enabled"] is False

        r_on = client.post("/commentary/toggle", json={"enabled": True})
        assert r_on.status_code == 200
        assert r_on.json()["enabled"] is True

        # Trigger a Red manual fault and verify commentary clip is generated
        client.post(
            "/red/manual",
            json={"node": "S0", "mode": "slow"},
            headers={"X-ByzHeist-Sid": "tts-browser"},
        )
        with client.websocket_connect("/ws?sid=tts-browser") as ws:
            st1 = ws.receive_json()
            assert st1["commentary"]["enabled"] is True
            assert st1["commentary"]["latest_clip"] is not None
            assert "S0" in st1["commentary"]["latest_clip"]["text"]


@pytest.mark.live
@pytest.mark.skipif(not has_api_key(), reason="GEMINI_API_KEY is not set")
@pytest.mark.asyncio
async def test_live_tts_smoke() -> None:
    counter = CallBudgetCounter(max_calls_per_min=10)
    tts = ArenaCommentator(counter=counter, use_fake=False, min_interval_sec=1.0)
    tts.set_enabled(True)
    clip = await tts.announce(
        "blue_verdict",
        "Blue watchdog convicted replica S0 with cryptographic proof and restored quorum!",
    )
    assert clip is not None
    raw = base64.b64decode(clip["audio_b64"])
    assert len(raw) > 500
