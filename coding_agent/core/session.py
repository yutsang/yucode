from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_log = logging.getLogger("yucode.session")

Role = Literal["system", "user", "assistant", "tool"]
SESSION_VERSION = 1


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class Message:
    role: Role
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_provider_message(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.role == "assistant" and self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                }
                for tool_call in self.tool_calls
            ]
        if self.role == "tool" and self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in self.tool_calls
            ]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        tool_calls = [
            ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
            for tc in d.get("tool_calls", [])
        ]
        return cls(
            role=d["role"],
            content=d.get("content", ""),
            tool_call_id=d.get("tool_call_id"),
            tool_calls=tool_calls,
        )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens

    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Usage:
        return cls(
            input_tokens=int(d.get("input_tokens", 0)),
            output_tokens=int(d.get("output_tokens", 0)),
            cache_creation_input_tokens=int(d.get("cache_creation_input_tokens", 0)),
            cache_read_input_tokens=int(d.get("cache_read_input_tokens", 0)),
        )


class UsageTracker:
    def __init__(self) -> None:
        self._turns: int = 0
        self._cumulative: Usage = Usage()

    @classmethod
    def from_session(cls, session: Session) -> UsageTracker:
        tracker = cls()
        tracker._cumulative = Usage(
            input_tokens=session.usage.input_tokens,
            output_tokens=session.usage.output_tokens,
            cache_creation_input_tokens=session.usage.cache_creation_input_tokens,
            cache_read_input_tokens=session.usage.cache_read_input_tokens,
        )
        tracker._turns = sum(1 for m in session.messages if m.role == "assistant")
        return tracker

    @property
    def turns(self) -> int:
        return self._turns

    @property
    def total_input_tokens(self) -> int:
        return self._cumulative.input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self._cumulative.output_tokens

    def cumulative_usage(self) -> Usage:
        return self._cumulative

    def cost_summary(self) -> dict[str, Any]:
        """Structured cost/usage summary for CLI and JSON output."""
        return {
            "turns": self._turns,
            "total_tokens": self._cumulative.total_tokens(),
            "input_tokens": self._cumulative.input_tokens,
            "output_tokens": self._cumulative.output_tokens,
            "cache_creation_input_tokens": self._cumulative.cache_creation_input_tokens,
            "cache_read_input_tokens": self._cumulative.cache_read_input_tokens,
        }

    def record(self, usage: Usage) -> None:
        self._turns += 1
        self._cumulative.add(usage)


@dataclass
class AssistantResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


@dataclass
class Session:
    messages: list[Message] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    version: int = SESSION_VERSION
    created_at: float = field(default_factory=time.time)
    model: str = ""

    def add_message(self, message: Message) -> None:
        self.messages.append(message)

    def provider_messages(self, system_prompt: str) -> list[dict[str, Any]]:
        return [{"role": "system", "content": system_prompt}] + [
            message.to_provider_message() for message in self.messages
        ]

    # ---- persistence -------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self.version,
            "created_at": self.created_at,
            "model": self.model,
            "usage": self.usage.to_dict(),
            "messages": [m.to_dict() for m in self.messages],
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Session:
        data = json.loads(path.read_text(encoding="utf-8"))
        messages = [Message.from_dict(m) for m in data.get("messages", [])]
        usage = Usage.from_dict(data.get("usage", {}))
        return cls(
            messages=messages,
            usage=usage,
            version=data.get("version", SESSION_VERSION),
            created_at=data.get("created_at", 0.0),
            model=data.get("model", ""),
        )

    @staticmethod
    def sessions_dir(workspace: Path) -> Path:
        from ..config.settings import state_dir
        return state_dir(workspace) / "sessions"

    def save_to_workspace(self, workspace: Path, session_id: str) -> Path:
        path = self.sessions_dir(workspace) / f"{session_id}.json"
        self.save(path)
        return path

    @classmethod
    def load_from_workspace(cls, workspace: Path, session_id: str) -> Session:
        path = cls.sessions_dir(workspace) / f"{session_id}.json"
        return cls.load(path)

    @classmethod
    def list_sessions(cls, workspace: Path) -> list[dict[str, Any]]:
        sessions_dir = cls.sessions_dir(workspace)
        if not sessions_dir.is_dir():
            return []
        entries = []
        for f in sorted(sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                messages = data.get("messages", [])
                title = ""
                for m in messages:
                    if isinstance(m, dict) and m.get("role") == "user":
                        raw = (m.get("content") or "").strip()
                        title = raw[:80] + ("…" if len(raw) > 80 else "")
                        break
                entries.append({
                    "id": f.stem,
                    "created_at": data.get("created_at", 0),
                    "model": data.get("model", ""),
                    "message_count": len(messages),
                    "title": title,
                })
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning("Skipping corrupt session file %s: %s", f.name, exc)
                continue
        return entries
