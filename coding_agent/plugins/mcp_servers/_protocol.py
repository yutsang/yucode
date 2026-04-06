"""Minimal MCP stdio JSON-RPC protocol helpers.

Provides a base class that handles the MCP handshake, tool listing, and
tool dispatch over stdin/stdout JSON-RPC using Content-Length framing
(matching the MCP specification).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

ToolHandler = Callable[[dict[str, Any]], Any]


class McpStdioServer:
    def __init__(self, name: str, version: str = "0.1.0"):
        self.name = name
        self.version = version
        self._tools: dict[str, tuple[dict[str, Any], ToolHandler]] = {}

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        self._tools[name] = (
            {"name": name, "description": description, "inputSchema": input_schema},
            handler,
        )

    def serve(self) -> None:
        reader = sys.stdin.buffer
        while True:
            header_line = b""
            while True:
                byte = reader.read(1)
                if not byte:
                    return
                header_line += byte
                if header_line.endswith(b"\r\n\r\n"):
                    break
                if header_line.endswith(b"\n\n"):
                    break
            header_text = header_line.decode("utf-8", errors="replace")
            content_length = 0
            for line in header_text.strip().splitlines():
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
            if content_length <= 0:
                continue
            body = reader.read(content_length)
            if len(body) < content_length:
                return
            try:
                request = json.loads(body)
            except json.JSONDecodeError:
                continue
            response = self._dispatch(request)
            if response is not None:
                self._send(response)

    def _send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n"
        sys.stdout.buffer.write(header.encode("utf-8"))
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method", "")
        req_id = request.get("id")

        if method == "initialize":
            return self._ok(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            })

        if method == "notifications/initialized":
            return None

        if method == "tools/list":
            tool_list = [spec for spec, _ in self._tools.values()]
            return self._ok(req_id, {"tools": tool_list})

        if method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            if tool_name not in self._tools:
                return self._error(req_id, -32601, f"Unknown tool: {tool_name}")
            _, handler = self._tools[tool_name]
            try:
                result = handler(args)
                text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
                return self._ok(req_id, {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                })
            except Exception as exc:
                return self._ok(req_id, {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                })

        return self._error(req_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _ok(req_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
