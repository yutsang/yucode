"""Post-process model output to remove repetitive multi-pass answers.

Weak-tier models (Qwen3-class) often emit the same answer 2-3 times in one
response, with slight rewording. Each "pass" may be one paragraph or several
structured sections. We detect periodicity in paragraph similarity:

  Pass 1 (3 paragraphs):    P0 P1 P2
  Pass 2 (3 paragraphs):    P3 P4 P5
  Periodic structure:       P3≈P0, P4≈P1, P5≈P2  →  period=3
  Keep paragraphs[3:]       = pass 2 only

Conservative defaults — skips short responses, requires at least 2 matching
pairs to confirm a pattern. Falls back to whole-paragraph similarity when
lead-in matching fails (passes that share content but use different
section headers).
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace + strip — used for similarity input."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _similarity(a: str, b: str) -> float:
    """Ratio in [0, 1] between two normalized strings."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _detect_period(
    keys: list[str],
    *,
    sim_threshold: float,
) -> int | None:
    """Find the dominant repetition period across all matching paragraph pairs.

    Returns the most-common (i - j) distance between pairs of similar
    paragraphs, or None if no consistent pattern is found.
    """
    periods: list[int] = []
    for i in range(1, len(keys)):
        if not keys[i]:
            continue
        for j in range(i):
            if not keys[j]:
                continue
            if _similarity(keys[i], keys[j]) >= sim_threshold:
                periods.append(i - j)
    if not periods:
        return None
    period, count = Counter(periods).most_common(1)[0]
    # Require ≥2 matching pairs to confirm a real repetition pattern.
    # A single match could be coincidence (a recurring phrase, not a full pass).
    if count < 2:
        return None
    return period


def dedup_repetitive_response(
    text: str,
    *,
    min_total_len: int = 600,
    sim_threshold: float = 0.65,
    leadin_chars: int = 120,
    fallback_threshold: float = 0.55,
    max_paragraphs: int = 60,
) -> str:
    """Return *text* with multi-pass repetition collapsed to its final pass.

    Returns *text* unchanged when no restart pattern is detected or when the
    response is too short / too long to analyze safely.
    """
    if not text or len(text) < min_total_len:
        return text

    paragraphs = text.split("\n\n")
    if len(paragraphs) < 3 or len(paragraphs) > max_paragraphs:
        return text

    # Pass 1: lead-in (first 120 chars) similarity — fast, catches most cases.
    leadins = [_normalize(p)[:leadin_chars] for p in paragraphs]
    period = _detect_period(leadins, sim_threshold=sim_threshold)

    # Pass 2: whole-paragraph similarity — catches passes that use different
    # section headers but repeat the same body content. Lower threshold
    # because longer strings are noisier.
    if period is None:
        whole = [_normalize(p) for p in paragraphs]
        period = _detect_period(whole, sim_threshold=fallback_threshold)

    if period is None:
        return text

    last_pass_start = len(paragraphs) - period
    if last_pass_start <= 0 or last_pass_start >= len(paragraphs):
        return text

    return "\n\n".join(paragraphs[last_pass_start:]).strip()
