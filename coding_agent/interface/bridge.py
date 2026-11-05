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

# How many workspaces/tabs may hold a live runtime at once. Each carries a tool
# registry, an MCP manager and a whole conversation, so this is about memory,
# not correctness -- an evicted session is saved and can be resumed.
_MAX_LIVE_SESSIONS = 8


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

    @staticmethod
    def _session_key(workspace: Path, session_key: str | None) -> str:
        """The one rule for which key a request's session lives under.

        This fallback used to be spelled out separately in each place that
        needed it, and `/clear` only ever knew the `str(workspace)` half: when
        the editor supplied a `session_id`, chat stored its conversation under
        that id while the slash commands went looking under the workspace path.
        """
        return session_key or str(workspace)

    def _get_or_create_session(self, workspace: Path, config_path: str | None, session_key: str | None = None) -> _BridgeSession:
        key = self._session_key(workspace, session_key)
        if key in self._sessions:
            # Re-insert to mark it most recently used; see _evict_oldest.
            self._sessions[key] = self._sessions.pop(key)
            return self._sessions[key]
        config = load_app_config(config_path, workspace=workspace)
        agent_session = Session(model=config.provider.model)
        runtime = AgentRuntime(workspace, config, session=agent_session)
        bridge_session = _BridgeSession(runtime=runtime, config=config)
        self._sessions[key] = bridge_session
        self._evict_oldest()
        return bridge_session

    def _evict_oldest(self) -> None:
        """Bound the live-session table, saving what gets dropped.

        One AgentRuntime is held per session_id for the life of the process,
        and an editor is a long-lived process that hands out a fresh id per
        window or per tab. Nothing ever removed an entry except /clear, so the
        table only grew -- each entry holding a tool registry, an MCP manager
        and the full conversation.

        The evicted conversation is written out first, for the same reason
        /clear saves before dropping: the bridge is where a user is least
        likely to have another copy. A save that fails is not worth taking the
        process down for, so it is reported and the entry still goes.
        """
        while len(self._sessions) > _MAX_LIVE_SESSIONS:
            key, evicted = next(iter(self._sessions.items()))
            del self._sessions[key]
            if not evicted.runtime.session.messages:
                continue
            try:
                saved = evicted.runtime.save_session()
            except OSError as exc:
                self._emit({"type": "warning", "message": (
                    f"Dropped the least recently used session ({key}) to stay "
                    f"under {_MAX_LIVE_SESSIONS}, but could not save it: {exc}"
                )})
                continue
            self._emit({"type": "notice", "message": (
                f"Session {key} was idle longest and has been closed to stay "
                f"under {_MAX_LIVE_SESSIONS} open sessions. Saved to {saved} — "
                f"reopen it with /resume {evicted.runtime.session.session_id}"
            )})

    def _live_config(self, workspace: Path, config_path: str | None, session_key: str | None):
        """The config the running session is actually using, if there is one.

        Reading the file instead means /status describes a session that may not
        exist: the chat handler loaded its config when the session was created,
        and an edit to settings.yml since then does not reach it. Falls back to
        the file when no session has been started, which is the only case where
        the file is the answer -- and does so without constructing a runtime,
        since that has process-wide effects (see /tools).
        """
        key = self._session_key(workspace, session_key)
        existing = self._sessions.get(key)
        if existing is not None:
            return existing.config
        return load_app_config(config_path, workspace=workspace)

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
                # session_key has to travel with the slash command: /clear and
                # /resume address the same session map that chat writes to.
                self._handle_slash(parsed.command, parsed.arguments, workspace, config_path, session_key)
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
                "session_id": self._session_key(workspace, session_key),
            })
            return
        raise ValueError(f"Unsupported bridge command: {command}")

    def _handle_slash(self, command: str, arguments: str, workspace: Path, config_path: str | None, session_key: str | None = None) -> None:
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
                "  /resume   List saved sessions, or /resume <id> to reopen one\n"
                "\nUse @path to attach workspace files as context."
            )})
        elif command == "status":
            config = self._live_config(workspace, config_path, session_key)
            self._emit({"type": "slash_result", "command": "status", "text": (
                f"provider: {config.provider.name}/{config.provider.model}\n"
                f"permission: {config.runtime.permission_mode}\n"
                f"max_iterations: {config.runtime.max_iterations}\n"
                f"mcp_servers: {len(config.mcp)}\n"
                f"skills: {len(list_skills(workspace))}"
            )})
        elif command == "tools":
            # The live session's registry, not a throwaway runtime built from
            # whatever is on disk. Constructing an AgentRuntime has a
            # process-wide side effect -- it installs the env-scrub policy from
            # its own config -- so answering /tools used to silently re-point
            # credential withholding for every workspace this process serves,
            # at the settings of the file rather than of the running session.
            bridge_session = self._get_or_create_session(workspace, config_path, session_key)
            names = bridge_session.runtime.tools.list_names()
            self._emit({"type": "slash_result", "command": "tools", "text": "\n".join(names)})
        elif command == "mcp":
            config = self._live_config(workspace, config_path, session_key)
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
            # Save before dropping, matching the CLI's /clear. Deleting the
            # entry alone discarded the conversation outright -- the editor
            # bridge is exactly where a user is least likely to have another
            # copy of it.
            key = self._session_key(workspace, session_key)
            text = "Session cleared."
            # Look up without removing: dropping the entry first meant a failed
            # save still lost the conversation, which is the exact outcome this
            # is here to prevent (and unlike the CLI's /clear, which aborts).
            bridge_session = self._sessions.get(key)
            if bridge_session is not None and bridge_session.runtime.session.messages:
                try:
                    saved = bridge_session.runtime.save_session()
                except OSError as exc:
                    self._emit({"type": "slash_result", "command": "clear", "text": (
                        f"Not clearing: could not save the current session ({exc}). "
                        "Preserve it first, then try again."
                    )})
                    return
                text = (
                    f"Session cleared. Previous conversation saved to {saved} — "
                    f"reopen it with /resume {bridge_session.runtime.session.session_id}"
                )
            self._sessions.pop(key, None)
            self._emit({"type": "slash_result", "command": "clear", "text": text})
        elif command == "resume":
            self._handle_resume(arguments.strip(), workspace, config_path, session_key)
        else:
            self._emit({"type": "slash_result", "command": command, "text": f"Unknown command: /{command}"})

    def _handle_resume(self, session_id: str, workspace: Path, config_path: str | None, session_key: str | None = None) -> None:
        """Reopen a saved session, mirroring the CLI's /resume.

        /clear ends by telling the user to `reopen it with /resume <id>`, and
        without this branch that instruction answered `Unknown command:
        /resume` -- a recovery path that existed only in the message, in the
        one interface where (by /clear's own reasoning) the user is least
        likely to hold another copy of the conversation.
        """
        if not session_id:
            saved = Session.list_sessions(workspace)
            if saved:
                text = "\n".join(
                    f"{s['id']}  {s['message_count']} msgs  {s['title']}".rstrip()
                    for s in saved[:10]
                )
            else:
                text = "No saved sessions found."
            self._emit({"type": "slash_result", "command": "resume", "text": text})
            return
        try:
            loaded = Session.load_from_workspace(workspace, session_id)
        except (FileNotFoundError, OSError, ValueError):
            # Nothing is swapped in on failure, so whatever is open stays open.
            self._emit({"type": "slash_result", "command": "resume",
                        "text": f"Session `{session_id}` not found."})
            return
        bridge_session = self._get_or_create_session(workspace, config_path, session_key)
        # Swapping the runtime's session is the same move the CLI's /resume
        # makes; the key matches /clear's so the two commands act on one
        # conversation.
        bridge_session.runtime.session = loaded
        self._emit({"type": "slash_result", "command": "resume", "text": (
            f"Resumed session `{session_id}` ({len(loaded.messages)} messages)."
        )})

    def _emit(self, payload: dict[str, Any]) -> None:
        self.output_stream.write(json.dumps(payload) + "\n")
        self.output_stream.flush()


class _BridgeSession:
    __slots__ = ("runtime", "config")

    def __init__(self, runtime: AgentRuntime, config: Any) -> None:
        self.runtime = runtime
        self.config = config
