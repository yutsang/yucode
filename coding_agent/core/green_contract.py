"""Green-level contract for merge readiness.

Port of ``claw-code-main/rust/crates/runtime/src/green_contract.rs``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class GreenLevel(IntEnum):
    TARGETED_TESTS = 1
    PACKAGE = 2
    WORKSPACE = 3
    MERGE_READY = 4

    def as_str(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class GreenContractOutcome:
    satisfied: bool
    required_level: GreenLevel
    observed_level: GreenLevel | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "satisfied": self.satisfied,
            "required_level": self.required_level.as_str(),
        }
        if self.observed_level is not None:
            d["observed_level"] = self.observed_level.as_str()
        return d


class GreenContract:
    def __init__(self, required_level: GreenLevel) -> None:
        self.required_level = required_level

    def evaluate(self, observed_level: GreenLevel | None) -> GreenContractOutcome:
        if observed_level is None:
            return GreenContractOutcome(satisfied=False, required_level=self.required_level)
        return GreenContractOutcome(
            satisfied=observed_level >= self.required_level,
            required_level=self.required_level,
            observed_level=observed_level,
        )

    def is_satisfied_by(self, observed_level: GreenLevel) -> bool:
        return observed_level >= self.required_level
