from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..config import McpServerConfig
from ..core.errors import McpError

_log = logging.getLogger("yucode.mcp")


class McpConnectionStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


class McpLifecyclePhase(str, Enum):
    SPAWN_CONNECT = "spawn_connect"
    INITIALIZE_HANDSHAKE = "initialize_handshake"
    TOOL_DISCOVERY = "tool_discovery"
    RESOURCE_DISCOVERY = "resource_discovery"
    INVOCATION = "invocation"
    SERVER_REGISTRATION = "server_registration"
    ERROR_SURFACING = "error_surfacing"


@dataclass(frozen=True)
class McpTool:
    server_name: str
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def prefixed_name(self) -> str:
        return f"mcp__{self.server_name}__{self.name}".replace("-", "_")


@dataclass
class McpServerState:
    name: str
    status: McpConnectionStatus = McpConnectionStatus.DISCONNECTED
    tools: list[McpTool] = field(default_factory=list)
    error_message: str | None = None


@dataclass
class McpDiscoveryFailure:
    server_name: str
    phase: McpLifecyclePhase
    error: str
    recoverable: bool = False


@dataclass
class McpDegradedReport:
    failed_servers: list[McpDiscoveryFailure] = field(default_factory=list)
    unsupported_servers: list[str] = field(default_factory=list)
    recovery_recommendations: list[str] = field(default_factory=list)


@dataclass
class McpDiscoveryReport:
    tools: list[McpTool] = field(default_factory=list)
    failed_servers: list[McpDiscoveryFailure] = field(default_factory=list)
    unsupported_servers: list[str] = field(default_factory=list)
    degraded_startup: McpDegradedReport | None = None


_DEFAULT_CALL_TIMEOUT = 30
_DEFAULT_RECONNECT_ATTEMPTS = 2


