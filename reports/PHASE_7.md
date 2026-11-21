# Phase 7 report
Status: DONE
Finished at: 4:21 PM EST (Nov 21, 2026)

## Built
- `README.md`: complete hackathon documentation — one-liner, quick start, `"Try it live"` Cloudflare quick-tunnel steps (`cloudflared tunnel --url http://localhost:8000` + `ARENA_ACCESS_CODE` prompt, without exposing any secret), ASCII architecture diagram, Gemini 4 + Decisions API + TTS model roles table, final 16-scenario live evaluation table from `EVAL.md`, honest known limits, and **"What was built at the hackathon"** declaration.
- `EVAL.md` & `eval/results.json`: updated with sub-second (`~0.39 s` median) proof-based TTQ across `H02, H03, H04, H07, H10`, `"Conviction basis"` (`signed proof` vs `model judgement`) and `"Evidence a third party can verify"` (`yes`/`no`) columns, and honest comparative analysis.
- `reports/PHASE_7.md`: 1-minute live demo video checklist + secret audit verification.

## 1-Minute Demo Video Checklist (`4:30 – 5:00 PM`)
1. **`0:00 – 0:10` — Healthy PBFT Bank & Public Tunnel:**
   - Start `ByzHeist\.venv\Scripts\python.exe -m byzheist.server` and `cloudflared tunnel --url http://localhost:8000`.
   - Show the 4-replica ring committing transfers (`Bank total: 10,000`, `Honest replicas agree: YES`).
2. **`0:10 – 0:25` — Judge Voice Attack (`Gemini 4 Carbon`):**
   - Hold the microphone button (on laptop or phone over `https://<random>.trycloudflare.com/?code=...`) and speak: *"Make the primary slow without triggering a view change."*
   - Point out the live mic level meter, **What Red Heard** (`heard` transcript), and Carbon's `AttackPlan` (`S0 · slow · delay_ms ~ 1180ms`).
3. **`0:25 – 0:42` — Visual Telemetry & Blue Verdict (`Gemini Decision 1` + `Gemini 4 Neon`):**
   - Point to **What Blue sees** (the rendered 4-panel telemetry PNG showing elevated outbound latency on `S0` and depressed throughput) and calibrated **Decision 1** bars.
   - Click **Approve Quarantine & Replace** on the pending card.
4. **`0:42 – 0:52` — Quarantine, Fresh Key Replacement & Invariant Check:**
   - Watch the two-step banner (`S0 quarantined` → `S0 replaced · fresh key · state at seq N`), `new key` badge on `S0`, `"Red evicted from S0"` notice, and throughput rebounding while **`Bank total: 10,000`** and **`Honest replicas agree: YES`** never waver.
   - *(Optional 5 s beat)*: Click manual `S0 -> equivocate` to show sub-second (`< 0.4 s`) automatic signed-proof conviction without human intervention.
5. **`0:52 – 1:00` — Held-Out Evaluation Proof (`EVAL.md`):**
   - Scroll to the `EVAL.md` / `README.md` table: `15/16` (`93.8%`) caught, **`0.39 s` median TTQ with third-party-verifiable Ed25519 proofs** on cryptographic attacks vs `~11 s` unverified text guesses in Baseline B, `0` false quarantines, `0` referee breaks, and `H08 lie_reply` honestly documented.

## Secret Scan & Tests
- `git log -p` scanned for `GEMINI_API_KEY=<non-empty>`, `AIza`, `AQ.`, and `ARENA_ACCESS_CODE=<non-empty>`: **empty (0 secrets in git history)**.
- Full offline test suite (`pytest -m "not live" -q`): **all tests passing**.

## Deviations from PLAN.md
- none

## Known issues
- minor: `cloudflared` quick-tunnel URLs (`https://<random>.trycloudflare.com`) rotate whenever `cloudflared` restarts; update `ARENA_PUBLIC_URL` in `.env` if re-running `scripts/red_campaign.py`.
