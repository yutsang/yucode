"""Lightweight metrics collection for tool usage, session stats, and security events.

Data is held in memory and can be serialized to JSON for post-hoc analysis.
The runtime wires MetricsCollector into _execute_tool and run_turn.
Audit events are optionally persisted to append-only JSONL files.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.session import Usage

_log = logging.getLogger("yucode.metrics")


@dataclass
class ToolMetrics:
    name: str
    call_count: int = 0
    total_duration_ms: float = 0.0
    error_count: int = 0

    @property
    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / self.call_count if self.call_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "call_count": self.call_count,
            "total_duration_ms": round(self.total_duration_ms, 1),
            "avg_duration_ms": round(self.avg_duration_ms, 1),
            "error_count": self.error_count,
        }


@dataclass
class SessionMetrics:
    iterations: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "iterations": self.iterations,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
        }


@dataclass
class SecurityEvent:
    timestamp: float
    event_type: str
    tool_name: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "tool_name": self.tool_name,
            "detail": self.detail,
        }


class AuditLogger:
    """Append-only JSONL audit log under .yucode/audit/."""

    def __init__(self, workspace_root: Path | None = None, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._workspace_root = workspace_root

    def log(self, event: dict[str, Any]) -> None:
        if not self._enabled or not self._workspace_root:
            return
        try:
            audit_dir = self._workspace_root / ".yucode" / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y-%m-%d")
            path = audit_dir / f"{date_str}.jsonl"
            entry = {**event, "logged_at": datetime.now().isoformat()}
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            _log.debug("Audit log write failed: %s", exc)


class MetricsCollector:
    """Collects per-tool and per-session metrics plus security events."""

    def __init__(
        self,
        *,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._tool_metrics: dict[str, ToolMetrics] = {}
        self._session_metrics = SessionMetrics()
        self._security_events: list[SecurityEvent] = []
        self._audit = audit_logger or AuditLogger(enabled=False)

    def record_tool_call(
        self,
        tool_name: str,
        duration_seconds: float,
        *,
        is_error: bool = False,
    ) -> None:
        if tool_name not in self._tool_metrics:
            self._tool_metrics[tool_name] = ToolMetrics(name=tool_name)
        metrics = self._tool_metrics[tool_name]
        metrics.call_count += 1
        metrics.total_duration_ms += duration_seconds * 1000
        if is_error:
            metrics.error_count += 1

    def record_session(self, iterations: int, usage: Usage) -> None:
        self._session_metrics.iterations += iterations
        self._session_metrics.total_input_tokens += usage.input_tokens
        self._session_metrics.total_output_tokens += usage.output_tokens

    def record_security_event(
        self,
        event_type: str,
        tool_name: str,
        detail: str = "",
    ) -> None:
        event = SecurityEvent(
            timestamp=time.time(),
            event_type=event_type,
            tool_name=tool_name,
            detail=detail,
        )
        self._security_events.append(event)
        self._audit.log({
            "type": "security_event",
            "event_type": event_type,
            "tool_name": tool_name,
            "detail": detail,
        })

    @property
    def tool_metrics(self) -> dict[str, ToolMetrics]:
        return dict(self._tool_metrics)

    @property
    def session_metrics(self) -> SessionMetrics:
        return self._session_metrics

    @property
    def security_events(self) -> list[SecurityEvent]:
        return list(self._security_events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": {name: m.to_dict() for name, m in sorted(self._tool_metrics.items())},
            "session": self._session_metrics.to_dict(),
            "security_events": [e.to_dict() for e in self._security_events],
        }

    def save(self, workspace_root: Path, filename: str = "metrics.json") -> Path:
        metrics_dir = workspace_root / ".yucode" / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        path = metrics_dir / filename
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path
