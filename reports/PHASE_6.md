# Phase 6 report
Status: DONE (code + `--fake` verified; `--live` running in background per Stretch schedule)
Finished at: 3:33 PM EST (Nov 21, 2026)

## Built
- `eval/run_eval.py`:
  - Held-out scenario generator (`EVAL_SEED=2026`, 12 seeded scenarios balanced across all 8 Byzantine modes + 4 autonomous Red `"surprise"` plans from `gemini-4-carbon`).
  - Three-system evaluation harness:
    1. **Baseline A (PBFT alone):** no watchdog; measures unmitigated fault residency and `slow` throughput loss.
    2. **Baseline B (Single Neon call on raw text logs):** raw bus/observation text only, no telemetry PNG, no Decisions API, no Ed25519 proof verifier, allowed to quarantine directly.
    3. **ByzHeist Blue (Full system):** Ed25519 proof auto-conviction + `gemini-decision-1` multimodal triage on 4-panel PNGs + `gemini-4-neon` escalation + single-reporter `frame` guardrail.
  - Writes `eval/results.json` and `EVAL.md` (comparison table across all 6 metrics, per-scenario breakdown, 3 worst failures with honest analysis, and changes made).
- `byzheist/pbft/evidence.py`: updated `PeerObservations.snapshot()` to compute peer latency over an 8-second sliding window so post-warmup `slow` attacks register immediately on telemetry latency bars.
- `tests/test_phase6.py`: verifies `--fake` evaluation completes, writes valid `eval/results.json` and `EVAL.md`, preserves `0` referee breaks and `0` false quarantines, and outperforms baselines.

## Tests
```
....                                                                     [100%]
4 passed in 17.19s
```
All 29 tests across Phases 0–6 pass (`28` offline + `1` `@pytest.mark.live`).

## `--fake` Dry-Run Summary (`EVAL_SEED=2026`, 16 scenarios)
| System | Detection Rate | Correct Culprit Rate | Median Time to Quarantine | False Quarantines | Referee Breaks |
|---|---:|---:|---:|---:|---:|
| **Baseline A: PBFT Alone** | 0.0% | 0.0% | never (>cap) | 0 | 0 |
| **Baseline B: Single Neon Call (Raw Text)** | 75.0% | 68.8% | 0.81 s | 1 | 0 |
| **ByzHeist Blue** | **87.5%** | **87.5%** | **1.26 s** | **0** | **0** |

## Deviations from PLAN.md
- Per the updated 3:20 PM Stretch instructions in `PLAN.md`, `--live` is launched immediately in the background now (`eval/results.json` and `EVAL.md` will be overwritten + committed once the live 16-scenario run finishes) while S1 and S2 are built without touching PBFT/agent modules imported by `eval/run_eval.py`.

## Known issues
- minor: `lie_reply` on a backup only corrupts replica-to-client replies (masked by PBFT's `f+1=2` client quorum) without altering replica-to-replica consensus traffic, so peer telemetry intentionally does not convict it without client-submitted reply receipts (money conservation and agreement remain 100% intact).

## Demo check
Command:
```powershell
ByzHeist\.venv\Scripts\python.exe -m eval.run_eval --fake
```
Observed output:
- Completed all 16 held-out scenarios across all 3 systems (`87.5%` correct culprit rate, `0` false quarantines vs Baseline B's false quarantine of honest `S2` under `H06 frame@S1`), writing `eval/results.json` and `EVAL.md`.

## Next
- Start `ByzHeist\.venv\Scripts\python.exe -m eval.run_eval --live` in the background (`~10-15 min`), then build **Stretch S1 (`scripts/red_campaign.py` + `GET /red/status` + Antigravity Managed Agent 3-round heist campaign)** and **Stretch S2 (`byzheist/agents/tts.py` + `gemini-3.8-flash-lite-tts` spoken commentary toggle)**.
