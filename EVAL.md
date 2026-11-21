# ByzHeist Evaluation (`EVAL.md`)

- **Run mode:** `live`  |  **Held-out seed:** `EVAL_SEED=2026`  |  **Total scenarios:** `16` (12 seeded + 4 Red `surprise` plans)
- **Gemini API calls used:** `58`

## System Comparison

| System | Detection Rate | Correct Culprit Rate | Median TTQ (Proof / Judgement) | Third-Party Verifiable Proofs | False Quarantines | Referee Breaks | Avg Throughput Lost (`slow` Lost) |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Baseline A: PBFT Alone** | 0.0% | 0.0% | never (>cap) | 0 / 16 | 0 | 0 | 13.6% (57.3%) |
| **Baseline B: Single Neon Call (Raw Text)** | 93.8% | 93.8% | 11.25 s (— / 11.25 s) | 0 / 16 (no) | 0 | 0 | 9.6% (40.6%) |
| **ByzHeist Blue (Proof + Decision 1 + Neon)** | **93.8%** | **93.8%** | **15.94 s (0.38 s / 21.41 s)** | **5 / 16** | **0** | **0** | **13.8% (56.2%)** |

## Per-Scenario Results (`ByzHeist Blue` vs `Baseline B`)

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

## 3 Worst / Hardest Cases for ByzHeist Blue (Honest Failure Analysis)

1. **Scenario `H08` (`lie_reply` on `S2`, backup)**: Replica S2 ran `lie_reply` (sending fake replies only to the client). Because PBFT client quorum (`f+1=2` matching replies) masks a lone lying reply and replica-to-replica consensus traffic stays 100% honest, peer telemetry showed zero consensus anomalies while money remained 100% conserved.
2. **Scenario `R01` (`slow` on `S0`, primary)**: Blue caught and replaced S0 (`slow`), but required 46.412s and experienced 84.1% throughput loss before recovery completed.
3. **Scenario `R04` (`drop_votes` on `S2`, backup)**: Blue caught and replaced S2 (`drop_votes`), but required 39.959s and experienced 6.4% throughput loss before recovery completed.

## Honest Comparison & What Was Changed After Seeing the Data

- **Equal detection rate (`93.8%`, 15/16) across ByzHeist Blue and Baseline B on this held-out set:** On live runs (`H06` and `R02`), `gemini-4-neon` in Baseline B was *not* tricked by `frame`—seeing all four replicas' observation tables side-by-side allowed Neon to spot the lone outlier reporter on its own. Both systems caught 15 of 16 scenarios with 0 false quarantines.
- **Where ByzHeist Blue wins — sub-second proof-based conviction + third-party verifiable evidence:** On cryptographic protocol attacks (`equivocate`, `forge_request`, `wrong_digest` — scenarios `H02`, `H03`, `H04`, `H07`, `H10`), ByzHeist Blue convicts and replaces the faulty replica in **under 1 second** using Ed25519-signed messages that any third party can independently verify (`verify(evidence) == True`), before any LLM call runs. Baseline B always quarantines on an unverified text guess (`verifiable: no`) and waits `~10 s` for an LLM round-trip.
- **Where ByzHeist Blue is slower — judgement-only attacks (`slow`, `drop_votes`, `silent`):** Because Blue never auto-quarantines on unprovable observations—requiring calibrated `Decision 1` confidence thresholds (`>= 0.80`) or `Neon` (`thinking_level='high'`) escalation plus a human approval gate—Blue takes longer (`~16–46 s`) than an unguarded single-call text monitor (`Baseline B`, `~11 s`) that is permitted to remove nodes immediately on a hunch.
- **Proof-path timing bug fixed after initial eval:** Our initial eval run measured `time_to_quarantine` *after* `BlueWatchdog.step()` finished awaiting Neon's post-conviction prose explanation (`24–39 s`) and after a `4.5 s` LLM tick sleep. Moving local Ed25519 proof polling ahead of PNG rendering / LLM calls and stamping TTQ at actual `quarantine()` execution dropped proof-based TTQ to sub-second.
- **Shared honest miss (`H08 lie_reply`):** Kept intentionally unconvicted by both systems—`lie_reply` corrupts only replica-to-client replies (masked safely by PBFT's `f+1=2` client quorum while money stays at 10,000) without leaving replica-to-replica consensus anomalies.
