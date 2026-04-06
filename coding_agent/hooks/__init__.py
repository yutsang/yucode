"""Pre/post tool-use hook system.

Hooks are shell commands that receive a JSON payload on stdin and communicate
results via exit codes:
  - 0: allow (stdout is captured as feedback)
  - 2: deny (tool execution is blocked)
  - other non-zero: warn (tool execution continues, warning is appended)

Environment variables set for every hook invocation:
  HOOK_EVENT          "PreToolUse" or "PostToolUse"
  HOOK_TOOL_NAME      name of the tool
  HOOK_TOOL_INPUT     raw tool input string
  HOOK_TOOL_IS_ERROR  "1" if previous result was an error, else "0"
  HOOK_TOOL_OUTPUT    (PostToolUse only) tool output string
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class HookEvent(Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"


@dataclass
class HookRunResult:
    denied: bool = False
    messages: list[str] = field(default_factory=list)

    @classmethod
    def allow(cls, messages: list[str] | None = None) -> HookRunResult:
        return cls(denied=False, messages=messages or [])

    @property
    def is_denied(self) -> bool:
        return self.denied


@dataclass
class HookConfig:
    pre_tool_use: list[str] = field(default_factory=list)
    post_tool_use: list[str] = field(default_factory=list)
    pre_compact: list[str] = field(default_factory=list)
    post_compact: list[str] = field(default_factory=list)


class HookRunner:
    def __init__(self, config: HookConfig | None = None) -> None:
        self.config = config or HookConfig()

    def run_pre_tool_use(self, tool_name: str, tool_input: str) -> HookRunResult:
        return self._run_commands(
            HookEvent.PRE_TOOL_USE,
            self.config.pre_tool_use,
            tool_name,
            tool_input,
        )

    def run_post_tool_use(
        self,
        tool_name: str,
        tool_input: str,
        tool_output: str,
        is_error: bool,
    ) -> HookRunResult:
        return self._run_commands(
            HookEvent.POST_TOOL_USE,
            self.config.post_tool_use,
            tool_name,
            tool_input,
            tool_output=tool_output,
            is_error=is_error,
        )

    def run_pre_compact(self, message_count: int, estimated_tokens: int) -> HookRunResult:
        return self._run_commands(
            HookEvent.PRE_COMPACT,
            self.config.pre_compact,
            "compact",
            json.dumps({"message_count": message_count, "estimated_tokens": estimated_tokens}),
        )

    def run_post_compact(self, removed_count: int, summary_length: int) -> HookRunResult:
        return self._run_commands(
            HookEvent.POST_COMPACT,
            self.config.post_compact,
            "compact",
            json.dumps({"removed_count": removed_count, "summary_length": summary_length}),
        )

    def _run_commands(
        self,
        event: HookEvent,
        commands: list[str],
        tool_name: str,
        tool_input: str,
        tool_output: str | None = None,
        is_error: bool = False,
    ) -> HookRunResult:
        if not commands:
            return HookRunResult.allow()

        try:
            parsed_input = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            parsed_input = {"raw": tool_input}

        payload = json.dumps({
            "hook_event_name": event.value,
            "tool_name": tool_name,
            "tool_input": parsed_input,
            "tool_input_json": tool_input,
            "tool_output": tool_output,
            "tool_result_is_error": is_error,
        })

        messages: list[str] = []
        for command in commands:
            outcome = self._run_single(command, event, tool_name, tool_input, tool_output, is_error, payload)
            if outcome.kind == _OutcomeKind.ALLOW:
                if outcome.message:
                    messages.append(outcome.message)
            elif outcome.kind == _OutcomeKind.DENY:
                msg = outcome.message or f"{event.value} hook denied tool `{tool_name}`"
                messages.append(msg)
                return HookRunResult(denied=True, messages=messages)
            else:
                messages.append(outcome.message or "")

        return HookRunResult.allow(messages)

    @staticmethod
    def _run_single(
        command: str,
        event: HookEvent,
        tool_name: str,
        tool_input: str,
        tool_output: str | None,
        is_error: bool,
        payload: str,
    ) -> _HookOutcome:
        env = dict(os.environ)
        env["HOOK_EVENT"] = event.value
        env["HOOK_TOOL_NAME"] = tool_name
        env["HOOK_TOOL_INPUT"] = tool_input
        env["HOOK_TOOL_IS_ERROR"] = "1" if is_error else "0"
        if tool_output is not None:
            env["HOOK_TOOL_OUTPUT"] = tool_output

        shell_cmd, shell_flag = ("cmd", "/C") if platform.system() == "Windows" else ("sh", "-lc")

        try:
            result = subprocess.run(
                [shell_cmd, shell_flag, command],
                input=payload.encode(),
                capture_output=True,
                timeout=30,
            )
        except Exception as exc:
            return _HookOutcome(
                _OutcomeKind.WARN,
                f"{event.value} hook `{command}` failed to start for `{tool_name}`: {exc}",
            )

        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        message = stdout or None

        code = result.returncode
        if code == 0:
            return _HookOutcome(_OutcomeKind.ALLOW, message)
        if code == 2:
            return _HookOutcome(_OutcomeKind.DENY, message)

        warn_msg = f"Hook `{command}` exited with status {code}; allowing tool execution to continue"
        if stdout:
            warn_msg += f": {stdout}"
        elif stderr:
            warn_msg += f": {stderr}"
        return _HookOutcome(_OutcomeKind.WARN, warn_msg)


class _OutcomeKind(Enum):
    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"


@dataclass
class _HookOutcome:
    kind: _OutcomeKind
    message: str | None = None


def merge_hook_feedback(messages: list[str], output: str, denied: bool) -> str:
    if not messages:
        return output
    sections = []
    if output.strip():
        sections.append(output)
    label = "Hook feedback (denied)" if denied else "Hook feedback"
    sections.append(f"{label}:\n" + "\n".join(messages))
    return "\n\n".join(sections)
