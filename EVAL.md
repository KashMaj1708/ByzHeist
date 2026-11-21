# ByzHeist Evaluation (`EVAL.md`)

- **Run mode:** `fake`  |  **Held-out seed:** `EVAL_SEED=2026`  |  **Total scenarios:** `16` (12 seeded + 4 Red `surprise` plans)
- **Gemini API calls used:** `60`

## System Comparison

| System | Detection Rate | Correct Culprit Rate | Median Time to Quarantine | False Quarantines (Honest Removed) | Referee Breaks | Avg Throughput Lost (`slow` Lost) |
|---|---:|---:|---:|---:|---:|---:|
| **Baseline A: PBFT Alone** | 0.0% | 0.0% | never (>cap) | 0 | 0 | 10.8% (25.4%) |
| **Baseline B: Single Neon Call (Raw Text)** | 75.0% | 68.8% | 0.81 s | 1 | 0 | 14.8% (25.0%) |
| **ByzHeist Blue (Proof + Decision 1 + Neon)** | **87.5%** | **87.5%** | **1.26 s** | **0** | **0** | **42.1% (48.2%)** |

## Per-Scenario Results (`ByzHeist Blue` vs `Baseline B`)

| ID | Source | Attack (`node` / `role`) | Baseline B Quarantine | ByzHeist Blue Quarantine | Blue TTQ | Referee OK |
|---|---|---|---|---|---:|---|
| `H01` | `seeded_2026` | `drop_votes` (`S1` / backup) | missed | S1 (OK) | 2.52s | YES |
| `H02` | `seeded_2026` | `forge_request` (`S0` / primary) | S0 (OK) | S0 (OK) | 1.26s | YES |
| `H03` | `seeded_2026` | `equivocate` (`S0` / primary) | S0 (OK) | S0 (OK) | 1.33s | YES |
| `H04` | `seeded_2026` | `wrong_digest` (`S1` / backup) | S1 (OK) | S1 (OK) | 1.26s | YES |
| `H05` | `seeded_2026` | `slow` (`S3` / backup) | S3 (OK) | S3 (OK) | 1.34s | YES |
| `H06` | `seeded_2026` | `frame` (`S1` / backup) | S2 (FALSE!) | not quarantined | — | YES |
| `H07` | `seeded_2026` | `wrong_digest` (`S2` / backup) | S2 (OK) | S2 (OK) | 1.26s | YES |
| `H08` | `seeded_2026` | `lie_reply` (`S2` / backup) | missed | not quarantined | — | YES |
| `H09` | `seeded_2026` | `silent` (`S3` / backup) | missed | S3 (OK) | 1.21s | YES |
| `H10` | `seeded_2026` | `wrong_digest` (`S3` / backup) | S3 (OK) | S3 (OK) | 1.30s | YES |
| `H11` | `seeded_2026` | `slow` (`S0` / primary) | S0 (OK) | S0 (OK) | 1.27s | YES |
| `H12` | `seeded_2026` | `drop_votes` (`S1` / backup) | missed | S1 (OK) | 1.18s | YES |
| `R01` | `red_surprise` | `slow` (`S0` / primary) | S0 (OK) | S0 (OK) | 1.29s | YES |
| `R02` | `red_surprise` | `slow` (`S1` / backup) | S1 (OK) | S1 (OK) | 1.22s | YES |
| `R03` | `red_surprise` | `slow` (`S2` / backup) | S2 (OK) | S2 (OK) | 1.26s | YES |
| `R04` | `red_surprise` | `slow` (`S3` / backup) | S3 (OK) | S3 (OK) | 1.22s | YES |

## 3 Worst / Hardest Cases for ByzHeist Blue (Honest Failure Analysis)

1. **Scenario `H08` (`lie_reply` on `S2`, backup)**: Replica S2 ran `lie_reply` (sending fake replies only to the client). Because PBFT client quorum (`f+1=2` matching replies) masks a lone lying reply and replica-to-replica consensus traffic stays 100% honest, peer telemetry showed zero consensus anomalies while money remained 100% conserved.
2. **Scenario `H06` (`frame` on `S1`, backup)**: Replica S1 ran `frame` (backup); Blue kept monitoring without crossing the quarantine confidence threshold before the 30 s cap.
3. **Scenario `H01` (`drop_votes` on `S1`, backup)**: Blue caught and replaced S1 (`drop_votes`), but required 2.517s and experienced 29.4% throughput loss before recovery completed.

## What Was Changed After Seeing Failure Modes

- **Single-reporter `frame` protection (`_is_framed_victim` + 3-peer median latency bars):** Without a structural check, an LLM or raw-text monitor (`Baseline B`) can be tricked when one Byzantine replica reports huge fake mismatches/latency against an honest peer. ByzHeist Blue renders a 4x4 `Reporter x Target` heatmap alongside 3-peer consensus latency bars and enforces in code that unprovable observations never auto-quarantine and single-reporter accusations cannot target a peer corroborated as healthy by the other 2 replicas.
- **`lie_reply` visibility boundary (known architectural limit, kept honest):** Because `lie_reply` only sends bogus replies on the replica-to-client link while participating honestly in replica-to-replica PBFT consensus, peer replicas see zero missing votes or digest mismatches (client `f+1` quorum absorbs the lie safely so money conservation never breaks, though convicting `lie_reply` would require signed client-submitted reply-mismatch receipts).
