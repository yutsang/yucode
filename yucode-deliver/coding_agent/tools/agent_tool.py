from __future__ import annotations

import threading
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
                    "or provide explicit allowed_tools."
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

    result_holder: list[Any] = []
    error_holder: list[Exception] = []

    def _run() -> None:
        try:
            summary = sub_runtime.run_turn(prompt, max_steps_override=max_steps)
            result_holder.append(summary.final_text)
        except Exception as exc:
            error_holder.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        return tool_error_response(
            f"Sub-agent timed out after {timeout}s",
            error_code="agent_timeout",
        )

    if error_holder:
        return tool_error_response(
            f"Sub-agent failed: {error_holder[0]}",
            error_code="agent_error",
        )

    return result_holder[0] if result_holder else tool_error_response(
        "Sub-agent returned no output",
        error_code="agent_empty",
    )
