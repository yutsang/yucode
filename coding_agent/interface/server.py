"""HTTP + SSE session server.

Provides a REST API for managing agent sessions, compatible with the
Rust ``server`` crate layout.  Uses ``aiohttp`` (optional dependency).

Endpoints:
  POST   /sessions              Create a new session
  GET    /sessions              List all sessions
  GET    /sessions/{id}         Get session details
  POST   /sessions/{id}/message Send a user message (runs the model)
  GET    /sessions/{id}/events  SSE event stream
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


def _require_aiohttp():
    try:
        from aiohttp import web
        return web
    except ImportError as exc:
        raise RuntimeError(
            "aiohttp is required for the server. Install with: "
            "pip install yucode-agent[server]"
        ) from exc


class SessionStore:
    def __init__(self, workspace_root: Path) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._runtimes: dict[str, Any] = {}
        self._next_id: int = 1
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._workspace_root = workspace_root

    def create_session(self) -> str:
        from ..config import load_app_config
        from ..core.runtime import AgentRuntime
        from ..core.session import Session

        session_id = f"session-{self._next_id}"
        self._next_id += 1
        self._sessions[session_id] = {
            "id": session_id,
            "created_at": int(time.time() * 1000),
            "messages": [],
        }
        self._subscribers[session_id] = []

        config = load_app_config(workspace=self._workspace_root)
        agent_session = Session(model=config.provider.model)
        runtime = AgentRuntime(self._workspace_root, config, session=agent_session)
        self._runtimes[session_id] = runtime
        return session_id

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "id": s["id"],
                "created_at": s["created_at"],
                "message_count": len(s["messages"]),
            }
            for s in sorted(self._sessions.values(), key=lambda x: x["id"])
        ]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    def get_runtime(self, session_id: str) -> Any | None:
        return self._runtimes.get(session_id)

    def add_message(self, session_id: str, role: str, content: str) -> dict[str, Any] | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        message = {"role": role, "content": content}
        session["messages"].append(message)
        self._notify(session_id, {"type": "message", "session_id": session_id, "message": message})
        return message

    def _notify(self, session_id: str, event: dict[str, Any]) -> None:
        for queue in self._subscribers.get(session_id, []):
            queue.put_nowait(event)

    def subscribe(self, session_id: str) -> asyncio.Queue | None:
        if session_id not in self._sessions:
            return None
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(session_id, [])
        if queue in subs:
            subs.remove(queue)


def create_app(workspace_root: Path | None = None) -> Any:
    web = _require_aiohttp()

    app = web.Application()
    ws_root = workspace_root or Path.cwd()
    store = SessionStore(ws_root)
    app["store"] = store
    app["workspace_root"] = ws_root

    app.router.add_post("/sessions", handle_create_session)
    app.router.add_get("/sessions", handle_list_sessions)
    app.router.add_get("/sessions/{id}", handle_get_session)
    app.router.add_post("/sessions/{id}/message", handle_send_message)
    app.router.add_get("/sessions/{id}/events", handle_stream_events)
    return app


async def handle_create_session(request: Any) -> Any:
    web = _require_aiohttp()
    store: SessionStore = request.app["store"]
    session_id = store.create_session()
    return web.json_response({"session_id": session_id}, status=201)


async def handle_list_sessions(request: Any) -> Any:
    web = _require_aiohttp()
    store: SessionStore = request.app["store"]
    return web.json_response({"sessions": store.list_sessions()})


async def handle_get_session(request: Any) -> Any:
    web = _require_aiohttp()
    store: SessionStore = request.app["store"]
    session_id = request.match_info["id"]
    session = store.get_session(session_id)
    if session is None:
        return web.json_response({"error": f"session `{session_id}` not found"}, status=404)
    return web.json_response({
        "id": session["id"],
        "created_at": session["created_at"],
        "session": {"messages": session["messages"]},
    })


async def handle_send_message(request: Any) -> Any:
    web = _require_aiohttp()
    store: SessionStore = request.app["store"]
    session_id = request.match_info["id"]
    body = await request.json()
    message_text = body.get("message", "")

    result = store.add_message(session_id, "user", message_text)
    if result is None:
        return web.json_response({"error": f"session `{session_id}` not found"}, status=404)

    runtime = store.get_runtime(session_id)
    if runtime is None:
        return web.json_response({"error": f"no runtime for session `{session_id}`"}, status=500)

    loop = asyncio.get_event_loop()

    def _event_cb(event: dict[str, Any]) -> None:
        store._notify(session_id, {"type": "agent_event", "session_id": session_id, "event": event})

    try:
        summary = await loop.run_in_executor(
            None, lambda: runtime.orchestrate(message_text, event_callback=_event_cb),
        )
    except Exception as exc:
        store.add_message(session_id, "assistant", f"Error: {exc}")
        return web.json_response({"error": str(exc)}, status=500)

    store.add_message(session_id, "assistant", summary.final_text)
    return web.json_response({
        "final_text": summary.final_text,
        "iterations": summary.iterations,
    })


async def handle_stream_events(request: Any) -> Any:
    web = _require_aiohttp()
    store: SessionStore = request.app["store"]
    session_id = request.match_info["id"]
    session = store.get_session(session_id)
    if session is None:
        return web.json_response({"error": f"session `{session_id}` not found"}, status=404)

    queue = store.subscribe(session_id)
    if queue is None:
        return web.json_response({"error": f"session `{session_id}` not found"}, status=404)

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
    await response.prepare(request)

    snapshot_event = {
        "type": "snapshot",
        "session_id": session_id,
        "session": {"messages": session["messages"]},
    }
    await response.write(f"event: snapshot\ndata: {json.dumps(snapshot_event)}\n\n".encode())

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
                event_type = event.get("type", "message")
                await response.write(f"event: {event_type}\ndata: {json.dumps(event)}\n\n".encode())
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionError):
        pass
    finally:
        store.unsubscribe(session_id, queue)

    return response


def run_server(host: str = "127.0.0.1", port: int = 8080, workspace_root: Path | None = None) -> None:
    web = _require_aiohttp()
    app = create_app(workspace_root)
    web.run_app(app, host=host, port=port)
