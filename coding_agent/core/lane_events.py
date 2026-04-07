"""Lane lifecycle events for orchestration tracking.

Port of ``claw-code-main/rust/crates/runtime/src/lane_events.rs``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LaneEventName(str, Enum):
    STARTED = "lane.started"
    READY = "lane.ready"
    PROMPT_MISDELIVERY = "lane.prompt_misdelivery"
    BLOCKED = "lane.blocked"
    RED = "lane.red"
    GREEN = "lane.green"
    COMMIT_CREATED = "lane.commit.created"
    PR_OPENED = "lane.pr.opened"
    MERGE_READY = "lane.merge.ready"
    FINISHED = "lane.finished"
    FAILED = "lane.failed"
    RECONCILED = "lane.reconciled"
    MERGED = "lane.merged"
    SUPERSEDED = "lane.superseded"
    CLOSED = "lane.closed"
    BRANCH_STALE_AGAINST_MAIN = "branch.stale_against_main"


class LaneEventStatus(str, Enum):
    RUNNING = "running"
    READY = "ready"
    BLOCKED = "blocked"
    RED = "red"
    GREEN = "green"
    COMPLETED = "completed"
    FAILED = "failed"
    RECONCILED = "reconciled"
    MERGED = "merged"
    SUPERSEDED = "superseded"
    CLOSED = "closed"


class LaneFailureClass(str, Enum):
    PROMPT_DELIVERY = "prompt_delivery"
    TRUST_GATE = "trust_gate"
    BRANCH_DIVERGENCE = "branch_divergence"
    COMPILE = "compile"
    TEST = "test"
    PLUGIN_STARTUP = "plugin_startup"
    MCP_STARTUP = "mcp_startup"
    MCP_HANDSHAKE = "mcp_handshake"
    GATEWAY_ROUTING = "gateway_routing"
    TOOL_RUNTIME = "tool_runtime"
    INFRA = "infra"


@dataclass
class LaneEventBlocker:
    failure_class: LaneFailureClass
    detail: str = ""


@dataclass
class LaneCommitProvenance:
    commit: str
    branch: str
    worktree: str = ""
    canonical_commit: str = ""
    superseded_by: str = ""
    lineage: list[str] = field(default_factory=list)


@dataclass
class LaneEvent:
    event: LaneEventName
    status: LaneEventStatus
    emitted_at: float = field(default_factory=time.time)
    failure_class: LaneFailureClass | None = None
    detail: str = ""
    data: dict[str, Any] | None = None

    @classmethod
    def started(cls, emitted_at: float | None = None) -> LaneEvent:
        return cls(event=LaneEventName.STARTED, status=LaneEventStatus.RUNNING, emitted_at=emitted_at or time.time())

    @classmethod
    def finished(cls, detail: str = "", emitted_at: float | None = None) -> LaneEvent:
        return cls(event=LaneEventName.FINISHED, status=LaneEventStatus.COMPLETED, detail=detail, emitted_at=emitted_at or time.time())

    @classmethod
    def commit_created(cls, detail: str, provenance: LaneCommitProvenance, emitted_at: float | None = None) -> LaneEvent:
        return cls(
            event=LaneEventName.COMMIT_CREATED, status=LaneEventStatus.RUNNING,
            detail=detail, emitted_at=emitted_at or time.time(),
            data={"commit": provenance.commit, "branch": provenance.branch, "worktree": provenance.worktree},
        )

    @classmethod
    def superseded(cls, detail: str, provenance: LaneCommitProvenance, emitted_at: float | None = None) -> LaneEvent:
        return cls(
            event=LaneEventName.SUPERSEDED, status=LaneEventStatus.SUPERSEDED,
            detail=detail, emitted_at=emitted_at or time.time(),
            data={"superseded_by": provenance.superseded_by, "commit": provenance.commit},
        )

    @classmethod
    def blocked(cls, blocker: LaneEventBlocker, emitted_at: float | None = None) -> LaneEvent:
        return cls(
            event=LaneEventName.BLOCKED, status=LaneEventStatus.BLOCKED,
            failure_class=blocker.failure_class, detail=blocker.detail, emitted_at=emitted_at or time.time(),
        )

    @classmethod
    def failed(cls, blocker: LaneEventBlocker, emitted_at: float | None = None) -> LaneEvent:
        return cls(
            event=LaneEventName.FAILED, status=LaneEventStatus.FAILED,
            failure_class=blocker.failure_class, detail=blocker.detail, emitted_at=emitted_at or time.time(),
        )

    def with_detail(self, detail: str) -> LaneEvent:
        self.detail = detail
        return self

    def with_data(self, data: dict[str, Any]) -> LaneEvent:
        self.data = data
        return self

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"event": self.event.value, "status": self.status.value, "emitted_at": self.emitted_at}
        if self.failure_class:
            d["failure_class"] = self.failure_class.value
        if self.detail:
            d["detail"] = self.detail
        if self.data:
            d["data"] = self.data
        return d


def dedupe_superseded_commit_events(events: list[LaneEvent]) -> list[LaneEvent]:
    """Keep only the latest CommitCreated per canonical commit key."""
    latest_index_by_commit: dict[str, int] = {}
    for idx, ev in enumerate(events):
        if ev.event == LaneEventName.COMMIT_CREATED and ev.data:
            latest_index_by_commit[ev.data.get("commit", "")] = idx

    result: list[LaneEvent] = []
    for idx, ev in enumerate(events):
        if ev.event == LaneEventName.COMMIT_CREATED and ev.data:
            key = ev.data.get("commit", "")
            if latest_index_by_commit.get(key) != idx:
                continue
        result.append(ev)
    return result
