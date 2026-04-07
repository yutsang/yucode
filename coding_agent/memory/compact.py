"""Session compaction -- summarise older messages to free context window.

Port of ``rust/crates/runtime/src/compact.rs``.  Uses a char/4 heuristic for
token estimation.  Summaries include message stats, tool names, recent user
requests, pending work, key files, and a message-level timeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.session import Message

_CONTINUATION_PREAMBLE = (
    "This session is being continued from a previous conversation that ran out "
    "of context. The summary below covers the earlier portion of the "
    "conversation.\n\n"
)
_RECENT_MESSAGES_NOTE = "Recent messages are preserved verbatim."
_DIRECT_RESUME = (
    "Continue the conversation from where it left off without asking the user "
    "any further questions. Resume directly — do not acknowledge the summary, "
    "do not recap what was happening, and do not preface with continuation text."
)

_INTERESTING_EXTENSIONS = frozenset(
    ["rs", "ts", "tsx", "js", "json", "md", "py", "yaml", "yml", "toml"]
)


CompactStrategy = str  # "heuristic" | "llm"

LlmCompactor = None  # type alias placeholder for callable


@dataclass
class CompactionConfig:
    preserve_recent_messages: int = 4
    max_estimated_tokens: int = 10_000
    strategy: CompactStrategy = "heuristic"
    llm_compactor: Any = None


@dataclass
class CompactionResult:
    summary: str = ""
    formatted_summary: str = ""
    compacted_messages: list[Message] = field(default_factory=list)
    removed_message_count: int = 0


# ---- token estimation -----------------------------------------------------

def estimate_message_tokens(msg: Message) -> int:
    size = len(msg.content or "")
    for tc in msg.tool_calls:
        size += len(tc.name) + len(tc.arguments)
    return size // 4 + 1


def estimate_session_tokens(messages: list[Message]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


# ---- compaction logic ------------------------------------------------------

def _compacted_prefix_len(messages: list[Message]) -> int:
    if messages and messages[0].role == "system":
        text = messages[0].content or ""
        if text.startswith(_CONTINUATION_PREAMBLE):
            return 1
    return 0


def should_compact(messages: list[Message], config: CompactionConfig) -> bool:
    start = _compacted_prefix_len(messages)
    compactable = messages[start:]
    if len(compactable) <= config.preserve_recent_messages:
        return False
    return sum(estimate_message_tokens(m) for m in compactable) >= config.max_estimated_tokens


def compact_session(
    messages: list[Message],
    config: CompactionConfig | None = None,
) -> CompactionResult:
    from ..core.session import Message as Msg

    cfg = config or CompactionConfig()
    if not should_compact(messages, cfg):
        return CompactionResult(compacted_messages=list(messages))

    prefix_len = _compacted_prefix_len(messages)
    existing_summary = _extract_existing_summary(messages[0]) if prefix_len else None
    keep_from = max(len(messages) - cfg.preserve_recent_messages, prefix_len)
    removed = messages[prefix_len:keep_from]
    preserved = messages[keep_from:]

    if cfg.strategy == "llm" and cfg.llm_compactor is not None:
        raw_summary = _llm_summarize(removed, cfg.llm_compactor)
    else:
        raw_summary = _summarize_messages(removed)

    summary = _merge_summaries(existing_summary, raw_summary)
    formatted = format_compact_summary(summary)
    continuation = get_compact_continuation(summary, suppress_follow_up=True, recent_preserved=bool(preserved))

    compacted = [Msg(role="system", content=continuation)] + list(preserved)
    return CompactionResult(
        summary=summary,
        formatted_summary=formatted,
        compacted_messages=compacted,
        removed_message_count=len(removed),
    )


def _llm_summarize(messages: list[Message], compactor: Any) -> str:
    """Use an LLM-backed compactor to produce a context summary.

    ``compactor`` should be a callable accepting a list of message dicts and
    returning a summary string.  Falls back to heuristic if it fails.
    """
    try:
        msg_dicts = [
            {"role": m.role, "content": m.content or "", "tool_calls": [{"name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls]}
            for m in messages
        ]
        result = compactor(msg_dicts)
        if isinstance(result, str) and result.strip():
            return f"<summary>\n{result.strip()}\n</summary>"
    except Exception:
        pass
    return _summarize_messages(messages)


# ---- summary building -----------------------------------------------------

def _summarize_messages(messages: list[Message]) -> str:
    user_count = sum(1 for m in messages if m.role == "user")
    asst_count = sum(1 for m in messages if m.role == "assistant")
    tool_count = sum(1 for m in messages if m.role == "tool")

    tool_names: set[str] = set()
    for m in messages:
        for tc in m.tool_calls:
            tool_names.add(tc.name)

    lines = [
        "<summary>",
        "Conversation summary:",
        f"- Scope: {len(messages)} earlier messages compacted "
        f"(user={user_count}, assistant={asst_count}, tool={tool_count}).",
    ]

    if tool_names:
        lines.append(f"- Tools mentioned: {', '.join(sorted(tool_names))}.")

    recent_user = _recent_role_summaries(messages, "user", 3)
    if recent_user:
        lines.append("- Recent user requests:")
        lines.extend(f"  - {r}" for r in recent_user)

    pending = _infer_pending_work(messages)
    if pending:
        lines.append("- Pending work:")
        lines.extend(f"  - {p}" for p in pending)

    key_files = _collect_key_files(messages)
    if key_files:
        lines.append(f"- Key files referenced: {', '.join(key_files)}.")

    current = _infer_current_work(messages)
    if current:
        lines.append(f"- Current work: {current}")

    lines.append("- Key timeline:")
    for m in messages:
        role = m.role
        parts = []
        if m.content and m.content.strip():
            parts.append(_truncate(m.content, 160))
        for tc in m.tool_calls:
            parts.append(f"tool_use {tc.name}({_truncate(tc.arguments, 80)})")
        content = " | ".join(parts) if parts else "(empty)"
        lines.append(f"  - {role}: {content}")

    lines.append("</summary>")
    return "\n".join(lines)


def _merge_summaries(existing: str | None, new_summary: str) -> str:
    if not existing:
        return new_summary

    prev_highlights = _extract_highlights(existing)
    new_formatted = format_compact_summary(new_summary)
    new_highlights = _extract_highlights(new_formatted)
    new_timeline = _extract_timeline(new_formatted)

    lines = ["<summary>", "Conversation summary:"]
    if prev_highlights:
        lines.append("- Previously compacted context:")
        lines.extend(f"  {h}" for h in prev_highlights)
    if new_highlights:
        lines.append("- Newly compacted context:")
        lines.extend(f"  {h}" for h in new_highlights)
    if new_timeline:
        lines.append("- Key timeline:")
        lines.extend(f"  {t}" for t in new_timeline)
    lines.append("</summary>")
    return "\n".join(lines)


# ---- formatting helpers ---------------------------------------------------

def format_compact_summary(summary: str) -> str:
    without_analysis = _strip_tag_block(summary, "analysis")
    content = _extract_tag_content(without_analysis, "summary")
    if content is not None:
        formatted = without_analysis.replace(
            f"<summary>{content}</summary>",
            f"Summary:\n{content.strip()}",
        )
    else:
        formatted = without_analysis
    return _collapse_blank_lines(formatted).strip()


def get_compact_continuation(
    summary: str,
    *,
    suppress_follow_up: bool = True,
    recent_preserved: bool = True,
) -> str:
    base = f"{_CONTINUATION_PREAMBLE}{format_compact_summary(summary)}"
    if recent_preserved:
        base += f"\n\n{_RECENT_MESSAGES_NOTE}"
    if suppress_follow_up:
        base += f"\n{_DIRECT_RESUME}"
    return base


# ---- private helpers -------------------------------------------------------

def _recent_role_summaries(messages: list[Message], role: str, limit: int) -> list[str]:
    hits = [_truncate(m.content, 160) for m in reversed(messages) if m.role == role and (m.content or "").strip()]
    return list(reversed(hits[:limit]))


def _infer_pending_work(messages: list[Message]) -> list[str]:
    keywords = ("todo", "next", "pending", "follow up", "remaining")
    hits: list[str] = []
    for m in reversed(messages):
        text = (m.content or "").lower()
        if any(kw in text for kw in keywords):
            hits.append(_truncate(m.content or "", 160))
            if len(hits) >= 3:
                break
    return list(reversed(hits))


def _collect_key_files(messages: list[Message]) -> list[str]:
    files: set[str] = set()
    for m in messages:
        for token in (m.content or "").split():
            candidate = token.strip(",.;:)(\"'`")
            if "/" in candidate and _has_interesting_ext(candidate):
                files.add(candidate)
        for tc in m.tool_calls:
            for token in tc.arguments.split():
                candidate = token.strip(",.;:)(\"'`")
                if "/" in candidate and _has_interesting_ext(candidate):
                    files.add(candidate)
    return sorted(files)[:8]


def _has_interesting_ext(candidate: str) -> bool:
    ext = PurePosixPath(candidate).suffix.lstrip(".").lower()
    return ext in _INTERESTING_EXTENSIONS


def _infer_current_work(messages: list[Message]) -> str | None:
    for m in reversed(messages):
        if (m.content or "").strip():
            return _truncate(m.content or "", 200)
    return None


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\u2026"


def _extract_tag_content(text: str, tag: str) -> str | None:
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    s = text.find(start_tag)
    if s < 0:
        return None
    s += len(start_tag)
    e = text.find(end_tag, s)
    if e < 0:
        return None
    return text[s:e]


def _strip_tag_block(text: str, tag: str) -> str:
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    s = text.find(start_tag)
    e = text.find(end_tag)
    if s >= 0 and e >= 0:
        return text[:s] + text[e + len(end_tag):]
    return text


def _collapse_blank_lines(text: str) -> str:
    lines: list[str] = []
    last_blank = False
    for line in text.splitlines():
        is_blank = not line.strip()
        if is_blank and last_blank:
            continue
        lines.append(line)
        last_blank = is_blank
    return "\n".join(lines)


def _extract_existing_summary(msg: Message) -> str | None:
    if msg.role != "system":
        return None
    text = msg.content or ""
    rest = text.removeprefix(_CONTINUATION_PREAMBLE)
    if rest == text:
        return None
    for sep in (f"\n\n{_RECENT_MESSAGES_NOTE}", f"\n{_DIRECT_RESUME}"):
        idx = rest.find(sep)
        if idx >= 0:
            rest = rest[:idx]
    return rest.strip()


def _extract_highlights(summary: str) -> list[str]:
    lines: list[str] = []
    in_timeline = False
    for line in format_compact_summary(summary).splitlines():
        trimmed = line.rstrip()
        if not trimmed or trimmed in ("Summary:", "Conversation summary:"):
            continue
        if trimmed == "- Key timeline:":
            in_timeline = True
            continue
        if in_timeline:
            continue
        lines.append(trimmed)
    return lines


def _extract_timeline(summary: str) -> list[str]:
    lines: list[str] = []
    in_timeline = False
    for line in format_compact_summary(summary).splitlines():
        trimmed = line.rstrip()
        if trimmed == "- Key timeline:":
            in_timeline = True
            continue
        if not in_timeline:
            continue
        if not trimmed:
            break
        lines.append(trimmed)
    return lines
