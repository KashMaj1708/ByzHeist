"""Observable cluster telemetry snapshot and 4-panel PNG rendering for Blue."""

from __future__ import annotations

import io
from pathlib import Path
import statistics
import time
from typing import Any

import matplotlib

matplotlib.use("Agg")
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
import numpy as np  # noqa: E402

from byzheist import config  # noqa: E402
from byzheist.pbft.cluster import Cluster  # noqa: E402


def _compute_problem_matrix(
    replica_ids: tuple[str, ...],
    reported_by_replica: dict[str, dict[str, dict[str, float | int]]],
) -> tuple[list[list[float]], list[list[float]]]:
    """Compute 4x4 problem score matrix (missing_votes + mismatches + positive latency z-score)."""
    n = len(replica_ids)
    off_diag_lats: list[float] = []
    for i, r_row in enumerate(replica_ids):
        row_obs = reported_by_replica.get(r_row, {})
        for j, r_col in enumerate(replica_ids):
            if i == j:
                continue
            lat = float(row_obs.get(r_col, {}).get("avg_latency_ms", 0.0))
            if lat > 0.0:
                off_diag_lats.append(lat)

    if off_diag_lats:
        med_lat = float(statistics.median(off_diag_lats))
        mad = float(statistics.median([abs(x - med_lat) for x in off_diag_lats]))
        scale = max(1.4826 * mad, 5.0)
    else:
        med_lat = 15.0
        scale = 5.0

    scores: list[list[float]] = [[0.0 for _ in range(n)] for _ in range(n)]
    z_matrix: list[list[float]] = [[0.0 for _ in range(n)] for _ in range(n)]
    for i, r_row in enumerate(replica_ids):
        row_obs = reported_by_replica.get(r_row, {})
        for j, r_col in enumerate(replica_ids):
            if i == j:
                continue
            cell = row_obs.get(r_col, {})
            missing = float(cell.get("missing_votes", 0))
            mismatches = float(cell.get("mismatches", 0))
            lat = float(cell.get("avg_latency_ms", 0.0))
            z_lat = max(0.0, (lat - med_lat) / scale) if lat > 0.0 else 0.0
            z_matrix[i][j] = round(z_lat, 2)
            scores[i][j] = round(missing + mismatches + z_lat, 2)

    return scores, z_matrix