class StdioMcpClient:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._request_id = 0
        self._lock = threading.Lock()
        self._initialized = False
        self.status = McpConnectionStatus.DISCONNECTED
        self.error_message: str | None = None
        self._call_timeout: int = _DEFAULT_CALL_TIMEOUT
        self._reconnect_attempts: int = 0
        self._max_reconnect_attempts: int = _DEFAULT_RECONNECT_ATTEMPTS

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
        try:
            payload = self._call("tools/call", {"name": name, "arguments": arguments})
            return payload.get("content", payload)
        except McpError:
            if self._reconnect_attempts < self._max_reconnect_attempts:
                self._reconnect_attempts += 1
                _log.info("MCP server `%s` call failed; attempting reconnect (%d/%d)", self.config.name, self._reconnect_attempts, self._max_reconnect_attempts)
                self.reset()
                self._ensure_initialized()
                payload = self._call("tools/call", {"name": name, "arguments": arguments})
                self._reconnect_attempts = 0
                return payload.get("content", payload)
            raise

    def reset(self) -> None:
        """Tear down and reset the client for a fresh connection attempt."""
        self.shutdown()
        self._reconnect_attempts = 0

    def list_resources(self) -> list[dict[str, Any]]:
        self._ensure_initialized()
        payload = self._call("resources/list", {})
        return list(payload.get("resources", []))

    def read_resource(self, uri: str) -> Any:
        self._ensure_initialized()
        payload = self._call("resources/read", {"uri": uri})
        return payload.get("contents", payload)

    def shutdown(self) -> None:
        with self._lock:
            if self._process:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=5)
                except Exception:
                    with suppress(Exception):
                        self._process.kill()
                self._process = None
            self._initialized = False
            self.status = McpConnectionStatus.DISCONNECTED

    def _ensure_initialized(self) -> None:
        if self.config.transport != "stdio":
            self.status = McpConnectionStatus.ERROR
            self.error_message = f"Transport `{self.config.transport}` not implemented"
            raise McpError(
                f"MCP transport `{self.config.transport}` is not yet implemented for "
                f"server `{self.config.name}`. Currently only `stdio` is supported.",
                server_name=self.config.name,
                recoverable=False,
            )
        with self._lock:
            if self._initialized:
                return
            self.status = McpConnectionStatus.CONNECTING
            try:
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
                        "clientInfo": {"name": "yucode-agent", "version": "0.2.0"},
                    },
                )
                self._initialized = True
                self.status = McpConnectionStatus.CONNECTED
                self.error_message = None
            except Exception as exc:
                self.status = McpConnectionStatus.ERROR
                self.error_message = str(exc)
                raise

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
            self.status = McpConnectionStatus.ERROR
            self.error_message = str(exc)
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
        self._clients: dict[str, StdioMcpClient] = {}
        self._unsupported_servers: list[str] = []
        for server in servers:
            if server.transport != "stdio":
                self._unsupported_servers.append(server.name)
                _log.info("MCP server `%s` uses unsupported transport `%s`; skipping", server.name, server.transport)
                continue
            self._clients[server.name] = StdioMcpClient(server)
        self._tool_index: dict[str, McpTool] = {}
        self._failed_servers: set[str] = set()
        self._server_states: dict[str, McpServerState] = {}
        self._discovery_failures: list[McpDiscoveryFailure] = []

    @property
    def server_states(self) -> dict[str, McpServerState]:
        return dict(self._server_states)

    @property
    def failed_server_names(self) -> set[str]:
        return set(self._failed_servers)

    def tool_specs(self) -> list[dict[str, Any]]:
        return self._discover_tools_best_effort()

    def _discover_tools_best_effort(self) -> list[dict[str, Any]]:
        """Best-effort discovery: healthy servers contribute tools, failures are tracked."""
        specs: list[dict[str, Any]] = []
        self._discovery_failures.clear()
        for name, client in self._clients.items():
            if name in self._failed_servers:
                continue
            state = McpServerState(name=name)
            try:
                tools = client.list_tools()
                state.status = McpConnectionStatus.CONNECTED
                state.tools = tools
                for tool in tools:
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
                state.status = McpConnectionStatus.ERROR
                state.error_message = str(exc)
                phase = _classify_failure_phase(exc)
                self._discovery_failures.append(McpDiscoveryFailure(
                    server_name=name,
                    phase=phase,
                    error=str(exc),
                    recoverable=phase in (McpLifecyclePhase.TOOL_DISCOVERY, McpLifecyclePhase.SPAWN_CONNECT),
                ))
            self._server_states[name] = state
        return specs

    def discovery_report(self) -> McpDiscoveryReport:
        all_tools = list(self._tool_index.values())
        degraded = None
        if self._discovery_failures or self._unsupported_servers:
            recommendations: list[str] = []
            for f in self._discovery_failures:
                if f.recoverable:
                    recommendations.append(f"Retry server `{f.server_name}` ({f.phase.value})")
                else:
                    recommendations.append(f"Check config for server `{f.server_name}`")
            degraded = McpDegradedReport(
                failed_servers=list(self._discovery_failures),
                unsupported_servers=list(self._unsupported_servers),
                recovery_recommendations=recommendations,
            )
        return McpDiscoveryReport(
            tools=all_tools,
            failed_servers=list(self._discovery_failures),
            unsupported_servers=list(self._unsupported_servers),
            degraded_startup=degraded,
        )

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

    def shutdown(self) -> None:
        for client in self._clients.values():
            client.shutdown()


def _classify_failure_phase(exc: McpError) -> McpLifecyclePhase:
    msg = str(exc).lower()
    if "not running" in msg or "spawn" in msg or "not yet implemented" in msg:
        return McpLifecyclePhase.SPAWN_CONNECT
    if "initialize" in msg or "handshake" in msg:
        return McpLifecyclePhase.INITIALIZE_HANDSHAKE
    if "tools/list" in msg or "list tools" in msg:
        return McpLifecyclePhase.TOOL_DISCOVERY
    return McpLifecyclePhase.ERROR_SURFACING


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
