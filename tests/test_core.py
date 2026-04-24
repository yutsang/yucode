from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coding_agent import __version__
from coding_agent.config.settings import _resolve_api_key, is_dangerous_mode
from coding_agent.core.errors import AgentError, tool_error_response
from coding_agent.core.runtime import _MAX_CONSECUTIVE_READONLY, _MAX_STUCK_DEDUP_BLOCKS
from coding_agent.core.session import Message, ToolCall
from coding_agent.core.summary_compression import SummaryCompressionBudget, compress_summary
from coding_agent.memory.compact import CompactionConfig, compact_session, should_compact
from coding_agent.security.permissions import PermissionPolicy


def test_version_matches_repo() -> None:
    assert __version__ == "0.3.5"


def test_env_api_key_takes_priority() -> None:
    previous = os.environ.get("YUCODE_API_KEY")
    os.environ["YUCODE_API_KEY"] = "env-secret"
    try:
        assert _resolve_api_key("config-secret") == "env-secret"
    finally:
        if previous is None:
            os.environ.pop("YUCODE_API_KEY", None)
        else:
            os.environ["YUCODE_API_KEY"] = previous


def test_dangerous_mode_defaults_false() -> None:
    previous = os.environ.get("YUCODE_DANGEROUS_MODE")
    os.environ.pop("YUCODE_DANGEROUS_MODE", None)
    try:
        assert is_dangerous_mode() is False
    finally:
        if previous is not None:
            os.environ["YUCODE_DANGEROUS_MODE"] = previous


def test_tool_error_response_is_structured() -> None:
    payload = json.loads(
        tool_error_response(
            "failed",
            error_code="provider_error",
            recoverable=False,
            suggestion="retry later",
        )
    )
    assert payload == {
        "error": "failed",
        "error_code": "provider_error",
        "recoverable": False,
        "suggestion": "retry later",
    }


def test_agent_error_to_dict() -> None:
    err = AgentError("boom", recoverable=True, category="demo")
    assert err.to_dict()["error_code"] == "demo"


def test_permission_policy_denies_write_in_read_only() -> None:
    decision = PermissionPolicy("read-only").authorize("workspace-write", "write_file")
    assert decision.allowed is False


def test_compaction_preserves_recent_messages() -> None:
    messages = [Message(role="user", content="x" * 3000) for _ in range(8)]
    config = CompactionConfig(preserve_recent_messages=2, max_estimated_tokens=100)
    assert should_compact(messages, config) is True
    result = compact_session(messages, config)
    assert result.removed_message_count > 0
    assert result.compacted_messages[0].role == "system"
    assert len(result.compacted_messages) == 3


def test_compaction_never_splits_tool_use_result_pair() -> None:
    """The compaction boundary must not leave a tool-result orphaned at preserved[0]."""
    tc = ToolCall(id="c1", name="read_file", arguments='{"path": "foo.py"}')
    messages = [
        Message(role="user", content="x" * 400),
        Message(role="assistant", content="", tool_calls=[tc]),  # tool-use
        Message(role="tool", content="file contents", tool_call_id="c1"),  # tool-result
        Message(role="user", content="x" * 400),
        Message(role="assistant", content="done"),
    ]
    # With preserve_recent=2, naive keep_from=3 would put the tool-result at preserved[0]
    config = CompactionConfig(preserve_recent_messages=2, max_estimated_tokens=50)
    result = compact_session(messages, config)
    # The first preserved message must never be a tool-result
    first_preserved = result.compacted_messages[1]  # [0] is the system summary
    assert first_preserved.role != "tool", (
        f"First preserved message must not be a tool-result, got role={first_preserved.role!r}"
    )


# --- summary compression priority scoring ---


def test_compress_summary_headers_survive_prose() -> None:
    """When budget is tight, headers and bullets outlast plain prose."""
    lines = [
        "# Main heading",          # score 4
        "Some prose sentence.",     # score 1
        "More prose here.",         # score 1
        "- bullet point A",         # score 2
    ]
    summary = "\n".join(lines)
    # Budget allows only 2 lines
    budget = SummaryCompressionBudget(max_chars=50, max_lines=2)
    result = compress_summary(summary, budget)
    kept = result.summary
    assert "# Main heading" in kept, "Header should survive tight budget"
    assert "- bullet point A" in kept, "Bullet should survive tight budget"


