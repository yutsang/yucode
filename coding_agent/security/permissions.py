"""Permission policy with five ordered modes, enforcement, and rules.

Mode ordering (lowest to highest):
  read-only < workspace-write < danger-full-access < prompt < allow

The ``PermissionEnforcer`` provides a non-interactive gate used during tool
dispatch.  ``PermissionPolicy`` handles the full interactive flow including
optional prompting and rule evaluation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
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

PERMISSION_MODE_ALIASES: dict[str, PermissionMode] = {
    "plan": "read-only",
    "default": "read-only",
    "acceptEdits": "workspace-write",
    "auto": "workspace-write",
    "dontAsk": "danger-full-access",
}

_READ_ONLY_COMMANDS = frozenset([
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "file",
    "stat", "du", "df", "which", "whoami", "hostname", "uname", "date",
    "pwd", "echo", "env", "printenv", "id", "test", "true", "false",
    "diff", "sort", "uniq", "tr", "cut", "awk", "less", "more",
    "git status", "git log", "git diff", "git show", "git branch",
    "git remote", "git tag", "cargo check", "cargo test", "cargo clippy",
    "python -c", "python3 -c", "node -e",
])

_MUTATING_INDICATORS = frozenset([
    ">", ">>", "2>", "&>",   # output/error redirects
    "-i", "--in-place",       # in-place edit flags (sed, perl, etc.)
    ";", "&&", "||",          # command chaining — anything after ; could mutate
    "$(", "`",                # subshell expansion — arbitrary code execution
    "sudo",                   # privilege escalation
])


def resolve_permission_mode(label: str) -> PermissionMode:
    """Resolve a permission mode string, supporting aliases."""
    if label in PERMISSION_ORDER:
        return label  # type: ignore[return-value]
    if label in PERMISSION_MODE_ALIASES:
        return PERMISSION_MODE_ALIASES[label]
    raise ValueError(f"Unknown permission mode: {label}")


@dataclass(frozen=True)
class PermissionRequest:
    tool_name: str
    tool_input: str
    required_mode: PermissionMode
    current_mode: PermissionMode = "read-only"
    reason: str = ""


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class PermissionOverride:
    """Hook-sourced override for permission decisions."""
    action: Literal["allow", "deny", "ask"] = "allow"
    reason: str = ""


@dataclass(frozen=True)
class PermissionContext:
    """Context from pre-hooks that can influence permission decisions."""
    override: PermissionOverride | None = None
    updated_input: str | None = None


class PermissionPrompter(Protocol):
    def decide(self, request: PermissionRequest) -> PermissionDecision: ...


@dataclass(frozen=True)
class PermissionRule:
    """A single allow/deny/ask rule (e.g. ``bash(git:*)`` or ``write_file``)."""
    tool_pattern: str
    subject_pattern: str = ""

    def matches(self, tool_name: str, tool_input: str) -> bool:
        if not re.match(self.tool_pattern.replace("*", ".*"), tool_name):
            return False
        if not self.subject_pattern:
            return True
        subject = _extract_permission_subject(tool_name, tool_input)
        return bool(re.match(self.subject_pattern.replace("*", ".*"), subject))


@dataclass
class PermissionRules:
    allow: list[PermissionRule] = field(default_factory=list)
    deny: list[PermissionRule] = field(default_factory=list)
    ask: list[PermissionRule] = field(default_factory=list)


class PermissionPolicy:
    def __init__(
        self,
        mode: PermissionMode,
        rules: PermissionRules | None = None,
    ) -> None:
        self.mode = mode
        self._tool_overrides: dict[str, PermissionMode] = {}
        self.rules = rules or PermissionRules()

    def with_tool_requirement(self, tool_name: str, required: PermissionMode) -> PermissionPolicy:
        self._tool_overrides[tool_name] = required
        return self

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        return self._tool_overrides.get(tool_name, "danger-full-access")

    def authorize(
        self,
        tool_permission: PermissionMode,
        tool_name: str,
        tool_input: str = "",
        prompter: PermissionPrompter | None = None,
        context: PermissionContext | None = None,
    ) -> PermissionDecision:
        if self.mode == "allow":
            return PermissionDecision(True)

        for rule in self.rules.deny:
            if rule.matches(tool_name, tool_input):
                return PermissionDecision(False, f"Denied by rule: {rule.tool_pattern}")

        if context and context.override:
            ov = context.override
            if ov.action == "deny":
                return PermissionDecision(False, ov.reason or "Denied by hook override")
            if ov.action == "ask" and prompter is not None:
                return prompter.decide(PermissionRequest(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    required_mode=tool_permission,
                    current_mode=self.mode,
                    reason=ov.reason,
                ))
            if ov.action == "allow":
                for ask_rule in self.rules.ask:
                    if ask_rule.matches(tool_name, tool_input):
                        if prompter is not None:
                            return prompter.decide(PermissionRequest(
                                tool_name=tool_name,
                                tool_input=tool_input,
                                required_mode=tool_permission,
                                current_mode=self.mode,
                                reason="Required by ask rule despite hook allow",
                            ))
                        return PermissionDecision(False, "Ask rule requires confirmation")
                return PermissionDecision(True, ov.reason)

        for rule in self.rules.ask:
            if rule.matches(tool_name, tool_input):
                if prompter is not None:
                    return prompter.decide(PermissionRequest(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        required_mode=tool_permission,
                        current_mode=self.mode,
                        reason=f"Ask rule: {rule.tool_pattern}",
                    ))
                return PermissionDecision(False, "Ask rule requires confirmation but no prompter")

        for rule in self.rules.allow:
            if rule.matches(tool_name, tool_input):
                return PermissionDecision(True)

        if self.mode == "prompt":
            if prompter is not None:
                return prompter.decide(PermissionRequest(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    required_mode=tool_permission,
                    current_mode=self.mode,
                    reason=f"Tool `{tool_name}` requires `{tool_permission}`",
                ))
            return PermissionDecision(
                False,
                f"Tool `{tool_name}` requires `{tool_permission}` but no prompter is available.",
            )

        current_level = PERMISSION_ORDER[self.mode]
        required_level = PERMISSION_ORDER[tool_permission]

        if current_level >= required_level:
            return PermissionDecision(True)

        return PermissionDecision(
            False,
            f"Tool `{tool_name}` requires `{tool_permission}` but runtime is `{self.mode}`.",
        )


# ---------------------------------------------------------------------------
# PermissionEnforcer -- non-interactive gate for tool dispatch
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnforcementResult:
    allowed: bool
    tool_name: str = ""
    active_mode: str = ""
    required_mode: str = ""
    reason: str = ""


class PermissionEnforcer:
    """Non-interactive permission gate used during tool execution.

    In ``prompt`` mode, ``check()`` returns Allowed (deferring to the
    interactive flow), matching Rust ``permission_enforcer.rs`` semantics.
    """

    def __init__(self, policy: PermissionPolicy, workspace_root: Path | None = None) -> None:
        self.policy = policy
        self.workspace_root = workspace_root

    def check(self, tool_name: str, tool_input: str = "") -> EnforcementResult:
        """Non-interactive check: prompt mode defers (returns allowed)."""
        if self.policy.mode == "allow":
            return EnforcementResult(allowed=True, tool_name=tool_name)
        if self.policy.mode == "prompt":
            return EnforcementResult(allowed=True, tool_name=tool_name)

        decision = self.policy.authorize(
            self.policy.required_mode_for(tool_name),
            tool_name,
            tool_input,
        )
        return EnforcementResult(
            allowed=decision.allowed,
            tool_name=tool_name,
            active_mode=self.policy.mode,
            required_mode=self.policy.required_mode_for(tool_name),
            reason=decision.reason,
        )

    def check_file_write(self, file_path: str) -> EnforcementResult:
        """Enforce workspace boundary for file write operations."""
        mode = self.policy.mode
        if mode == "read-only":
            return EnforcementResult(
                allowed=False, tool_name="write_file",
                active_mode=mode, required_mode="workspace-write",
                reason="File writes are not allowed in read-only mode",
            )
        if mode in ("danger-full-access", "allow"):
            return EnforcementResult(allowed=True, tool_name="write_file")
        if mode == "prompt":
            return EnforcementResult(
                allowed=False, tool_name="write_file",
                active_mode=mode, required_mode="workspace-write",
                reason="File write requires confirmation in prompt mode",
            )
        if mode == "workspace-write" and self.workspace_root:
            resolved = Path(file_path)
            if not resolved.is_absolute():
                resolved = self.workspace_root / resolved
            resolved = resolved.resolve()
            ws = self.workspace_root.resolve()
            if ws not in (resolved, *resolved.parents):
                return EnforcementResult(
                    allowed=False, tool_name="write_file",
                    active_mode=mode, required_mode="workspace-write",
                    reason=f"Path `{resolved}` is outside workspace `{ws}`",
                )
        return EnforcementResult(allowed=True, tool_name="write_file")

    def check_bash(self, command: str) -> EnforcementResult:
        """Enforce bash command restrictions based on permission mode."""
        mode = self.policy.mode
        if mode in ("danger-full-access", "allow"):
            return EnforcementResult(allowed=True, tool_name="bash")
        if mode == "prompt":
            return EnforcementResult(
                allowed=False, tool_name="bash",
                active_mode=mode, required_mode="danger-full-access",
                reason="Bash requires confirmation in prompt mode",
            )
        if mode == "read-only":
            if _is_read_only_command(command):
                return EnforcementResult(allowed=True, tool_name="bash")
            return EnforcementResult(
                allowed=False, tool_name="bash",
                active_mode=mode, required_mode="danger-full-access",
                reason="Only read-only commands are allowed in read-only mode",
            )
        return EnforcementResult(allowed=True, tool_name="bash")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_read_only_command(command: str) -> bool:
    """Heuristic: does the command look read-only?"""
    stripped = command.strip()
    if not stripped:
        return False
    for indicator in _MUTATING_INDICATORS:
        if indicator in stripped:
            return False
    # Skip leading ``KEY=val`` environment-variable assignments so
    # ``FOO=bar ls`` is recognised the same as ``ls``.
    tokens = stripped.split()
    start = 0
    for tok in tokens:
        if "=" in tok:
            name, _, _ = tok.partition("=")
            if name and all(c.isalnum() or c == "_" for c in name):
                start += 1
                continue
        break
    tokens = tokens[start:]
    if not tokens:
        return False
    first_token = tokens[0]
    for prefix_len in (1, 2):
        prefix = " ".join(tokens[:prefix_len])
        if prefix in _READ_ONLY_COMMANDS:
            return True
    return first_token in _READ_ONLY_COMMANDS


def _extract_permission_subject(tool_name: str, tool_input: str) -> str:
    """Extract the subject string for rule matching from tool input."""
    try:
        parsed = json.loads(tool_input)
        if isinstance(parsed, dict):
            for key in ("command", "path", "file_path", "uri", "name"):
                if key in parsed:
                    return str(parsed[key])
    except (json.JSONDecodeError, TypeError):
        pass
    return tool_input if tool_input else ""


def parse_permission_rule(rule_str: str) -> PermissionRule:
    """Parse a rule string like ``bash(git:*)`` into a PermissionRule."""
    match = re.match(r"^([^(]+?)(?:\((.+)\))?$", rule_str.strip())
    if not match:
        return PermissionRule(tool_pattern=rule_str.strip())
    return PermissionRule(
        tool_pattern=match.group(1).strip(),
        subject_pattern=(match.group(2) or "").strip(),
    )
