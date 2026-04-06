from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from ..config import McpServerConfig
from ..core.errors import McpError

_log = logging.getLogger("yucode.mcp")


@dataclass(frozen=True)
class McpTool:
    server_name: str
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def prefixed_name(self) -> str:
        return f"mcp__{self.server_name}__{self.name}".replace("-", "_")


class StdioMcpClient:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._request_id = 0
        self._lock = threading.Lock()
        self._initialized = False

    def list_tools(self) -> list[McpTool]:
        self._ensure_initialized()
        payload = self._call("tools/list", {})
        tools = payload.get("tools", [])
        return [
            McpTool(
                server_name=self.config.name,
                name=str(tool.get("name", "")),
                description=str(tool.get("description", "")),
                input_schema=tool.get("inputSchema", {"type": "object", "properties": {}}),
            )
            for tool in tools
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self._ensure_initialized()
        payload = self._call("tools/call", {"name": name, "arguments": arguments})
        return payload.get("content", payload)

    def list_resources(self) -> list[dict[str, Any]]:
        self._ensure_initialized()
        payload = self._call("resources/list", {})
        return list(payload.get("resources", []))

    def read_resource(self, uri: str) -> Any:
        self._ensure_initialized()
        payload = self._call("resources/read", {"uri": uri})
        return payload.get("contents", payload)

    def _ensure_initialized(self) -> None:
        if self.config.transport != "stdio":
            raise McpError(
                f"MCP transport `{self.config.transport}` is not yet implemented for "
                f"server `{self.config.name}`. Currently only `stdio` is supported. "
                f"Contributions for SSE/HTTP/WS transports are welcome.",
                server_name=self.config.name,
                recoverable=False,
            )
        with self._lock:
            if self._initialized:
                return
            self._process = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, **self.config.env},
            )
            self._call(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "yucode-agent", "version": "0.1.0"},
                },
            )
            self._initialized = True

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise McpError(
                f"MCP client process for `{self.config.name}` is not running",
                server_name=self.config.name,
            )
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        serialized = json.dumps(payload).encode("utf-8")
        message = b"Content-Length: " + str(len(serialized)).encode("ascii") + b"\r\n\r\n" + serialized
        try:
            self._process.stdin.write(message)
            self._process.stdin.flush()
            response = _read_lsp_message(self._process.stdout)
        except Exception as exc:
            raise McpError(
                f"Communication with MCP server `{self.config.name}` failed: {exc}",
                server_name=self.config.name,
            ) from exc
        if "error" in response:
            raise McpError(
                f"MCP error for {method} on `{self.config.name}`: {response['error']}",
                server_name=self.config.name,
            )
        return dict(response.get("result", {}))


class McpManager:
    def __init__(self, servers: list[McpServerConfig]) -> None:
        self._clients = {server.name: StdioMcpClient(server) for server in servers}
        self._tool_index: dict[str, McpTool] = {}
        self._failed_servers: set[str] = set()

    def tool_specs(self) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for name, client in self._clients.items():
            if name in self._failed_servers:
                continue
            try:
                for tool in client.list_tools():
                    self._tool_index[tool.prefixed_name] = tool
                    specs.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool.prefixed_name,
                                "description": tool.description or f"MCP tool {tool.name}",
                                "parameters": tool.input_schema or {"type": "object", "properties": {}},
                            },
                        }
                    )
            except McpError as exc:
                _log.warning("MCP server `%s` failed to list tools: %s — disabling", name, exc)
                self._failed_servers.add(name)
        return specs

    def execute_prefixed_tool(self, prefixed_name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tool_index.get(prefixed_name)
        if not tool:
            raise McpError(f"Unknown MCP tool: {prefixed_name}", recoverable=False)
        if tool.server_name in self._failed_servers:
            raise McpError(
                f"MCP server `{tool.server_name}` was disabled due to earlier failures",
                server_name=tool.server_name,
                recoverable=False,
            )
        client = self._clients[tool.server_name]
        return client.call_tool(tool.name, arguments)

    def list_resources(self, server_name: str) -> list[dict[str, Any]]:
        return self._clients[server_name].list_resources()

    def read_resource(self, server_name: str, uri: str) -> Any:
        return self._clients[server_name].read_resource(uri)


def _read_lsp_message(stream: Any) -> dict[str, Any]:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if line == b"":
            raise RuntimeError("MCP server closed the stream")
        stripped = line.strip().decode("ascii", errors="replace")
        if not stripped:
            break
        name, value = stripped.split(":", 1)
        headers[name.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        raise RuntimeError("MCP response missing Content-Length header")
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))
