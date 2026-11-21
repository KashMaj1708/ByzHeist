"""Anti-cheat fault budget enforcement and Red/Blue capability isolation."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import TYPE_CHECKING, Any, Callable

from byzheist import config
from byzheist.pbft.faults import ALL_MODES, CALIBRATION_ONLY_MODES, FaultConfig

if TYPE_CHECKING:
    from byzheist.pbft.cluster import Cluster


@dataclass(frozen=True)
class BudgetResult:
    """Result of a budget-checked Red/Judge action; truthy iff allowed."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


class FaultBudget:
    """Enforces at most f=1 Byzantine node (2 in calibration) and logs refused actions."""

    def __init__(self, cluster: Cluster, calibration: bool = False) -> None:
        self._cluster = cluster
        self.calibration: bool = calibration
        self.anti_cheat_log: list[dict[str, Any]] = []

    @property
    def max_byzantine(self) -> int:
        return 2 if self.calibration else config.F

    def set_calibration(self, enabled: bool) -> None:
        self.calibration = bool(enabled)

    def _log_refusal(self, actor: str, action: str, reason: str, **extra: Any) -> BudgetResult:
        entry = {
            "timestamp": time.time(),
            "actor": actor,
            "action": action,
            "reason": reason,
            **extra,
        }
        self.anti_cheat_log.append(entry)
        return BudgetResult(ok=False, reason=reason)

    def active_byzantine_nodes(self) -> set[str]:
        return self._cluster._active_byzantine_ids()

    def set_fault(
        self,
        node: str,
        mode: str,
        params: dict[str, Any] | None = None,
        actor: str = "red",
    ) -> BudgetResult:
        """Validate against budget & allowed modes, then apply FaultConfig to node."""
        if node not in self._cluster.replica_ids:
            return self._log_refusal(
                actor,
                "set_fault",
                f"Invalid replica id {node!r}",
                node=node,
                mode=mode,
            )

        if mode not in ALL_MODES:
            return self._log_refusal(
                actor,
                "set_fault",
                f"Forbidden or unknown fault mode {mode!r}",
                node=node,
                mode=mode,
            )

        if mode in CALIBRATION_ONLY_MODES and not self.calibration:
            return self._log_refusal(
                actor,
                "set_fault",
                f"Mode {mode!r} requires calibration=True",
                node=node,
                mode=mode,
            )

        active = self.active_byzantine_nodes()
        if mode != "honest" and node not in active and len(active) >= self.max_byzantine:
            return self._log_refusal(
                actor,
                "set_fault",
                (
                    f"Fault budget exceeded: {sorted(active)} already Byzantine "
                    f"(limit={self.max_byzantine}, calibration={self.calibration})"
                ),
                node=node,
                mode=mode,
            )

        self._cluster._apply_fault(node, FaultConfig(mode=mode, params=dict(params or {})))
        return BudgetResult(ok=True, reason="applied")

    def red_interface(self, actor: str = "red") -> RedSandbox:
        return RedSandbox(self, actor=actor)

    def blue_interface(
        self,
        telemetry_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> BlueSandbox:
        return BlueSandbox(self, self._cluster, telemetry_fn=telemetry_fn)


class RedSandbox:
    """Capability-restricted handle for Red: ONLY `set_fault` is allowed."""

    def __init__(self, budget: FaultBudget, actor: str = "red") -> None:
        self._budget = budget
        self._actor = actor

    def on_node_replaced(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self._budget._cluster.add_replace_listener(callback)

    def set_fault(
        self,
        node: str,
        mode: str,
        params: dict[str, Any] | None = None,
    ) -> BudgetResult:
        return self._budget.set_fault(node=node, mode=mode, params=params, actor=self._actor)

    def __getattr__(self, name: str) -> Callable[..., BudgetResult]:
        def _forbidden(*args: Any, **kwargs: Any) -> BudgetResult:
            return self._budget._log_refusal(
                self._actor,
                name,
                f"Forbidden action {name!r}: Red may only call set_fault(node, mode, params)",
            )

        return _forbidden


class BlueSandbox:
    """Capability-restricted handle for Blue: ONLY quarantine, replace, read_telemetry."""

    def __init__(
        self,
        budget: FaultBudget,
        cluster: Cluster,
        telemetry_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._budget = budget
        self._cluster = cluster
        self._telemetry_fn = telemetry_fn

    def quarantine(
        self,
        node_id: str,
        attack_type: str = "unknown",
        source: str = "manual",
    ) -> dict[str, Any]:
        return self._cluster.quarantine(node_id, attack_type=attack_type, source=source)

    def replace(
        self,
        node_id: str,
        attack_type: str = "unknown",
        source: str = "manual",
    ) -> dict[str, Any]:
        return self._cluster.replace(node_id, attack_type=attack_type, source=source)

    def read_telemetry(self) -> dict[str, Any]:
        if self._telemetry_fn is None:
            raise RuntimeError("Telemetry provider not configured")
        return self._telemetry_fn()

    def __getattr__(self, name: str) -> Callable[..., BudgetResult]:
        def _forbidden(*args: Any, **kwargs: Any) -> BudgetResult:
            return self._budget._log_refusal(
                "blue",
                name,
                f"Forbidden action {name!r}: Blue cannot read FaultConfig or ground truth",
            )

        return _forbidden
