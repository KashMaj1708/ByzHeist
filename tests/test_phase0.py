"""Phase 0 tests: skeleton module imports, config sanity, and repository hygiene."""

from __future__ import annotations

import importlib
from pathlib import Path

from byzheist import config


EXPECTED_MODULES = [
    "byzheist",
    "byzheist.config",
    "byzheist.referee",
    "byzheist.telemetry",
    "byzheist.server",
    "byzheist.pbft",
    "byzheist.pbft.crypto",
    "byzheist.pbft.messages",
    "byzheist.pbft.network",
    "byzheist.pbft.replica",
    "byzheist.pbft.client",
    "byzheist.pbft.cluster",
    "byzheist.pbft.faults",
    "byzheist.pbft.evidence",
    "byzheist.agents",
    "byzheist.agents.gemini",
    "byzheist.agents.blue",
    "byzheist.agents.red",
    "byzheist.agents.budget",
    "eval.run_eval",
]


def test_all_skeleton_modules_import() -> None:
    for mod_name in EXPECTED_MODULES:
        mod = importlib.import_module(mod_name)
        assert mod is not None, f"Failed to import {mod_name}"


def test_config_pbft_and_ledger_sanity() -> None:
    assert config.N == 4
    assert config.F == 1
    assert config.QUORUM == 3
    assert config.CLIENT_QUORUM == 2
    assert config.REPLICA_IDS == ("S0", "S1", "S2", "S3")

    assert config.NUM_ACCOUNTS == 100
    assert config.INITIAL_BALANCE == 100
    assert config.EXPECTED_TOTAL == 10_000
    assert len(config.ACCOUNT_IDS) == 100
    assert config.ACCOUNT_IDS[0] == "A0"
    assert config.ACCOUNT_IDS[-1] == "A99"

    assert 0 < config.LATENCY_MIN_MS < config.LATENCY_MAX_MS
    assert config.LATENCY_RANGE_MS == (config.LATENCY_MIN_MS, config.LATENCY_MAX_MS)
    assert config.VIEW_CHANGE_TIMEOUT_MS == 1500
    assert config.CLIENT_TIMEOUT_MS == 2000
    assert config.MAX_CALLS_PER_MIN > 0


def test_config_model_ids() -> None:
    required_models = {
        "frontier_deep": "gemini-4-carbon",
        "frontier_fast": "gemini-4-neon",
        "fast_workhorse": "gemini-4-hydrogen",
        "decisions": "gemini-decision-1",
        "antigravity_agent": "antigravity-preview-09-2026",
        "transcribe_batch": "gemini-3.5-transcribe",
        "tts_fast": "gemini-3.8-flash-lite-tts",
    }
    for key, expected in required_models.items():
        assert config.MODEL_IDS.get(key) == expected


def test_third_party_dependencies_import() -> None:
    for pkg in ("nacl.signing", "fastapi", "uvicorn", "httpx", "matplotlib", "google.genai", "dotenv"):
        mod = importlib.import_module(pkg)
        assert mod is not None


def test_repo_skeleton_files_exist() -> None:
    root: Path = config.PROJECT_ROOT
    assert (root / ".gitignore").is_file()
    assert (root / ".env.example").is_file()
    assert (root / "README.md").is_file()
    assert (root / "byzheist" / "web" / "index.html").is_file()

    gitignore_text = (root / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "__pycache__/", ".venv/", "out/*.png"):
        assert pattern in gitignore_text

    readme_text = (root / "README.md").read_text(encoding="utf-8")
    assert "What was built at the hackathon" in readme_text
    assert "Nov" in readme_text and "2026" in readme_text
