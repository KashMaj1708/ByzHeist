# Phase 5 report
Status: DONE
Finished at: 3:18 PM EST (Nov 21, 2026)

## Built
- `.env.example`: added `ARENA_ACCESS_CODE=` placeholder alongside `GEMINI_API_KEY=`.
- `byzheist/server.py`:
  - FastAPI + `uvicorn` (`python -m byzheist.server` binding to `127.0.0.1:8000`).
  - ~4 Hz WebSocket broadcast (`/ws`) + immediate initial state push.
  - Endpoints: `GET /`, `WS /ws`, `POST /red/voice`, `POST /red/text`, `POST /red/surprise`, `POST /red/manual`, `POST /blue/approve`, `POST /blue/deny`, `POST /round/reveal`, `POST /round/reset`, `GET /telemetry.png`.
  - Tunnel-aware access-code protection (`ARENA_ACCESS_CODE`): skips check on `127.0.0.1` **only when neither `Cf-Connecting-IP` nor `X-Forwarded-For` is present**; otherwise serves a 401 code prompt on `GET /`, accepts `GET /?code=<code>` to set an `httponly` auth cookie, and rejects unauthorized REST (`401`) and WebSocket (`1008`) requests.
  - Shared Red seat lock (`60 s` inactivity timeout): first browser sending a Red action claims the Red seat; other browsers see `"Red seat taken"` (`409` on Red endpoints) while keeping live viewing and Blue `Approve / Deny` controls active.
- `byzheist/web/index.html`:
  - Single-page vanilla JS + SVG arena UI (zero build step, no Streamlit):
    - **Center:** 4-replica PBFT ring + client node, animated flying message dots from `network.event_log` colour-coded by message type, 5 node visual states (`honest`, `suspected`, `quarantined`, `replacing`, `byzantine_revealed`), big `Bank total: 10,000` pill (flashes red on violation) and `Honest replicas agree: YES/NO` pill.
    - **Left (Judge = Red):** hold-to-talk microphone button (`getUserMedia` + `MediaRecorder` `webm/opus` → `POST /red/voice`) with live Web Audio `AnalyserNode` input-level meter, **"What Red Heard"** (`heard`) display, Red's `narrative`, text fallback, `Surprise Me` button, manual per-node mode buttons, and `Calibration round (Red gets 2 nodes)` toggle.
    - **Right (Blue):** live calibrated Gemini Decision 1 bars (`under_attack`, `suspect`, `attack_type`, `severity`), clickable `/telemetry.png` thumbnail (the exact PNG Blue saw), `Pending Quarantine` card with **Approve / Deny**, verified cryptographic evidence list, Gemini 4 Neon explanation, and quarantine/replace timeline.
    - **Bottom strip:** anti-cheat audit log, 60 s SVG throughput sparkline, and `Reveal Ground Truth` / `Reset Round` buttons.
- `tests/test_phase5.py`: offline `TestClient` tests verifying HTML, `/telemetry.png`, `/ws` state stream, `POST /red/manual` + anti-cheat budget refusal log, Cloudflare tunnel `Cf-Connecting-IP` access-code enforcement + `httponly` cookie unlock, and the 60 s shared Red seat lock.

## Live API Verification (Pre-Phase 5 Gate)
1. `.venv\Scripts\pytest.exe -m live -q`:
```
.                                                                        [100%]
1 passed, 25 deselected in 15.69s
```
Passed on the first run against the real `gemini-decision-1` and `gemini-4-neon` APIs — **zero fixes needed**.

2. **Raw Live Gemini 4 Carbon Red Plan (`"make the primary slow"`)**:
```json
{
  "timestamp": 1795291752.1848617,
  "source": "gemini-4-carbon",
  "plan": {
    "heard": "The judge requested making the current primary slow.",
    "node": "S0",
    "mode": "slow",
    "params": {
      "delay_ms": 1180.0
    },
    "narrative": "Injecting a 1180ms outbound delay on the current primary S0—tuned to ~79% of the 1500ms view-change timeout—strangles cluster throughput to a crawl while skating just beneath the view-change threshold and leaving zero cryptographic proof of malice."
  },
  "applied": true,
  "reason": "applied"
}
```

3. **Raw Live Gemini Decision 1 Response (on the rendered 4-panel telemetry PNG under that `1180ms` slow primary)**:
```json
{
  "suspect": {
    "type": "choice",
    "confidence": 0.6015,
    "choice": "S0",
    "probabilities": {
      "S0": 0.6015,
      "other": 0.234,
      "S1": 0.0435,
      "S3": 0.085,
      "S2": 0.036
    }
  },
  "severity": {
    "type": "score",
    "confidence": 0.7224,
    "probabilities": {
      "0": 0.1064,
      "2": 0.1712,
      "1": 0.7224
    },
    "score": 1.0648,
    "legend": {
      "0": "Normal healthy PBFT operation with low latency and zero anomalies.",
      "1": "Degraded throughput, missing backup votes, or single-reporter framing anomaly.",
      "2": "Under active high-confidence Byzantine attack requiring operator intervention."
    }
  },
  "attack_type": {
    "type": "choice",
    "confidence": 0.9307,
    "choice": "slow",
    "probabilities": {
      "slow": 0.9307,
      "other": 0.0265,
      "lie_reply": 0.0024,
      "wrong_digest": 0.0025,
      "forge_request": 0.0007,
      "drop_votes": 0.0025,
      "frame": 0.0332,
      "equivocation": 0.0015
    }
  },
  "under_attack": {
    "type": "noul",
    "confidence": 0.5611,
    "noul": 0.5611
  }
}
```

## Tests
```
..                                                                       [100%]
2 passed in 7.33s
```
All 28 tests across Phases 0–5 (`27` offline + `1` `@pytest.mark.live`) pass; 0 failed, 0 skipped.

## Deviations from PLAN.md
- Added `POST /round/reveal` button so judges can reveal ground-truth Byzantine node state (`byzantine_revealed` purple highlight) at the end of a round before resetting, while keeping ground truth hidden during play.

## Known issues
- none

## Demo check
Command:
```powershell
ByzHeist\.venv\Scripts\python.exe -m byzheist.server
```
Verified via live `uvicorn` HTTP probe (`demo_phase5.py`): `GET / -> 200` (Arena HTML), `GET /telemetry.png -> 200` (`> 50 KB` valid PNG), WebSocket `/ws` streaming ~4 Hz JSON state.

## Next
- Phase 5 finished at **3:18 PM** (37 minutes ahead of the 3:55 PM target and well before the 3:45 PM stretch threshold!).
- Phase 6 (target 4:15 PM) will build `eval/run_eval.py` (`--fake` and `--live` on held-out `EVAL_SEED=2026` scenarios + 4 live Red `"surprise"` plans comparing Baseline A PBFT alone, Baseline B single raw-text Neon call, and full ByzHeist Blue, writing `eval/results.json` and `EVAL.md`) + `tests/test_phase6.py`.
