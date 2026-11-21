# ByzHeist: build plan

**One line:** Can an AI rob a Byzantine-fault-tolerant bank? A Red agent (or a judge, by voice) controls one replica of a PBFT bank and tries to steal money or degrade it without getting caught. A Blue watchdog agent watches visual telemetry, convicts the malicious replica with evidence, and quarantines and replaces it. A referee checks that money is conserved and honest replicas agree.

**Hackathon:** Gemini 4 NYC (Cerebral Valley x Google DeepMind), Sat Nov 21 2026. Submissions due 5:00 PM. **Build hard stop: 4:30 PM** (4:30 to 5:00 is reserved for the demo video and submission). Everything in this repo is built today, from scratch.

**Problem statement:** "5 Sigma" (reliable agent + evaluation on unseen inputs vs a baseline). Hard rules we must satisfy:
- At least two Gemini 4 models, each with a clear job, plus the Decisions API.
- Media must be central (not a text box): Blue reads **rendered telemetry images**; judges attack by **voice**.
- Live demo on unrehearsed inputs; evaluation on held-out attacks vs a baseline; honest failure analysis.
- Public repo. No API keys in git. No Streamlit. The dashboard must not be the main feature (the agents are).

---

## Workflow rules (read GEMINI.md too)

1. Work one phase at a time, in order.
2. At the end of each phase:
   - run the phase's tests (`pytest -q tests/test_phaseN*.py` plus all earlier phase tests),
   - write `reports/PHASE_N.md` using the template at the bottom,
   - `git add -A && git commit -m "Phase N: <summary>" && git push`,
   - **stop and wait** for "GREENLIGHT N+1" before starting the next phase.
3. Speed over polish. If it works and nothing is badly broken, move on. No refactors, no gold-plating, no extra abstractions.
4. Each phase has a target finish time. If you are running 15+ minutes over, cut scope inside the phase (note it in the report) rather than stall.

---

## Architecture (target end state)

```
byzheist/
  config.py            # all tunables + MODEL_IDS in one place
  pbft/
    crypto.py          # ed25519 sign/verify (PyNaCl), canonical JSON bytes
    messages.py        # dataclasses: Request, PrePrepare, Prepare, Commit, Reply, ViewChange, NewView
    network.py         # asyncio in-memory bus: per-link latency, drop, quarantine filter, event log
    replica.py         # PBFT state machine (normal case + view change), ledger execution
    client.py          # signed requests, waits for f+1 matching replies, retries/broadcast on timeout
    cluster.py         # builds n=4 replicas + network + client; start/stop; load generator
    faults.py          # Byzantine behaviour modes (Phase 2)
    evidence.py        # signed proof collection (Phase 2)
  referee.py           # money conservation + honest agreement (ground truth, Blue never sees it)
  telemetry.py         # what Blue is allowed to see + PNG rendering (Phase 3)
  agents/
    gemini.py          # SDK wrappers + FakeGemini for tests
    blue.py            # watchdog loop (Decisions API + Neon)
    red.py             # attacker planner (Carbon, audio in) + adaptive executor (Hydrogen)
    budget.py          # anti-cheat: fault budget + forbidden actions, logs blocked attempts
  server.py            # FastAPI + websocket; serves the UI
  web/index.html       # single page, vanilla JS + SVG
eval/run_eval.py       # held-out scenarios, baselines, writes eval/results.json + EVAL.md
tests/                 # one file per phase
reports/               # PHASE_N.md
```

**Key design decisions (do not change without a reason in the report):**
- Python 3.11+, asyncio, **single process**. All 4 replicas run in-process over an in-memory network with realistic latency (5 to 25 ms random per message). Must run on Windows (no fork, no uvloop).
- One cluster: **n = 4, f = 1, quorum = 2f+1 = 3**, client accepts on **f+1 = 2** matching replies.
- Ledger: 100 accounts (`A0`..`A99`), 100 units each, so the **total is always 10,000**.
- Every protocol message and every client request is **ed25519-signed** (PyNaCl). Replicas reject anything with a bad signature.
- **PREPARE carries the primary's signed PRE-PREPARE** it responds to. This makes equivocation provable: two primary-signed PRE-PREPAREs for the same (view, seq) with different digests is transferable proof.
- No checkpoints / garbage collection (demo runs are minutes long).
- Primary for view v is replica `v mod 4` (S0..S3).

