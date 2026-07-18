from __future__ import annotations

import json
import threading
import uuid
from typing import TYPE_CHECKING, Any

from ..core.errors import tool_error_response
from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry

_DEFAULT_AGENT_TIMEOUT = 300  # 5 minutes


def agent_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            ToolSpec(
                "agent",
                (
                    "Launch a sub-agent with a scoped task. Optionally specify a "
                    "role (research/work/validate) for automatic tool scoping, "
                    "or provide explicit allowed_tools. By default this blocks "
                    "until the sub-agent finishes (or times out after 5 minutes). "
                    "Pass run_in_background=true for a long task to get a task_id "
                    "back immediately instead of blocking — its result is delivered "
                    "as a system-reminder on a later tool call, same as a "
                    "background bash command."
                ),
                {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "The task for the sub-agent."},
                        "role": {
                            "type": "string",
                            "enum": ["research", "work", "validate"],
                            "description": "Worker role — auto-selects appropriate tools.",
                        },
                        "allowed_tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Explicit tool whitelist (overrides role).",
                        },
                        "run_in_background": {
                            "type": "boolean",
                            "description": "Return immediately with a task_id instead of blocking (default false).",
                        },
                    },
                    "required": ["prompt"],
                },
                "workspace-write",
                RiskLevel.HIGH,
            ),
            lambda args: _agent(registry, args),
        ),
    ]


def _resolve_allowed_tools(args: dict[str, Any]) -> list[str] | None:
    """Determine the tool whitelist from role or explicit list."""
    explicit = args.get("allowed_tools")
    if explicit:
        return list(explicit)

    role_name = args.get("role")
    if role_name:
        from ..core.coordinator import ROLE_TOOLS, WorkerRole
        try:
            role = WorkerRole(role_name)
        except ValueError:
            return None
        return list(ROLE_TOOLS.get(role, []))

    return None


def _agent(registry: ToolRegistry, args: dict[str, Any]) -> str:
    prompt = str(args["prompt"])
    allowed = _resolve_allowed_tools(args)
    run_in_background = bool(args.get("run_in_background", False))

    from ..core.runtime import AgentRuntime
    sub_config = registry.config
    if allowed:
        from ..config import AppConfig, ToolOptions
        sub_config = AppConfig(
            provider=registry.config.provider,
            runtime=registry.config.runtime,
            tools=ToolOptions(allowed=list(allowed), disabled=list(registry.config.tools.disabled)),
            mcp=registry.config.mcp,
            vscode=registry.config.vscode,
            instruction_files=registry.config.instruction_files,
            hooks=registry.config.hooks,
            plugins=registry.config.plugins,
            sandbox=registry.config.sandbox,
        )

    max_steps = registry.config.runtime.max_worker_steps
    timeout = _DEFAULT_AGENT_TIMEOUT
    sub_runtime = AgentRuntime(registry.workspace_root, sub_config, mcp_manager=registry.mcp_manager)

    result_holder: list[str] = []
    error_holder: list[BaseException] = []

    def _run() -> None:
        try:
            summary = sub_runtime.run_turn(prompt, max_steps_override=max_steps)
            result_holder.append(summary.final_text)
        except Exception as exc:
            error_holder.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    task_id = f"agent_{uuid.uuid4().hex[:8]}"

    if run_in_background:
        registry.register_subagent_task(task_id, prompt, thread, result_holder, error_holder)
        return json.dumps({
            "status": "started_background",
            "task_id": task_id,
            "message": (
                f"Sub-agent '{task_id}' started in the background. Its result will be "
                "delivered as a system-reminder after a later tool call — continue with "
                "other work in the meantime instead of waiting."
            ),
        }, indent=2)

    thread.join(timeout=timeout)

    if thread.is_alive():
        # The thread keeps running regardless (Python threads can't be
        # force-killed) -- track it instead of abandoning whatever it
        # eventually produces. Mirrors auto_background_on_timeout for bash
        # (tools/shell.py), but unconditional: there's no "kill" alternative
        # for a thread the way there is for a subprocess.
        registry.register_subagent_task(task_id, prompt, thread, result_holder, error_holder)
        return json.dumps({
            "status": "started_background",
            "task_id": task_id,
            "message": (
                f"Sub-agent '{task_id}' exceeded {timeout}s and was moved to the "
                "background instead of being abandoned; it is still running. Its result "
                "will be delivered as a system-reminder after a later tool call."
            ),
        }, indent=2)

    if error_holder:
        return tool_error_response(
            f"Sub-agent failed: {error_holder[0]}",
            error_code="agent_error",
        )

    return result_holder[0] if result_holder else tool_error_response(
        "Sub-agent returned no output",
        error_code="agent_empty",
    )
