"""Byzantine behaviour modes and per-replica FaultConfig."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


BYZANTINE_MODES: frozenset[str] = frozenset(
    {
        "silent",
        "equivocate",
        "wrong_digest",
        "slow",
        "drop_votes",
        "lie_reply",
        "forge_request",
        "frame",
    }
)

CALIBRATION_ONLY_MODES: frozenset[str] = frozenset({"collude"})
ALL_MODES: frozenset[str] = frozenset({"honest"}) | BYZANTINE_MODES | CALIBRATION_ONLY_MODES


@dataclass(frozen=True)
class FaultConfig:
    """Internal fault configuration for a single replica (hidden from Blue)."""

    mode: str = "honest"
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in ALL_MODES:
            raise ValueError(f"Unknown fault mode: {self.mode!r}")

    @property
    def is_byzantine(self) -> bool:
        return self.mode != "honest"
