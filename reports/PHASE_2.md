# Phase 2 report
Status: DONE
Finished at: 2:28 PM EST (Nov 21, 2026)

## Built
- `byzheist/pbft/faults.py`: `FaultConfig(mode, params)` covering all 8 Byzantine modes (`silent`, `equivocate`, `wrong_digest`, `slow`, `drop_votes`, `lie_reply`, `forge_request`, `frame`) plus calibration-only `collude`.
- `byzheist/pbft/evidence.py`: transferable Ed25519-signed `Evidence` (`equivocation`, `bad_digest`, `forged_request`), standalone `verify(evidence) -> bool` checking signatures against registered replica/client `VerifyKey`s, and `PeerObservations` tracking rolling peer latency, missing votes, and mismatches.
- `byzheist/pbft/replica.py`: outbound/telemetry fault injection for all 8 modes, automatic cryptographic evidence capture on honest replicas, delayed missing-vote auditing, and `frame` observation spoofing without corrupting signed evidence.
- `byzheist/pbft/cluster.py`: `verified_evidence()`, bidirectional `quarantine(id)`, `replace(id)` with fresh Ed25519 key rotation + `f+1` state-hash-verified ledger/checkpoint transfer, and serialized per-client `load_generator`.
- `byzheist/agents/budget.py`: `FaultBudget`, `RedSandbox`, and `BlueSandbox` enforcing at most `f=1` Byzantine replica (`2` when `calibration=True`), blocking forbidden capability calls (ledger edits, ground-truth reads), and recording every refusal in `anti_cheat_log`.
- `tests/test_phase2.py`: 6 async tests verifying all 8 modes under 5 s load (`total == 10,000`, honest agreement), `verify()` + tamper rejection on `equivocate`/`wrong_digest`/`forge_request`, `>50%` throughput drop without view change under `slow` primary, 2-node collusion divergence in calibration mode (`agreement_ok == False`), `quarantine` + `replace` full recovery to 4 agreeing replicas, and anti-cheat budget enforcement.

## Tests
```
.................                                                        [100%]
17 passed in 19.60s
```
All 17 tests (`5` Phase 0 + `6` Phase 1 + `6` Phase 2) passed; 0 failed, 0 skipped.

## Deviations from PLAN.md
- Registered a secondary client identity (`equiv-client`) so `equivocate` crafts a valid-signature alternate transfer to `A99`, keeping `equivocate` cleanly distinct from `forge_request` (invalid client signature) and allowing honest replicas to diverge in 2-node `calibration=True` mode (`equivocate` primary + `collude` backup).

## Known issues
- none

## Demo check
Command:
```powershell
ByzHeist\.venv\Scripts\python.exe demo_phase2.py
```
Observed output:
```
Caught equivocation on S0 at t=0.05s (verify=True), quarantined + replaced by t=0.67s -> all 4 healthy, referee={'total_ok': True, 'totals_per_replica': {'S0': 10000, 'S1': 10000, 'S2': 10000, 'S3': 10000}, 'agreement_ok': True, 'last_exec_seq_per_replica': {'S0': 1, 'S1': 1, 'S2': 1, 'S3': 1}}
```

## Next
- Phase 3 (target 2:50 PM) will implement `byzheist/telemetry.py` (`snapshot(cluster)` with zero ground-truth leakage + `render_png(snapshot)` 4-panel matplotlib figure saved to `out/telemetry_latest.png`) and `tests/test_phase3.py`.
