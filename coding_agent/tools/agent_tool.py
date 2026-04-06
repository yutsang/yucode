from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry


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


def _resolve_allowed_tools(
    args: dict[str, Any],
    config_disabled: list[str],
) -> list[str] | None:
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
    allowed = _resolve_allowed_tools(args, list(registry.config.tools.disabled))

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
    sub_runtime = AgentRuntime(registry.workspace_root, sub_config, mcp_manager=registry.mcp_manager)
    summary = sub_runtime.run_turn(prompt, max_steps_override=max_steps)
    return summary.final_text
