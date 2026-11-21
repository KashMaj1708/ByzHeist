"""Phase 6 tests: `--fake` evaluation completes and writes `eval/results.json` and `EVAL.md`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from byzheist import config
from eval.run_eval import run_evaluation


@pytest.mark.asyncio
async def test_fake_eval_completes_and_writes_results_json_and_eval_md(tmp_path: Path) -> None:
    results_json = tmp_path / "results.json"
    eval_md = tmp_path / "EVAL.md"

    await run_evaluation(
        live=False,
        fast=True,
        seeded_rounds=8,
        surprise_rounds=2,
        results_path=results_json,
        eval_md_path=eval_md,
    )

    assert results_json.is_file()
    assert eval_md.is_file()

    loaded = json.loads(results_json.read_text(encoding="utf-8"))
    assert loaded["mode"] == "fake"
    assert loaded["seed"] == config.EVAL_SEED
    assert loaded["num_scenarios"] == 10

    summary = loaded["summary"]
    for sys_key in ("baseline_a", "baseline_b", "byzheist_blue"):
        assert sys_key in summary
        for metric in (
            "detection_rate_pct",
            "correct_culprit_rate_pct",
            "median_time_to_quarantine_sec",
            "false_quarantines",
            "referee_breaks",
            "avg_throughput_lost_pct",
        ):
            assert metric in summary[sys_key]

    # Safety holds under f=1 across all systems; ByzHeist Blue avoids Baseline B's frame false quarantines
    assert summary["byzheist_blue"]["referee_breaks"] == 0
    assert summary["byzheist_blue"]["false_quarantines"] == 0
    assert summary["byzheist_blue"]["correct_culprit_rate_pct"] > summary["baseline_a"]["correct_culprit_rate_pct"]

    md_text = eval_md.read_text(encoding="utf-8")
    assert "## System Comparison" in md_text
    assert "Conviction basis" in md_text
    assert "Evidence a third party can verify" in md_text
    assert "## 3 Worst / Hardest Cases" in md_text
