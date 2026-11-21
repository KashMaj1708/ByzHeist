# Stretch S2 report — Spoken Arena Commentary (`gemini-3.8-flash-lite-tts`)
Status: DONE
Finished at: 3:51 PM EST (Nov 21, 2026)

## Built
- `byzheist/agents/tts.py`: `ArenaCommentator` synthesizing one-line spoken callouts via `gemini-3.8-flash-lite-tts` (`config.MODEL_IDS["tts_fast"]` on `v1beta`), wrapping raw PCM16 streams into standard RIFF WAV headers when needed, enforcing an `8.0 s` minimum cooldown between clips, counting every clip against the shared `CallBudgetCounter` (`MAX_CALLS_PER_MIN=30`), and defaulting to **off**.
- `byzheist/server.py`: wired commentary triggers on the 5 key arena events (`red_plan_applied`, `blue_verdict`, `quarantine_approved`, `replace_done`, `referee_break`), added `POST /commentary/toggle`, and exposed `commentary` state over `/ws`.
- `byzheist/web/index.html`: added header `Commentary (TTS)` checkbox toggle (default unchecked) playing new clips in the browser via HTML5 `Audio`.
- `tests/test_stretch_s2.py`: 2 offline unit/integration tests + 1 `@pytest.mark.live` TTS synthesis smoke test.

## Tests
```
...                                                                      [100%]
3 passed in 7.87s
```
All 3 tests (`2` offline + `1` live `gemini-3.8-flash-lite-tts` synthesis test) passed; 0 failed, 0 skipped.

## Deviations from PLAN.md
- Routed `gemini-3.8-flash-lite-tts` through the `v1beta` client (`http_options={"api_version": "v1beta"}`) after confirming the `v1` endpoint returned `404 Model not found` with a hint to use `v1beta`.

## Known issues
- none

## Demo check
Command:
```powershell
ByzHeist\.venv\Scripts\pytest.exe -c ByzHeist\pyproject.toml ByzHeist\tests\test_stretch_s2.py -q
```
Observed output:
- `3 passed in 7.87s` — live `gemini-3.8-flash-lite-tts` call returned playable WAV audio bytes (`> 500 bytes`), default toggle verified `False`, and 8 s cooldown + `MAX_CALLS_PER_MIN` integration verified.

## Next
- Collect the live 3-round Antigravity Managed Agent `out/red_playbook.md` output for `reports/STRETCH_S1.md`, collect the live 16-scenario `eval/results.json` & `EVAL.md` output, commit/push, and wait for **GREENLIGHT 7**.
