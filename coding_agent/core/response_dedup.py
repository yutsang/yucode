"""Post-process model output to remove repetitive multi-pass answers.

Weak-tier models (Qwen3-class) often emit the same answer 2-3 times in one
response, with slight rewording each pass. Observed examples:

  Pass 1: "The compaction mechanism in yucode is designed to manage..."
  Pass 2: "The compaction mechanism in yucode is designed to manage..."
  Pass 3: "The compaction mechanism in yucode is designed to manage..."

Each pass is separated by a double-newline. We detect "restart points" where
a paragraph's lead-in closely matches an earlier paragraph's lead-in, and keep
only the final pass (which tends to be the most polished).

Conservative defaults: only triggers on long responses (>=600 chars) with 3+
paragraphs and a clear restart signal — to avoid mangling normal answers that
happen to have section repetition.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace + strip — used for similarity input."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _similarity(a: str, b: str) -> float:
    """Ratio in [0, 1] between two normalized strings."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def dedup_repetitive_response(
    text: str,
    *,
    min_total_len: int = 600,
    sim_threshold: float = 0.65,
    leadin_chars: int = 120,
) -> str:
    """Return *text* with multi-pass repetition collapsed to its final pass.

    Returns *text* unchanged when no restart pattern is detected or when the
    response is too short to bother analyzing.
    """
    if not text or len(text) < min_total_len:
        return text

    paragraphs = text.split("\n\n")
    if len(paragraphs) < 3:
        return text

    leadins = [_normalize(p)[:leadin_chars] for p in paragraphs]

    # Find the LAST restart: paragraph i whose lead-in matches some earlier
    # paragraph j's lead-in above the threshold.
    last_restart: int | None = None
    for i in range(1, len(paragraphs)):
        if not leadins[i]:
            continue
        for j in range(i):
            if not leadins[j]:
                continue
            if _similarity(leadins[i], leadins[j]) >= sim_threshold:
                last_restart = i
                break

    if last_restart is None:
        return text

    return "\n\n".join(paragraphs[last_restart:]).strip()
