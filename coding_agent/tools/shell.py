from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..security.safety import check_bash_safety
from ..security.sandbox import SandboxConfig, build_linux_sandbox_command, resolve_sandbox_status
from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry

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
    out_dir = state_dir(registry.workspace_root) / "bash_output"
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
                     "Execute a shell command in the workspace. "
                     "Set run_in_background=true for long-running processes (servers, watchers).",
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


def _run_foreground_with_auto_background(
    registry: ToolRegistry, command: str, cwd: Path, timeout: int, verdict: Any,
) -> str:
    """Run *command* in the foreground, but convert it to a tracked
    background task on timeout instead of killing it.

    stdout+stderr are merged into one file from the start (needed so a
    timed-out process's in-progress output is already being captured when
    it gets handed off to background tracking)."""
    output_path = _bash_output_path(registry)
    with open(output_path, "w", encoding="utf-8", errors="replace") as log_file:
        proc = subprocess.Popen(
            command, cwd=cwd, shell=True,
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
                bg_env = {**os.environ, **dict(sandbox_cmd.env)}
                proc = subprocess.Popen(
                    [sandbox_cmd.program, *sandbox_cmd.args],
                    cwd=cwd, env=bg_env,
                    stdout=log_file, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            else:
                proc = subprocess.Popen(
                    command, cwd=cwd, shell=True,
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
            timeout=timeout, check=False, env={**os.environ, **env},
        )
    else:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            shell=True, timeout=timeout, check=False,
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
