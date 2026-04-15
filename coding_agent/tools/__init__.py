"""Tool layer -- registry, specs, and built-in tool implementations."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..plugins.mcp import McpManager
from ..security.permissions import PermissionMode

_log = logging.getLogger("yucode.tools")

ToolHandler = Callable[[dict[str, Any]], str]


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    permission: PermissionMode
    risk_level: RiskLevel = RiskLevel.LOW


@dataclass(frozen=True)
class ToolDefinition:
    spec: ToolSpec
    handler: ToolHandler


_TOOL_ALIASES: dict[str, str] = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "glob": "glob_search",
    "grep": "grep_search",
    "ls": "list_directory",
}

_STUB_PARAMS: dict[str, Any] = {"type": "object", "properties": {}}


def _stub_handler(name: str) -> ToolHandler:
    def handler(args: dict[str, Any]) -> str:
        return json.dumps({"status": "pending", "tool": name, "message": f"Tool `{name}` acknowledged"})
    return handler


def _make_stub_tool(name: str, description: str, permission: PermissionMode, risk: RiskLevel = RiskLevel.MEDIUM) -> ToolDefinition:
    return ToolDefinition(
        spec=ToolSpec(name=name, description=description, parameters=_STUB_PARAMS, permission=permission, risk_level=risk),
        handler=_stub_handler(name),
    )


def _registry_handler(
    action: Callable[[dict[str, Any]], Any],
    *,
    catch: tuple[type[Exception], ...] = (KeyError,),
) -> ToolHandler:
    """Create a tool handler that calls *action*, serialises the result, and
    catches expected exceptions as ``{"error": ...}`` responses."""
    def handler(args: dict[str, Any]) -> str:
        try:
            result = action(args)
            if result is None:
                return json.dumps({"error": "Not found"})
            if isinstance(result, str):
                return result
            if isinstance(result, list):
                return json.dumps([r.to_dict() if hasattr(r, "to_dict") else r for r in result], indent=2)
            if isinstance(result, dict):
                return json.dumps(result, indent=2)
            return json.dumps(result.to_dict(), indent=2)
        except catch as exc:
            return json.dumps({"error": str(exc)})
    return handler


def _run_task_create(args: dict[str, Any]) -> str:
    from ..core.task_registry import global_task_registry
    return json.dumps(global_task_registry().create(str(args.get("prompt", "")), str(args.get("description", ""))).to_dict(), indent=2)


def _run_task_packet(args: dict[str, Any]) -> str:
    from ..core.task_registry import global_task_registry
    try:
        task = global_task_registry().create_from_packet(args)
        return json.dumps(task.to_dict(), indent=2)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


def _run_task_get(args: dict[str, Any]) -> str:
    from ..core.task_registry import global_task_registry
    task = global_task_registry().get(str(args.get("task_id", "")))
    if task is None:
        return json.dumps({"error": "Task not found"})
    return json.dumps(task.to_dict(), indent=2)


def _run_task_list(args: dict[str, Any]) -> str:
    from ..core.task_registry import TaskStatus, global_task_registry
    status_str = args.get("status")
    status_filter = TaskStatus(status_str) if status_str and status_str in TaskStatus.__members__.values() else None
    tasks = global_task_registry().list(status_filter)
    return json.dumps([t.to_dict() for t in tasks], indent=2)


def _make_task_handler(method_name: str) -> ToolHandler:
    """Create a handler that calls ``global_task_registry().<method_name>(task_id, ...)``."""
    def handler(args: dict[str, Any]) -> str:
        from ..core.task_registry import global_task_registry
        try:
            method = getattr(global_task_registry(), method_name)
            if method_name == "update":
                result = method(str(args.get("task_id", "")), str(args.get("message", "")))
            elif method_name == "output":
                output = method(str(args.get("task_id", "")))
                return json.dumps({"task_id": args.get("task_id"), "output": output})
            else:
                result = method(str(args.get("task_id", "")))
            return json.dumps(result.to_dict(), indent=2)
        except (KeyError, ValueError) as exc:
            return json.dumps({"error": str(exc)})
    return handler

_run_task_stop = _make_task_handler("stop")
_run_task_update = _make_task_handler("update")
_run_task_output = _make_task_handler("output")


def _make_worker_handler(method_name: str, extra_args: list[str] | None = None) -> ToolHandler:
    """Create a handler that calls ``global_worker_registry().<method_name>(worker_id, ...)``."""
    def handler(args: dict[str, Any]) -> str:
        from ..core.worker_registry import global_worker_registry
        try:
            method = getattr(global_worker_registry(), method_name)
            call_args: list[Any] = [str(args.get("worker_id", ""))]
            for key in (extra_args or []):
                call_args.append(args.get(key))
            result = method(*call_args)
            if hasattr(result, "to_dict"):
                return json.dumps(result.to_dict(), indent=2)
            return json.dumps(result, indent=2)
        except (KeyError, ValueError) as exc:
            return json.dumps({"error": str(exc)})
    return handler

_run_worker_observe = _make_worker_handler("observe", ["screen_text"])
_run_worker_resolve_trust = _make_worker_handler("resolve_trust")
_run_worker_send_prompt = _make_worker_handler("send_prompt", ["prompt"])
_run_worker_restart = _make_worker_handler("restart")
_run_worker_terminate = _make_worker_handler("terminate")


def _run_worker_create(args: dict[str, Any]) -> str:
    from ..core.worker_registry import global_worker_registry
    worker = global_worker_registry().create(
        cwd=str(args.get("cwd", ".")),
        trusted_roots=args.get("trusted_roots"),
        auto_recover_prompt_misdelivery=bool(args.get("auto_recover", False)),
    )
    return json.dumps(worker.to_dict(), indent=2)


def _run_worker_get(args: dict[str, Any]) -> str:
    from ..core.worker_registry import global_worker_registry
    worker = global_worker_registry().get(str(args.get("worker_id", "")))
    if worker is None:
        return json.dumps({"error": "Worker not found"})
    return json.dumps(worker.to_dict(), indent=2)


def _run_worker_await_ready(args: dict[str, Any]) -> str:
    from ..core.worker_registry import global_worker_registry
    try:
        snap = global_worker_registry().await_ready(str(args.get("worker_id", "")))
        return json.dumps({"worker_id": snap.worker_id, "status": snap.status.value, "ready": snap.ready, "blocked": snap.blocked, "replay_prompt_ready": snap.replay_prompt_ready, "last_error": snap.last_error})
    except KeyError as exc:
        return json.dumps({"error": str(exc)})


def _run_team_create(args: dict[str, Any]) -> str:
    from ..core.task_registry import global_task_registry
    from ..core.team_cron_registry import global_team_registry
    task_ids = args.get("task_ids", [])
    team = global_team_registry().create(str(args.get("name", "")), task_ids)
    task_reg = global_task_registry()
    for tid in task_ids:
        with suppress(KeyError):
            task_reg.assign_team(tid, team.team_id)
    return json.dumps(team.to_dict(), indent=2)


def _run_team_delete(args: dict[str, Any]) -> str:
    from ..core.team_cron_registry import global_team_registry
    try:
        return json.dumps(global_team_registry().delete(str(args.get("team_id", ""))).to_dict(), indent=2)
    except KeyError as exc:
        return json.dumps({"error": str(exc)})


def _run_cron_create(args: dict[str, Any]) -> str:
    from ..core.team_cron_registry import global_cron_registry
    return json.dumps(global_cron_registry().create(str(args.get("schedule", "")), str(args.get("prompt", "")), str(args.get("description", ""))).to_dict(), indent=2)


def _run_cron_delete(args: dict[str, Any]) -> str:
    from ..core.team_cron_registry import global_cron_registry
    try:
        return json.dumps(global_cron_registry().delete(str(args.get("cron_id", ""))).to_dict(), indent=2)
    except KeyError as exc:
        return json.dumps({"error": str(exc)})


def _run_cron_list(args: dict[str, Any]) -> str:
    from ..core.team_cron_registry import global_cron_registry
    return json.dumps([e.to_dict() for e in global_cron_registry().list(enabled_only=bool(args.get("enabled_only", False)))], indent=2)


def _run_lsp(args: dict[str, Any]) -> str:
    from ..core.lsp_registry import global_lsp_registry
    return json.dumps(global_lsp_registry().dispatch(
        action=str(args.get("action", "")),
        path=str(args.get("path", "")),
        line=int(args.get("line", 0)),
        character=int(args.get("character", 0)),
        query=str(args.get("query", "")),
    ), indent=2)


def _run_ask_user(args: dict[str, Any]) -> str:
    question = str(args.get("question", args.get("text", "")))
    options = args.get("options", [])
    if not question:
        return json.dumps({"error": "No question provided"})
    try:
        print(f"\n  Agent question: {question}")
        if options:
            for i, opt in enumerate(options):
                label = opt if isinstance(opt, str) else opt.get("label", str(opt))
                print(f"    {i + 1}. {label}")
        answer = input("  Your answer: ").strip()
        return json.dumps({"answer": answer, "question": question})
    except (EOFError, KeyboardInterrupt):
        return json.dumps({"answer": "", "question": question, "cancelled": True})


def _run_remote_trigger(args: dict[str, Any]) -> str:
    import urllib.error
    import urllib.request
    url = str(args.get("url", ""))
    method = str(args.get("method", "POST")).upper()
    body = args.get("body")
    headers = args.get("headers", {})
    if not url:
        return json.dumps({"error": "No URL provided"})
    try:
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(str(k), str(v))
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            return json.dumps({"status": resp.status, "body": resp_body[:10000]})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _make_registry_tool(name: str, description: str, permission: PermissionMode, handler: ToolHandler, risk: RiskLevel = RiskLevel.MEDIUM) -> ToolDefinition:
    return ToolDefinition(
        spec=ToolSpec(name=name, description=description, parameters=_STUB_PARAMS, permission=permission, risk_level=risk),
        handler=handler,
    )


def _extended_tool_specs() -> list[ToolDefinition]:
    """Tool specs ported from claw-code-main with real registry-backed handlers."""
    return [
        _make_stub_tool("SendUserMessage", "Send a message to the user", "read-only", RiskLevel.LOW),
        _make_stub_tool("Config", "Read or update agent configuration", "workspace-write"),
        _make_stub_tool("EnterPlanMode", "Switch to planning mode", "workspace-write"),
        _make_stub_tool("ExitPlanMode", "Exit planning mode", "workspace-write"),
        _make_stub_tool("StructuredOutput", "Return a structured JSON result", "read-only", RiskLevel.LOW),
        _make_stub_tool("REPL", "Start an interactive REPL session", "danger-full-access", RiskLevel.HIGH),
        _make_stub_tool("PowerShell", "Execute a PowerShell command", "danger-full-access", RiskLevel.HIGH),
        _make_registry_tool("AskUserQuestion", "Ask the user a question", "read-only", _run_ask_user, RiskLevel.LOW),
        _make_registry_tool("TaskCreate", "Create a new background task", "danger-full-access", _run_task_create),
        _make_registry_tool("RunTaskPacket", "Run a structured task packet", "danger-full-access", _run_task_packet),
        _make_registry_tool("TaskGet", "Get task status", "read-only", _run_task_get, RiskLevel.LOW),
        _make_registry_tool("TaskList", "List all tasks", "read-only", _run_task_list, RiskLevel.LOW),
        _make_registry_tool("TaskStop", "Stop a running task", "danger-full-access", _run_task_stop),
        _make_registry_tool("TaskUpdate", "Update a task", "danger-full-access", _run_task_update),
        _make_registry_tool("TaskOutput", "Get task output", "read-only", _run_task_output, RiskLevel.LOW),
        _make_registry_tool("WorkerCreate", "Create a worker", "danger-full-access", _run_worker_create),
        _make_registry_tool("WorkerGet", "Get worker status", "read-only", _run_worker_get, RiskLevel.LOW),
        _make_registry_tool("WorkerObserve", "Observe worker activity", "read-only", _run_worker_observe, RiskLevel.LOW),
        _make_registry_tool("WorkerResolveTrust", "Resolve worker trust prompt", "danger-full-access", _run_worker_resolve_trust),
        _make_registry_tool("WorkerAwaitReady", "Wait for worker to be ready", "read-only", _run_worker_await_ready, RiskLevel.LOW),
        _make_registry_tool("WorkerSendPrompt", "Send a prompt to a worker", "danger-full-access", _run_worker_send_prompt),
        _make_registry_tool("WorkerRestart", "Restart a worker", "danger-full-access", _run_worker_restart),
        _make_registry_tool("WorkerTerminate", "Terminate a worker", "danger-full-access", _run_worker_terminate),
        _make_registry_tool("TeamCreate", "Create a team of workers", "danger-full-access", _run_team_create),
        _make_registry_tool("TeamDelete", "Delete a team", "danger-full-access", _run_team_delete),
        _make_registry_tool("CronCreate", "Create a cron job", "danger-full-access", _run_cron_create),
        _make_registry_tool("CronDelete", "Delete a cron job", "danger-full-access", _run_cron_delete),
        _make_registry_tool("CronList", "List cron jobs", "read-only", _run_cron_list, RiskLevel.LOW),
        _make_registry_tool("LSP", "Invoke LSP actions (diagnostics, hover, definition, etc.)", "read-only", _run_lsp),
        _make_stub_tool("ListMcpResources", "List resources from an MCP server", "read-only", RiskLevel.LOW),
        _make_stub_tool("ReadMcpResource", "Read a resource from an MCP server", "read-only", RiskLevel.LOW),
        _make_stub_tool("McpAuth", "Authenticate with an MCP server", "danger-full-access"),
        _make_registry_tool("RemoteTrigger", "Trigger a remote action", "danger-full-access", _run_remote_trigger),
        _make_stub_tool("MCP", "Call an MCP tool", "danger-full-access"),
    ]


class ToolRegistry:
    def __init__(
        self,
        workspace_root: Path,
        config: AppConfig,
        mcp_manager: McpManager | None = None,
        plugin_tools: list[Any] | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.config = config
        self.mcp_manager = mcp_manager
        self._builtin_names: set[str] = set()
        self._tools: dict[str, ToolDefinition] = {}

        for tool in self._builtin_tools():
            self._tools[tool.spec.name] = tool
            self._builtin_names.add(tool.spec.name)

        for tool in _extended_tool_specs():
            if tool.spec.name not in self._tools:
                self._tools[tool.spec.name] = tool
                self._builtin_names.add(tool.spec.name)

        if self.mcp_manager:
            self._wire_mcp_lifecycle_tools()
        if self.mcp_manager:
            for spec in self.mcp_manager.tool_specs():
                function = spec["function"]
                name = function["name"]
                if name in self._builtin_names:
                    _log.warning("MCP tool `%s` collides with built-in; skipping", name)
                    continue
                self._tools[name] = ToolDefinition(
                    spec=ToolSpec(
                        name=name,
                        description=function["description"],
                        parameters=function["parameters"],
                        permission="workspace-write",
                        risk_level=RiskLevel.MEDIUM,
                    ),
                    handler=lambda args, tool_name=name: self._run_mcp_tool(tool_name, args),
                )

        plugin_names: set[str] = set()
        if plugin_tools:
            for pt in plugin_tools:
                if pt.name in self._builtin_names:
                    _log.warning("Plugin tool `%s` collides with built-in; skipping", pt.name)
                    continue
                if pt.name in plugin_names:
                    _log.warning("Duplicate plugin tool `%s`; skipping", pt.name)
                    continue
                plugin_names.add(pt.name)
                self._tools[pt.name] = ToolDefinition(
                    spec=ToolSpec(
                        name=pt.name,
                        description=pt.description,
                        parameters=pt.input_schema,
                        permission=pt.permission,
                        risk_level=RiskLevel.MEDIUM,
                    ),
                    handler=lambda args, tool=pt: tool.execute(args),
                )
        self._apply_filters()

    def definitions_for_provider(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "parameters": tool.spec.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def permission_for(self, tool_name: str) -> PermissionMode:
        return self._tools[tool_name].spec.permission

    def risk_level_for(self, tool_name: str) -> RiskLevel:
        return self._tools[tool_name].spec.risk_level

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name not in self._tools:
            raise KeyError(f"Unknown tool: {tool_name}")
        return self._tools[tool_name].handler(arguments)

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    def all_tool_names(self) -> set[str]:
        """All registered tool names including MCP and plugins."""
        return set(self._tools.keys())

    def permission_specs(self) -> list[tuple[str, PermissionMode]]:
        """Return (name, permission) pairs for all tools."""
        return [(name, tool.spec.permission) for name, tool in self._tools.items()]

    def search_tools(self, query: str) -> list[ToolSpec]:
        """Search tools by name or description keyword."""
        q = query.lower()
        return [
            tool.spec for tool in self._tools.values()
            if q in tool.spec.name.lower() or q in tool.spec.description.lower()
        ]

    def _apply_filters(self) -> None:
        if self.config.tools.allowed:
            allowed = normalize_allowed_tools(
                self.config.tools.allowed,
                set(self._tools.keys()),
            )
            if allowed is not None:
                self._tools = {name: tool for name, tool in self._tools.items() if name in allowed}
        if self.config.tools.disabled:
            for name in self.config.tools.disabled:
                self._tools.pop(name, None)

    def _builtin_tools(self) -> list[ToolDefinition]:
        from .agent_tool import agent_tools
        from .filesystem import filesystem_tools
        from .misc import misc_tools
        from .notebook import notebook_tools
        from .office import office_tools
        from .shell import shell_tools
        from .web import web_tools

        tools: list[ToolDefinition] = []
        tools.extend(filesystem_tools(self))
        tools.extend(shell_tools(self))
        tools.extend(web_tools(self))
        tools.extend(notebook_tools(self))
        tools.extend(agent_tools(self))
        tools.extend(misc_tools(self))
        tools.extend(office_tools(self))
        return tools

    def _wire_mcp_lifecycle_tools(self) -> None:
        """Replace MCP lifecycle stubs with handlers that delegate to McpManager."""
        mgr = self.mcp_manager
        assert mgr is not None

        def list_resources(args: dict[str, Any]) -> str:
            server_name = str(args.get("server_name", args.get("server", "")))
            if not server_name:
                report = mgr.discovery_report()
                return json.dumps({"servers": [s.name for s in report.tools[:20]]})
            try:
                resources = mgr.list_resources(server_name)
                return json.dumps(resources, indent=2)
            except Exception as exc:
                return json.dumps({"error": str(exc)})

        def read_resource(args: dict[str, Any]) -> str:
            server_name = str(args.get("server_name", args.get("server", "")))
            uri = str(args.get("uri", ""))
            if not server_name or not uri:
                return json.dumps({"error": "server_name and uri are required"})
            try:
                result = mgr.read_resource(server_name, uri)
                return json.dumps(result, indent=2) if not isinstance(result, str) else result
            except Exception as exc:
                return json.dumps({"error": str(exc)})

        def mcp_call(args: dict[str, Any]) -> str:
            server = str(args.get("server_name", args.get("server", "")))
            tool = str(args.get("tool_name", args.get("tool", "")))
            tool_args = args.get("arguments", {})
            if not server or not tool:
                return json.dumps({"error": "server_name and tool_name are required"})
            prefixed = f"mcp__{server}__{tool}".replace("-", "_")
            try:
                result = mgr.execute_prefixed_tool(prefixed, tool_args)
                return json.dumps(result, indent=2) if not isinstance(result, str) else result
            except Exception as exc:
                return json.dumps({"error": str(exc)})

        for name, handler in [("ListMcpResources", list_resources), ("ReadMcpResource", read_resource), ("MCP", mcp_call)]:
            if name in self._tools:
                old_spec = self._tools[name].spec
                self._tools[name] = ToolDefinition(spec=old_spec, handler=handler)

    def _run_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if not self.mcp_manager:
            raise RuntimeError("No MCP manager configured")
        return json.dumps(self.mcp_manager.execute_prefixed_tool(tool_name, arguments), indent=2)

    def _resolve_path(self, raw_path: str, allow_outside_workspace: bool = False) -> Path:
        path = Path(raw_path)
        resolved = (path if path.is_absolute() else self.workspace_root / path).resolve()
        if not allow_outside_workspace:
            # Canonical path comparison prevents symlink escapes (parity with Rust)
            try:
                canonical = resolved.resolve(strict=False)
            except (OSError, ValueError):
                canonical = resolved
            ws_canonical = self.workspace_root.resolve(strict=False)
            if ws_canonical not in (canonical, *canonical.parents):
                raise ValueError(
                    f"Path `{resolved}` escapes the workspace (canonical: {canonical})"
                )
        return resolved


def _normalize_tool_name(name: str) -> str:
    return name.strip().replace("-", "_").lower()


def normalize_allowed_tools(
    names: list[str],
    known_tools: set[str] | None = None,
) -> set[str] | None:
    """Normalize a list of tool names into canonical form.

    Returns None if the input is empty (meaning no restriction).
    Resolves aliases (read -> read_file, etc.) and splits comma/whitespace tokens.
    """
    if not names:
        return None

    canonical_by_normalized: dict[str, str] = {}
    if known_tools:
        for name in known_tools:
            canonical_by_normalized[_normalize_tool_name(name)] = name

    result: set[str] = set()
    for entry in names:
        for token in entry.replace(",", " ").split():
            normalized = _normalize_tool_name(token)
            if normalized in _TOOL_ALIASES:
                normalized = _normalize_tool_name(_TOOL_ALIASES[normalized])
            if canonical_by_normalized and normalized in canonical_by_normalized:
                result.add(canonical_by_normalized[normalized])
            else:
                result.add(token.strip())
    return result if result else None
