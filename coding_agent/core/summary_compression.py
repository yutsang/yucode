"""Summary text compression for lane event details.

Port of ``claw-code-main/rust/crates/runtime/src/summary_compression.rs``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SummaryCompressionBudget:
    max_chars: int = 1200
    max_lines: int = 24
    max_line_chars: int = 160


@dataclass
class SummaryCompressionResult:
    summary: str
    original_chars: int
    compressed_chars: int
    original_lines: int
    compressed_lines: int
    removed_duplicate_lines: int = 0
    omitted_lines: int = 0
    truncated: bool = False


def compress_summary(
    summary: str,
    budget: SummaryCompressionBudget | None = None,
) -> SummaryCompressionResult:
    b = budget or SummaryCompressionBudget()
    original_chars = len(summary)
    raw_lines = summary.splitlines()
    original_lines = len(raw_lines)

    normalized: list[str] = []
    seen_lower: set[str] = set()
    removed_dupes = 0

    for line in raw_lines:
        trimmed = " ".join(line.split())
        if len(trimmed) > b.max_line_chars:
            trimmed = trimmed[:b.max_line_chars - 1] + "\u2026"
        lower = trimmed.lower()
        if lower in seen_lower and trimmed:
            removed_dupes += 1
            continue
        if trimmed:
            seen_lower.add(lower)
        normalized.append(trimmed)

    selected: list[str] = []
    char_count = 0
    omitted = 0

    for line in normalized:
        if len(selected) >= b.max_lines:
            omitted += 1
            continue
        if char_count + len(line) + 1 > b.max_chars and selected:
            omitted += 1
            continue
        selected.append(line)
        char_count += len(line) + 1

    if omitted > 0:
        selected.append(f"[{omitted} line(s) omitted]")

    result_text = "\n".join(selected)
    return SummaryCompressionResult(
        summary=result_text,
        original_chars=original_chars,
        compressed_chars=len(result_text),
        original_lines=original_lines,
        compressed_lines=len(selected),
        removed_duplicate_lines=removed_dupes,
        omitted_lines=omitted,
        truncated=omitted > 0 or removed_dupes > 0,
    )


def compress_summary_text(summary: str) -> str:
    return compress_summary(summary).summary
