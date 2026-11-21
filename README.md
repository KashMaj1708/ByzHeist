# ByzHeist

**Can an AI rob a Byzantine-fault-tolerant bank?** A Red agent (or a judge, by voice) controls one replica of a 4-node PBFT bank and tries to steal money or degrade consensus without getting caught. A Blue watchdog agent inspects rendered visual telemetry, convicts cryptographic protocol violations in under a second with third-party-verifiable Ed25519 proofs, escalates judgement-only faults through calibrated visual triage to a human operator gate, and quarantines and replaces faulty replicas with fresh keys and state transfer. A deterministic referee checks on every state transition that money is conserved (`Bank total = 10,000`) and all honest replicas agree.

---

## Quick Start

```powershell
# Python 3.11+ required (Python 3.12 recommended; google-genai >= 3.2.0)
cd ByzHeist
uv venv .venv --python 3.12
uv pip install --python .venv -r requirements.txt

# Configure environment (never commit .env)
Copy-Item .env.example .env
# Edit .env and set GEMINI_API_KEY=<your-key> and ARENA_ACCESS_CODE=<chosen-passcode>

# Run the offline deterministic test suite (FakeGemini, 0 network calls)
.\.venv\Scripts\pytest.exe -m "not live" -q

# Launch the live arena at http://localhost:8000
.\.venv\Scripts\python.exe -m byzheist.server
```

If `GEMINI_API_KEY` is omitted (or `BYZHEIST_FAKE_GEMINI=1` is set), the server runs end-to-end offline using `FakeGemini`.

---

## Try It Live (Cloudflare Quick Tunnel)

Judges can speak attacks from a phone over mobile data (`getUserMedia` requires HTTPS outside `localhost`):

```powershell
# 1. Install cloudflared once (Windows)
winget install --id Cloudflare.cloudflared

# 2. Terminal A — run ByzHeist bound to 127.0.0.1:8000 with ARENA_ACCESS_CODE set in .env
.\.venv\Scripts\python.exe -m byzheist.server

# 3. Terminal B — open an HTTPS quick tunnel
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000
```

1. Open `https://<random-words>.trycloudflare.com/?code=<your-ARENA_ACCESS_CODE>` on a phone or laptop.
2. The server exchanges `?code=...` for an `HttpOnly` session cookie and strips `?code=` from the browser URL bar immediately so screen recordings never expose the passcode. Direct `localhost` connections bypass the gate automatically.
3. **Shared Red seat lock:** One visitor holds the Red attacker controls at a time (`60 s` inactivity expiry). All other connected viewers stream the live `4 Hz` WebSocket state and can operate Blue's **Approve / Deny** quarantine gate.
4. Note: quick-tunnel URLs (`*.trycloudflare.com`) rotate whenever `cloudflared` restarts.

---

## Architecture

```text
                       +---------------------------------------------------+
                       |  Judge Browser / Phone (HTTPS Tunnel + 4 Hz WS)   |
                       |  Voice Mic -> /red/voice   Approve -> /blue/...   |
                       +----------------+------------------------+---------+
                                        |                        |
                                        v                        v
+---------------------------------------+----+    +--------------+----------------------------------+
| RED ATTACKER (FaultBudget: max f=1 node)   |    | BLUE WATCHDOG (Read-Only Telemetry + Replace)   |
| - Gemini 4 Carbon: voice/text -> AttackPlan|    | 1. Fast Path: verify(Ed25519 Evidence) < 0.5s   |
| - Gemini 4 Hydrogen: 10s visual adapt loop |    |    -> immediate auto-quarantine + replace       |
| - Antigravity Managed Agent: 3-round       |    | 2. Visual Path: 1200x800 4-panel telemetry.png  |
|   campaign -> PLAYBOOK.md                  |    |    -> Gemini Decision 1 (noul + choice + score) |
+----------------------+---------------------+    |    -> Gemini 4 Neon (thinking="high") escalate  |
                       | set_fault(node, mode)    |    -> Human Approval Gate (slow/drop/silent)    |
                       v                          +--------------+----------------------------------+
+----------------------------------------------------------------+----------------------------------+
| 4-REPLICA ED25519 PBFT BANK CLUSTER (S0 primary, S1, S2, S3 | n = 4, f = 1, quorum = 2f + 1 = 3)  |
| - Normal-case 3-phase consensus (PRE-PREPARE -> PREPARE -> COMMIT) + View-Change / New-View       |
| - 8 fault modes: equivocate, wrong_digest, forge_request, slow, drop_votes, lie_reply, silent,    |
|   frame (+ 2-node collude in Calibration Mode to prove the referee catches a broken f=1 bound)    |
| - Quarantine & Replace: fresh Ed25519 keypair + state transfer at committed seq + Red eviction    |
| - Deterministic Referee: checks sum(accounts) == 10,000 and honest ledger equality every commit   |
+---------------------------------------------------------------------------------------------------+
```

