from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..memory.skills import load_skill
from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry


def misc_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            ToolSpec("todo_write", "Write or update the local todo list.",
                     {"type": "object", "properties": {"todos": {"type": "array"}, "merge": {"type": "boolean"}}, "required": ["todos"]},
                     "workspace-write", RiskLevel.MEDIUM),
            lambda args: _todo_write(registry, args),
        ),
        ToolDefinition(
            ToolSpec("mcp_list_resources", "List resources from a configured MCP server.",
                     {"type": "object", "properties": {"server": {"type": "string"}}, "required": ["server"]},
                     "read-only", RiskLevel.LOW),
            lambda args: _mcp_list_resources(registry, args),
        ),
        ToolDefinition(
            ToolSpec("mcp_read_resource", "Read a resource from a configured MCP server.",
                     {"type": "object", "properties": {"server": {"type": "string"}, "uri": {"type": "string"}}, "required": ["server", "uri"]},
                     "read-only", RiskLevel.LOW),
            lambda args: _mcp_read_resource(registry, args),
        ),
        ToolDefinition(
            ToolSpec("load_skill", "Load a skill by name. Returns the full SKILL.md body.",
                     {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                     "read-only", RiskLevel.LOW),
            lambda args: _load_skill(registry, args),
        ),
        ToolDefinition(
            ToolSpec("sleep", "Pause execution for the given number of milliseconds.",
                     {"type": "object", "properties": {"milliseconds": {"type": "integer"}}, "required": ["milliseconds"]},
                     "read-only", RiskLevel.LOW),
            lambda args: _sleep(args),
        ),
        ToolDefinition(
            ToolSpec("tool_search", "Search available tools by keyword.",
                     {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                     "read-only", RiskLevel.LOW),
            lambda args: _tool_search(registry, args),
        ),
        ToolDefinition(
            ToolSpec("config_read", "Read the current agent configuration.",
                     {"type": "object", "properties": {}},
                     "read-only", RiskLevel.LOW),
            lambda args: _config_read(registry, args),
        ),
        ToolDefinition(
            ToolSpec("structured_output", "Return a structured JSON result.",
                     {"type": "object", "properties": {"data": {}}, "required": ["data"]},
                     "read-only", RiskLevel.LOW),
            lambda args: _structured_output(args),
        ),
        ToolDefinition(
            ToolSpec("config_write", "Update a runtime configuration value.",
                     {"type": "object", "properties": {
                         "key": {"type": "string", "description": "Dot-separated config key (e.g. 'runtime.max_iterations')."},
                         "value": {"type": "string", "description": "New value."},
                     }, "required": ["key", "value"]},
                     "workspace-write", RiskLevel.MEDIUM),
            lambda args: _config_write(registry, args),
        ),
    ]


def _todo_write(registry: ToolRegistry, args: dict[str, Any]) -> str:
    from ..config.settings import state_dir
    todos_path = state_dir(registry.workspace_root) / "todos.json"
    todos_path.parent.mkdir(parents=True, exist_ok=True)
    incoming = args["todos"]
    merge = bool(args.get("merge", True))
    existing: list[dict[str, Any]] = []
    if merge and todos_path.exists():
        existing = json.loads(todos_path.read_text(encoding="utf-8"))
    if merge:
        index = {item["id"]: item for item in existing if isinstance(item, dict) and "id" in item}
        for item in incoming:
            index[item["id"]] = item
        payload = list(index.values())
    else:
        payload = incoming
    todos_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return f"Updated {todos_path}"


def _mcp_list_resources(registry: ToolRegistry, args: dict[str, Any]) -> str:
    if not registry.mcp_manager:
        raise RuntimeError("No MCP manager configured")
    return json.dumps(registry.mcp_manager.list_resources(str(args["server"])), indent=2)


def _mcp_read_resource(registry: ToolRegistry, args: dict[str, Any]) -> str:
    if not registry.mcp_manager:
        raise RuntimeError("No MCP manager configured")
    return json.dumps(registry.mcp_manager.read_resource(str(args["server"]), str(args["uri"])), indent=2)


def _load_skill(registry: ToolRegistry, args: dict[str, Any]) -> str:
    name = str(args["name"])
    skill = load_skill(registry.workspace_root, name)
    if not skill:
        raise ValueError(f"Skill `{name}` not found")
    return skill.load_body()


def _sleep(args: dict[str, Any]) -> str:
    ms = int(args["milliseconds"])
    time.sleep(ms / 1000.0)
    return f"Slept for {ms}ms"


def _tool_search(registry: ToolRegistry, args: dict[str, Any]) -> str:
    query = str(args["query"]).lower()
    matches = []
    for name in registry.list_names():
        tool = registry._tools[name]
        if query in name.lower() or query in tool.spec.description.lower():
            matches.append({"name": name, "description": tool.spec.description})
    return json.dumps(matches, indent=2)


def _config_read(registry: ToolRegistry, args: dict[str, Any]) -> str:
    return json.dumps(registry.config.as_prompt_safe_dict(), indent=2)


def _structured_output(args: dict[str, Any]) -> str:
    return json.dumps(args.get("data", {}), indent=2, default=str)


def _config_write(registry: ToolRegistry, args: dict[str, Any]) -> str:
    from ..config import dump_yaml, load_yaml, resolve_config_path
    key = str(args["key"])
    value = str(args["value"])
    path = resolve_config_path()
    raw = load_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raw = {}
    parts = key.split(".")
    target = raw
    for part in parts[:-1]:
        target = target.setdefault(part, {})
        if not isinstance(target, dict):
            return json.dumps({"error": f"Cannot traverse key: {key}"})
    old = target.get(parts[-1])
    if value.lower() in ("true", "false"):
        target[parts[-1]] = value.lower() == "true"
    elif value.isdigit():
        target[parts[-1]] = int(value)
    else:
        try:
            target[parts[-1]] = float(value)
        except ValueError:
            target[parts[-1]] = value
    path.write_text(dump_yaml(raw), encoding="utf-8")
    return f"Set {key} = {target[parts[-1]]} (was {old})"
