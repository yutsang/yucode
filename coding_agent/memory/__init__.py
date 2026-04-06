"""Memory and context layer -- compaction, prompt assembly, skill discovery."""

from .compact import (
    CompactionConfig,
    CompactionResult,
    compact_session,
    estimate_session_tokens,
    should_compact,
)
from .skills import SkillInfo, list_skills, load_skill, skill_summaries_for_prompt

__all__ = [
    "CompactionConfig",
    "CompactionResult",
    "SkillInfo",
    "compact_session",
    "estimate_session_tokens",
    "list_skills",
    "load_skill",
    "should_compact",
    "skill_summaries_for_prompt",
]
