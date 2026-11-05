"""In-memory worker lifecycle registry with state machine.

Port of ``claw-code-main/rust/crates/runtime/src/worker_boot.rs``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class WorkerStatus(str, Enum):
    SPAWNING = "spawning"
    TRUST_REQUIRED = "trust_required"
    READY_FOR_PROMPT = "ready_for_prompt"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"


class WorkerFailureKind(str, Enum):
    TRUST_GATE = "trust_gate"
    PROMPT_DELIVERY = "prompt_delivery"
    PROTOCOL = "protocol"
    PROVIDER = "provider"


class WorkerEventKind(str, Enum):
    SPAWNING = "spawning"
    TRUST_REQUIRED = "trust_required"
    TRUST_RESOLVED = "trust_resolved"
    READY_FOR_PROMPT = "ready_for_prompt"
    PROMPT_MISDELIVERY = "prompt_misdelivery"
    RUNNING = "running"
    RESTARTED = "restarted"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass
class WorkerEvent:
    seq: int
    kind: WorkerEventKind
    status: WorkerStatus
    detail: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class WorkerFailure:
    kind: WorkerFailureKind
    message: str
    created_at: float = field(default_factory=time.time)


@dataclass
class Worker:
    worker_id: str
    cwd: str
    status: WorkerStatus = WorkerStatus.SPAWNING
    trust_auto_resolve: bool = False
    trust_gate_cleared: bool = False
    auto_recover_prompt_misdelivery: bool = False
    prompt_delivery_attempts: int = 0
    last_prompt: str = ""
    replay_prompt: str = ""
    last_error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    events: list[WorkerEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "cwd": self.cwd,
            "status": self.status.value,
            "trust_gate_cleared": self.trust_gate_cleared,
            "prompt_delivery_attempts": self.prompt_delivery_attempts,
            "last_error": self.last_error,
            "event_count": len(self.events),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class WorkerReadySnapshot:
    worker_id: str
    status: WorkerStatus
    ready: bool
    blocked: bool
    replay_prompt_ready: bool
    last_error: str = ""


class WorkerRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, Worker] = {}
        self._counter: int = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"worker_{self._counter}"

    def _push_event(self, worker: Worker, kind: WorkerEventKind, detail: str = "") -> None:
        seq = len(worker.events) + 1
        worker.events.append(WorkerEvent(seq=seq, kind=kind, status=worker.status, detail=detail))
        worker.updated_at = time.time()

    def create(
        self,
        cwd: str,
        trusted_roots: list[str] | None = None,
        auto_recover_prompt_misdelivery: bool = False,
    ) -> Worker:
        with self._lock:
            worker_id = self._next_id()
            trust_auto = False
            if trusted_roots:
                for root in trusted_roots:
                    if cwd.startswith(root):
                        trust_auto = True
                        break
            worker = Worker(
                worker_id=worker_id,
                cwd=cwd,
                trust_auto_resolve=trust_auto,
                auto_recover_prompt_misdelivery=auto_recover_prompt_misdelivery,
            )
            self._push_event(worker, WorkerEventKind.SPAWNING)
            self._workers[worker_id] = worker
            return worker

    def get(self, worker_id: str) -> Worker | None:
        with self._lock:
            return self._workers.get(worker_id)

    def resolve_trust(self, worker_id: str) -> Worker:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise KeyError(f"Worker `{worker_id}` not found")
            if worker.status != WorkerStatus.TRUST_REQUIRED:
                raise ValueError(f"Worker `{worker_id}` is not in trust_required state")
            worker.trust_gate_cleared = True
            worker.status = WorkerStatus.READY_FOR_PROMPT
            self._push_event(worker, WorkerEventKind.TRUST_RESOLVED)
            self._push_event(worker, WorkerEventKind.READY_FOR_PROMPT)
            return worker

    def send_prompt(self, worker_id: str, prompt: str | None = None) -> Worker:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise KeyError(f"Worker `{worker_id}` not found")
            if worker.status != WorkerStatus.READY_FOR_PROMPT:
                raise ValueError(f"Worker `{worker_id}` is not in ready_for_prompt state")
            effective_prompt = (prompt or "").strip() or worker.replay_prompt
            worker.last_prompt = effective_prompt
            worker.prompt_delivery_attempts += 1
            worker.status = WorkerStatus.RUNNING
            worker.replay_prompt = ""
            self._push_event(worker, WorkerEventKind.RUNNING, detail=effective_prompt[:200])
            return worker

    def observe(self, worker_id: str, screen_text: str = "") -> Worker:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise KeyError(f"Worker `{worker_id}` not found")
            lower = screen_text.lower()
            if worker.status == WorkerStatus.SPAWNING:
                if "trust" in lower or "permission" in lower:
                    if worker.trust_auto_resolve:
                        worker.trust_gate_cleared = True
                        worker.status = WorkerStatus.READY_FOR_PROMPT
                        self._push_event(worker, WorkerEventKind.TRUST_RESOLVED, "auto")
                        self._push_event(worker, WorkerEventKind.READY_FOR_PROMPT)
                    else:
                        worker.status = WorkerStatus.TRUST_REQUIRED
                        self._push_event(worker, WorkerEventKind.TRUST_REQUIRED)
                elif "ready" in lower or "prompt" in lower:
                    worker.status = WorkerStatus.READY_FOR_PROMPT
                    self._push_event(worker, WorkerEventKind.READY_FOR_PROMPT)
            elif worker.status == WorkerStatus.RUNNING and ("error" in lower or "failed" in lower):
                if worker.auto_recover_prompt_misdelivery and worker.prompt_delivery_attempts < 3:
                    worker.replay_prompt = worker.last_prompt
                    worker.status = WorkerStatus.READY_FOR_PROMPT
                    self._push_event(worker, WorkerEventKind.PROMPT_MISDELIVERY, screen_text[:200])
                else:
                    worker.status = WorkerStatus.FAILED
                    worker.last_error = screen_text[:500]
                    self._push_event(worker, WorkerEventKind.FAILED, screen_text[:200])
            return worker

    def await_ready(self, worker_id: str) -> WorkerReadySnapshot:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise KeyError(f"Worker `{worker_id}` not found")
            return WorkerReadySnapshot(
                worker_id=worker.worker_id,
                status=worker.status,
                ready=worker.status == WorkerStatus.READY_FOR_PROMPT,
                blocked=worker.status in (WorkerStatus.TRUST_REQUIRED, WorkerStatus.FAILED),
                replay_prompt_ready=bool(worker.replay_prompt),
                last_error=worker.last_error,
            )

    def restart(self, worker_id: str) -> Worker:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise KeyError(f"Worker `{worker_id}` not found")
            worker.status = WorkerStatus.SPAWNING
            worker.last_error = ""
            worker.prompt_delivery_attempts = 0
            self._push_event(worker, WorkerEventKind.RESTARTED)
            return worker

    def terminate(self, worker_id: str) -> Worker:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise KeyError(f"Worker `{worker_id}` not found")
            worker.status = WorkerStatus.FINISHED
            self._push_event(worker, WorkerEventKind.FINISHED, "terminated")
            return worker

    def observe_completion(
        self, worker_id: str, finish_reason: str = "", tokens_output: int = 0,
    ) -> Worker:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise KeyError(f"Worker `{worker_id}` not found")
            if finish_reason in ("unknown", "error", "") or tokens_output == 0:
                worker.status = WorkerStatus.FAILED
                worker.last_error = f"Provider failure: {finish_reason}"
                self._push_event(worker, WorkerEventKind.FAILED, finish_reason)
            else:
                worker.status = WorkerStatus.FINISHED
                self._push_event(worker, WorkerEventKind.FINISHED, finish_reason)
            return worker

    def __len__(self) -> int:
        with self._lock:
            return len(self._workers)


_global_worker_registry: WorkerRegistry | None = None
_global_lock = threading.Lock()


def global_worker_registry() -> WorkerRegistry:
    global _global_worker_registry
    with _global_lock:
        if _global_worker_registry is None:
            _global_worker_registry = WorkerRegistry()
        return _global_worker_registry
