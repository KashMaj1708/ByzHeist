# Phase 0 report
Status: DONE
Finished at: 1:22 PM EST (Nov 21, 2026)

## Built
- `.gitignore`: ignores `.env`, `__pycache__/`, `.venv/`, `out/*.png`, and Python build/test caches while keeping `.env.example` tracked.
- `.env.example`: placeholder `GEMINI_API_KEY=` template for local setup.
- `requirements.txt` & `pyproject.toml`: Python 3.11+ package config (`pynacl`, `fastapi`, `uvicorn[standard]`, `httpx`, `matplotlib`, `google-genai>=3.2.0`, `python-dotenv`, `pytest`, `pytest-asyncio`) and pytest configuration (`asyncio_mode = "auto"`, `live` marker).
- `README.md`: project one-liner, quick-start instructions using `uv` + Python 3.12 `.venv`, and the **"What was built at the hackathon"** declaration for Nov 21, 2026.
- `byzheist/config.py`: PBFT cluster tunables (`N=4`, `F=1`, `QUORUM=3`, `CLIENT_QUORUM=2`, latency `5..25 ms`, `VIEW_CHANGE_TIMEOUT_MS=1500`, `CLIENT_TIMEOUT_MS=2000`), ledger parameters (`100` accounts x `100` = `10,000`), `MAX_CALLS_PER_MIN`, `EVAL_SEED=2026`, and verified `MODEL_IDS` copied from the Gemini 4 NYC Hackathon Builder Guide.
- `byzheist/`, `byzheist/pbft/*`, `byzheist/agents/*`, `byzheist/web/index.html`, `eval/run_eval.py`: complete module skeleton matching the target architecture in `PLAN.md`.
- `tests/test_phase0.py`: offline unit tests verifying all skeleton module imports, third-party dependency imports, PBFT/ledger/model-ID config sanity, and repository hygiene.

## Tests
```
.....                                                                    [100%]
5 passed in 2.05s
```
All 5 tests passed; 0 failed, 0 skipped.

## Deviations from PLAN.md
- Provisioned a local CPython 3.12.16 virtual environment at `.venv/` via `uv` because the host machine's default `PATH` Python is 3.10.11, whereas `google-genai>=3.2.0` requires Python >= 3.11 (`google-genai==3.6.1` installed cleanly in `.venv`).

## Known issues
- minor: Host default `python` on `PATH` is Python 3.10.11; commands should be run via `.venv\Scripts\python.exe` / `.venv\Scripts\pytest.exe` (or after activating `.venv\Scripts\activate`).

## Demo check
Command:
```powershell
.\.venv\Scripts\python.exe -c "from byzheist import config; print(f'n={config.N}, quorum={config.QUORUM}, total={config.EXPECTED_TOTAL}, models={len(config.MODEL_IDS)}')"
```
Observed output:
```
n=4, quorum=3, total=10000, models=24
```

## Next
- Phase 1 will implement Ed25519 signing/canonical JSON (`crypto.py`), PBFT message types (`messages.py`), asyncio in-memory network bus with latency/drop/quarantine/event log (`network.py`), normal-case + view-change replica state machine (`replica.py`), client (`client.py`), cluster runner (`cluster.py`), referee (`referee.py`), and `tests/test_phase1.py`.
- Always invoke `.venv\Scripts\pytest.exe -q` and `.venv\Scripts\python.exe` so Python 3.12.16 and `google-genai==3.6.1` / `pynacl==1.6.2` are used.
