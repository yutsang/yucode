"""Branch lock collision detection for parallel lanes.

Port of ``claw-code-main/rust/crates/runtime/src/branch_lock.rs``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BranchLockIntent:
    lane_id: str
    branch: str
    worktree: str = ""
    modules: list[str] = field(default_factory=list)


@dataclass
class BranchLockCollision:
    branch: str
    module: str
    lane_ids: list[str] = field(default_factory=list)


def detect_branch_lock_collisions(intents: list[BranchLockIntent]) -> list[BranchLockCollision]:
    """Detect same-branch intents with overlapping module scopes."""
    collisions: list[BranchLockCollision] = []
    seen: set[tuple[str, str, str]] = set()

    for i, a in enumerate(intents):
        for b in intents[i + 1:]:
            if a.branch != b.branch:
                continue
            a_modules = a.modules or [""]
            b_modules = b.modules or [""]
            for am in a_modules:
                for bm in b_modules:
                    if _modules_overlap(am, bm):
                        key = (a.branch, min(am, bm), max(am, bm))
                        if key not in seen:
                            seen.add(key)
                            lane_ids = sorted({a.lane_id, b.lane_id})
                            collisions.append(BranchLockCollision(
                                branch=a.branch,
                                module=am or bm or "(root)",
                                lane_ids=lane_ids,
                            ))
    return sorted(collisions, key=lambda c: (c.branch, c.module))


def _modules_overlap(a: str, b: str) -> bool:
    if not a or not b:
        return True
    return a.startswith(b) or b.startswith(a)
