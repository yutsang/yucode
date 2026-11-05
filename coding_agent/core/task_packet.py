"""Structured task packet format with validation.

Port of ``claw-code-main/rust/crates/runtime/src/task_packet.rs``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskPacket:
    objective: str
    scope: str = ""
    repo: str = ""
    branch_policy: str = ""
    acceptance_tests: list[str] = field(default_factory=list)
    commit_policy: str = ""
    reporting_contract: str = ""
    escalation_policy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "scope": self.scope,
            "repo": self.repo,
            "branch_policy": self.branch_policy,
            "acceptance_tests": list(self.acceptance_tests),
            "commit_policy": self.commit_policy,
            "reporting_contract": self.reporting_contract,
            "escalation_policy": self.escalation_policy,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskPacket:
        return cls(
            objective=str(data.get("objective", "")),
            scope=str(data.get("scope", "")),
            repo=str(data.get("repo", "")),
            branch_policy=str(data.get("branch_policy", "")),
            acceptance_tests=[str(t) for t in data.get("acceptance_tests", [])],
            commit_policy=str(data.get("commit_policy", "")),
            reporting_contract=str(data.get("reporting_contract", "")),
            escalation_policy=str(data.get("escalation_policy", "")),
        )


class TaskPacketValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"Task packet validation failed: {'; '.join(errors)}")


def validate_packet(packet: TaskPacket) -> TaskPacket:
    """Validate a task packet, raising ``TaskPacketValidationError`` on failure."""
    errors: list[str] = []
    if not packet.objective.strip():
        errors.append("objective must not be empty")
    for i, test in enumerate(packet.acceptance_tests):
        if not test.strip():
            errors.append(f"acceptance_tests[{i}] must not be empty")
    if errors:
        raise TaskPacketValidationError(errors)
    return packet
