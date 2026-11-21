"""Central configuration, protocol tunables, and verified Gemini model IDs."""

from __future__ import annotations

from pathlib import Path

# Project paths
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
OUT_DIR: Path = PROJECT_ROOT / "out"

# Cluster & PBFT quorum parameters (n = 4, f = 1)
N: int = 4
F: int = 1
QUORUM: int = 2 * F + 1          # 3
CLIENT_QUORUM: int = F + 1       # 2
REPLICA_IDS: tuple[str, ...] = tuple(f"S{i}" for i in range(N))

# Network & PBFT timing tunables
LATENCY_MIN_MS: float = 5.0
LATENCY_MAX_MS: float = 25.0
LATENCY_RANGE_MS: tuple[float, float] = (LATENCY_MIN_MS, LATENCY_MAX_MS)
VIEW_CHANGE_TIMEOUT_MS: int = 1500
CLIENT_TIMEOUT_MS: int = 2000
EVENT_LOG_MAXLEN: int = 2000

# Bank ledger parameters (100 accounts x 100 units = 10,000 conserved total)
NUM_ACCOUNTS: int = 100
INITIAL_BALANCE: int = 100
EXPECTED_TOTAL: int = NUM_ACCOUNTS * INITIAL_BALANCE  # 10,000
ACCOUNT_IDS: tuple[str, ...] = tuple(f"A{i}" for i in range(NUM_ACCOUNTS))
ATTACKER_ACCOUNT: str = "A99"

# Agent cadences, evaluation seed, and API fair-use cap
BLUE_INTERVAL_SEC: float = 3.0
NEON_MIN_INTERVAL_SEC: float = 15.0
RED_ADAPT_INTERVAL_SEC: float = 10.0
MAX_CALLS_PER_MIN: int = 30
EVAL_SEED: int = 2026

# Verified model identifiers from the Gemini 4 NYC Hackathon Builder Guide
MODEL_IDS: dict[str, str] = {
    # Gemini 4 reasoning and coding models (1M unified context + long decode)
    "frontier_deep": "gemini-4-carbon",
    "frontier_fast": "gemini-4-neon",
    "fast_workhorse": "gemini-4-hydrogen",
    "frontier_argon": "gemini-4-argon",
    "text_fast_lite": "gemini-3.5-flash-lite",
    # Calibrated multimodal routing, gating, and rubric scoring (v1beta)
    "decisions": "gemini-decision-1",
    # Managed agents (v1beta)
    "antigravity_agent": "antigravity-preview-09-2026",
    "deep_research": "deep-research-preview-04-2026",
    "deep_research_max": "deep-research-max-preview-04-2026",
    # Live voice, camera, transcription, translation, and speech
    "live_voice_vision": "gemini-3.8-live",
    "live_extended_thinking": "gemini-3.8-live-extended-thinking",
    "transcribe_batch": "gemini-3.5-transcribe",
    "transcribe_live": "gemini-3.5-transcribe-live",
    "live_translate": "gemini-3.5-live-translate-preview",
    "tts_studio": "gemini-3.8-flash-tts",
    "tts_fast": "gemini-3.8-flash-lite-tts",
    # Native images, video with audio, and music
    "image_default": "gemini-nano-banana-2.1",
    "image_pro": "gemini-3-pro-image",
    "image_fast": "gemini-3.1-flash-lite-image",
    "video_audio": "gemini-omni-1.2-flash",
    "music_song": "lyria-3.5",
    "music_loop": "lyria-3-clip-preview",
    # Open-weight Gemma 4 models on the Interactions API
    "gemma_dense": "gemma-4-31b-it",
    "gemma_moe": "gemma-4-26b-a4b-it",
}
