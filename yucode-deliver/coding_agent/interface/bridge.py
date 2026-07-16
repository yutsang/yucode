from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

from ..config import ensure_default_config, load_app_config, resolve_config_path
from ..core.runtime import AgentRuntime
from ..core.session import Session
from ..memory.skills import list_skills
from .commands import InputKind, parse_input


class BridgeServer:
    def __init__(self, input_stream: Any = None, output_stream: Any = None) -> None:
        self.input_stream = input_stream or sys.stdin
        self.output_stream = output_stream or sys.stdout
        self._sessions: dict[str, _BridgeSession] = {}

    def serve_forever(self) -> int:
        self._emit({"type": "ready", "config_path": str(resolve_config_path())})
        for line in self.input_stream:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                self._dispatch(request)
            except Exception as exc:  # noqa: BLE001
                self._emit({"type": "error", "message": str(exc), "traceback": traceback.format_exc()})
        return 0

    def _get_or_create_session(self, workspace: Path, config_path: str | None, session_key: str | None = None) -> _BridgeSession:
        key = session_key or str(workspace)
        if key in self._sessions:
            return self._sessions[key]
        config = load_app_config(config_path, workspace=workspace)
        agent_session = Session(model=config.provider.model)
        runtime = AgentRuntime(workspace, config, session=agent_session)
        bridge_session = _BridgeSession(runtime=runtime, config=config)
        self._sessions[key] = bridge_session
        return bridge_session

    def _dispatch(self, request: dict[str, Any]) -> None:
        command = request.get("command")
        if command == "handshake":
            self._emit({"type": "handshake", "ok": True, "version": "0.1.0"})
            return
        if command == "config_path":
            path = ensure_default_config()
            self._emit({"type": "config_path", "path": str(path)})
            return
        if command == "load_config":
            config = load_app_config(request.get("config_path"))
            self._emit({"type": "config", "config": config.as_prompt_safe_dict()})
            return
        if command == "chat":
            workspace = Path(request["workspace"])
            raw_prompt = str(request["prompt"])
            config_path = request.get("config_path")
            session_key = request.get("session_id")

            parsed = parse_input(raw_prompt, workspace)
            if parsed.kind == InputKind.SLASH:
                self._handle_slash(parsed.command, parsed.arguments, workspace, config_path)
                return

            bridge_session = self._get_or_create_session(workspace, config_path, session_key)
            self._emit({
                "type": "provider_info",
                "provider": bridge_session.config.provider.name,
                "model": bridge_session.config.provider.model,
            })
            summary = bridge_session.runtime.orchestrate(
                parsed.effective_prompt, event_callback=self._emit,
            )
            self._emit({
                "type": "chat_result",
                "final_text": summary.final_text,
                "iterations": summary.iterations,
                "session_id": session_key or str(workspace),
            })
            return
        raise ValueError(f"Unsupported bridge command: {command}")

    def _handle_slash(self, command: str, arguments: str, workspace: Path, config_path: str | None) -> None:
        if command == "help":
            self._emit({"type": "slash_result", "command": "help", "text": (
                "Available commands:\n"
                "  /help     Show this help\n"
                "  /status   Runtime status\n"
                "  /config   Show config\n"
                "  /tools    List tools\n"
                "  /mcp      List MCP servers\n"
                "  /skills   List skills\n"
                "  /clear    Clear history\n"
                "\nUse @path to attach workspace files as context."
            )})
        elif command == "status":
            config = load_app_config(config_path, workspace=workspace)
            self._emit({"type": "slash_result", "command": "status", "text": (
                f"provider: {config.provider.name}/{config.provider.model}\n"
                f"permission: {config.runtime.permission_mode}\n"
                f"max_iterations: {config.runtime.max_iterations}\n"
                f"mcp_servers: {len(config.mcp)}\n"
                f"skills: {len(list_skills(workspace))}"
            )})
        elif command == "tools":
            config = load_app_config(config_path, workspace=workspace)
            rt = AgentRuntime(workspace, config)
            names = rt.tools.list_names()
            self._emit({"type": "slash_result", "command": "tools", "text": "\n".join(names)})
        elif command == "mcp":
            config = load_app_config(config_path, workspace=workspace)
            if config.mcp:
                lines = [f"{s.name}: {s.command} {' '.join(s.args)}".strip() for s in config.mcp]
                self._emit({"type": "slash_result", "command": "mcp", "text": "\n".join(lines)})
            else:
                self._emit({"type": "slash_result", "command": "mcp", "text": "No MCP servers configured."})
        elif command == "skills":
            found = list_skills(workspace)
            if found:
                lines = [f"{s.name}: {s.description}" for s in found]
                self._emit({"type": "slash_result", "command": "skills", "text": "\n".join(lines)})
            else:
                self._emit({"type": "slash_result", "command": "skills", "text": "No skills found."})
        elif command == "config":
            path = resolve_config_path(config_path)
            self._emit({"type": "slash_result", "command": "config", "text": path.read_text(encoding="utf-8")})
        elif command == "clear":
            key = str(workspace)
            if key in self._sessions:
                del self._sessions[key]
            self._emit({"type": "slash_result", "command": "clear", "text": "Session cleared."})
        else:
            self._emit({"type": "slash_result", "command": command, "text": f"Unknown command: /{command}"})

    def _emit(self, payload: dict[str, Any]) -> None:
        self.output_stream.write(json.dumps(payload) + "\n")
        self.output_stream.flush()


class _BridgeSession:
    __slots__ = ("runtime", "config")

    def __init__(self, runtime: AgentRuntime, config: Any) -> None:
        self.runtime = runtime
        self.config = config
