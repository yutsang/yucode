"""Tool layer -- registry, specs, and built-in tool implementations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ..config import AppConfig
from ..plugins.mcp import McpManager
from ..security.permissions import PermissionMode


ToolHandler = Callable[[dict[str, Any]], str]


class RiskLevel(str, Enum):
    """Intrinsic risk level of a tool, separate from the permission policy."""
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
        self._tools = {tool.spec.name: tool for tool in self._builtin_tools()}
        if self.mcp_manager:
            for spec in self.mcp_manager.tool_specs():
                function = spec["function"]
                name = function["name"]
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
        if plugin_tools:
            for pt in plugin_tools:
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

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        return self._tools[tool_name].handler(arguments)

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    def _apply_filters(self) -> None:
        if self.config.tools.allowed:
            allowed = set(self.config.tools.allowed)
            self._tools = {name: tool for name, tool in self._tools.items() if name in allowed}
        if self.config.tools.disabled:
            for name in self.config.tools.disabled:
                self._tools.pop(name, None)

    def _builtin_tools(self) -> list[ToolDefinition]:
        from .filesystem import filesystem_tools
        from .shell import shell_tools
        from .web import web_tools
        from .notebook import notebook_tools
        from .agent_tool import agent_tools
        from .misc import misc_tools
        from .office import office_tools

        tools: list[ToolDefinition] = []
        tools.extend(filesystem_tools(self))
        tools.extend(shell_tools(self))
        tools.extend(web_tools(self))
        tools.extend(notebook_tools(self))
        tools.extend(agent_tools(self))
        tools.extend(misc_tools(self))
        tools.extend(office_tools(self))
        return tools

    def _run_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if not self.mcp_manager:
            raise RuntimeError("No MCP manager configured")
        return json.dumps(self.mcp_manager.execute_prefixed_tool(tool_name, arguments), indent=2)

    def _resolve_path(self, raw_path: str, allow_outside_workspace: bool = False) -> Path:
        path = Path(raw_path)
        resolved = (path if path.is_absolute() else self.workspace_root / path).resolve()
        if not allow_outside_workspace and self.workspace_root not in (resolved, *resolved.parents):
            raise ValueError(f"Path `{resolved}` escapes the workspace")
        return resolved
