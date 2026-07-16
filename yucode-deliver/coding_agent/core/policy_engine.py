"""Automation policy engine for lane orchestration.

Port of ``claw-code-main/rust/crates/runtime/src/policy_engine.rs``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_STALE_BRANCH_THRESHOLD_SECONDS = 3600


class PolicyCondition(str, Enum):
    GREEN_AT = "green_at"
    STALE_BRANCH = "stale_branch"
    STARTUP_BLOCKED = "startup_blocked"
    LANE_COMPLETED = "lane_completed"
    LANE_RECONCILED = "lane_reconciled"
    REVIEW_PASSED = "review_passed"
    SCOPED_DIFF = "scoped_diff"
    TIMED_OUT = "timed_out"


class PolicyAction(str, Enum):
    MERGE_TO_DEV = "merge_to_dev"
    MERGE_FORWARD = "merge_forward"
    RECOVER_ONCE = "recover_once"
    ESCALATE = "escalate"
    CLOSEOUT_LANE = "closeout_lane"
    CLEANUP_SESSION = "cleanup_session"
    RECONCILE = "reconcile"
    NOTIFY = "notify"
    BLOCK = "block"


class ReconcileReason(str, Enum):
    ALREADY_MERGED = "already_merged"
    SUPERSEDED = "superseded"
    EMPTY_DIFF = "empty_diff"
    MANUAL_CLOSE = "manual_close"


class LaneBlocker(str, Enum):
    NONE = "none"
    STARTUP = "startup"
    EXTERNAL = "external"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DiffScope(str, Enum):
    FULL = "full"
    SCOPED = "scoped"


@dataclass
class LaneContext:
    lane_id: str
    green_level: int = 0
    branch_freshness_seconds: float = 0.0
    blocker: LaneBlocker = LaneBlocker.NONE
    review_status: ReviewStatus = ReviewStatus.PENDING
    diff_scope: DiffScope = DiffScope.FULL
    completed: bool = False
    reconciled: bool = False

    @classmethod
    def reconciled_context(cls, lane_id: str) -> LaneContext:
        return cls(lane_id=lane_id, completed=True, reconciled=True)


@dataclass
class PolicyRule:
    name: str
    condition: PolicyCondition
    action: PolicyAction
    priority: int = 0
    green_level_required: int = 0
    timeout_seconds: float = 0.0
    escalation_reason: str = ""
    reconcile_reason: ReconcileReason | None = None
    notify_channel: str = ""

    def matches(self, context: LaneContext) -> bool:
        if self.condition == PolicyCondition.GREEN_AT:
            return context.green_level >= self.green_level_required
        if self.condition == PolicyCondition.STALE_BRANCH:
            return context.branch_freshness_seconds >= _STALE_BRANCH_THRESHOLD_SECONDS
        if self.condition == PolicyCondition.STARTUP_BLOCKED:
            return context.blocker != LaneBlocker.NONE
        if self.condition == PolicyCondition.LANE_COMPLETED:
            return context.completed
        if self.condition == PolicyCondition.LANE_RECONCILED:
            return context.reconciled
        if self.condition == PolicyCondition.REVIEW_PASSED:
            return context.review_status == ReviewStatus.APPROVED
        if self.condition == PolicyCondition.SCOPED_DIFF:
            return context.diff_scope == DiffScope.SCOPED
        if self.condition == PolicyCondition.TIMED_OUT:
            return context.branch_freshness_seconds >= self.timeout_seconds
        return False


class PolicyEngine:
    def __init__(self, rules: list[PolicyRule] | None = None) -> None:
        self.rules = sorted(rules or [], key=lambda r: r.priority)

    def evaluate(self, context: LaneContext) -> list[PolicyAction]:
        return [rule.action for rule in self.rules if rule.matches(context)]
