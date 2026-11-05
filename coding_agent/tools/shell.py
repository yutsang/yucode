from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.shellexec import shell_invocation
from ..security.env_scrub import (
    PRESERVED_ENV_PATTERNS,
    SECRET_ENV_PATTERNS,
    env_name_matches,
    scrub_secrets,
)
from ..security.safety import check_bash_safety
from ..security.sandbox import SandboxConfig, build_linux_sandbox_command, resolve_sandbox_status
from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from ..config.settings import RuntimeOptions
    from . import ToolRegistry

_log = logging.getLogger("yucode.tools.shell")

# Max output size to prevent context blowout (parity with Rust bash.rs)
_MAX_OUTPUT_SIZE = 128 * 1024  # 128 KB per stream (stdout/stderr)
# When a stream exceeds _MAX_OUTPUT_SIZE, keep head+tail (instead of head-only)
# so a test-suite's failure summary — usually at the end — isn't discarded.
# Sized so a single large stream's truncated preview stays comfortably under
# the runtime's outer per-tool-result cap (_MAX_TOOL_RESULT_BYTES, 32 KB) —
# the disk-backed file is the real recovery path for anything beyond this.
_TRUNCATE_HEAD_SIZE = 16 * 1024
_TRUNCATE_TAIL_SIZE = 8 * 1024


def _bash_output_path(registry: ToolRegistry) -> Path:
    """Return a fresh path for a full bash-output log under workspace state."""
    from ..config.settings import state_dir
    from ..observability.metrics import prune_state_subdir

    root = state_dir(registry.workspace_root)
    # Every background command and every over-cap result leaves a log here and
    # nothing ever removed one. The registry is the only reliable in-use
    # signal: a background task can run for hours between writes, so an mtime
    # old enough to look disposable says nothing about whether its process
    # still holds the file open -- and the runtime reads that log back when the
    # task finishes. Completed-but-unreported tasks stay in the map too, so
    # protecting every entry (not just the live ones) is the right set.
    prune_state_subdir(
        root, "bash_output",
        protected=[task.output_path for task in registry.background_tasks.values()],
    )
    out_dir = root / "bash_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{uuid.uuid4().hex[:12]}.log"


def _truncate_stream(label: str, text: str, output_path: Path) -> tuple[str, bool]:
    """Truncate *text* to head+tail with a pointer to the full-output file.

    Unlike head-only truncation, this keeps the tail — where a test runner's
    failure summary or a long build's final error usually lands — instead of
    only the command's initial output."""
    if len(text) <= _MAX_OUTPUT_SIZE:
        return text, False
    head = text[:_TRUNCATE_HEAD_SIZE]
    tail = text[-_TRUNCATE_TAIL_SIZE:]
    combined = (
        f"{head}\n\n[{label} truncated: {len(text):,} bytes total, showing first "
        f"{_TRUNCATE_HEAD_SIZE:,} and last {_TRUNCATE_TAIL_SIZE:,} bytes. "
        f"Full output at: {output_path}. Use read_file with offset/limit to "
        f"inspect a specific section.]\n\n{tail}"
    )
    return combined, True


def shell_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            ToolSpec("bash",
                     # States the contract, not just the capability. Everything
                     # below is behaviour the harness actually imposes, and
                     # without it the model has to discover each one by hitting
                     # it -- most expensively the refusals, which it would
                     # otherwise retry in a slightly different form.
                     "Execute a shell command in the workspace.\n"
                     "- Each call gets a fresh shell: no cd, no exported variable, and no "
                     "activated virtualenv survives to the next call. Use working_directory "
                     "instead of `cd`, and combine dependent steps in one command.\n"
                     "- On Windows commands run through cmd.exe, not bash. Prefer portable "
                     "invocations (`python -m ...`) over shell-specific syntax.\n"
                     "- A refusal from the safety patterns is policy, not a transient error. "
                     "Do not retry the same intent with different phrasing; either work "
                     "another way or tell the user why you stopped.\n"
                     "- Output beyond the cap is truncated head+tail, and the full text is "
                     "written to a file whose path is in the result. Read that file rather "
                     "than re-running the command.\n"
                     "- Set run_in_background=true for long-running processes (servers, "
                     "watchers); a foreground command that exceeds its timeout is moved to "
                     "the background rather than killed, and reported back when it finishes.",
                     {"type": "object", "properties": {
                         "command": {"type": "string", "description": "Shell command to execute."},
                         "timeout": {"type": "integer", "description": "Timeout in seconds (default from config)."},
                         "working_directory": {"type": "string", "description": "Working directory (default: workspace root)."},
                         "run_in_background": {"type": "boolean", "description": "Run in background, return immediately with PID."},
                         "description": {"type": "string", "description": "Brief description of what this command does."},
                     }, "required": ["command"]},
                     "danger-full-access", RiskLevel.HIGH),
            lambda args: _bash(registry, args),
        ),
    ]