---

## Model Roles

All model identifiers live in [`byzheist/config.py`](byzheist/config.py) (`temperature`, `top_p`, `top_k`, and `thinking_budget` are never passed to Gemini 4 calls; capacity is split into a reserved `8 calls/min` judge pool and a `22 calls/min` background pool):

| Role | Model ID | API Surface | Responsibility |
|---|---|---|---|
| **Red Planner (Voice & Text)** | `gemini-4-carbon` | `models.generate_content` (structured JSON, `thinking_level="low"`) | Transcribes judge voice (`audio/webm` / WAV) or text intent and plans `{heard, node, mode, params, narrative}` |
| **Red Adaptive Loop** | `gemini-4-hydrogen` | `models.generate_content` (`thinking_level="low"`) | Inspects rendered `telemetry.png` every `10 s` while an attack is active to `keep`, `tweak_params`, or `switch` |
| **Blue Visual Triage** | `gemini-decision-1` | `v1beta` `client.decisions.decide` | Calibrated visual triage on `telemetry.png`: `under_attack` (`noul`), `suspect` (`choice`), `attack_type` (`choice`), `severity` (`score` rubric `0..2`) |
| **Blue Escalation & Explanation** | `gemini-4-neon` | `models.generate_content` (`thinking_level="high"` / `"low"`) | Deep escalation when `Decision 1` confidence is `0.40..0.80` or `frame` vs `slow` is ambiguous, plus post-conviction operator explanations |
| **Managed Red Campaign (S1)** | `antigravity-preview-09-2026` | `v1alpha` Interactions API (`environment="remote"`) | Runs a 3-round autonomous Red campaign (`scripts/red_campaign.py`) and writes `PLAYBOOK.md` |
| **Arena Commentary (S2)** | `gemini-3.8-flash-lite-tts` | `v1beta` TTS / AUDIO | Optional rate-capped spoken commentary for round events |

---

## Held-Out Live Evaluation (`EVAL_SEED=2026`)

Generated by [`eval/run_eval.py`](eval/run_eval.py) across `16` held-out scenarios (`12` deterministic seeded scenarios from `EVAL_SEED=2026` + `4` live `gemini-4-carbon` `surprise` plans; full report in [`EVAL.md`](EVAL.md) and raw metrics in [`eval/results.json`](eval/results.json)):

### System Comparison

| System | Detection Rate | Correct Culprit Rate | Median TTQ (Proof / Judgement) | Third-Party Verifiable Proofs | False Quarantines | Referee Breaks | Avg Throughput Lost (`slow` Lost) |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Baseline A: PBFT Alone** | 0.0% | 0.0% | never (>cap) | 0 / 16 | 0 | 0 | 13.6% (57.3%) |
| **Baseline B: Single Neon Call (Raw Text)** | 93.8% | 93.8% | 11.25 s (— / 11.25 s) | 0 / 16 (no) | 0 | 0 | 9.6% (40.6%) |
| **ByzHeist Blue (Proof + Decision 1 + Neon)** | **93.8%** | **93.8%** | **15.94 s (0.38 s / 21.41 s)** | **5 / 16** | **0** | **0** | **13.8% (56.2%)** |

### Per-Scenario Breakdown

