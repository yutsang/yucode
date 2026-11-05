"""Lightweight metrics collection for tool usage, session stats, and security events.

Data is held in memory and can be serialized to JSON for post-hoc analysis.
The runtime wires MetricsCollector into _execute_tool and run_turn.
Audit events are optionally persisted to append-only JSONL files.

Also home to the retention policy for the whole `state_dir` tree (see
`prune_state_subdir`): it lives here because the decision about what may be
deleted is the same decision as what the audit log is for, and because this
module is a leaf both `core.runtime` and `tools.shell` can import.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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

    def __init__(
        self,
        workspace_root: Path | None = None,
        *,
        enabled: bool = True,
        session_id: str | Callable[[], str] = "",
        subagent_id: str = "",
    ) -> None:
        self._enabled = enabled
        self._workspace_root = workspace_root
        # Stamped on every row. Without it an audit line cannot be tied to the
        # session file or compaction archive it belongs to, which is most of
        # what an audit trail is for: one day's log is otherwise an
        # undifferentiated pile of commands from every run that touched this
        # workspace, real and test alike.
        #
        # May be a callable, resolved per row rather than snapshotted here:
        # the runtime rebinds its Session on /clear and /resume, so an id
        # captured at construction goes stale on the first /clear and every
        # later row is filed under the *first* session -- while the metrics
        # snapshot and archives written beside it use the new one.
        self._session_id = session_id
        # Set only by a subagent runtime, which mints a throwaway Session of
        # its own. `session_id` above then carries the *parent's* id so the row
        # points at a session file that exists, and this says which worker
        # produced it -- an orphan uuid in the one field a reader correlates on
        # is worse than no field, but the worker's identity is still worth
        # keeping. A plain string, not a callable: a subagent's session is
        # never rebound (there is no /clear inside a worker).
        self._subagent_id = subagent_id

    def log(self, event: dict[str, Any]) -> None:
        if not self._enabled or not self._workspace_root:
            return
        try:
            from ..config.settings import state_dir
            audit_dir = state_dir(self._workspace_root) / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y-%m-%d")
            path = audit_dir / f"{date_str}.jsonl"
            session_id = self._session_id
            entry = {
                **event,
                "session_id": session_id() if callable(session_id) else session_id,
                "cwd": str(self._workspace_root),
                "logged_at": datetime.now().isoformat(),
            }
            if self._subagent_id:
                entry["subagent_id"] = self._subagent_id
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            _log.debug("Audit log write failed: %s", exc)


# ---------------------------------------------------------------------------
# Retention for the state directory
#
# Everything the agent spills under `state_dir` was written and never removed:
# a full copy of every over-cap tool result (`tool_output/`, 128 KB at a time
# from bash alone), every background command's log (`bash_output/`), a full
# pre-compaction transcript (`archives/`), one JSON per turn (`metrics/`) and
# one per checkpoint. The deployment target is a locked-down company Windows
# box with no admin rights, so no cleanup job can be installed there: if the
# agent does not bound its own state tree, nothing does.
#
# Two directories are deliberately absent from the table, and their absence is
# the policy -- see tests/test_audit_and_spill.py::TestAuditIsNotSwept:
#
# * `audit/` -- the trail answers "what did the agent do to this machine?" on
#   a box with no other audit mechanism, so deleting it destroys the evidence
#   exactly when there was most of it to keep. It also does not need the help:
#   a row is written only for a state-changing tool call and is a few hundred
#   bytes, already rotated one file per day, so a heavy day costs ~1 MB
#   against the tens of MB a single session can spill. Its growth is bounded
#   by actions actually taken, not by how much output a tool happened to
#   produce -- two orders of magnitude apart, and only one of them is
#   evidence.
# * `sessions/` -- the user's own conversation history, which `/resume` reads
#   back. Deleting it is deleting their work, not the agent's litter.
#
# Both limits below are per directory. A byte budget is what actually bounds
# the disk (file sizes here span 32 KB to many MB, so a file count would not);
# the age limit is what stops a directory nobody touches from holding stale
# data for years. Defaults, all deliberately generous enough that a single
# session never trips them:
#
#   tool_output  32 MB / 7 days  -- ~250 bash-sized (128 KB) spills. A spill is
#                                   a pointer the model reads back within the
#                                   session that produced it; past that it is
#                                   dead weight.
#   bash_output  32 MB / 7 days  -- same artefact, same reasoning.
#   archives     32 MB / 30 days -- ~80 pre-compaction transcripts at ~400 KB.
#                                   Compaction is rare (it may never fire in
#                                   real use), and an archive is forensic, so
#                                   it gets the longer window.
#   checkpoints   8 MB / 30 days -- never restored in real use to date.
#   metrics       8 MB / 30 days -- a few KB per session: thousands of runs.
#
# Override both with YUCODE_STATE_RETENTION_MB / _DAYS (0 or negative disables
# that half). One pair of knobs rather than ten: the realistic operator need is
# "yucode is using too much disk", not per-directory tuning.
_RETENTION_MB_ENV = "YUCODE_STATE_RETENTION_MB"
_RETENTION_DAYS_ENV = "YUCODE_STATE_RETENTION_DAYS"

_RETENTION: dict[str, tuple[int, int]] = {
    # <state_dir subdirectory>: (megabytes, days)
    "tool_output": (32, 7),
    "bash_output": (32, 7),
    "archives": (32, 30),
    "checkpoints": (8, 30),
    "metrics": (8, 30),
}

# A file this recently modified is treated as in use. Covers what
# `background_tasks` cannot: a foreground command's log before its process is
# registered, another thread's subagent mid-write, and the spill file whose
# path was handed to the model on the line above. A process actively writing
# keeps its mtime fresh, so this holds for as long as it produces output.
_IN_USE_GRACE_SECONDS = 300.0

# One sweep per directory per this many seconds, per process. The state tree
# can be a roaming-profile network path on the target box, where a stat-walk
# per bash call is not free -- and nothing accumulates fast enough in five
# minutes to be worth paying for it more often.
_SWEEP_INTERVAL_SECONDS = 300.0

_last_sweep: dict[str, float] = {}
_sweep_lock = threading.Lock()


def _env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def retention_limits(name: str) -> tuple[int, int] | None:
    """Return (max_bytes, max_age_days) for a `state_dir` subdirectory.

    None means the directory has no retention policy and must not be swept.
    """
    default = _RETENTION.get(name)
    if default is None:
        return None
    megabytes = max(_env_int(_RETENTION_MB_ENV, default[0]), 0)
    days = max(_env_int(_RETENTION_DAYS_ENV, default[1]), 0)
    return megabytes * 1024 * 1024, days


def sweep_state_dir(
    directory: Path,
    *,
    max_bytes: int,
    max_age_days: int,
    protected: Iterable[Path] = (),
) -> list[Path]:
    """Delete files under *directory*, oldest first, until it is within limits.

    Walks subdirectories too: `web_fetch` artifacts live one level below
    `tool_output/` and are the same kind of disposable pointer, so they share
    the same budget.

    Nothing in *protected* is touched, and neither is anything modified inside
    the in-use grace window. Deleting a file a live process still holds open
    is the one failure mode that would be worse than the disk usage: the
    runtime reads a background task's log back when the task finishes.
    """
    now = time.time()
    keep = set(protected)
    entries: list[tuple[float, int, Path]] = []
    for root, _dirs, names in os.walk(directory):
        for name in names:
            path = Path(root) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
    entries.sort(key=lambda e: e[0])

    total = sum(size for _, size, _ in entries)
    deleted: list[Path] = []
    for mtime, size, path in entries:
        if path in keep or now - mtime < _IN_USE_GRACE_SECONDS:
            continue
        too_old = max_age_days > 0 and now - mtime > max_age_days * 86_400
        over_budget = max_bytes > 0 and total > max_bytes
        if not (too_old or over_budget):
            # Sorted oldest first: everything after this is newer (so not too
            # old) and the total only shrinks, so there is nothing left to do.
            break
        try:
            path.unlink()
        except OSError:
            continue  # held open (Windows) or already gone -- retry next sweep
        total -= size
        deleted.append(path)
    return deleted


def prune_state_subdir(
    state_root: Path, name: str, *, protected: Iterable[Path] = (),
) -> None:
    """Throttled retention sweep of one `state_dir` subdirectory.

    Called by whichever writer owns the directory, just before it writes: that
    keeps the cost proportional to use (a directory nobody writes to is never
    walked) and needs no startup scan or background thread, which a CLI has
    nowhere to put. Never raises -- housekeeping must not fail the tool call
    that triggered it.
    """
    limits = retention_limits(name)
    if limits is None or not any(limits):
        # No policy, or an operator who turned both limits off: do not pay for
        # a stat-walk that cannot delete anything.
        return
    directory = state_root / name
    key = str(directory)
    now = time.monotonic()
    with _sweep_lock:
        last = _last_sweep.get(key)
        if last is not None and now - last < _SWEEP_INTERVAL_SECONDS:
            return
        _last_sweep[key] = now
    try:
        deleted = sweep_state_dir(
            directory, max_bytes=limits[0], max_age_days=limits[1], protected=protected,
        )
    except Exception as exc:  # noqa: BLE001 -- see docstring
        _log.debug("State retention sweep of %s failed: %s", directory, exc)
        return
    if deleted:
        _log.debug("Retention: removed %d file(s) from %s", len(deleted), directory)


# Workspace-write tools whose only effect is the agent's own bookkeeping.
#
# The audit log answers one question: on a machine with no other audit
# mechanism, what did the agent do to it? A tool earns a row when it can
# (a) change something the user owns, (b) execute a command, or (c) alter the
# agent's own authority. A tool that only rewrites its private scratch state
# under `state_dir` -- state that mirrors what the transcript already holds --
# answers none of that, and `todo_write` is the highest-frequency tool in a
# session, so its contentless `arg_keys: ["todos"]` rows bury the file writes
# and shell commands the log exists to preserve.
#
# Checked against every registered non-read-only tool; `todo_write` is the
# only one that qualifies today. Two near misses, kept audited on purpose:
# `config_write` writes agent-owned config but can flip the permission mode
# (rule c), and `memory_save`/`memory_delete` write durable instructions that
# steer later sessions, at workspace scope inside the user's own project
# directory (rule a).
_AGENT_STATE_ONLY_TOOLS = frozenset({"todo_write"})


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

    def record_tool_audit(
        self,
        tool_name: str,
        raw_arguments: str,
        output: str,
        *,
        is_error: bool,
        permission: str = "",
    ) -> None:
        """Persist a state-changing tool call to the audit trail.

        record_security_event only logs denials/redactions — this covers the
        common case (a command that actually ran), which is what makes the
        audit log useful as a local self-protection record on a machine with
        no other audit mechanism.

        *permission* is the tool's declared permission level; anything above
        read-only is recorded. The previous hardcoded trio missed every other
        writer — `apply_patch`, `fill_pptx_shape_text`, `fill_pptx_table`,
        `append_word`, `write_excel_cell`, `edit_notebook_cell` all changed
        files on disk and left no trace. Callers that don't pass a permission
        fall back to the original trio, so nothing silently stops logging.

        Tools in _AGENT_STATE_ONLY_TOOLS are excluded regardless -- see the
        rule there for why "declares workspace-write" is not on its own enough
        to be worth a row.
        """
        if tool_name in _AGENT_STATE_ONLY_TOOLS:
            return
        writes = permission in ("workspace-write", "danger-full-access")
        if not writes and tool_name not in ("bash", "write_file", "edit_file"):
            return
        try:
            args = json.loads(raw_arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        entry: dict[str, Any] = {"type": "tool_audit", "tool_name": tool_name, "is_error": is_error}
        if tool_name == "bash":
            entry["command"] = args.get("command", "")
            try:
                payload = json.loads(output)
                if isinstance(payload, dict) and "returncode" in payload:
                    entry["returncode"] = payload["returncode"]
            except (json.JSONDecodeError, TypeError):
                pass
        elif "path" in args:
            entry["path"] = args.get("path", "")
        else:
            # A writer with no `path` argument (e.g. edit_notebook_cell keyed
            # by cell index, or an MCP tool). Record the argument keys so the
            # row still says what was touched, without dumping payloads that
            # may be large or contain content already redacted elsewhere.
            entry["arg_keys"] = sorted(args)[:10]
        self._audit.log(entry)

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
        from ..config.settings import state_dir
        root = state_dir(workspace_root)
        # One snapshot per session, written every turn -- the accumulation
        # that first made this worth measuring (1,249 files after a single
        # day's work; see tests/conftest.py).
        prune_state_subdir(root, "metrics")
        metrics_dir = root / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        path = metrics_dir / filename
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path