---

## Phase 0: Skeleton (target: 1:25 PM)

**Build**
- Folder layout above (empty modules are fine), `pyproject.toml` or `requirements.txt`: `pynacl fastapi uvicorn[standard] httpx matplotlib google-genai>=3.2.0 python-dotenv pytest pytest-asyncio`.
- `.gitignore` (must include `.env`, `__pycache__/`, `.venv/`, `*.png` under `out/`), `.env.example` with `GEMINI_API_KEY=`.
- `config.py` with tunables (latency range, view-change timeout = 1500 ms, client timeout = 2000 ms, n, f, accounts, initial balance) and `MODEL_IDS` copied from the builder guide:
  `frontier_deep="gemini-4-carbon"`, `frontier_fast="gemini-4-neon"`, `fast_workhorse="gemini-4-hydrogen"`, `decisions="gemini-decision-1"`, `antigravity_agent="antigravity-preview-09-2026"`, `transcribe_batch="gemini-3.5-transcribe"`, `tts_fast="gemini-3.8-flash-lite-tts"`.
- `README.md` with the one-liner and a section **"What was built at the hackathon"** stating that everything in this repo was written on Nov 21, 2026.

**Tests** (`tests/test_phase0.py`): imports work; config values sane (quorum == 3, total == 10,000).

---

## Phase 1: PBFT that works (target: 2:05 PM)

**Build**
- `crypto.py`, `messages.py` (canonical JSON with sorted keys for digests/signatures).
- `network.py`: `send(src, dst, msg)` and `broadcast`; per-message random latency; per-link drop rules; a `blocked_senders` set per receiver (used for quarantine in Phase 2); an append-only **event log** of `{t, src, dst, type, view, seq, digest}` (ring buffer, last ~2000) for the UI animation.
- `replica.py` normal case: REQUEST → PRE-PREPARE (primary assigns seq) → PREPARE (2f matching + pre-prepare = prepared) → COMMIT (2f+1 = committed-local) → execute strictly in seq order → signed REPLY.
- Request validation: client signature valid, timestamp > last executed timestamp for that client (replay protection), amount > 0, sender balance sufficient (else execute as a no-op "rejected" reply, still ordered).
- `replica.py` view change (simplified PBFT): each backup starts a timer when it has a pending request; on expiry broadcast signed VIEW-CHANGE(v+1, prepared certificates). New primary on 2f+1 VIEW-CHANGEs broadcasts NEW-VIEW re-proposing the highest-view prepared request for each seq (null request for gaps). Replicas validate NEW-VIEW against the included VIEW-CHANGEs.
- `client.py`: submit transfer, wait for f+1 matching replies; on timeout broadcast the request to all replicas and keep waiting.
- `cluster.py`: `Cluster.start()`, `stop()`, `submit(x, y, amt)`, `load_generator(rate_hz=5)` sending random transfers, `state_hashes()`, `balances(replica_id)`.
- `referee.py`: `check(cluster, honest_ids) -> {total_ok, totals_per_replica, agreement_ok, last_exec_seq_per_replica}`. Agreement compares state hashes of honest replicas at their common executed prefix.

**Tests** (`tests/test_phase1.py`, use short timeouts so the suite runs in under 60 s):
1. 50 random transfers commit; all 4 replicas identical state hash; total 10,000.
2. One crashed backup (drop all its traffic): 50 transfers still commit; 3 live replicas agree.
3. Crashed primary: view change happens (view >= 1), transfers keep committing, total conserved.
4. Replay: resubmitting an old signed request does not execute twice.
5. Bad client signature is rejected.

---

## Phase 2: Byzantine behaviours, evidence, quarantine, replace (target: 2:35 PM)

