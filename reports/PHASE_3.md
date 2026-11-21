# Phase 3 report
Status: DONE
Finished at: 2:46 PM EST (Nov 21, 2026)

## Built
- `byzheist/telemetry.py`:
  - `snapshot(cluster) -> dict`: per-replica `view`, `is_primary`, `quarantined`, `last_exec_seq`, 10 s `commits_per_sec`, 3-peer median `median_outbound_latency_ms`, `reported_observations`, and `evidence_count`; cluster 60 s `throughput_timeline`, `view_change_times`, `quarantine_times`, `replace_times`, `recent_events`, `4x4` `problem_matrix`, and cryptographically `verified_evidence` (zero ground-truth keys).
  - `render_png(snapshot) -> bytes`: `1200x800` 4-panel `matplotlib` (`Agg` backend) telemetry dashboard (4x4 problem heatmap, peer-consensus median outbound latency bars, 60 s throughput curve with view-change & quarantine markers, and verified cryptographic proof / replica status panel), persisted to `out/telemetry_latest.png`.
- `byzheist/pbft/network.py` & `byzheist/pbft/replica.py`: switched message send/arrival latency measurement from coarse 15.6 ms `monotonic()` to high-resolution `perf_counter()` and added `view_change_mono_times` so Windows sub-millisecond link latencies and 60 s timelines are exact.
- `tests/test_phase3.py`: 3 async tests verifying zero ground-truth key leakage in `snapshot()`, valid `> 10 KB` PNG rendering across honest / `slow` / `equivocate` runs, and single-reporter `frame` heatmap asymmetry (`lie_score > 50` vs honest `< 5` while peer-consensus median latency bar stays normal).

## Tests
```
..............                                                           [100%]
14 passed in 24.18s
......                                                                   [100%]
6 passed in 11.18s
```
All 20 tests across Phases 0–3 (`5` Phase 0 + `6` Phase 1 + `6` Phase 2 + `3` Phase 3) passed; 0 failed, 0 skipped.

## Deviations from PLAN.md
- Used 3-peer median across non-quarantined reporters for Panel 2's outbound latency bars so a single `frame` liar lights up only its own heatmap cell `(row=liar, col=target)` without distorting the target's peer-consensus latency bar.

## Known issues
- none

## Demo check
Command:
```powershell
ByzHeist\.venv\Scripts\pytest.exe -c ByzHeist\pyproject.toml ByzHeist\tests\test_phase3.py -q
```
Observed output:
- `3 passed in 6.09s`, `out/telemetry_latest.png` generated (`> 50 KB`, `1200x800` valid PNG) showing cell `(Reporter S3, Target S1) = 206.5 (m:25/x:12, 855ms)` in bright crimson while honest reporters `S0, S2` show `~0.0` and `S1`'s peer-consensus latency bar stays at normal `~15 ms`.

## Next
- Phase 4 (target 3:25 PM) will verify exact Gemini 4 & Decisions API SDK call shapes via official Gemini docs/skills, then implement `byzheist/agents/gemini.py` (`decide`, `reason`, call cap, `FakeGemini`), `byzheist/agents/blue.py` (proof auto-quarantine + Decision 1 triage + Neon escalation), `byzheist/agents/red.py` (Carbon multimodal voice/text `AttackPlan` with `heard` field + Hydrogen 10 s adaptive loop), and `tests/test_phase4.py` (offline + `@pytest.mark.live` smoke test).