| ID | Source | Attack (`node` / `role`) | Baseline B Quarantine (Verifiable?) | ByzHeist Blue Quarantine | Blue TTQ | Conviction basis | Evidence a third party can verify | Referee OK |
|---|---|---|---|---|---:|---|---|---|
| `H01` | `seeded_2026` | `drop_votes` (`S1` / backup) | S1 (OK, verifiable: no) | S1 (OK) | 18.27s | model judgement | no | YES |
| `H02` | `seeded_2026` | `forge_request` (`S0` / primary) | S0 (OK, verifiable: no) | S0 (OK) | 0.19s | signed proof | yes | YES |
| `H03` | `seeded_2026` | `equivocate` (`S0` / primary) | S0 (OK, verifiable: no) | S0 (OK) | 0.40s | signed proof | yes | YES |
| `H04` | `seeded_2026` | `wrong_digest` (`S1` / backup) | S1 (OK, verifiable: no) | S1 (OK) | 0.39s | signed proof | yes | YES |
| `H05` | `seeded_2026` | `slow` (`S3` / backup) | S3 (OK, verifiable: no) | S3 (OK) | 22.34s | model judgement | no | YES |
| `H06` | `seeded_2026` | `frame` (`S1` / backup) | S1 (OK, verifiable: no) | S1 (OK) | 5.98s | model judgement | no | YES |
| `H07` | `seeded_2026` | `wrong_digest` (`S2` / backup) | S2 (OK, verifiable: no) | S2 (OK) | 0.38s | signed proof | yes | YES |
| `H08` | `seeded_2026` | `lie_reply` (`S2` / backup) | missed (no) | not quarantined | — | — | no | YES |
| `H09` | `seeded_2026` | `silent` (`S3` / backup) | S3 (OK, verifiable: no) | S3 (OK) | 15.94s | model judgement | no | YES |
| `H10` | `seeded_2026` | `wrong_digest` (`S3` / backup) | S3 (OK, verifiable: no) | S3 (OK) | 0.19s | signed proof | yes | YES |
| `H11` | `seeded_2026` | `slow` (`S0` / primary) | S0 (OK, verifiable: no) | S0 (OK) | 33.38s | model judgement | no | YES |
| `H12` | `seeded_2026` | `drop_votes` (`S1` / backup) | S1 (OK, verifiable: no) | S1 (OK) | 20.51s | model judgement | no | YES |
| `R01` | `red_surprise` | `slow` (`S0` / primary) | S0 (OK, verifiable: no) | S0 (OK) | 46.41s | model judgement | no | YES |
| `R02` | `red_surprise` | `frame` (`S3` / backup) | S3 (OK, verifiable: no) | S3 (OK) | 6.43s | model judgement | no | YES |
| `R03` | `red_surprise` | `drop_votes` (`S3` / backup) | S3 (OK, verifiable: no) | S3 (OK) | 22.31s | model judgement | no | YES |
| `R04` | `red_surprise` | `drop_votes` (`S2` / backup) | S2 (OK, verifiable: no) | S2 (OK) | 39.96s | model judgement | no | YES |

### Key Takeaways

- **Equal detection rate (`15/16`, `93.8%`) with zero false quarantines and zero referee invariant breaks** across both ByzHeist Blue and Baseline B (`gemini-4-neon` on all four peer tables was not fooled by `frame` in `H06`/`R02`).
- **Sub-second verifiable cryptographic conviction (`0.19 s – 0.40 s`, median `0.38 s`):** On protocol attacks (`equivocate`, `forge_request`, `wrong_digest`), ByzHeist Blue verifies Ed25519 signatures locally and quarantines + replaces the faulty replica before any LLM call runs, producing `Evidence` bundles any third party can independently verify (`5/16` vs `0/16` for Baseline B, which always acts on an unverified text guess after `~11 s`).
- **Human-gated caution on judgement-only faults (`slow`, `drop_votes`, `silent`):** Because Blue never auto-quarantines without a cryptographic proof—requiring calibrated `Decision 1` confidence (`>= 0.80`) or `Neon` (`thinking_level="high"`) escalation plus human operator approval—judgement-only attacks take `~16–46 s` vs `~11 s` for an unguarded text monitor.
- **Honest miss (`H08 lie_reply`):** `lie_reply` alters only replica-to-client responses (safely masked by PBFT's `f+1=2` matching-reply quorum while money stays at `10,000`) and leaves zero replica-to-replica consensus anomalies unless clients submit signed reply receipts to Blue.

---

## Known Limits

- **Single-process asyncio cluster:** Replicas communicate over an in-memory delay/jitter bus (`byzheist/pbft/network.py`) without disk persistence or PBFT checkpoint garbage collection (round reset clears message logs).
- **Client-path `lie_reply`:** Masked by `f+1=2` client quorum; convicting `lie_reply` requires forwarding signed client reply receipts into Blue's evidence collector.
- **Ephemeral quick tunnels:** `cloudflared tunnel --url http://localhost:8000` assigns a new `*.trycloudflare.com` hostname each time `cloudflared` restarts.

---

## What was built at the hackathon

Everything in this repository—Ed25519 signing, PBFT normal-case consensus and view-change state machine, 8 Byzantine fault modes plus 2-node `collude` calibration mode, transferable cryptographic `Evidence` verification, replica quarantine and fresh-key state-transfer replacement, `FaultBudget` anti-cheat sandboxes, 4-panel Pillow visual telemetry renderer, `Gemini 4 Carbon` / `Hydrogen` / `Neon` + `Gemini Decision 1` + `Gemini 3.8 Flash Lite TTS` + `Antigravity Managed Agent` integrations, FastAPI + `4 Hz` WebSocket arena UI, and the 16-scenario evaluation harness—was designed and written from scratch on **Saturday, November 21, 2026** at the **Build with Gemini 4 NYC Hackathon** (Cerebral Valley × Google DeepMind).
