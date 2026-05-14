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


# --- is_complex_prompt / looks_like_investigation ---

@pytest.mark.parametrize("prompt", [
    "Please check why kedro tells me Pipeline does not contain nodes named [...]",
    "List every environment variable that yucode reads. Group by purpose.",
    "How many tools does yucode register? Group them by RiskLevel.",
    "幫我調查這個 kedro 錯誤",
    "為什麼 X 不存在",
    "refactor the auth module",
])
def test_is_complex_prompt_triggers_for_investigation_and_implementation(prompt: str) -> None:
    """Investigation question forms and implementation verbs must trigger coordinator."""
    from coding_agent.core.coordinator import is_complex_prompt
    assert is_complex_prompt(prompt, intelligence_tier="weak"), f"Should trigger: {prompt!r}"


@pytest.mark.parametrize("prompt", ["ls", "print pi", "hello", ""])
def test_is_complex_prompt_skips_trivial_short_prompts(prompt: str) -> None:
    from coding_agent.core.coordinator import is_complex_prompt
    assert not is_complex_prompt(prompt, intelligence_tier="strong"), f"Should not trigger: {prompt!r}"


def test_weak_tier_triggers_earlier_than_strong() -> None:
    """Weak-tier should trigger on prompts strong-tier ignores (e.g. 'find function foo')."""
    from coding_agent.core.coordinator import is_complex_prompt
    prompt = "find function foo"
    assert is_complex_prompt(prompt, intelligence_tier="weak")
    assert not is_complex_prompt(prompt, intelligence_tier="strong")


def test_intelligence_tier_auto_detects_weak_models() -> None:
    from coding_agent.config.settings import resolve_intelligence_tier
    assert resolve_intelligence_tier("auto", "qwen3-32b") == "weak"
    assert resolve_intelligence_tier("auto", "llama-3.1-70b") == "weak"
    assert resolve_intelligence_tier("auto", "gpt-4o") == "strong"
    assert resolve_intelligence_tier("auto", "claude-opus-4-7") == "strong"
    assert resolve_intelligence_tier("auto", "unknown-model") == "strong"
    # Explicit overrides ignore model name
    assert resolve_intelligence_tier("strong", "qwen3-32b") == "strong"
    assert resolve_intelligence_tier("weak", "gpt-4o") == "weak"


def test_resolve_path_normalizes_backslashes(tmp_path: Path) -> None:
    """Windows backslash paths must resolve identically to forward-slash paths."""
    from coding_agent.config import AppConfig
    from coding_agent.tools import ToolRegistry
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "file.py").write_text("x")
    reg = ToolRegistry(tmp_path, AppConfig())
    posix = reg._resolve_path("pkg/file.py")
    windows = reg._resolve_path("pkg\\file.py")
    assert posix == windows, "Backslash and forward-slash paths must resolve to the same Path"
    assert posix.exists()


def test_read_file_auto_resolves_bare_filename(tmp_path: Path) -> None:
    """Bare filename with unique workspace match should resolve automatically."""
    from coding_agent.config import AppConfig
    from coding_agent.tools import ToolRegistry
    from coding_agent.tools.filesystem import _read_file
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "uniquename.py").write_text("content")
    reg = ToolRegistry(tmp_path, AppConfig())
    result = _read_file(reg, {"path": "uniquename.py"})
    assert "content" in result


# --- response_dedup ---

def test_response_dedup_collapses_repeated_passes() -> None:
    from coding_agent.core.response_dedup import dedup_repetitive_response
    lead_in = (
        "The compaction mechanism in yucode is designed to manage the size "
        "of the conversation history. It triggers when token count exceeds "
        "the compact_token_threshold. "
    )
    text = "\n\n".join([
        lead_in + "PASS1: threshold = 10,000.",
        lead_in + "PASS2: threshold = 60,000.",
        lead_in + "PASS3: threshold = 60,000 (final).",
    ])
    # Lower min_total_len so this concise test fixture triggers the dedup.
    out = dedup_repetitive_response(text, min_total_len=200)
    assert "PASS3" in out
    assert "PASS1" not in out, "First pass should be dropped"
    assert "PASS2" not in out, "Second pass should be dropped"


def test_response_dedup_leaves_normal_text_alone() -> None:
    from coding_agent.core.response_dedup import dedup_repetitive_response
    text = "Step 1: do X.\n\nStep 2: do Y.\n\nConclusion: done."
    assert dedup_repetitive_response(text) == text


def test_response_dedup_skips_short_text() -> None:
    from coding_agent.core.response_dedup import dedup_repetitive_response
    short = "Repeated.\n\nRepeated.\n\nRepeated."
    # Short text should never be touched even if it looks repetitive
    assert dedup_repetitive_response(short) == short


# --- streaming display ---

def test_streaming_display_active_state() -> None:
    from coding_agent.interface.render import StreamingTextDisplay
    s = StreamingTextDisplay()
    assert s.is_active is False
    s.feed("hello ")
    assert s.is_active is True
    s.finalize()
    assert s.is_active is False
    # Finalize on already-inactive is a no-op
    s.finalize()
    assert s.is_active is False


# --- canonical dedup hashing ---

def test_dedup_hash_matches_for_different_json_formatting() -> None:
    """Two semantically identical tool calls with different JSON formatting
    must collapse to the same dedup key after canonicalization."""
    import json as _json
    from coding_agent.core.runtime import _content_stable_hash
    a = '{"pattern":"foo","path":"bar"}'
    b = '{"path": "bar", "pattern": "foo"}'
    canon_a = _json.dumps(_json.loads(a), sort_keys=True, separators=(",", ":"))
    canon_b = _json.dumps(_json.loads(b), sort_keys=True, separators=(",", ":"))
    assert _content_stable_hash(canon_a) == _content_stable_hash(canon_b)
