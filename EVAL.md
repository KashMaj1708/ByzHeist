# ByzHeist Evaluation (`EVAL.md`)

- **Run mode:** `live`  |  **Held-out seed:** `EVAL_SEED=2026`  |  **Total scenarios:** `16` (12 seeded + 4 Red `surprise` plans)
- **Gemini API calls used:** `58`

## System Comparison

| System | Detection Rate | Correct Culprit Rate | Median Time to Quarantine | False Quarantines (Honest Removed) | Referee Breaks | Avg Throughput Lost (`slow` Lost) |
|---|---:|---:|---:|---:|---:|---:|
| **Baseline A: PBFT Alone** | 0.0% | 0.0% | never (>cap) | 0 | 0 | 13.6% (57.3%) |
| **Baseline B: Single Neon Call (Raw Text)** | 93.8% | 93.8% | 10.96 s | 0 | 0 | 8.8% (40.6%) |
| **ByzHeist Blue (Proof + Decision 1 + Neon)** | **93.8%** | **93.8%** | **23.97 s** | **0** | **0** | **13.2% (56.2%)** |

## Per-Scenario Results (`ByzHeist Blue` vs `Baseline B`)

| ID | Source | Attack (`node` / `role`) | Baseline B Quarantine | ByzHeist Blue Quarantine | Blue TTQ | Referee OK |
|---|---|---|---|---|---:|---|
| `H01` | `seeded_2026` | `drop_votes` (`S1` / backup) | S1 (OK) | S1 (OK) | 18.27s | YES |
| `H02` | `seeded_2026` | `forge_request` (`S0` / primary) | S0 (OK) | S0 (OK) | 26.58s | YES |
| `H03` | `seeded_2026` | `equivocate` (`S0` / primary) | S0 (OK) | S0 (OK) | 26.08s | YES |
| `H04` | `seeded_2026` | `wrong_digest` (`S1` / backup) | S1 (OK) | S1 (OK) | 30.33s | YES |
| `H05` | `seeded_2026` | `slow` (`S3` / backup) | S3 (OK) | S3 (OK) | 22.34s | YES |
| `H06` | `seeded_2026` | `frame` (`S1` / backup) | S1 (OK) | S1 (OK) | 5.98s | YES |
| `H07` | `seeded_2026` | `wrong_digest` (`S2` / backup) | S2 (OK) | S2 (OK) | 39.22s | YES |
| `H08` | `seeded_2026` | `lie_reply` (`S2` / backup) | missed | not quarantined | — | YES |
| `H09` | `seeded_2026` | `silent` (`S3` / backup) | S3 (OK) | S3 (OK) | 15.94s | YES |
| `H10` | `seeded_2026` | `wrong_digest` (`S3` / backup) | S3 (OK) | S3 (OK) | 23.97s | YES |
| `H11` | `seeded_2026` | `slow` (`S0` / primary) | S0 (OK) | S0 (OK) | 33.38s | YES |
| `H12` | `seeded_2026` | `drop_votes` (`S1` / backup) | S1 (OK) | S1 (OK) | 20.51s | YES |
| `R01` | `red_surprise` | `slow` (`S0` / primary) | S0 (OK) | S0 (OK) | 46.41s | YES |
| `R02` | `red_surprise` | `frame` (`S3` / backup) | S3 (OK) | S3 (OK) | 6.43s | YES |
| `R03` | `red_surprise` | `drop_votes` (`S3` / backup) | S3 (OK) | S3 (OK) | 22.31s | YES |
| `R04` | `red_surprise` | `drop_votes` (`S2` / backup) | S2 (OK) | S2 (OK) | 39.96s | YES |

## 3 Worst / Hardest Cases for ByzHeist Blue (Honest Failure Analysis)

1. **Scenario `H08` (`lie_reply` on `S2`, backup)**: Replica S2 ran `lie_reply` (sending fake replies only to the client). Because PBFT client quorum (`f+1=2` matching replies) masks a lone lying reply and replica-to-replica consensus traffic stays 100% honest, peer telemetry showed zero consensus anomalies while money remained 100% conserved.
2. **Scenario `R01` (`slow` on `S0`, primary)**: Blue caught and replaced S0 (`slow`), but required 46.412s and experienced 84.1% throughput loss before recovery completed.
3. **Scenario `R04` (`drop_votes` on `S2`, backup)**: Blue caught and replaced S2 (`drop_votes`), but required 39.959s and experienced 6.4% throughput loss before recovery completed.

## What Was Changed After Seeing Failure Modes

- **Single-reporter `frame` protection (`_is_framed_victim` + 3-peer median latency bars):** Without a structural check, an LLM or raw-text monitor (`Baseline B`) can be tricked when one Byzantine replica reports huge fake mismatches/latency against an honest peer. ByzHeist Blue renders a 4x4 `Reporter x Target` heatmap alongside 3-peer consensus latency bars and enforces in code that unprovable observations never auto-quarantine and single-reporter accusations cannot target a peer corroborated as healthy by the other 2 replicas.
- **`lie_reply` visibility boundary (known architectural limit, kept honest):** Because `lie_reply` only sends bogus replies on the replica-to-client link while participating honestly in replica-to-replica PBFT consensus, peer replicas see zero missing votes or digest mismatches (client `f+1` quorum absorbs the lie safely so money conservation never breaks, though convicting `lie_reply` would require signed client-submitted reply-mismatch receipts).
