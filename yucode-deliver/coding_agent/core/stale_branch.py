"""Git-based branch freshness detection.

Port of ``claw-code-main/rust/crates/runtime/src/stale_branch.rs``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class BranchFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    DIVERGED = "diverged"


class StaleBranchPolicy(str, Enum):
    AUTO_REBASE = "auto_rebase"
    AUTO_MERGE_FORWARD = "auto_merge_forward"
    WARN_ONLY = "warn_only"
    BLOCK = "block"


class StaleBranchAction(str, Enum):
    NOOP = "noop"
    WARN = "warn"
    BLOCK = "block"
    REBASE = "rebase"
    MERGE_FORWARD = "merge_forward"


@dataclass
class FreshnessResult:
    freshness: BranchFreshness
    commits_behind: int = 0
    commits_ahead: int = 0
    missing_fixes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"freshness": self.freshness.value, "commits_behind": self.commits_behind, "commits_ahead": self.commits_ahead}
        if self.missing_fixes:
            d["missing_fixes"] = self.missing_fixes
        return d


def check_freshness(branch: str, main_ref: str = "main", repo_path: str | Path = ".") -> FreshnessResult:
    """Check how fresh ``branch`` is relative to ``main_ref``."""
    repo = str(repo_path)
    behind = _rev_list_count(f"{branch}..{main_ref}", repo)
    ahead = _rev_list_count(f"{main_ref}..{branch}", repo)

    if behind == 0:
        return FreshnessResult(freshness=BranchFreshness.FRESH, commits_ahead=ahead)

    missing = _missing_fix_subjects(main_ref, branch, repo, limit=5)

    if ahead == 0:
        return FreshnessResult(freshness=BranchFreshness.STALE, commits_behind=behind, missing_fixes=missing)

    return FreshnessResult(freshness=BranchFreshness.DIVERGED, commits_behind=behind, commits_ahead=ahead, missing_fixes=missing)


def apply_policy(result: FreshnessResult, policy: StaleBranchPolicy) -> tuple[StaleBranchAction, str]:
    if result.freshness == BranchFreshness.FRESH:
        return StaleBranchAction.NOOP, ""

    msg = f"Branch is {result.commits_behind} commit(s) behind {('and ' + str(result.commits_ahead) + ' ahead') if result.commits_ahead else ''}"
    if result.missing_fixes:
        msg += f"; missing fixes: {', '.join(result.missing_fixes[:3])}"

    if policy == StaleBranchPolicy.BLOCK:
        return StaleBranchAction.BLOCK, msg
    if policy == StaleBranchPolicy.AUTO_REBASE:
        return StaleBranchAction.REBASE, msg
    if policy == StaleBranchPolicy.AUTO_MERGE_FORWARD:
        return StaleBranchAction.MERGE_FORWARD, msg
    return StaleBranchAction.WARN, msg


def _rev_list_count(range_spec: str, repo: str) -> int:
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", range_spec],
            capture_output=True, text=True, timeout=10, cwd=repo,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except Exception:
        return 0


def _missing_fix_subjects(main_ref: str, branch: str, repo: str, limit: int = 5) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"--max-count={limit}", f"{branch}..{main_ref}"],
            capture_output=True, text=True, timeout=10, cwd=repo,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [line.strip() for line in result.stdout.strip().splitlines()[:limit]]
    except Exception:
        pass
    return []
