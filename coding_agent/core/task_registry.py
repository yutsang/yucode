"""In-memory task lifecycle registry.

Port of ``claw-code-main/rust/crates/runtime/src/task_registry.rs``.
Thread-safe CRUD over ``Task`` records with lifecycle states.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED)


@dataclass
class TaskMessage:
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class Task:
    task_id: str
    prompt: str
    description: str = ""
    status: TaskStatus = TaskStatus.CREATED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    messages: list[TaskMessage] = field(default_factory=list)
    output: str = ""
    team_id: str = ""
    task_packet: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "prompt": self.prompt,
            "description": self.description,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.messages),
            "output_length": len(self.output),
            "team_id": self.team_id,
        }


class TaskRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._counter: int = 0

    def _next_id(self) -> str:
        self._counter += 1
        ts_hex = hex(int(time.time()))[2:]
        return f"task_{ts_hex}_{self._counter}"

    def create(self, prompt: str, description: str = "") -> Task:
        with self._lock:
            task_id = self._next_id()
            task = Task(task_id=task_id, prompt=prompt, description=description)
            self._tasks[task_id] = task
            return task

    def create_from_packet(self, packet: dict[str, Any]) -> Task:
        objective = str(packet.get("objective", "")).strip()
        if not objective:
            raise ValueError("Task packet must have a non-empty objective")
        with self._lock:
            task_id = self._next_id()
            task = Task(
                task_id=task_id,
                prompt=objective,
                description=str(packet.get("scope", "")),
                task_packet=packet,
            )
            self._tasks[task_id] = task
            return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return t

    def list(self, status_filter: TaskStatus | None = None) -> list[Task]:
        with self._lock:
            tasks = list(self._tasks.values())
        if status_filter is not None:
            tasks = [t for t in tasks if t.status == status_filter]
        return tasks

    def stop(self, task_id: str) -> Task:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"Task `{task_id}` not found")
            if task.status.is_terminal:
                raise ValueError(f"Task `{task_id}` is already in terminal state `{task.status.value}`")
            task.status = TaskStatus.STOPPED
            task.updated_at = time.time()
            return task

    def update(self, task_id: str, message: str) -> Task:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"Task `{task_id}` not found")
            task.messages.append(TaskMessage(role="system", content=message))
            task.updated_at = time.time()
            return task

    def output(self, task_id: str) -> str:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"Task `{task_id}` not found")
            return task.output

    def append_output(self, task_id: str, text: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"Task `{task_id}` not found")
            task.output += text
            task.updated_at = time.time()

    def set_status(self, task_id: str, status: TaskStatus) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"Task `{task_id}` not found")
            task.status = status
            task.updated_at = time.time()

    def assign_team(self, task_id: str, team_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"Task `{task_id}` not found")
            task.team_id = team_id
            task.updated_at = time.time()

    def remove(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.pop(task_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)


_global_task_registry: TaskRegistry | None = None
_global_lock = threading.Lock()


def global_task_registry() -> TaskRegistry:
    global _global_task_registry
    with _global_lock:
        if _global_task_registry is None:
            _global_task_registry = TaskRegistry()
        return _global_task_registry
