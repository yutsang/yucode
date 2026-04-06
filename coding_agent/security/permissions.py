"""Permission policy with five ordered modes and interactive prompting.

Mode ordering (lowest to highest):
  read-only < workspace-write < danger-full-access < prompt < allow

In ``prompt`` mode the runtime will ask a ``PermissionPrompter`` callback
before executing tools that exceed the base mode.  ``allow`` mode grants
everything unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

PermissionMode = Literal[
    "read-only",
    "workspace-write",
    "danger-full-access",
    "prompt",
    "allow",
]

PERMISSION_ORDER: dict[PermissionMode, int] = {
    "read-only": 0,
    "workspace-write": 1,
    "danger-full-access": 2,
    "prompt": 3,
    "allow": 4,
}


@dataclass(frozen=True)
class PermissionRequest:
    tool_name: str
    tool_input: str
    required_mode: PermissionMode


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""


class PermissionPrompter(Protocol):
    def decide(self, request: PermissionRequest) -> PermissionDecision: ...


class PermissionPolicy:
    def __init__(self, mode: PermissionMode) -> None:
        self.mode = mode
        self._tool_overrides: dict[str, PermissionMode] = {}

    def with_tool_requirement(self, tool_name: str, required: PermissionMode) -> PermissionPolicy:
        self._tool_overrides[tool_name] = required
        return self

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        return self._tool_overrides.get(tool_name, "read-only")

    def authorize(
        self,
        tool_permission: PermissionMode,
        tool_name: str,
        tool_input: str = "",
        prompter: PermissionPrompter | None = None,
    ) -> PermissionDecision:
        if self.mode == "allow":
            return PermissionDecision(True)

        current_level = PERMISSION_ORDER[self.mode]
        required_level = PERMISSION_ORDER[tool_permission]

        if current_level >= required_level:
            return PermissionDecision(True)

        if self.mode == "prompt" and prompter is not None:
            return prompter.decide(PermissionRequest(
                tool_name=tool_name,
                tool_input=tool_input,
                required_mode=tool_permission,
            ))

        return PermissionDecision(
            False,
            f"Tool `{tool_name}` requires `{tool_permission}` but runtime is `{self.mode}`.",
        )
