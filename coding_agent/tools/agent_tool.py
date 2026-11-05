from __future__ import annotations

import json
import os
import threading
import uuid
from typing import TYPE_CHECKING, Any

from ..core.errors import tool_error_response
from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry

_DEFAULT_AGENT_TIMEOUT = 300  # 5 minutes

# Recursion guard. Without a `role`, the sub-agent inherits the full tool set
# -- including `agent` itself -- so nothing stopped a sub-agent from spawning
# another one, forever, a thread per level. Depth 1 (a sub-agent may not spawn
# further sub-agents) matches grok-build's hard limit and is what the
# coordinator path already effectively enforces, since ROLE_TOOLS never
# contains `agent`.
_DEFAULT_MAX_AGENT_DEPTH = 1
_MAX_AGENT_DEPTH_ENV = "YUCODE_MAX_AGENT_DEPTH"

# Depth of the runtime executing the *current* tool call. Thread-local rather
# than a plain global because each sub-agent runs in its own thread and its
# tools execute inline on that thread -- `_run` stamps the child's depth as
# its first act, so a nested `agent` call sees the right number.
_agent_depth = threading.local()


def _current_agent_depth() -> int:
    return getattr(_agent_depth, "value", 0)


def _max_agent_depth() -> int:
    raw = os.environ.get(_MAX_AGENT_DEPTH_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_AGENT_DEPTH
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_AGENT_DEPTH
    return value if value >= 0 else _DEFAULT_MAX_AGENT_DEPTH


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


def _annotate_stop_reason(summary: Any, max_steps: int) -> str:
    """Mark a sub-agent result that was cut off at its step limit.

    A truncated half-result is otherwise byte-for-byte indistinguishable from a
    finished one, so the parent builds on unfinished work as if it were fact.

    `ended_mid_tool_call` is the whole signal, and the only one: `run_turn`
    returns from inside its loop exactly when the model answered with no tool
    calls, so a last message still carrying tool calls means "cut off",
    whatever cut it off.

    Gating that on `iterations >= max_steps` as well excluded the cases this
    check exists to catch. `run_turn` sets `budget_exhausted = True` inside an
    iteration and only breaks at the top of the NEXT one, and its stuck-dedup
    force-exit leaves the loop early too -- both report an `iterations` well
    under `max_steps`, so both were handed back to the parent bare, as if
    finished. (`iterations >= max_steps` is not sufficient either: a sub-agent
    that answers on its very last allowed step reports exactly `max_steps`,
    which is why `ended_mid_tool_call` and not the step count is the test.)
    """
    from ..core.coordinator import ended_mid_tool_call

    final_text = summary.final_text
    if not ended_mid_tool_call(summary):
        return final_text
    return (
        f"[INCOMPLETE: the sub-agent was cut off mid-tool-call after "
        f"{summary.iterations} step(s) — its {max_steps}-step limit, the "
        "tool-call budget, or a stuck-loop exit. Everything below is partial "
        "work — verify it before building on it, and re-delegate the "
        "remainder if needed.]\n\n"
        + final_text
    )


def _agent(registry: ToolRegistry, args: dict[str, Any]) -> str:
    prompt = str(args["prompt"])
    allowed = _resolve_allowed_tools(args)
    run_in_background = bool(args.get("run_in_background", False))

    depth = _current_agent_depth()
    max_depth = _max_agent_depth()
    if depth >= max_depth:
        return tool_error_response(
            f"Sub-agent depth limit reached (current depth {depth}, max {max_depth}). "
            "Sub-agents at this depth may not spawn further sub-agents.",
            error_code="agent_depth_exceeded",
            recoverable=False,
            suggestion=(
                "Do this work directly with your own tools instead of delegating it, "
                f"or raise the limit via the {_MAX_AGENT_DEPTH_ENV} environment variable."
            ),
        )

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
        _agent_depth.value = depth + 1
        try:
            summary = sub_runtime.run_turn(prompt, max_steps_override=max_steps)
            result_holder.append(_annotate_stop_reason(summary, max_steps))
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
