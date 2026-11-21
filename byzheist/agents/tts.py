"""Stretch S2: Spoken arena commentary via `gemini-3.8-flash-lite-tts` (8s cooldown, call-capped)."""

from __future__ import annotations

import base64
from collections import deque
import os
import struct
import time
from typing import Any

from byzheist import config
from byzheist.agents.gemini import CallBudgetCounter, RateLimitExceededError


ALLOWED_COMMENTARY_EVENTS: frozenset[str] = frozenset(
    {
        "red_plan_applied",
        "blue_verdict",
        "quarantine_approved",
        "replace_done",
        "referee_break",
    }
)


def wrap_pcm16_as_wav(pcm_bytes: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """Wrap raw 16-bit little-endian PCM bytes in a standard 44-byte RIFF WAV header."""
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    data_size = len(pcm_bytes)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        16,
        b"data",
        data_size,
    )
    return header + pcm_bytes


def make_fake_wav_clip() -> bytes:
    """Generate a tiny valid 50ms WAV clip for offline `FakeGemini` tests."""
    num_samples = 1200  # 50ms at 24kHz
    pcm = b"\x00\x00" * num_samples
    return wrap_pcm16_as_wav(pcm, sample_rate=24000, channels=1)


class ArenaCommentator:
    """Generates one-line TTS audio clips for key arena events (default off, <= 1 clip / 8s)."""

    def __init__(
        self,
        counter: CallBudgetCounter,
        use_fake: bool = True,
        min_interval_sec: float = 8.0,
        api_key: str | None = None,
    ) -> None:
        self.counter = counter
        self.use_fake = use_fake
        self.min_interval_sec = min_interval_sec
        self.enabled: bool = False  # Default off per PLAN.md S2
        self._last_clip_mono: float = -1e9
        self._clip_seq: int = 0
        self.latest_clip: dict[str, Any] | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=25)
        self._client: Any = None
        self._api_key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def _ensure_client(self) -> Any:
        if self._client is None:
            from google import genai

            if not self._api_key:
                raise RuntimeError("GEMINI_API_KEY is not set for live TTS")
            self._client = genai.Client(
                api_key=self._api_key,
                http_options={"api_version": "v1beta"},
            )
        return self._client

    async def announce(
        self,
        event_type: str,
        line: str,
        force: bool = False,
    ) -> dict[str, Any] | None:
        """Synthesize a spoken one-liner if `enabled`, allowed event, and >= 8s since last clip."""
        if not self.enabled:
            return None
        if event_type not in ALLOWED_COMMENTARY_EVENTS:
            return None
        clean_line = " ".join(line.strip().split())
        if not clean_line:
            return None

        now_mono = time.monotonic()
        if not force and (now_mono - self._last_clip_mono) < self.min_interval_sec:
            return None

        try:
            self.counter.record_call()
        except RateLimitExceededError:
            return None

        self._last_clip_mono = now_mono

        if self.use_fake:
            audio_bytes = make_fake_wav_clip()
            mime_type = "audio/wav"
        else:
            audio_bytes, mime_type = await self._synthesize_live(clean_line)

        self._clip_seq += 1
        clip = {
            "id": self._clip_seq,
            "timestamp": time.time(),
            "event_type": event_type,
            "text": clean_line,
            "mime_type": mime_type,
            "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
        }
        self.latest_clip = clip
        self.history.append(clip)
        return clip

    async def _synthesize_live(self, text_line: str) -> tuple[bytes, str]:
        client = self._ensure_client()
        prompt = f"Read this live security arena callout crisply: {text_line}"
        try:
            inter = await client.aio.interactions.create(
                model=config.MODEL_IDS["tts_fast"],
                input=prompt,
                response_modalities=["audio"],
            )
            out_audio = inter.output_audio
            if out_audio is not None and getattr(out_audio, "data", None):
                raw_data = out_audio.data
                raw_bytes = (
                    bytes(raw_data)
                    if isinstance(raw_data, (bytes, bytearray))
                    else base64.b64decode(str(raw_data))
                )
                mime = str(getattr(out_audio, "mime_type", None) or "audio/wav").lower()
                if "l16" in mime or "pcm" in mime or (
                    "wav" in mime and not raw_bytes.startswith(b"RIFF")
                ):
                    sr = int(getattr(out_audio, "sample_rate", None) or 24000)
                    raw_bytes = wrap_pcm16_as_wav(raw_bytes, sample_rate=sr)
                    mime = "audio/wav"
                return raw_bytes, mime
        except Exception:
            pass

        # Fallback via v1beta `models.generate_content` AUDIO modality
        from google.genai import types

        resp = await client.aio.models.generate_content(
            model=config.MODEL_IDS["tts_fast"],
            contents=text_line,
            config=types.GenerateContentConfig(response_modalities=["AUDIO"]),
        )
        part = resp.candidates[0].content.parts[0]
        inline = part.inline_data
        raw_bytes = (
            bytes(inline.data)
            if isinstance(inline.data, (bytes, bytearray))
            else base64.b64decode(str(inline.data))
        )
        mime = str(inline.mime_type or "audio/wav").lower()
        if "l16" in mime or "pcm" in mime or not raw_bytes.startswith((b"RIFF", b"ID3", b"\xff\xfb", b"OggS")):
            raw_bytes = wrap_pcm16_as_wav(raw_bytes, sample_rate=24000)
            mime = "audio/wav"
        return raw_bytes, mime