**Build `faults.py`** (per-replica `FaultConfig` with `mode` + `params`; applied to the replica's outbound path or its reported telemetry):

| mode | who | behaviour |
|---|---|---|
| `silent` | any | sends nothing |
| `equivocate` | primary | for each request, sends the real PRE-PREPARE to one honest backup and a signed PRE-PREPARE for a **forged transfer to the attacker account** (same view/seq) to the others |
| `wrong_digest` | backup | sends PREPARE/COMMIT with a bogus digest |
| `slow` | any (best as primary) | delays every outbound message by `delay_ms` (default 0.8 x view-change timeout) so nothing times out but throughput collapses |
| `drop_votes` | backup | drops its PREPARE/COMMIT to a random subset |
| `lie_reply` | any | sends the client a reply with a fake result |
| `forge_request` | primary | injects a transfer from a victim to the attacker account with a fake client signature |
| `frame` | any | behaves honestly in the protocol but **lies in its telemetry observations**, reporting a chosen honest `target` as slow / sending bad digests |

**Build `evidence.py`**: honest replicas record **provable** misbehaviour with the signed messages attached:
- equivocation: two primary-signed PRE-PREPAREs, same (v, seq), different digests,
- bad digest: a signed PREPARE/COMMIT whose digest conflicts with the primary's signed PRE-PREPARE,
- forged request: a signed PRE-PREPARE containing a request whose client signature fails.
Evidence must be verifiable by anyone (a `verify(evidence) -> bool` function). Separately, each replica keeps **unprovable observations** per peer: avg latency, missing votes, mismatches seen. (`frame` mode corrupts only the observations a Byzantine replica reports, never the signed evidence.)

**Build quarantine + replace** (in `cluster.py`):
- `quarantine(id)`: every honest replica ignores messages from `id`. Quorum stays 3 of 4, so the cluster runs on the 3 honest replicas (note: one more fault would stall liveness; this is why we replace).
- `replace(id)`: reset the node to honest with a fresh key pair re-registered with peers, state transfer (ledger + last executed seq + client timestamps) from f+1 replicas with matching state hashes, then un-quarantine. Cluster back to 4 healthy.

**Build `agents/budget.py`** (the anti-cheat rules, enforced in code):
- At most **f = 1** Byzantine replica at a time, shared by AI Red and the judge, unless `calibration=True` (allows 2).
- Red's only interface is `set_fault(node, mode, params)`; no ledger writes, no network config, no evidence edits. Blue's interface is `quarantine/replace/read_telemetry`; Blue can never read `FaultConfig` (ground truth). Every refused call is appended to an **anti-cheat log** with timestamp, actor and reason.

**Tests** (`tests/test_phase2.py`):
1. For each mode with f = 1 under load for ~5 s: referee total == 10,000 and honest replicas agree.
2. `equivocate` and `wrong_digest` and `forge_request` produce evidence that `verify()` accepts and that names the right replica.
3. `slow` primary: measured throughput drops by > 50% vs honest, and no view change triggers.
4. Calibration: 2 colluding Byzantine replicas (equivocating primary + a backup that prepares/commits both versions) cause honest replicas to diverge; referee reports `agreement_ok == False`.
5. Quarantine then replace restores 4 healthy replicas that agree with the rest.
6. Budget: a second Byzantine node is refused without calibration and appears in the anti-cheat log.

---

## Phase 3: Telemetry Blue can see + images (target: 2:50 PM)

**Build `telemetry.py`**
- `snapshot(cluster) -> dict`: per replica `{view, last_exec_seq, commits_per_sec (10 s window), reported_observations, evidence_count}`, cluster `{throughput timeline (last 60 s), view-change times, recent events}`, plus the **verified evidence list**. Must NOT include fault configs or the referee's honest set.
- `render_png(snapshot) -> bytes` (matplotlib, Agg backend, ~1200x800):
  1. 4x4 heatmap "reported problems: row reports column" (missing votes + mismatches + latency z-score),
  2. per-replica median outbound latency bars,
  3. commit throughput over the last 60 s with vertical lines at view changes and quarantines,
  4. table/text panel listing verified evidence (type, accused replica).
- Also save the latest frame to `out/telemetry_latest.png` for debugging.

**Tests** (`tests/test_phase3.py`): snapshot has no ground-truth keys; PNG renders (valid PNG header, > 10 KB) for honest, `slow`, and `equivocate` runs; heatmap row/col for a `frame`d node reflects the lie.

---

## Phase 4: Agents (target: 3:25 PM)

Confirm exact SDK call shapes with the official Gemini API skill / docs server before writing them (`npx skills add google-gemini/gemini-skills --skill gemini-api-dev`, docs MCP `https://gemini-api-docs-mcp.dev`). Builder-guide constraints: no temperature/top_p/top_k/thinking_budget on Gemini 4 models; no `minimal` thinking level on Neon/Hydrogen; use native structured output or Decisions questions, never "please reply in JSON" prompts.

**`agents/gemini.py`**: thin wrappers `decide(image_png, text, questions)`, `reason(model, inputs, schema, thinking_level)`, plus `FakeGemini` returning canned answers so tests run offline. A call counter and a hard cap (`MAX_CALLS_PER_MIN` in config) to stay within fair use.

**`agents/blue.py`** (watchdog), loop every 3 s:
1. Snapshot + PNG.
2. **If verified evidence names replica X** → quarantine X immediately (proof-based, no judgement needed), then `replace(X)`. Neon writes a one-paragraph explanation for the UI.
3. Else call **Gemini Decision 1** on the PNG + compact evidence/observation text with questions:
   - `under_attack` (yes/no),
   - `suspect` (choice: S0, S1, S2, S3, other),
   - `attack_type` (choice: equivocation, wrong_digest, slow, drop_votes, lie_reply, forge_request, frame, other),
   - `severity` (score: normal / degraded / under active attack).
4. If `under_attack` > 0.7 and `suspect` confidence >= 0.8 → create a **pending quarantine** that needs human approval (UI approve/deny). Below that: if 0.5 to 0.8, escalate to **Gemini 4 Neon** (thinking high) with the PNG + snapshot for a structured verdict `{suspect, attack_type, rationale, recommend_quarantine}`; Neon at most once per 15 s.
5. Blue must know `frame` exists: unprovable observations from a single reporter are weak evidence. Never auto-quarantine on observations alone.
6. Emit every verdict (with confidences) to the UI event stream.

**`agents/red.py`** (attacker):
- `plan(audio_bytes | text | "surprise")` → **Gemini 4 Carbon** with the audio as a multimodal input (judge speaks the attack) and a system prompt listing the fault modes, the budget, and the goal (steal money or degrade service without being caught; framing an honest replica is a valid strategy). Structured output `AttackPlan {heard, node, mode, params, narrative}` (`heard` = what the judge asked for, in Carbon's words). Executor validates through `budget.py` then calls `set_fault`.
- Adaptive loop every 10 s: **Gemini 4 Hydrogen** sees Red's own status (has Blue flagged me? throughput impact?) and the PNG, returns `{keep | tweak params | switch mode}`.
- Manual path (no LLM) for UI buttons: `set_fault(node, mode)` through the same budget check.

**Tests** (`tests/test_phase4.py`, with `FakeGemini`): proof-based auto-quarantine of an equivocating primary within 10 s; Decision-path creates a pending (not executed) quarantine for `slow`; `frame` alone never causes an auto-quarantine; Red plan that violates the budget is refused and logged. **One live smoke test** marked `@pytest.mark.live` (skipped unless `GEMINI_API_KEY` is set) that calls Decision 1 and Neon once each.

---

## Phase 5: UI the judges control (target: 3:55 PM)

`server.py` (FastAPI + uvicorn) and `web/index.html` (vanilla JS + SVG, no build step):
- **Center:** 4 replicas in a ring, animated dots for messages from the event log (colour by type), node colour: honest / Byzantine-revealed-after-round / suspected / quarantined / replacing. Big counter "Bank total: 10,000" that turns red if it ever changes, plus "Honest replicas agree: yes/no".
- **Left (judge = Red):** hold-to-talk mic button using the laptop's default microphone (`getUserMedia` + MediaRecorder, webm/opus → `POST /red/voice`; works because the page is served on `localhost`, a secure context). Show a live input-level meter while recording and, after planning, display **what Red heard** (Carbon returns a `heard` field with its transcription of the judge's request) so a misheard command is visible, text box fallback, "Surprise me" button, manual mode buttons per node, "Calibration round (Red gets 2 nodes)" toggle. Red's narrative shown after planning.
- **Right (Blue):** live Decision 1 answers with confidence bars, current telemetry PNG thumbnail (the exact image Blue saw), verified evidence list, pending quarantine card with **Approve / Deny**, Neon explanation, quarantine/replace timeline.
- **Bottom strip:** anti-cheat log, throughput sparkline, "Reset round" button.
- Websocket pushes state ~4x per second. Endpoints: `GET /`, `WS /ws`, `POST /red/voice|text|surprise|manual`, `POST /blue/approve|deny`, `POST /round/reset`, `GET /telemetry.png`.
- Must run with one command: `python -m byzheist.server` → http://localhost:8000.

**Tests** (`tests/test_phase5.py`): server starts with `FakeGemini`; `/` returns HTML; websocket sends a state message; `POST /red/manual` sets a fault and a budget violation shows in the anti-cheat log.

---

## Phase 6: Evaluation + proof it works (target: 4:15 PM)

`eval/run_eval.py --live` (and `--fake` for a dry run):
- **Held-out scenarios:** 12 rounds generated from a seed **not used during development** (`EVAL_SEED=2026`): random mode x target role (primary/backup) x params, plus 4 plans produced live by Red's "surprise me". Each round: honest warm-up 5 s, attack starts, cap 30 s.
- **Systems compared:**
  - **Baseline A, PBFT alone:** no watchdog. Measures how long the Byzantine replica stays in the cluster (expected: forever for backups) and throughput lost under `slow`.
  - **Baseline B, single model call:** one Neon call per tick on the raw text logs only (no image, no Decisions API, no evidence verification), allowed to quarantine directly.
  - **ByzHeist Blue** (full system).
- **Metrics per system:** detection rate, correct-culprit rate, median time to quarantine, **false quarantines (honest node removed)**, rounds where referee broke, throughput lost.
- Write `eval/results.json` and `EVAL.md`: results table, the 3 worst failures with what happened, and what was changed after seeing them (if anything).
- Respect the call cap. If time is short, cut to 8 rounds and say so.

**Tests**: `--fake` run completes and writes both files.

---

## Phase 7: Ship (target: 4:30 PM, hard stop)

- README: one-liner, how to run, architecture sketch, model roles table (Carbon = Red planner from voice, Hydrogen = Red adaptation, Decision 1 = Blue triage on telemetry images, Neon = Blue escalation + explanations), eval table from `EVAL.md`, known limits, **"What was built at the hackathon"**.
- Confirm no secrets in git history (`git log -p | grep -i "api_key\|AIza"` must be empty).
- 1-minute demo video checklist in `reports/PHASE_7.md`: judge voice attack → Blue verdict → approve quarantine → replace → counter holds → eval table.

---

## Stretch (only if Phase 5 is green before 3:45 PM; otherwise skip entirely)
1. Run Red and Blue as **Antigravity Managed Agents** in a self-hosted environment (`gemini-api environments connect`) inside a container, with the arena's HTTP API as their only tool (targets the $5,000 Managed Agents prize).
2. Gemini 3.8 Live for streaming voice instead of push-to-talk.
3. Post-round video report: record the UI, Neon agentic video understanding finds the exact attack start timestamp.
4. TTS commentator calling the action.

---

## Report template (`reports/PHASE_N.md`)

```
# Phase N report
Status: DONE | DONE WITH CUTS | BLOCKED
Finished at: <time>

## Built
- <file>: <one line>

## Tests
<paste the pytest summary line(s); list any failing/skipped test and why>

## Deviations from PLAN.md
- <what and why, or "none">

## Known issues
- <severity: dire | minor> <issue>

## Demo check
<command to run + what you saw (e.g. "50 txns committed in 4.1 s, view change at t=2.3 s")>

## Next
<anything the next phase needs to know>
```
