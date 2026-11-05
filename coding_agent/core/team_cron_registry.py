"""In-memory Team and Cron registries.

Port of ``claw-code-main/rust/crates/runtime/src/team_cron_registry.rs``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TeamStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    DELETED = "deleted"


@dataclass
class Team:
    team_id: str
    name: str
    task_ids: list[str] = field(default_factory=list)
    status: TeamStatus = TeamStatus.CREATED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "name": self.name,
            "task_ids": list(self.task_ids),
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TeamRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._teams: dict[str, Team] = {}
        self._counter: int = 0

    def create(self, name: str, task_ids: list[str] | None = None) -> Team:
        with self._lock:
            self._counter += 1
            team_id = f"team_{self._counter}"
            team = Team(team_id=team_id, name=name, task_ids=list(task_ids or []))
            self._teams[team_id] = team
            return team

    def get(self, team_id: str) -> Team | None:
        with self._lock:
            return self._teams.get(team_id)

    def list(self) -> list[Team]:
        with self._lock:
            return list(self._teams.values())

    def delete(self, team_id: str) -> Team:
        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                raise KeyError(f"Team `{team_id}` not found")
            team.status = TeamStatus.DELETED
            team.updated_at = time.time()
            return team

    def __len__(self) -> int:
        with self._lock:
            return len(self._teams)


@dataclass
class CronEntry:
    cron_id: str
    schedule: str
    prompt: str
    description: str = ""
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_run_at: float | None = None
    run_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cron_id": self.cron_id,
            "schedule": self.schedule,
            "prompt": self.prompt,
            "description": self.description,
            "enabled": self.enabled,
            "run_count": self.run_count,
            "last_run_at": self.last_run_at,
            "created_at": self.created_at,
        }


class CronRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, CronEntry] = {}
        self._counter: int = 0

    def create(self, schedule: str, prompt: str, description: str = "") -> CronEntry:
        with self._lock:
            self._counter += 1
            cron_id = f"cron_{self._counter}"
            entry = CronEntry(cron_id=cron_id, schedule=schedule, prompt=prompt, description=description)
            self._entries[cron_id] = entry
            return entry

    def get(self, cron_id: str) -> CronEntry | None:
        with self._lock:
            return self._entries.get(cron_id)

    def list(self, enabled_only: bool = False) -> list[CronEntry]:
        with self._lock:
            entries = list(self._entries.values())
        if enabled_only:
            entries = [e for e in entries if e.enabled]
        return entries

    def delete(self, cron_id: str) -> CronEntry:
        with self._lock:
            entry = self._entries.pop(cron_id, None)
            if entry is None:
                raise KeyError(f"Cron entry `{cron_id}` not found")
            return entry

    def disable(self, cron_id: str) -> None:
        with self._lock:
            entry = self._entries.get(cron_id)
            if entry is None:
                raise KeyError(f"Cron entry `{cron_id}` not found")
            entry.enabled = False
            entry.updated_at = time.time()

    def record_run(self, cron_id: str) -> None:
        with self._lock:
            entry = self._entries.get(cron_id)
            if entry is None:
                raise KeyError(f"Cron entry `{cron_id}` not found")
            entry.run_count += 1
            entry.last_run_at = time.time()
            entry.updated_at = time.time()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_global_team_registry: TeamRegistry | None = None
_global_cron_registry: CronRegistry | None = None
_global_lock = threading.Lock()


def global_team_registry() -> TeamRegistry:
    global _global_team_registry
    with _global_lock:
        if _global_team_registry is None:
            _global_team_registry = TeamRegistry()
        return _global_team_registry


def global_cron_registry() -> CronRegistry:
    global _global_cron_registry
    with _global_lock:
        if _global_cron_registry is None:
            _global_cron_registry = CronRegistry()
        return _global_cron_registry
