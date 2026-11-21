"""Ground-truth referee checking money conservation and honest replica agreement."""

from __future__ import annotations

from typing import Any, Iterable

from byzheist import config
from byzheist.pbft.cluster import Cluster


def check(
    cluster: Cluster,
    honest_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Verify total money conservation (10,000) and state agreement across honest replicas."""
    honest_list = list(honest_ids) if honest_ids is not None else list(cluster.replica_ids)

    totals_per_replica: dict[str, int] = {
        rid: sum(cluster.balances(rid).values()) for rid in cluster.replica_ids
    }
    last_exec_seq_per_replica: dict[str, int] = {
        rid: cluster.replicas[rid].last_exec_seq for rid in cluster.replica_ids
    }

    total_ok = all(totals_per_replica[rid] == config.EXPECTED_TOTAL for rid in honest_list)

    if not honest_list:
        agreement_ok = True
    else:
        common_seq = min(last_exec_seq_per_replica[rid] for rid in honest_list)
        prefix_hashes = {
            cluster.replicas[rid].state_hash_at(common_seq) for rid in honest_list
        }
        agreement_ok = None not in prefix_hashes and len(prefix_hashes) == 1

    return {
        "total_ok": total_ok,
        "totals_per_replica": totals_per_replica,
        "agreement_ok": agreement_ok,
        "last_exec_seq_per_replica": last_exec_seq_per_replica,
    }