def snapshot(cluster: Cluster) -> dict[str, Any]:
    """Build Blue-safe telemetry snapshot (strictly excludes FaultConfig and referee ground truth)."""
    now_mono = time.monotonic()
    now_wall = time.time()
    uptime_s = max(0.5, now_mono - getattr(cluster, "start_mono", now_mono))
    rate_window_s = min(10.0, uptime_s)

    replica_ids = cluster.replica_ids
    reported_map: dict[str, dict[str, dict[str, float | int]]] = {
        rid: cluster.replicas[rid].get_reported_observations() for rid in replica_ids
    }
    problem_matrix, latency_z_matrix = _compute_problem_matrix(replica_ids, reported_map)

    verified_evs = cluster.verified_evidence()
    verified_dicts = [
        {
            "kind": ev.kind,
            "accused": ev.accused,
            "reporter": ev.reporter,
            "view": ev.view,
            "seq": ev.seq,
            "timestamp": ev.timestamp,
        }
        for ev in verified_evs
    ]

    replicas_info: dict[str, dict[str, Any]] = {}
    for rid in replica_ids:
        rep = cluster.replicas[rid]
        recent_commits = sum(1 for ts in rep.commit_timestamps if (now_mono - ts) <= 10.0)
        cps = round(recent_commits / rate_window_s, 2)

        # Peer-consensus median outbound latency for `rid` across ALL non-quarantined peers
        peer_meds = [
            float(reported_map[peer][rid]["median_latency_ms"])
            for peer in replica_ids
            if peer != rid and peer not in cluster.quarantined
        ]
        median_outbound_ms = round(float(statistics.median(peer_meds)), 2) if peer_meds else 0.0

        replicas_info[rid] = {
            "view": rep.view,
            "is_primary": rep.is_primary(),
            "quarantined": rid in cluster.quarantined,
            "last_exec_seq": rep.last_exec_seq,
            "commits_per_sec": cps,
            "median_outbound_latency_ms": median_outbound_ms,
            "reported_observations": reported_map[rid],
            "evidence_count": len(rep.evidence),
        }

    # 60-second cluster throughput timeline (1-second bins over [-59..0])
    # Use the median commit count per second across active replicas to represent cluster commits.
    bins = list(range(-59, 1))
    per_replica_bins: list[list[int]] = []
    for rid in replica_ids:
        if rid in cluster.quarantined:
            continue
        counts = [0] * 60
        for ts in cluster.replicas[rid].commit_timestamps:
            rel = ts - now_mono
            if -60.0 < rel <= 0.0:
                idx = min(59, max(0, int(rel + 60.0)))
                counts[idx] += 1
        per_replica_bins.append(counts)

    throughput_timeline: list[dict[str, float]] = []
    for b_idx, t_rel in enumerate(bins):
        if per_replica_bins:
            vals = sorted(r_bins[b_idx] for r_bins in per_replica_bins)
            c_val = float(vals[len(vals) // 2])
        else:
            c_val = 0.0
        throughput_timeline.append({"t_rel": float(t_rel), "commits_per_sec": c_val})

    # Deduplicate view-change relative timestamps (within 0.25s buckets over last 60s)
    raw_vc_rels: list[float] = []
    for rid in replica_ids:
        for mono_t in cluster.replicas[rid].view_change_mono_times:
            rel = mono_t - now_mono
            if -60.0 <= rel <= 0.0:
                raw_vc_rels.append(round(rel, 2))
    raw_vc_rels.sort()
    view_change_times: list[float] = []
    for rel in raw_vc_rels:
        if not view_change_times or abs(rel - view_change_times[-1]) > 0.25:
            view_change_times.append(rel)

    quarantine_times = [
        {
            "t_rel": round(ev["mono_t"] - now_mono, 2),
            "replica_id": ev["replica_id"],
        }
        for ev in cluster.quarantine_events
        if -60.0 <= (ev["mono_t"] - now_mono) <= 0.0
    ]

    replace_times = [
        {
            "t_rel": round(ev["mono_t"] - now_mono, 2),
            "replica_id": ev["replica_id"],
        }
        for ev in cluster.replace_events
        if -60.0 <= (ev["mono_t"] - now_mono) <= 0.0
    ]

    recent_events = [
        {
            "t": ev["t"],
            "src": ev["src"],
            "dst": ev["dst"],
            "type": ev["type"],
            "view": ev["view"],
            "seq": ev["seq"],
            "digest": (ev["digest"][:10] if isinstance(ev["digest"], str) else None),
        }
        for ev in list(cluster.network.event_log)[-40:]
    ]

    return {
        "timestamp": now_wall,
        "uptime_sec": round(uptime_s, 2),
        "replica_ids": list(replica_ids),
        "replicas": replicas_info,
        "problem_matrix": problem_matrix,
        "latency_z_matrix": latency_z_matrix,
        "verified_evidence": verified_dicts,
        "cluster": {
            "quarantined": sorted(cluster.quarantined),
            "throughput_timeline": throughput_timeline,
            "view_change_times": view_change_times,
            "quarantine_times": quarantine_times,
            "replace_times": replace_times,
            "recent_events": recent_events,
        },
    }


def render_png(
    snap: dict[str, Any],
    output_path: Path | None = None,
) -> bytes:
    """Render 1200x800 4-panel visual telemetry frame for Blue and save latest PNG."""
    replica_ids: list[str] = list(snap.get("replica_ids", config.REPLICA_IDS))
    replicas = snap["replicas"]
    matrix = np.array(snap["problem_matrix"], dtype=float)
    cluster_data = snap["cluster"]
    verified_ev = snap["verified_evidence"]

    bg_color = "#0f172a"
    panel_color = "#1e293b"
    text_color = "#f8fafc"
    muted_color = "#94a3b8"
    grid_color = "#334155"

    fig = Figure(figsize=(12, 8), dpi=100, facecolor=bg_color)
    canvas = FigureCanvasAgg(fig)
    gs = fig.add_gridspec(2, 2, hspace=0.35, wspace=0.26, left=0.10, right=0.97, top=0.865, bottom=0.08)

    fig.suptitle(
        f"ByzHeist Visual Telemetry  |  Uptime {snap.get('uptime_sec', 0):.1f}s  |  "
        f"Verified Proofs: {len(verified_ev)}  |  Quarantined: {cluster_data['quarantined'] or 'None'}",
        color=text_color,
        fontsize=13,
        fontweight="bold",
        y=0.96,
    )

    # ------------------------------------------------------------------
    # Panel 1 (Top-Left): 4x4 Reported Problems Heatmap
    # ------------------------------------------------------------------
    ax1 = fig.add_subplot(gs[0, 0], facecolor=panel_color)
    vmax = max(10.0, float(np.max(matrix)) if matrix.size else 10.0)
    im = ax1.imshow(matrix, cmap="YlOrRd", vmin=0.0, vmax=vmax, aspect="auto")
    ax1.set_title(
        "1. Reported Problems: Row Reports Column\n(missing votes + mismatches + latency z-score)",
        color=text_color,
        fontsize=10.5,
        fontweight="bold",
        pad=8,
    )
    ax1.set_xticks(range(len(replica_ids)))
    ax1.set_yticks(range(len(replica_ids)))
    ax1.set_xticklabels([f"Target {r}" for r in replica_ids], color=text_color, fontsize=9)
    ax1.set_yticklabels([f"Reporter {r}" for r in replica_ids], color=text_color, fontsize=9)

    for i, r_row in enumerate(replica_ids):
        obs_row = replicas[r_row]["reported_observations"]
        for j, r_col in enumerate(replica_ids):
            val = matrix[i, j]
            if i == j:
                label = "—"
                cell_fg = "#64748b"
            else:
                cell = obs_row.get(r_col, {})
                mv = int(cell.get("missing_votes", 0))
                mm = int(cell.get("mismatches", 0))
                lat = float(cell.get("avg_latency_ms", 0.0))
                label = f"{val:.1f}\n(m:{mv}/x:{mm}\n{lat:.0f}ms)"
                cell_fg = "#ffffff" if val > 0.45 * vmax else "#0f172a"
            ax1.text(
                j,
                i,
                label,
                ha="center",
                va="center",
                color=cell_fg,
                fontsize=7.5,
                fontweight="bold" if val >= 5.0 else "normal",
            )
    cbar = fig.colorbar(im, ax=ax1, fraction=0.046, pad=0.04)
    cbar.ax.yaxis.set_tick_params(color=muted_color, labelcolor=muted_color, labelsize=8)

    # ------------------------------------------------------------------
    # Panel 2 (Top-Right): Per-Replica Median Outbound Latency Bars
    # ------------------------------------------------------------------
    ax2 = fig.add_subplot(gs[0, 1], facecolor=panel_color)
    med_lats = [float(replicas[r]["median_outbound_latency_ms"]) for r in replica_ids]
    bar_labels = [
        f"{r}\n({'PRI' if replicas[r]['is_primary'] else 'BKP'}"
        f"{', QUAR' if replicas[r]['quarantined'] else ''})"
        for r in replica_ids
    ]
    bar_colors = [
        "#ef4444" if lat > 120.0 else ("#f59e0b" if lat > 50.0 else "#38bdf8")
        for lat in med_lats
    ]
    bars = ax2.bar(bar_labels, med_lats, color=bar_colors, edgecolor="#e2e8f0", linewidth=0.8, width=0.55)
    ax2.set_title(
        "2. Per-Replica Peer-Consensus Outbound Latency (ms)",
        color=text_color,
        fontsize=10.5,
        fontweight="bold",
        pad=8,
    )
    ax2.set_ylabel("Median Latency (ms)", color=muted_color, fontsize=9)
    ax2.set_ylim(0.0, max(60.0, max(med_lats, default=0.0) * 1.25))
    ax2.grid(axis="y", color=grid_color, linestyle="--", alpha=0.6)
    ax2.tick_params(colors=text_color, labelsize=8.5)
    for spine in ax2.spines.values():
        spine.set_color(grid_color)
    for rect, lat in zip(bars, med_lats):
        ax2.text(
            rect.get_x() + rect.get_width() / 2.0,
            rect.get_height() + max(1.5, ax2.get_ylim()[1] * 0.02),
            f"{lat:.1f} ms",
            ha="center",
            va="bottom",
            color=text_color,
            fontsize=8.5,
            fontweight="bold",
        )

    # ------------------------------------------------------------------
    # Panel 3 (Bottom-Left): Commit Throughput Over Last 60s + Markers
    # ------------------------------------------------------------------
    ax3 = fig.add_subplot(gs[1, 0], facecolor=panel_color)
    timeline = cluster_data["throughput_timeline"]
    xs = [pt["t_rel"] for pt in timeline]
    ys = [pt["commits_per_sec"] for pt in timeline]
    ax3.plot(xs, ys, color="#22c55e", linewidth=2.0, label="Commits/s")
    ax3.fill_between(xs, ys, color="#22c55e", alpha=0.18)

    vc_labeled = False
    for vc_t in cluster_data["view_change_times"]:
        ax3.axvline(
            vc_t,
            color="#facc15",
            linestyle="--",
            linewidth=1.6,
            label="View change" if not vc_labeled else None,
        )
        vc_labeled = True

    q_labeled = False
    for q_ev in cluster_data["quarantine_times"]:
        ax3.axvline(
            q_ev["t_rel"],
            color="#ef4444",
            linestyle="-.",
            linewidth=1.8,
            label=f"Quarantine ({q_ev['replica_id']})" if not q_labeled else None,
        )
        q_labeled = True

    ax3.set_title(
        "3. Cluster Commit Throughput (Last 60s)",
        color=text_color,
        fontsize=10.5,
        fontweight="bold",
        pad=8,
    )
    ax3.set_xlabel("Seconds Relative to Now", color=muted_color, fontsize=9)
    ax3.set_ylabel("Commits / sec", color=muted_color, fontsize=9)
    ax3.set_xlim(-60.0, 0.5)
    ax3.set_ylim(0.0, max(8.0, max(ys, default=0.0) * 1.25))
    ax3.grid(True, color=grid_color, linestyle="--", alpha=0.6)
    ax3.tick_params(colors=text_color, labelsize=8.5)
    for spine in ax3.spines.values():
        spine.set_color(grid_color)
    ax3.legend(loc="upper left", facecolor=bg_color, edgecolor=grid_color, labelcolor=text_color, fontsize=8)

    # ------------------------------------------------------------------
    # Panel 4 (Bottom-Right): Verified Evidence & Replica Summary Table
    # ------------------------------------------------------------------
    ax4 = fig.add_subplot(gs[1, 1], facecolor=panel_color)
    ax4.axis("off")
    ax4.set_title(
        "4. Cryptographically Verified Evidence & Replica Status",
        color=text_color,
        fontsize=10.5,
        fontweight="bold",
        pad=8,
    )

    lines: list[str] = []
    lines.append("REPLICA STATUS (10s window):")
    lines.append("ID   Role  View  ExecSeq  Commits/s  ProofCnt  Quarantined")
    lines.append("-" * 58)
    for r in replica_ids:
        ri = replicas[r]
        role = "PRI" if ri["is_primary"] else "BKP"
        lines.append(
            f"{r:<4} {role:<5} v={ri['view']:<3} seq={ri['last_exec_seq']:<4} "
            f"{ri['commits_per_sec']:>7.2f}/s  {ri['evidence_count']:>8}  "
            f"{'YES' if ri['quarantined'] else 'no'}"
        )

    lines.append("")
    lines.append("CRYPTOGRAPHICALLY VERIFIED EVIDENCE:")
    lines.append("-" * 58)
    if not verified_ev:
        lines.append("NONE — 0 signed proofs collected. Check heatmap/latency")
        lines.append("Note: Single-reporter heatmap spike alone may be `frame`!")
        ev_box_color = "#14532d"
    else:
        for ev in verified_ev[:6]:
            lines.append(
                f"[PROOF] kind={ev['kind']}  accused={ev['accused']}  "
                f"(v={ev['view']}, seq={ev['seq']}, by {ev['reporter']})"
            )
        ev_box_color = "#7f1d1d"

    ax4.text(
        0.03,
        0.95,
        "\n".join(lines),
        transform=ax4.transAxes,
        va="top",
        ha="left",
        family="monospace",
        fontsize=8.3,
        color=text_color,
        bbox={
            "boxstyle": "round,pad=0.5",
            "facecolor": ev_box_color,
            "edgecolor": "#64748b",
            "alpha": 0.45,
        },
    )

    buf = io.BytesIO()
    canvas.print_png(buf)
    png_bytes = buf.getvalue()

    target_path = output_path if output_path is not None else (config.OUT_DIR / "telemetry_latest.png")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(png_bytes)

    return png_bytes
