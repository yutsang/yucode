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
import shlex
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
    # cmd.exe builtins — without these, read-only mode over-blocks on Windows
    # (bash runs through cmd.exe there; see AGENT_UPGRADE_NOTES.md section 4).
    # PowerShell invocations deliberately stay non-read-only.
    "dir", "type", "findstr", "where", "tree", "ver", "tasklist", "systeminfo",
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
    """A single allow/deny/ask rule (e.g. ``bash(git:*)`` or ``write_file``).

    ``*`` is the only wildcard; every other character is matched literally.
    For shell tools the subject is matched against each chained segment as
    well as the whole command, so ``bash(rm -rf *)`` also stops
    ``cd /tmp && rm -rf /``.  Callers evaluating an *allow* list must pass
    ``match_segments=False`` -- see ``PermissionPolicy.authorize``.
    """
    tool_pattern: str
    subject_pattern: str = ""

    def matches(self, tool_name: str, tool_input: str, *, match_segments: bool = True) -> bool:
        if not re.match(_pattern_to_regex(self.tool_pattern), tool_name):
            return False
        if not self.subject_pattern:
            return True
        subject = _extract_permission_subject(tool_name, tool_input)
        pattern = _pattern_to_regex(self.subject_pattern)
        if not match_segments:
            return bool(re.match(pattern, subject.strip()))
        return any(re.match(pattern, c) for c in _subject_candidates(tool_name, subject))


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

        # Allow rules match the whole command only, never its segments.
        # Segment matching is what makes deny/ask rules un-bypassable; on an
        # allow list it does the reverse -- `bash(ls*)` would grant
        # `rm -rf / && ls` because one segment is `ls`.
        # Residual footgun this cannot fix: matching is a *prefix* match, so
        # `bash(ls*)` does still grant `ls && rm -rf /`. Keep allow rules
        # narrow; the backstop is the deny list, checked first and against
        # every segment.
        for rule in self.rules.allow:
            if rule.matches(tool_name, tool_input, match_segments=False):
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

        # Say how to lift the block, not just that it exists. The shipped
        # default (`workspace-write`) denies every `danger-full-access` tool --
        # `bash` among them -- and the bare "requires X but runtime is Y" gave
        # no hint that this is a one-line config change, so the denial read as
        # a defect rather than a setting.
        return PermissionDecision(
            False,
            f"Tool `{tool_name}` requires `{tool_permission}` but runtime is `{self.mode}`. "
            f"To allow it, set `runtime.permission_mode: {tool_permission}` in your config "
            f"(~/.yucode/settings.yml), or add a targeted rule under "
            f"`permission_rules.allow` such as `{tool_name}`.",
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
    # Case-insensitive: cmd.exe command names have no case convention (DIR,
    # Dir, and dir are all equally valid), unlike Unix commands.
    first_token = tokens[0].lower()
    for prefix_len in (1, 2):
        prefix = " ".join(tokens[:prefix_len]).lower()
        if prefix in _READ_ONLY_COMMANDS:
            return True
    return first_token in _READ_ONLY_COMMANDS


# Operators that chain a second command onto the first, plus the delimiters
# that *nest* one command inside another. Quoting is deliberately ignored:
# over-splitting can only produce extra candidates, which on a deny/ask list
# errs towards blocking.
#
# The nesting delimiters matter as much as the chaining ones. Every candidate
# is matched with a prefix `re.match`, so an inner command that sits in the
# middle of its segment is invisible to the rule: `echo $(rm -rf /)` ran a
# denied command while matching nothing, and so did `(rm -rf /)`,
# `{ rm -rf /; }` and `` echo `rm -rf /` ``.
_SHELL_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||&|\$\(|[`(){}]|\r\n|\n|\r")

# A backslash at end of line continues the same command. Splitting on the
# newline first would have turned `rm \<nl> -rf /` into the two fragments
# `rm \` and `-rf /`, neither of which a `rm -rf *` rule matches.
_LINE_CONTINUATION = re.compile(r"\\[ \t]*\r?\n")

_SHELL_TOOL_NAMES = frozenset({"bash", "shell", "run_command"})

# Wrappers that run another command as their argument. `sudo rm -rf /` and
# `timeout 5 rm -rf /` are the same command as `rm -rf /` as far as a rule is
# concerned, so the stripped form is offered as an extra candidate. The second
# group are shell keywords/builtins that only introduce the next command:
# `if true; then rm -rf /; fi` splits into a segment `then rm -rf /`.
_COMMAND_WRAPPERS = frozenset({
    "env", "timeout", "nice", "sudo", "doas", "nohup", "setsid", "xargs",
    "eval", "exec", "command", "builtin", "time", "then", "else", "do", "!",
})
_WRAPPER_ARG = re.compile(r"^-|^\d+(?:\.\d+)?[smhd]?$")

# Wrapper options that take a *separate* value. `_WRAPPER_ARG` above drops the
# option but not its argument, which then sits exactly where the command name
# should be: `sudo -u root rm -rf /` unwrapped to `root rm -rf /`, and neither
# that nor the raw segment prefix-matches a `rm -rf *` rule -- one flag walked
# past the deny list.
#
# Per-wrapper and deliberately short. Consuming one token too many is as bad as
# one too few: if `-n` (sudo's non-interactive flag, which takes no value) were
# listed here, `sudo -n rm -rf /` would unwrap to `-rf /` and match nothing.
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({
        "-u", "-g", "-p", "-C", "-h", "-r", "-t", "-T", "-U",
        "--user", "--group", "--prompt", "--host", "--role", "--type",
    }),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "xargs": frozenset({"-I", "-i", "-L", "-n", "-P", "-s", "-E", "-d", "-a"}),
}

# `sh -c "rm -rf /"` hides the command inside a quoted argument, where
# whitespace splitting leaves the quote glued to the command name and no rule
# ever prefix-matches it. The `-c` argument is extracted with `shlex` (see
# `_shell_c_command`) and fed back through the same candidate collection, so
# `sh -c 'cd /tmp && sudo rm -rf /'` is analysed like any other command line.
# `su` earns its place here for the same reason: its `-c` argument is a shell
# command line, whether or not a user name precedes the flag.
_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "su"})
# `-c`, and clustered short options ending in it (`bash -lc '...'`).
_SHELL_C_FLAG = re.compile(r"^-[A-Za-z]*c$")
# A nested `-c` string can itself invoke a shell. Bounded so a hand-crafted
# `sh -c "sh -c \"sh -c ...\""` cannot spin the matcher.
_MAX_SHELL_NESTING = 3


def _pattern_to_regex(pattern: str) -> str:
    """Compile a rule pattern where ``*`` is the only wildcard.

    Everything else is escaped: without this a rule such as
    ``bash(rm -rf /home/.*)`` is read as a regex, and `.`, `(`, `[` in a rule
    silently match commands the author never wrote a rule for.
    """
    return ".*".join(re.escape(part) for part in pattern.split("*"))


def _shell_segments(command: str) -> list[str]:
    """Split a shell command on chaining/nesting delimiters, trimming each."""
    joined = _LINE_CONTINUATION.sub(" ", command)
    return [seg.strip() for seg in _SHELL_SEGMENT_SPLIT.split(joined) if seg.strip()]


def _is_env_assignment(token: str) -> bool:
    name, sep, _ = token.partition("=")
    return bool(sep and name and all(c.isalnum() or c == "_" for c in name))


def _strip_command_wrappers(segment: str) -> str:
    """Drop leading ``KEY=val`` assignments and wrapper commands.

    ``FOO=1 sudo timeout 5 rm -rf /`` -> ``rm -rf /``, and
    ``sudo -u root rm -rf /`` -> ``rm -rf /``.
    """
    tokens = segment.split()
    wrapper = ""
    while tokens:
        if _is_env_assignment(tokens[0]):
            tokens = tokens[1:]
            continue
        # Basename, so `/usr/bin/sudo rm -rf /` unwraps like `sudo rm -rf /`.
        base = tokens[0].rsplit("/", 1)[-1]
        if base in _COMMAND_WRAPPERS:
            wrapper = base
            tokens = tokens[1:]
            continue
        # Only consume options/durations once a wrapper introduced them, so a
        # command that legitimately starts with a flag is left alone.
        if wrapper and tokens[0] in _WRAPPER_VALUE_FLAGS.get(wrapper, frozenset()):
            # Take the option's value with it -- see _WRAPPER_VALUE_FLAGS.
            tokens = tokens[2:]
            continue
        if wrapper and _WRAPPER_ARG.match(tokens[0]):
            tokens = tokens[1:]
            continue
        break
    return " ".join(tokens)


def _shell_c_command(segment: str) -> str | None:
    """Return the command string that ``sh -c "..."`` would run, else None.

    Quote-aware on purpose: the command lives inside a quoted argument, so the
    whitespace splitting used everywhere else leaves the quote glued to the
    command name and no rule prefix-matches it.  ``shlex`` is the stdlib POSIX
    tokenizer; on an unbalanced quote it raises and we decline rather than
    guess -- the raw segment is still offered as a candidate on its own.
    """
    if "-" not in segment:
        return None
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None
    index = 0
    while index < len(tokens) and _is_env_assignment(tokens[index]):
        index += 1
    if index >= len(tokens):
        return None
    if tokens[index].rsplit("/", 1)[-1] not in _SHELL_INTERPRETERS:
        return None
    for position in range(index + 1, len(tokens) - 1):
        if _SHELL_C_FLAG.match(tokens[position]):
            return tokens[position + 1]
    return None


def _subject_candidates(tool_name: str, subject: str) -> list[str]:
    """Every form of ``subject`` a deny/ask rule should be tested against.

    The whole command first, then -- for shell tools -- each chained segment,
    its unwrapped form, and anything it hands to a nested shell via ``-c``.
    A prefix match against the raw string alone is trivially bypassed by
    prepending anything at all.
    """
    candidates = [subject.strip()]
    if tool_name.lower() not in _SHELL_TOOL_NAMES:
        return candidates
    _collect_shell_candidates(subject, candidates, depth=0)
    return candidates


def _collect_shell_candidates(command: str, candidates: list[str], depth: int) -> None:
    for segment in _shell_segments(command):
        candidates.append(segment)
        unwrapped = _strip_command_wrappers(segment)
        if unwrapped and unwrapped != segment:
            candidates.append(unwrapped)
        if depth >= _MAX_SHELL_NESTING:
            continue
        inner = _shell_c_command(unwrapped or segment)
        if inner and inner.strip():
            _collect_shell_candidates(inner, candidates, depth + 1)


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
