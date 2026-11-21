"""Tests for judge-pool reservation, transient API error 503 handling, short audio 400 validation, and idle Blue skip."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from byzheist import telemetry
from byzheist.agents.blue import BlueWatchdog
from byzheist.agents.gemini import FakeGemini, RateLimitExceededError
from byzheist.agents.tts import wrap_pcm16_as_wav
from byzheist.pbft.cluster import Cluster
from byzheist.server import create_app


def _make_wav_duration(seconds: float, sample_rate: int = 16000) -> bytes:
    num_samples = int(seconds * sample_rate)
    return wrap_pcm16_as_wav(b"\x01\x00" * num_samples, sample_rate=sample_rate, channels=1)


def test_full_background_pool_still_allows_judge_voice_plan() -> None:
    fake = FakeGemini(max_calls_per_min=10, judge_reserved_per_min=8)
    app = create_app(
        use_fake_gemini=True,
        load_rate_hz=0.0,
        blue_interval_sec=60.0,
        red_adapt_interval_sec=60.0,
        gemini_override=fake,
        judge_retry_delay_sec=0.05,
    )
    with TestClient(app) as client:
        # Exhaust the background pool completely (limit = 10 - 8 = 2 calls)
        while True:
            try:
                fake.counter.record_call(priority="background")
            except RateLimitExceededError:
                break

        # Confirm another background call is blocked
        with pytest.raises(RateLimitExceededError):
            fake.counter.record_call(priority="background")

        # Judge voice plan (`>= 0.5s` and `>= 2 KB`) uses reserved judge pool and succeeds!
        valid_wav = _make_wav_duration(0.8, sample_rate=16000)  # ~25.6 KB, 0.8s
        resp = client.post(
            "/red/voice",
            content=valid_wav,
            headers={
                "Content-Type": "audio/wav",
                "X-Audio-Duration-Ms": "800",
                "X-ByzHeist-Sid": "judge-voice-1",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["applied"] is True
        assert data["plan"]["heard"] != ""


def test_forced_api_error_retries_once_and_returns_503_json_not_500() -> None:
    attempts = {"count": 0}

    def _failing_reason(*args, **kwargs):
        attempts["count"] += 1
        raise RuntimeError("Upstream Gemini 503 service unavailable")

    fake = FakeGemini(max_calls_per_min=30, judge_reserved_per_min=8, reason_fn=_failing_reason)
    app = create_app(
        use_fake_gemini=True,
        load_rate_hz=0.0,
        blue_interval_sec=60.0,
        red_adapt_interval_sec=60.0,
        gemini_override=fake,
        judge_retry_delay_sec=0.05,
    )
    with TestClient(app) as client:
        resp = client.post(
            "/red/text",
            json={"text": "make the primary slow"},
            headers={"X-ByzHeist-Sid": "judge-err-test"},
        )
        assert resp.status_code == 503
        body = resp.json()
        assert "error" in body and "catching its breath" in body["error"]
        assert isinstance(body["retry_after_s"], int) and body["retry_after_s"] >= 1
        # Must have retried once (2 attempts total)
        assert attempts["count"] == 2


def test_too_short_or_tiny_audio_clip_returns_400_without_calling_gemini() -> None:
    fake = FakeGemini(max_calls_per_min=30, judge_reserved_per_min=8)
    app = create_app(
        use_fake_gemini=True,
        load_rate_hz=0.0,
        blue_interval_sec=60.0,
        red_adapt_interval_sec=60.0,
        gemini_override=fake,
    )
    with TestClient(app) as client:
        calls_before = fake.call_count

        # 1. Under 2 KB raw blob -> 400, 0 Gemini calls
        r_small = client.post(
            "/red/voice",
            content=b"tiny-webm-clip" * 20,
            headers={"Content-Type": "audio/webm", "X-ByzHeist-Sid": "judge-short"},
        )
        assert r_small.status_code == 400
        assert "hold the button while you speak" in r_small.json()["error"]
        assert fake.call_count == calls_before

        # 2. Over 2 KB (e.g. ~9.6 KB at 24kHz) but under 0.5s (0.2s WAV) -> 400, 0 Gemini calls
        short_wav = _make_wav_duration(0.2, sample_rate=24000)
        assert len(short_wav) > 2048
        r_short = client.post(
            "/red/voice",
            content=short_wav,
            headers={"Content-Type": "audio/wav", "X-ByzHeist-Sid": "judge-short"},
        )
        assert r_short.status_code == 400
        assert "hold the button while you speak" in r_short.json()["error"]
        assert fake.call_count == calls_before


@pytest.mark.asyncio
async def test_idle_blue_skips_decision1_when_telemetry_unchanged_but_renders_png() -> None:
    cluster = Cluster(seed=808, latency_range_ms=(1.5, 4.0), view_change_timeout_ms=300.0, client_timeout_ms=300.0)
    await cluster.start()
    fake = FakeGemini()
    blue_sb = cluster.budget.blue_interface(telemetry_fn=lambda: telemetry.snapshot(cluster))
    blue = BlueWatchdog(sandbox=blue_sb, gemini=fake, interval_sec=3.0)
    try:
        v1 = await blue.step()
        assert v1["mode"] == "monitoring"
        assert len(fake.decision_calls) == 1
        assert blue.latest_png is not None

        # Clear latest_png to prove tick 2 still renders the PNG while skipping Decision 1
        blue.latest_png = None
        v2 = await blue.step()
        assert v2["mode"] == "idle_skipped"
        assert len(fake.decision_calls) == 1  # No extra Decision 1 call!
        assert blue.latest_png is not None and blue.latest_png.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        await cluster.stop()
