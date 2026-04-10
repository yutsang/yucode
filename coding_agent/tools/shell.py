from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING, Any

from ..security.safety import check_bash_safety
from ..security.sandbox import SandboxConfig, build_linux_sandbox_command, resolve_sandbox_status
from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry

# Max output size to prevent context blowout (parity with Rust bash.rs)
_MAX_OUTPUT_SIZE = 128 * 1024  # 128 KB per stream (stdout/stderr)


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
        if sandbox_cmd:
            bg_env = {**os.environ, **dict(sandbox_cmd.env)}
            proc = subprocess.Popen(
                [sandbox_cmd.program, *sandbox_cmd.args],
                cwd=cwd, env=bg_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            proc = subprocess.Popen(
                command, cwd=cwd, shell=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        bg_output: dict[str, Any] = {
            "pid": proc.pid,
            "background": True,
            "command": command,
        }
        if verdict.warning:
            bg_output["safety_warning"] = verdict.reason
        return json.dumps(bg_output, indent=2)

    if sandbox_cmd:
        env = dict(sandbox_cmd.env)
        result = subprocess.run(
            [sandbox_cmd.program, *sandbox_cmd.args],
            cwd=cwd, capture_output=True, text=True,
            timeout=timeout, check=False, env={**os.environ, **env},
        )
    else:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True,
            shell=True, timeout=timeout, check=False,
        )

    stdout = result.stdout
    stderr = result.stderr
    truncated = False
    if len(stdout) > _MAX_OUTPUT_SIZE:
        stdout = stdout[:_MAX_OUTPUT_SIZE] + f"\n\n[stdout truncated: {len(result.stdout):,} bytes total, showing first {_MAX_OUTPUT_SIZE:,}]"
        truncated = True
    if len(stderr) > _MAX_OUTPUT_SIZE:
        stderr = stderr[:_MAX_OUTPUT_SIZE] + f"\n\n[stderr truncated: {len(result.stderr):,} bytes total, showing first {_MAX_OUTPUT_SIZE:,}]"
        truncated = True

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