# The pattern lists live in security/env_scrub.py -- the bash tool is only one
# of four places that spawn children (hooks, plugin tools, and stdio MCP
# servers are the others), and they all filter against the same lists.
_SECRET_ENV_PATTERNS = SECRET_ENV_PATTERNS
_PRESERVED_ENV_PATTERNS = PRESERVED_ENV_PATTERNS


def _env_name_matches(name: str, patterns: Iterable[str]) -> bool:
    """Back-compat alias for `security.env_scrub.env_name_matches`."""
    return env_name_matches(name, patterns)


def _scrub_secrets(env: dict[str, str], runtime: RuntimeOptions) -> dict[str, str]:
    """Drop credential-looking variables from a bash-tool child environment."""
    return scrub_secrets(
        env,
        enabled=runtime.shell_env_filter_secrets,
        extra_patterns=runtime.shell_env_exclude_patterns,
        allowlist=runtime.shell_env_allowlist,
        context="bash",
    )


def _child_env(runtime: RuntimeOptions) -> dict[str, str]:
    """Environment for bash-tool child processes: force UTF-8 I/O in any
    Python child. A child Python writing to a PIPE defaults to the locale
    code page (cp1252 on a Western-locale Windows box, even when the
    interactive console itself is UTF-8), so print()ing CJK crashes it with
    UnicodeEncodeError before a single byte reaches the parent — observed
    on a real Windows 11 box via diagnose_env.py (rc=1, empty stdout). On a
    python-only locked-down machine, python IS the common child; non-python
    children simply ignore these variables.

    The inherited environment is scrubbed of credential-looking names first
    (see _scrub_secrets) — passing **os.environ straight through meant any
    command the model could be talked into running could read the provider
    API key."""
    return {
        **_scrub_secrets(dict(os.environ), runtime),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }


def _run_foreground_with_auto_background(
    registry: ToolRegistry, command: str, cwd: Path, timeout: int, verdict: Any,
) -> str:
    """Run *command* in the foreground, but convert it to a tracked
    background task on timeout instead of killing it.

    stdout+stderr are merged into one file from the start (needed so a
    timed-out process's in-progress output is already being captured when
    it gets handed off to background tracking)."""
    output_path = _bash_output_path(registry)
    popen_args, use_shell = shell_invocation(command)
    with open(output_path, "w", encoding="utf-8", errors="replace") as log_file:
        proc = subprocess.Popen(
            popen_args, cwd=cwd, shell=use_shell, env=_child_env(registry.config.runtime),
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    # log_file is now closed; the child holds its own fd to the same file
    # (see the background branch in _bash for the same pattern).

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        task_id = str(proc.pid)
        registry.register_background_task(task_id, command, proc, output_path)
        bg_output: dict[str, Any] = {
            "pid": proc.pid,
            "task_id": task_id,
            "background": True,
            "command": command,
            "output_file": str(output_path),
            "auto_backgrounded": True,
            "note": (
                f"Command exceeded its {timeout}s timeout and was moved to "
                "background instead of being killed; it is still running."
            ),
        }
        if verdict.warning:
            bg_output["safety_warning"] = verdict.reason
        return json.dumps(bg_output, indent=2)

    text = output_path.read_text(encoding="utf-8", errors="replace")
    stdout, truncated = (text, False)
    if len(text) > _MAX_OUTPUT_SIZE:
        stdout, truncated = _truncate_stream("stdout", text, output_path)
    output: dict[str, Any] = {
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": "(merged into stdout for this command — auto_background_on_timeout is enabled)",
    }
    if truncated:
        output["truncated"] = True
    if verdict.warning:
        output["safety_warning"] = verdict.reason
    return json.dumps(output, indent=2)


def _bash(registry: ToolRegistry, args: dict[str, Any]) -> str:
    command = str(args["command"])
    timeout = int(args.get("timeout", registry.config.runtime.shell_timeout_seconds))
    cwd = registry._resolve_path(str(args.get("working_directory", registry.workspace_root)))
    background = bool(args.get("run_in_background", False))

    verdict = check_bash_safety(command)
    if verdict.blocked:
        return json.dumps({
            "error": f"Command blocked by safety check: {verdict.reason}",
            "safety_level": verdict.level,
        }, indent=2)

    sandbox_dict = {
        "enabled": registry.config.sandbox.enabled,
        "namespace_restrictions": registry.config.sandbox.namespace_restrictions,
        "network_isolation": registry.config.sandbox.network_isolation,
        "filesystem_mode": registry.config.sandbox.filesystem_mode,
        "allowed_mounts": list(registry.config.sandbox.allowed_mounts),
    }
    sandbox_cfg = SandboxConfig.from_dict(sandbox_dict)
    sandbox_status = resolve_sandbox_status(sandbox_cfg, cwd)
    sandbox_cmd = build_linux_sandbox_command(command, cwd, sandbox_status)

    if background:
        output_path = _bash_output_path(registry)
        # Output streams to disk (not DEVNULL) so a later tool call can see
        # what the task produced — see runtime.py::_check_completed_background_tasks,
        # which polls registry.background_tasks and surfaces a system-reminder
        # once the process exits.
        with open(output_path, "w", encoding="utf-8", errors="replace") as log_file:
            if sandbox_cmd:
                bg_env = {**_child_env(registry.config.runtime), **dict(sandbox_cmd.env)}
                proc = subprocess.Popen(
                    [sandbox_cmd.program, *sandbox_cmd.args],
                    cwd=cwd, env=bg_env,
                    stdout=log_file, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            else:
                popen_args, use_shell = shell_invocation(command)
                proc = subprocess.Popen(
                    popen_args, cwd=cwd, shell=use_shell, env=_child_env(registry.config.runtime),
                    stdout=log_file, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        # log_file is now closed; the child holds its own fd to the same file
        # (it doesn't need the parent to keep it open for a detached process).
        task_id = str(proc.pid)
        registry.register_background_task(task_id, command, proc, output_path)
        bg_output: dict[str, Any] = {
            "pid": proc.pid,
            "task_id": task_id,
            "background": True,
            "command": command,
            "output_file": str(output_path),
        }
        if verdict.warning:
            bg_output["safety_warning"] = verdict.reason
        return json.dumps(bg_output, indent=2)

    if registry.config.runtime.auto_background_on_timeout and not sandbox_cmd:
        # Popen + a non-killing wait, instead of subprocess.run(timeout=...)
        # (which always kills on timeout) — lets a command that's still
        # useful past its deadline keep running instead of losing its work.
        # Scoped to the non-sandboxed path: sandbox_cmd wraps the command in
        # a bwrap/etc invocation, and supporting the same non-killing wait
        # there is out of scope for this opt-in feature's first cut.
        return _run_foreground_with_auto_background(registry, command, cwd, timeout, verdict)

    # encoding="utf-8" (not the text=True default, which falls back to the OS
    # locale's preferred encoding — cp1252 on a Western-locale Windows box)
    # is required so output containing non-ASCII text (e.g. Chinese account
    # names/remarks in a databook) decodes instead of crashing the reader
    # thread with a UnicodeDecodeError. errors="replace" is a safety net for
    # any tool that genuinely emits a different encoding, rather than a hard
    # crash on the rare byte sequence UTF-8 can't decode.
    if sandbox_cmd:
        env = dict(sandbox_cmd.env)
        result = subprocess.run(
            [sandbox_cmd.program, *sandbox_cmd.args],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, check=False, env={**_child_env(registry.config.runtime), **env},
        )
    else:
        popen_args, use_shell = shell_invocation(command)
        result = subprocess.run(
            popen_args, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            shell=use_shell, timeout=timeout, check=False,
            env=_child_env(registry.config.runtime),
        )

    stdout = result.stdout
    stderr = result.stderr
    truncated = False
    if len(stdout) > _MAX_OUTPUT_SIZE or len(stderr) > _MAX_OUTPUT_SIZE:
        output_path = _bash_output_path(registry)
        output_path.write_text(
            f"=== STDOUT ===\n{result.stdout}\n\n=== STDERR ===\n{result.stderr}\n",
            encoding="utf-8", errors="replace",
        )
        stdout, stdout_truncated = _truncate_stream("stdout", stdout, output_path)
        stderr, stderr_truncated = _truncate_stream("stderr", stderr, output_path)
        truncated = stdout_truncated or stderr_truncated

    output: dict[str, Any] = {
        "returncode": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    if truncated:
        output["truncated"] = True
    if verdict.warning:
        output["safety_warning"] = verdict.reason

    return json.dumps(output, indent=2)
