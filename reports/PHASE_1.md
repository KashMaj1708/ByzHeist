# Phase 1 report
Status: DONE
Finished at: 1:52 PM EST (Nov 21, 2026)

## Built
- `byzheist/pbft/crypto.py`: Ed25519 keypair generation, canonical sorted-key JSON serialization, SHA-256 hashing, payload sign/verify (`PyNaCl`), and canonical `ledger_hash`.
- `byzheist/pbft/messages.py`: frozen dataclasses (`Request`, `PrePrepare`, `Prepare`, `Commit`, `Reply`, `PreparedCert`, `ViewChange`, `NewView`) with `signable_dict()`, `to_dict()`/`from_dict()`, and Ed25519 `.signed()`/`.verify()`; `Prepare` carries the primary's signed `PrePrepare`.
- `byzheist/pbft/network.py`: single-process `InMemoryNetwork` with random per-link latency (`5..25 ms` default), link/node drop controls, per-receiver `blocked_senders` quarantine filter, and bounded `event_log` ring buffer (`maxlen=2000`).
- `byzheist/pbft/replica.py`: PBFT normal-case state machine (`REQUEST -> PRE_PREPARE -> PREPARE -> COMMIT -> ordered execute -> REPLY`), replay protection, client/replica Ed25519 signature checks, insufficient-balance ordered `"rejected"` execution, out-of-order message buffering, and simplified PBFT view change (`VIEW_CHANGE` + `NEW_VIEW` validated against `2f+1` `ViewChange`s and `PreparedCert`s).
- `byzheist/pbft/client.py`: `PBFTClient` signing transfers, tracking view hints, waiting for `f+1 = 2` matching verified replies, and broadcasting to all replicas on timeout.
- `byzheist/pbft/cluster.py`: 4-replica `Cluster` (`start()`, `stop()`, `submit()`, `load_generator(rate_hz=5)`, `state_hashes()`, `balances(replica_id)`).
- `byzheist/referee.py`: ground-truth `check(cluster, honest_ids)` checking `10,000` conservation and state-hash agreement across honest replicas at their common executed prefix.
- `tests/test_phase1.py`: 6 async tests covering 50 random transfers, crashed backup, crashed primary + view change (`view >= 1`), replay protection (both immediate duplicate and stale timestamp), forged client signature rejection, and insufficient-balance `"rejected"` ordering.

## Tests
```
...........                                                              [100%]
11 passed in 7.21s
```
All 11 tests (`5` in `test_phase0.py` + `6` in `test_phase1.py`) passed; 0 failed, 0 skipped.

## Deviations from PLAN.md
- Added per-sequence `state_hash_history` on `Replica` so `referee.check()` can deterministically compare honest replicas at their exact common executed prefix (`min(last_exec_seq)`) even while live asynchronous load is in flight.

## Known issues
- minor: On Windows, `asyncio.new_event_loop()` opens an internal loopback TCP `socket.socketpair()`, so `pytest` / `asyncio` scripts must run with loopback sockets allowed rather than inside a strict no-socket sandbox.

## Demo check
Command:
```powershell
ByzHeist\.venv\Scripts\python.exe -c "import asyncio, time; from byzheist import referee; from byzheist.pbft.cluster import Cluster; ..."
```
Observed output:
```
50 txns committed in 5.04s (primary S0 crashed at t=1.52s -> new view=1); referee={'total_ok': True, 'totals_per_replica': {'S0': 10000, 'S1': 10000, 'S2': 10000, 'S3': 10000}, 'agreement_ok': True, 'last_exec_seq_per_replica': {'S0': 25, 'S1': 50, 'S2': 50, 'S3': 50}}
```

## Next
- Phase 2 will add the 8 Byzantine fault modes (`faults.py`), transferable cryptographic evidence (`evidence.py`) + per-peer unprovable observations, `quarantine(id)` + `replace(id)` with key rotation and `f+1` state transfer in `cluster.py`, anti-cheat budget enforcement (`agents/budget.py`), and `tests/test_phase2.py`.