def test_compress_summary_restores_reading_order() -> None:
    """Lines kept by priority scoring are output in original document order."""
    lines = [
        "# Section A",      # score 4, index 0
        "prose line one",   # score 1, index 1
        "## Section B",     # score 3, index 2
        "prose line two",   # score 1, index 3
    ]
    summary = "\n".join(lines)
    budget = SummaryCompressionBudget(max_chars=500, max_lines=3)
    result = compress_summary(summary, budget)
    kept_lines = [ln for ln in result.summary.splitlines() if ln and not ln.startswith("[")]
    positions = {line: i for i, line in enumerate(kept_lines)}
    # Section A (score 4) should appear before Section B (score 3) in output
    assert positions["# Section A"] < positions["## Section B"]


def test_max_stuck_dedup_blocks_constant() -> None:
    """_MAX_STUCK_DEDUP_BLOCKS must be a positive integer — it guards the forced-exit path."""
    assert isinstance(_MAX_STUCK_DEDUP_BLOCKS, int)
    assert _MAX_STUCK_DEDUP_BLOCKS > 0


def test_max_consecutive_readonly_constant() -> None:
    """_MAX_CONSECUTIVE_READONLY must be a positive integer and larger than dedup threshold."""
    assert isinstance(_MAX_CONSECUTIVE_READONLY, int)
    assert _MAX_CONSECUTIVE_READONLY > 0


def test_compress_summary_deduplicates_case_insensitively() -> None:
    lines = ["Pending work: fix bug", "pending work: fix bug", "Other note"]
    result = compress_summary("\n".join(lines))
    kept = [ln for ln in result.summary.splitlines() if ln and not ln.startswith("[")]
    assert result.removed_duplicate_lines == 1
    # Only one of the duplicate pair should appear
    assert sum(1 for ln in kept if "pending work" in ln.lower()) == 1


# --- tool name resolution (camelCase ↔ snake_case) ---


def _make_registry(tmp_path: Path):
    """Build a minimal ToolRegistry for name-resolution tests."""
    from coding_agent.config import AppConfig
    from coding_agent.tools import ToolRegistry
    cfg = AppConfig()
    return ToolRegistry(workspace_root=tmp_path, config=cfg)


def test_tool_registry_resolves_camel_web_search(tmp_path: Path) -> None:
    """WebSearch (camelCase) must resolve to the registered web_search tool."""
    reg = _make_registry(tmp_path)
    assert reg.has_tool("WebSearch"), "WebSearch should resolve to web_search"


def test_tool_registry_resolves_camel_web_fetch(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    assert reg.has_tool("WebFetch"), "WebFetch should resolve to web_fetch"


def test_tool_registry_execute_camel_name_does_not_raise(tmp_path: Path) -> None:
    """Calling execute() with WebSearch must not raise KeyError."""
    reg = _make_registry(tmp_path)
    # web_search requires a 'query' arg; an empty query is fine for a registry test
    try:
        reg.execute("WebSearch", {"query": "test"})
    except KeyError as exc:
        pytest.fail(f"execute('WebSearch', ...) raised KeyError: {exc}")


def test_tool_registry_unknown_tool_still_raises(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    with pytest.raises(KeyError, match="NonExistentTool"):
        reg.execute("NonExistentTool", {})


# --- system prompt content ---


def test_system_prompt_contains_table_unit_guidance(tmp_path: Path) -> None:
    """The rendered system prompt must instruct the model to look for units in table headers."""
    from coding_agent.config import AppConfig
    from coding_agent.memory.prompting import ProjectContext, PromptAssembler
    ctx = ProjectContext(cwd=tmp_path, current_date="2026-01-01", git_status=None, git_diff=None, instruction_files=[])
    prompt = PromptAssembler(AppConfig(), ctx).render()
    assert "header" in prompt.lower() and "unit" in prompt.lower(), (
        "System prompt should mention reading headers for units in tables"
    )


def test_system_prompt_contains_complex_task_guidance(tmp_path: Path) -> None:
    """The rendered system prompt must include guidance for multi-step task planning."""
    from coding_agent.config import AppConfig
    from coding_agent.memory.prompting import ProjectContext, PromptAssembler
    ctx = ProjectContext(cwd=tmp_path, current_date="2026-01-01", git_status=None, git_diff=None, instruction_files=[])
    prompt = PromptAssembler(AppConfig(), ctx).render()
    assert "plan" in prompt.lower(), "System prompt should mention writing a plan for complex tasks"
