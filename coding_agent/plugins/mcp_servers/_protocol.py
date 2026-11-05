"""Minimal MCP stdio JSON-RPC protocol helpers.

Handles the MCP handshake, tool listing and tool dispatch over stdin/stdout.

Messages are newline-delimited JSON, which is what the MCP stdio transport
specifies. This file used to say Content-Length framing was "matching the MCP
specification"; it is not, it is LSP's convention, and the official SDK is
unambiguous -- `serializeMessage` there is `JSON.stringify(message) + '\\n'`
with no header anywhere. The consequence was that these servers could be read
by nothing except yucode's own client, which had the same misreading, so the
five domain servers here had never been reachable from VS Code, Claude Desktop,
Cursor or anything else that speaks MCP.

Content-Length input is still accepted, because yucode's own client emitted it
for a long time and an old configuration should not break.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ToolHandler = Callable[[dict[str, Any]], Any]


# Argument names that carry a filesystem path. Same approach as the runtime's
# _PATH_ARG_TOOLS: the schemas are hand-written and small enough to enumerate,
# and a name-based rule is checkable by reading it.
_PATH_ARG_SUFFIXES = ("path", "paths", "file", "filename", "directory", "dir")


def _looks_like_path_arg(key: str) -> bool:
    lowered = key.lower()
    return lowered in _PATH_ARG_SUFFIXES or lowered.endswith(("_path", "_file", "_dir"))


class McpStdioServer:
    def __init__(self, name: str, version: str = "0.1.0", root: Path | None = None):
        self.name = name
        self.version = version
        # Every path argument must resolve inside this directory. Defaults to
        # $YUCODE_MCP_ROOT, then the working directory.
        #
        # These servers took a path from the caller and read or wrote it with
        # no check at all, and four of their tools write: write_cell saves the
        # workbook, append_text and write_new save the document, create_pptx
        # and data_to_pptx mkdir their parents first. That was survivable only
        # for as long as nothing could reach them -- the wire format was wrong,
        # so the caller was always yucode itself. Fixing the framing made them
        # reachable from Copilot, which means the paths now come from a model
        # this machine's owner does not control.
        env_root = os.environ.get("YUCODE_MCP_ROOT", "").strip()
        self.root = (root or Path(env_root or Path.cwd())).expanduser().resolve()
        self._tools: dict[str, tuple[dict[str, Any], ToolHandler]] = {}

    def _guard_paths(self, args: dict[str, Any]) -> str:
        """Check every path argument against the root, and rewrite it absolute.

        Rewriting is not incidental. Checking alone resolves a relative path
        against the root while the handler goes on to open it against the
        process's working directory, so `in.csv` is judged in one place and
        read from another -- it passes the check and then fails to open, or
        worse, opens a different file with that name. Substituting the resolved
        path leaves one meaning: relative is relative to the served directory.

        Returns an error message, or "" when everything is in bounds. Mutates
        *args* in place only when it returns "".
        """
        rewritten: dict[str, Any] = {}
        for key, value in args.items():
            if not _looks_like_path_arg(key):
                continue
            values = value if isinstance(value, list) else [value]
            resolved_values = []
            for raw in values:
                if not isinstance(raw, str) or not raw.strip():
                    resolved_values.append(raw)
                    continue
                candidate = Path(raw.replace("\\", "/")).expanduser()
                resolved = (
                    candidate if candidate.is_absolute() else self.root / candidate
                ).resolve()
                if self.root != resolved and self.root not in resolved.parents:
                    return (
                        f"`{key}` points outside the served directory. "
                        f"{resolved} is not under {self.root}. Pass a path "
                        "inside it, or start the server with that directory as "
                        "its root."
                    )
                resolved_values.append(str(resolved))
            rewritten[key] = resolved_values if isinstance(value, list) else resolved_values[0]
        args.update(rewritten)
        return ""

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
            line = reader.readline()
            if not line:
                return
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower().startswith(b"content-length:"):
                # A client still using the old framing. Read its headers to the
                # blank line, then the body it declared.
                length = int(stripped.split(b":", 1)[1].strip())
                while True:
                    header = reader.readline()
                    if not header or header.strip() == b"":
                        break
                body = reader.read(length)
                if len(body) < length:
                    return
            else:
                body = stripped
            try:
                request = json.loads(body)
            except json.JSONDecodeError:
                continue
            response = self._dispatch(request)
            if response is not None:
                self._send(response)

    def _send(self, message: dict[str, Any]) -> None:
        # One line per message, newline-terminated: the MCP stdio transport.
        # json.dumps has no embedded newlines by default, which the transport
        # requires.
        body = json.dumps(message)
        sys.stdout.buffer.write(body.encode("utf-8") + b"\n")
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
            escape = self._guard_paths(args if isinstance(args, dict) else {})
            if escape:
                return self._ok(req_id, {
                    "content": [{"type": "text", "text": f"Error: {escape}"}],
                    "isError": True,
                })
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
