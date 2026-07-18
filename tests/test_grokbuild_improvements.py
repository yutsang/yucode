"""Tests for the grok-build-inspired improvements (2026-07-17):

- A1: layered tool-result pruning by turn age
- A2: compaction transcript pointer (full archive + continuation link)
- A3: bash output written to disk, head+tail truncation
- A4: structured LLM compactor prompt + degenerate-summary detection
- B5/B6: background task output capture, completion reminders, auto-background-on-timeout
- B7: edit_file nearest-match + unicode-confusable recovery hints
- C8: todo nudge + turn-end gate
- C9: post-tool reminder framework
- C10: AGENTS.md dynamic discovery reminder
- C11: compaction state reinjection
- C12: auto-compact trigger recalibration

Second pass (same date), a follow-up round after a deeper grok-build survey:
- P2-1: AskUserQuestion bounded timeout (was an unbounded blocking input())
- P2-2: skill summary budget-tiered truncation
- P2-3: skill dynamic discovery tracker (mirrors C10 for skill directories)
- P2-4: web_fetch overflow -> disk artifact + recovery pointer
- P2-5: apply_patch multi-file atomic patch tool (Codex-style format)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.core.runtime import AgentRuntime, _prepare_transcript_for_summary, _truncate_tool_output
from coding_agent.core.session import _HARD_CLEAR_PLACEHOLDER, Message, Session, ToolCall, Usage, _prune_tool_results
from coding_agent.memory.compact import (
    CompactionConfig,
    compact_session,
    format_transcript_location,
    get_compact_continuation,
    is_degenerate_summary,
)
from coding_agent.tools import ToolRegistry


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".yucode").mkdir()
    return tmp_path


@pytest.fixture
def strong_config() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(name="test", api_key="k", model="gpt-test", intelligence_tier="strong"),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )


# ---------------------------------------------------------------------------
# A1: layered tool-result pruning
# ---------------------------------------------------------------------------

class TestToolResultPruning:
    def _six_turn_session(self) -> list[Message]:
        messages: list[Message] = []
        for t in range(6):
            messages.append(Message(role="user", content=f"turn {t} question"))
            messages.append(Message(role="assistant", content="", tool_calls=[ToolCall(id=f"c{t}", name="bash", arguments="{}")]))
            messages.append(Message(role="tool", content="X" * 100, tool_call_id=f"c{t}"))
        return messages

    def test_keeps_last_n_turns_verbatim(self) -> None:
        rt = RuntimeOptions(prune_keep_last_turns=2, prune_soft_trim_threshold=50, prune_hard_clear_turns=4)
        messages = self._six_turn_session()
        pruned = _prune_tool_results(messages, rt)
        tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
        # chronological turn 5 (idx17) and turn 4 (idx14) are the last two -> untouched
        assert pruned[tool_indices[-1]].content == "X" * 100
        assert pruned[tool_indices[-2]].content == "X" * 100

    def test_hard_clears_beyond_age_threshold(self) -> None:
        rt = RuntimeOptions(prune_keep_last_turns=1, prune_soft_trim_threshold=50, prune_hard_clear_turns=2)
        messages = self._six_turn_session()
        pruned = _prune_tool_results(messages, rt)
        tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
        # oldest turn (0) is >= 2 turns back -> hard cleared
        assert pruned[tool_indices[0]].content == _HARD_CLEAR_PLACEHOLDER

    def test_soft_trims_middle_band(self) -> None:
        rt = RuntimeOptions(prune_keep_last_turns=1, prune_soft_trim_threshold=50, prune_soft_trim_head=10, prune_soft_trim_tail=10, prune_hard_clear_turns=4)
        messages = self._six_turn_session()
        pruned = _prune_tool_results(messages, rt)
        tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
        mid_content = pruned[tool_indices[2]].content  # chronological turn 2: 3 turns back
        assert "[...trimmed...]" in mid_content
        assert len(mid_content) < 100

    def test_never_mutates_original_messages(self) -> None:
        rt = RuntimeOptions(prune_keep_last_turns=0, prune_soft_trim_threshold=1, prune_hard_clear_turns=1000)
        messages = self._six_turn_session()
        _prune_tool_results(messages, rt)
        assert all(m.content == "X" * 100 for m in messages if m.role == "tool")

    def test_soft_trim_never_grows_content(self) -> None:
        """head+tail must not exceed the original content length even when
        soft_trim_threshold is misconfigured smaller than head+tail."""
        rt = RuntimeOptions(prune_keep_last_turns=0, prune_soft_trim_threshold=10, prune_soft_trim_head=1500, prune_soft_trim_tail=1500, prune_hard_clear_turns=1000)
        messages = [Message(role="tool", content="Y" * 300, tool_call_id="c0")]
        pruned = _prune_tool_results(messages, rt)
        assert len(pruned[0].content) <= 300

    def test_provider_messages_gated_on_context_usage(self) -> None:
        rt = RuntimeOptions(compact_token_threshold=100000, prune_keep_last_turns=0, prune_soft_trim_threshold=1, prune_hard_clear_turns=1000)
        session = Session()
        session.add_message(Message(role="user", content="hi"))
        session.add_message(Message(role="tool", content="Z" * 500, tool_call_id="c0"))
        messages = session.provider_messages("sys", pruning=rt)
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert tool_msgs[0]["content"] == "Z" * 500  # under threshold/2, untouched

    def test_provider_messages_disabled_flag(self) -> None:
        rt = RuntimeOptions(compact_token_threshold=10, prune_tool_results=False)
        session = Session()
        session.add_message(Message(role="user", content="hi"))
        session.add_message(Message(role="tool", content="Z" * 500, tool_call_id="c0"))
        messages = session.provider_messages("sys", pruning=rt)
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert tool_msgs[0]["content"] == "Z" * 500


# ---------------------------------------------------------------------------
# A2: compaction transcript pointer
# ---------------------------------------------------------------------------

class TestTranscriptPointer:
    def test_format_transcript_location_contains_path(self) -> None:
        hint = format_transcript_location("/tmp/archives/session_1.json")
        assert "/tmp/archives/session_1.json" in hint

    def test_continuation_includes_hint_only_when_path_given(self) -> None:
        with_path = get_compact_continuation("<summary>x</summary>", transcript_path="/a/b.json")
        without_path = get_compact_continuation("<summary>x</summary>", transcript_path=None)
        assert "/a/b.json" in with_path
        assert "/a/b.json" not in without_path

    def test_archive_stores_full_content_not_truncated(self, workspace: Path, strong_config: AppConfig) -> None:
        rt = AgentRuntime(workspace, strong_config)
        rt.session.add_message(Message(role="user", content="x" * 3000))
        path = rt._archive_before_compact()
        assert path is not None and path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["messages"][0]["content"]) == 3000

    def test_archive_filenames_are_unique_within_same_second(self, workspace: Path, strong_config: AppConfig) -> None:
        rt = AgentRuntime(workspace, strong_config)
        rt.session.add_message(Message(role="user", content="hi"))
        p1 = rt._archive_before_compact()
        p2 = rt._archive_before_compact()
        assert p1 != p2
        assert p1 is not None and p1.exists()
        assert p2 is not None and p2.exists()

    def test_compact_default_config_threads_transcript_pointer(self, workspace: Path) -> None:
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", compact_preserve_recent=1, compact_token_threshold=50),
        )
        rt = AgentRuntime(workspace, config)
        for i in range(8):
            rt.session.add_message(Message(role="user", content=f"msg {i} " + "x" * 200))
        result = rt.compact()
        assert result.removed_message_count > 0
        assert ".json" in result.compacted_messages[0].content

    def test_compact_session_without_transcript_path_omits_hint(self) -> None:
        messages = [Message(role="user", content="x" * 3000) for _ in range(8)]
        config = CompactionConfig(preserve_recent_messages=2, max_estimated_tokens=100)
        result = compact_session(messages, config)
        assert "pre-compaction transcript" not in result.compacted_messages[0].content


# ---------------------------------------------------------------------------
# A3: bash output to disk, head+tail truncation
# ---------------------------------------------------------------------------

class TestBashOutputToDisk:
    def test_large_stdout_is_disk_backed_with_tail_preserved(self, workspace: Path, strong_config: AppConfig) -> None:
        import sys
        rt = AgentRuntime(workspace, strong_config)
        script_path = workspace / "gen.py"
        script_path.write_text(
            "import sys\n"
            "sys.stdout.write('A' * 200000)\n"
            "sys.stdout.write('\\nFINAL_MARKER\\n')\n",
            encoding="utf-8",
        )
        out = rt._execute_tool("bash", json.dumps({"command": f"{sys.executable} {script_path}"}))
        payload = json.loads(out)
        assert payload["truncated"] is True
        assert "FINAL_MARKER" in payload["stdout"]
        assert "Full output at:" in payload["stdout"]

    def test_small_output_unaffected(self, workspace: Path, strong_config: AppConfig) -> None:
        rt = AgentRuntime(workspace, strong_config)
        out = rt._execute_tool("bash", json.dumps({"command": "echo hello"}))
        payload = json.loads(out)
        assert payload.get("truncated") is None
        assert "hello" in payload["stdout"]


# ---------------------------------------------------------------------------
# runtime._truncate_tool_output: JSON-safe outer cap
# ---------------------------------------------------------------------------

class TestTruncateToolOutputJsonSafety:
    def test_json_dict_stays_valid_after_truncation(self) -> None:
        payload = json.dumps({"returncode": 0, "stdout": "X" * 50000, "stderr": "Y" * 50000}, indent=2)
        result = _truncate_tool_output(payload, 4000)
        parsed = json.loads(result)  # must not raise
        assert parsed["returncode"] == 0
        assert len(parsed["stdout"]) < 50000
        assert len(parsed["stderr"]) < 50000

    def test_non_json_text_falls_back_to_plain_truncation(self) -> None:
        text = "line\n" * 20000
        result = _truncate_tool_output(text, 4000)
        assert "[Output capped at" in result
        assert len(result.encode("utf-8")) < len(text.encode("utf-8"))

    def test_json_array_stays_valid(self) -> None:
        # read_files returns a top-level JSON *array*, not a dict — the fix
        # must not assume a dict shape.
        payload = json.dumps([
            {"path": "a.py", "content": "A" * 40000},
            {"path": "b.py", "content": "short"},
        ])
        result = _truncate_tool_output(payload, 4000)
        parsed = json.loads(result)  # must not raise
        assert parsed[0]["path"] == "a.py"
        assert len(parsed[0]["content"]) < 40000
        assert parsed[1]["content"] == "short"


# ---------------------------------------------------------------------------
# A4: structured LLM compactor prompt + degenerate-summary detection
# ---------------------------------------------------------------------------

class TestPrepareTranscriptForSummary:
    def test_drops_tool_results_keeps_user_and_assistant(self) -> None:
        messages = [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "read_file", "arguments": "{}"}]},
            {"role": "tool", "content": "huge file contents that should not appear"},
            {"role": "assistant", "content": "found it, fixing now"},
        ]
        transcript = _prepare_transcript_for_summary(messages)
        assert "fix the bug" in transcript
        assert "found it, fixing now" in transcript
        assert "[Called tools: read_file]" in transcript
        assert "huge file contents that should not appear" not in transcript

    def test_content_is_not_truncated(self) -> None:
        long_text = "x" * 5000
        messages = [{"role": "user", "content": long_text}]
        transcript = _prepare_transcript_for_summary(messages)
        assert long_text in transcript


class _FakeProvider:
    """Duck-typed stand-in for OpenAICompatibleProvider.complete()."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def complete(self, messages, tools, **kwargs):  # noqa: ANN001
        self.calls.append(messages[0]["content"])
        next_response = self._responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        result = type("Resp", (), {})()
        result.text = next_response
        return result


class TestLlmCompactorRetry:
    def _runtime_with_fake_provider(self, workspace: Path, responses: list[str | Exception]) -> tuple[AgentRuntime, _FakeProvider]:
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", compact_strategy="llm"),
        )
        fake = _FakeProvider(responses)
        rt = AgentRuntime(workspace, config, provider=fake)  # type: ignore[arg-type]
        return rt, fake

    def test_good_response_returned_on_first_attempt(self, workspace: Path) -> None:
        good = "1. Primary Request and Intent:\n" + ("detail " * 100)
        rt, fake = self._runtime_with_fake_provider(workspace, [good])
        compactor = rt._make_llm_compactor()
        result = compactor([{"role": "user", "content": "hi"}])
        assert result == good
        assert len(fake.calls) == 1

    def test_degenerate_first_response_triggers_one_retry(self, workspace: Path) -> None:
        degenerate = "I'll summarize."
        good = "1. Primary Request and Intent:\n" + ("detail " * 100)
        rt, fake = self._runtime_with_fake_provider(workspace, [degenerate, good])
        compactor = rt._make_llm_compactor()
        result = compactor([{"role": "user", "content": "hi"}])
        assert result == good
        assert len(fake.calls) == 2

    def test_two_degenerate_responses_fall_back_to_empty(self, workspace: Path) -> None:
        degenerate = "short"
        rt, fake = self._runtime_with_fake_provider(workspace, [degenerate, degenerate])
        compactor = rt._make_llm_compactor()
        result = compactor([{"role": "user", "content": "hi"}])
        assert result == ""
        assert len(fake.calls) == 2

    def test_provider_exception_retries_then_falls_back(self, workspace: Path) -> None:
        rt, fake = self._runtime_with_fake_provider(workspace, [RuntimeError("network blip"), RuntimeError("still down")])
        compactor = rt._make_llm_compactor()
        result = compactor([{"role": "user", "content": "hi"}])
        assert result == ""
        assert len(fake.calls) == 2

    def test_exception_then_good_response_recovers(self, workspace: Path) -> None:
        good = "1. Primary Request and Intent:\n" + ("detail " * 100)
        rt, fake = self._runtime_with_fake_provider(workspace, [RuntimeError("network blip"), good])
        compactor = rt._make_llm_compactor()
        result = compactor([{"role": "user", "content": "hi"}])
        assert result == good

    def test_prompt_contains_structured_sections(self, workspace: Path) -> None:
        good = "1. Primary Request and Intent:\n" + ("detail " * 100)
        rt, fake = self._runtime_with_fake_provider(workspace, [good])
        compactor = rt._make_llm_compactor()
        compactor([{"role": "user", "content": "fix bug"}])
        prompt = fake.calls[0]
        assert "Primary Request and Intent" in prompt
        assert "Files and Code Artifacts" in prompt
        assert "All User Messages" in prompt
        assert "fix bug" in prompt


class TestIsDegenerateSummary:
    def test_short_text_is_degenerate(self) -> None:
        assert is_degenerate_summary("short") is True

    def test_long_summary_is_not_degenerate(self) -> None:
        assert is_degenerate_summary("1. Primary Request:\n" + ("word " * 200)) is False

    def test_wrapped_in_summary_tags_measures_inner_content(self) -> None:
        wrapped = "<summary>\n" + ("word " * 200) + "\n</summary>"
        assert is_degenerate_summary(wrapped) is False
        assert is_degenerate_summary("<summary>\nshort\n</summary>") is True


# ---------------------------------------------------------------------------
# B5/B6: background task output capture, completion reminders,
# auto-background-on-timeout
# ---------------------------------------------------------------------------

class TestBackgroundTaskCapture:
    def test_run_in_background_writes_output_to_disk_and_registers_task(self, workspace: Path, strong_config: AppConfig) -> None:
        rt = AgentRuntime(workspace, strong_config)
        out = rt._execute_tool("bash", json.dumps({
            "command": f"{__import__('sys').executable} -c \"print('BG_DONE')\"",
            "run_in_background": True,
        }))
        payload = json.loads(out)
        assert payload["background"] is True
        assert "output_file" in payload
        task_id = payload["task_id"]
        assert task_id in rt.tools.background_tasks

    def test_completion_reminder_fires_once_with_output_preview(self, workspace: Path, strong_config: AppConfig) -> None:
        import time as _time
        rt = AgentRuntime(workspace, strong_config)
        assert rt._check_completed_background_tasks() == ""
        out = rt._execute_tool("bash", json.dumps({
            "command": f"{__import__('sys').executable} -c \"print('DONE_MARKER')\"",
            "run_in_background": True,
        }))
        task_id = json.loads(out)["task_id"]
        deadline = _time.monotonic() + 5
        while rt.tools.background_tasks[task_id].popen.poll() is None and _time.monotonic() < deadline:
            _time.sleep(0.02)
        reminder = rt._check_completed_background_tasks()
        assert "<system-reminder>" in reminder
        assert "DONE_MARKER" in reminder
        assert "exit code: 0" in reminder
        # must not repeat
        assert rt._check_completed_background_tasks() == ""


class TestAutoBackgroundOnTimeout:
    def test_disabled_by_default(self) -> None:
        assert RuntimeOptions().auto_background_on_timeout is False

    def test_finishes_within_timeout_reports_synchronously(self, workspace: Path) -> None:
        import sys
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", auto_background_on_timeout=True),
        )
        rt = AgentRuntime(workspace, config)
        out = rt._execute_tool("bash", json.dumps({
            "command": f"{sys.executable} -c \"print('FAST_DONE')\"",
            "timeout": 5,
        }))
        payload = json.loads(out)
        assert payload.get("auto_backgrounded") is None
        assert "FAST_DONE" in payload["stdout"]

    def test_timeout_converts_to_background_instead_of_killing(self, workspace: Path) -> None:
        import sys
        import time as _time
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", auto_background_on_timeout=True),
        )
        rt = AgentRuntime(workspace, config)
        script = workspace / "slow.py"
        script.write_text("import time\ntime.sleep(0.5)\nprint('SLOW_DONE')\n", encoding="utf-8")
        out = rt._execute_tool("bash", json.dumps({
            "command": f"{sys.executable} {script}",
            "timeout": 0,
        }))
        payload = json.loads(out)
        assert payload.get("auto_backgrounded") is True
        assert payload["background"] is True
        task_id = payload["task_id"]
        deadline = _time.monotonic() + 3
        while rt.tools.background_tasks[task_id].popen.poll() is None and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert rt.tools.background_tasks[task_id].popen.returncode == 0, "process must have kept running, not been killed"
        content = Path(payload["output_file"]).read_text(encoding="utf-8")
        assert "SLOW_DONE" in content


# ---------------------------------------------------------------------------
# B7: edit_file nearest-match + unicode-confusable recovery hints
# ---------------------------------------------------------------------------

class TestEditFileRecoveryHints:
    def test_nearest_match_hint_on_near_miss(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "f.py").write_text("result = calculate_total(price, quantity)\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(ValueError) as exc_info:
            registry.execute("edit_file", {
                "path": "f.py",
                "old_string": "outcome = calculate_total(price, quantity)",
                "new_string": "outcome = calculate_total(price, quantity, tax)",
            })
        msg = str(exc_info.value)
        assert "Nearest match: line 1" in msg
        assert "calculate_total" in msg

    def test_no_nearest_match_hint_when_nothing_similar(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "f.py").write_text("x = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(ValueError) as exc_info:
            registry.execute("edit_file", {"path": "f.py", "old_string": "totally_unrelated_symbol_zzz", "new_string": "y"})
        assert "Nearest match" not in str(exc_info.value)

    def test_confusable_hint_fires_on_smart_quotes(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "f.py").write_text("greeting = ‘hello’\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(ValueError) as exc_info:
            registry.execute("edit_file", {
                "path": "f.py", "old_string": "greeting = 'hello'", "new_string": "greeting = 'bye'",
            })
        msg = str(exc_info.value)
        assert "Unicode typography" in msg
        assert "line(s) 1" in msg

    def test_no_confusable_hint_when_file_has_none(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "f.py").write_text("x = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(ValueError) as exc_info:
            registry.execute("edit_file", {"path": "f.py", "old_string": "y = 2", "new_string": "y = 3"})
        assert "Unicode typography" not in str(exc_info.value)

    def test_confusable_present_but_unrelated_gives_no_false_hint(self, workspace: Path, strong_config: AppConfig) -> None:
        # File has a smart quote SOMEWHERE, but old_string's normalized form
        # still doesn't match anywhere -- must not claim it's the cause.
        (workspace / "f.py").write_text("note = ‘hi’\nx = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(ValueError) as exc_info:
            registry.execute("edit_file", {"path": "f.py", "old_string": "totally_unrelated", "new_string": "y"})
        assert "Unicode typography" not in str(exc_info.value)

    def test_successful_edit_unaffected(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "f.py").write_text("a = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        result = registry.execute("edit_file", {"path": "f.py", "old_string": "a = 1", "new_string": "a = 2"})
        assert "Edited" in result
        assert (workspace / "f.py").read_text() == "a = 2\n"


# ---------------------------------------------------------------------------
# C8/C9/C10: todo nudge/gate, post-tool reminder framework, AGENTS.md discovery
# ---------------------------------------------------------------------------

class TestTodoNudge:
    def test_does_not_fire_before_threshold(self, workspace: Path) -> None:
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", todo_nudge_turns_since_write=3),
        )
        rt = AgentRuntime(workspace, config)
        rt._turns_since_todo_write = 2
        assert rt._check_todo_nudge() == ""

    def test_fires_at_threshold_then_respects_min_gap(self, workspace: Path) -> None:
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(
                permission_mode="danger-full-access",
                todo_nudge_turns_since_write=2, todo_nudge_min_gap_turns=3,
            ),
        )
        rt = AgentRuntime(workspace, config)
        rt._turns_since_todo_write = 2
        first = rt._check_todo_nudge()
        assert "<system-reminder>" in first and "todo_write" in first
        rt._turns_since_todo_write = 2
        assert rt._check_todo_nudge() == "", "must not re-fire within min gap"

    def test_disabled_via_config(self, workspace: Path) -> None:
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", todo_nudge_enabled=False),
        )
        rt = AgentRuntime(workspace, config)
        rt._turns_since_todo_write = 999
        assert rt._check_todo_nudge() == ""


class TestReadOpenTodos:
    def test_filters_to_pending_and_in_progress(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / ".yucode" / "todos.json").write_text(json.dumps([
            {"id": "1", "content": "a", "status": "pending"},
            {"id": "2", "content": "b", "status": "in_progress"},
            {"id": "3", "content": "c", "status": "completed"},
            {"id": "4", "content": "d", "status": "cancelled"},
        ]), encoding="utf-8")
        rt = AgentRuntime(workspace, strong_config)
        open_todos = rt._read_open_todos()
        assert {t["id"] for t in open_todos} == {"1", "2"}

    def test_missing_file_returns_empty(self, workspace: Path, strong_config: AppConfig) -> None:
        rt = AgentRuntime(workspace, strong_config)
        assert rt._read_open_todos() == []

    def test_malformed_json_returns_empty_not_raises(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / ".yucode" / "todos.json").write_text("{not valid json", encoding="utf-8")
        rt = AgentRuntime(workspace, strong_config)
        assert rt._read_open_todos() == []


class _FakeTurnResponse:
    def __init__(self, text: str, input_tokens: int = 0) -> None:
        self.text = text
        self.tool_calls: list = []
        self.usage = Usage(input_tokens=input_tokens)


class _FakeTurnProvider:
    def __init__(self, texts: list[str], input_tokens: int = 0) -> None:
        self._texts = list(texts)
        self.call_count = 0
        self._input_tokens = input_tokens

    def complete(self, messages, tools, **kwargs):  # noqa: ANN001
        self.call_count += 1
        text = self._texts.pop(0) if self._texts else "final answer"
        return _FakeTurnResponse(text, input_tokens=self._input_tokens)


class TestTodoGateEndToEnd:
    def test_forces_exactly_max_fires_retries_then_accepts(self, workspace: Path) -> None:
        (workspace / ".yucode" / "todos.json").write_text(json.dumps([
            {"id": "1", "content": "finish the refactor", "status": "in_progress"},
        ]), encoding="utf-8")
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(
                permission_mode="danger-full-access",
                todo_gate_enabled=True, todo_gate_max_fires_per_prompt=1, max_iterations=5,
            ),
        )
        fake = _FakeTurnProvider(["I'm done!", "Okay, truly done now."])
        rt = AgentRuntime(workspace, config, provider=fake)  # type: ignore[arg-type]
        summary = rt.run_turn("please finish the task")
        assert fake.call_count == 2
        assert summary.final_text == "Okay, truly done now."
        supervisor_msgs = [m for m in rt.session.messages if m.role == "user" and "SYSTEM SUPERVISOR" in (m.content or "")]
        assert any("finish the refactor" in m.content for m in supervisor_msgs)

    def test_disabled_by_default_is_a_noop(self, workspace: Path) -> None:
        (workspace / ".yucode" / "todos.json").write_text(json.dumps([
            {"id": "1", "content": "still open", "status": "pending"},
        ]), encoding="utf-8")
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access"),
        )
        fake = _FakeTurnProvider(["done"])
        rt = AgentRuntime(workspace, config, provider=fake)  # type: ignore[arg-type]
        summary = rt.run_turn("do something")
        assert fake.call_count == 1
        assert summary.final_text == "done"


class TestAgentsMdDiscovery:
    def test_finds_nested_instruction_file_near_accessed_path(self, workspace: Path, strong_config: AppConfig) -> None:
        sub = workspace / "packages" / "foo"
        sub.mkdir(parents=True)
        (sub / "AGENTS.md").write_text("Follow strict typing here.", encoding="utf-8")
        (sub / "mod.py").write_text("x = 1\n", encoding="utf-8")
        rt = AgentRuntime(workspace, strong_config)
        reminder = rt._check_agents_md_discovery("read_file", json.dumps({"path": "packages/foo/mod.py"}))
        assert "<system-reminder>" in reminder
        assert "AGENTS.md" in reminder
        assert "packages" in reminder

    def test_does_not_repeat_for_same_file(self, workspace: Path, strong_config: AppConfig) -> None:
        sub = workspace / "packages" / "foo"
        sub.mkdir(parents=True)
        (sub / "AGENTS.md").write_text("rules", encoding="utf-8")
        rt = AgentRuntime(workspace, strong_config)
        first = rt._check_agents_md_discovery("read_file", json.dumps({"path": "packages/foo/x.py"}))
        second = rt._check_agents_md_discovery("read_file", json.dumps({"path": "packages/foo/y.py"}))
        assert "<system-reminder>" in first
        assert second == ""

    def test_noop_for_tools_without_path_argument(self, workspace: Path, strong_config: AppConfig) -> None:
        rt = AgentRuntime(workspace, strong_config)
        assert rt._check_agents_md_discovery("bash", json.dumps({"command": "ls"})) == ""

    def test_disabled_via_config(self, workspace: Path) -> None:
        sub = workspace / "packages" / "foo"
        sub.mkdir(parents=True)
        (sub / "AGENTS.md").write_text("rules", encoding="utf-8")
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", agents_md_discovery_enabled=False),
        )
        rt = AgentRuntime(workspace, config)
        assert rt._check_agents_md_discovery("read_file", json.dumps({"path": "packages/foo/mod.py"})) == ""

    def test_seeded_files_from_initial_discovery_are_not_reannounced(self, workspace: Path, strong_config: AppConfig) -> None:
        # A file already surfaced via the initial cwd-rooted discovery
        # (memory/prompting.py) must not be re-announced by C10.
        (workspace / "CLAUDE.md").write_text("root rules", encoding="utf-8")
        rt = AgentRuntime(workspace, strong_config)
        # Simulate what run_turn() does at the top: seed from discovery.
        from coding_agent.memory.prompting import discover_project_context
        ctx = discover_project_context(workspace, current_date="2026-07-17", include_git_context=False)
        rt._agents_md_checked.update(f.path for f in ctx.instruction_files)
        reminder = rt._check_agents_md_discovery("read_file", json.dumps({"path": "CLAUDE.md"}))
        assert reminder == ""


# ---------------------------------------------------------------------------
# C11: compaction state reinjection (edited paths / open todos / running bg tasks)
# ---------------------------------------------------------------------------

class TestCompactionStateReinjection:
    def test_edited_paths_and_open_todos_survive_compaction(self, workspace: Path) -> None:
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", compact_preserve_recent=1, compact_token_threshold=50),
        )
        rt = AgentRuntime(workspace, config)
        rt._session_edited_paths.update({"src/foo.py", "src/bar.py"})
        (workspace / ".yucode" / "todos.json").write_text(json.dumps([
            {"id": "1", "content": "wire up the new endpoint", "status": "in_progress"},
            {"id": "2", "content": "old done thing", "status": "completed"},
        ]), encoding="utf-8")
        for i in range(8):
            rt.session.add_message(Message(role="user", content=f"msg {i} " + "x" * 200))
        result = rt.compact()
        continuation = result.compacted_messages[0].content
        assert "Files edited this session:" in continuation
        assert "src/foo.py" in continuation and "src/bar.py" in continuation
        assert "Open todos:" in continuation
        assert "wire up the new endpoint" in continuation
        assert "old done thing" not in continuation

    def test_no_reminder_block_when_nothing_to_report(self, workspace: Path) -> None:
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", compact_preserve_recent=1, compact_token_threshold=50),
        )
        rt = AgentRuntime(workspace, config)
        for i in range(8):
            rt.session.add_message(Message(role="user", content=f"msg {i} " + "y" * 200))
        result = rt.compact()
        continuation = result.compacted_messages[0].content
        assert "Files edited this session" not in continuation
        assert "Open todos" not in continuation

    def test_build_state_reminder_returns_none_when_empty(self, workspace: Path, strong_config: AppConfig) -> None:
        rt = AgentRuntime(workspace, strong_config)
        assert rt._build_compaction_state_reminder() is None

    def test_running_background_task_is_reported(self, workspace: Path, strong_config: AppConfig) -> None:
        import sys
        rt = AgentRuntime(workspace, strong_config)
        rt._execute_tool("bash", json.dumps({
            "command": f"{sys.executable} -c \"import time; time.sleep(2)\"",
            "run_in_background": True,
        }))
        reminder = rt._build_compaction_state_reminder()
        assert reminder is not None
        assert "Background tasks still running:" in reminder
        # cleanup: kill the still-running process so the test doesn't leak it
        for task in rt.tools.background_tasks.values():
            task.popen.kill()


# ---------------------------------------------------------------------------
# C12: auto-compact trigger recalibration (last-response tokens, not cumulative)
# ---------------------------------------------------------------------------

class TestAutoCompactRecalibration:
    def test_does_not_fire_on_cumulative_sum_alone(self, workspace: Path, strong_config: AppConfig) -> None:
        """Regression test for the pre-fix bug: once the LIFETIME cumulative
        sum crossed the threshold once, auto-compact fired on every
        subsequent turn forever, even after a previous compaction shrank the
        real context back down. Five small responses summing above the
        threshold must not trigger it -- only a single large one should."""
        from coding_agent.core.runtime import TurnSummary
        rt = AgentRuntime(workspace, strong_config)
        rt._auto_compact_threshold = 1000
        for _ in range(5):
            rt.usage_tracker.record(Usage(input_tokens=300))
        rt._last_response_input_tokens = 300
        assert rt.usage_tracker.total_input_tokens > rt._auto_compact_threshold  # sanity: cumulative IS over
        summary = TurnSummary(final_text="", iterations=1)
        rt._maybe_auto_compact(summary)
        assert summary.auto_compaction_performed is False

    def test_fires_when_last_response_alone_crosses_threshold(self, workspace: Path, strong_config: AppConfig) -> None:
        from coding_agent.core.runtime import TurnSummary
        rt = AgentRuntime(workspace, strong_config)
        rt._auto_compact_threshold = 1000
        for i in range(8):
            rt.session.add_message(Message(role="user", content=f"msg {i} " + "x" * 200))
        rt._last_response_input_tokens = 1500
        summary = TurnSummary(final_text="", iterations=1)
        rt._maybe_auto_compact(summary)
        assert summary.auto_compaction_performed is True

    def test_last_response_input_tokens_updates_after_run_turn(self, workspace: Path, strong_config: AppConfig) -> None:
        rt = AgentRuntime(workspace, strong_config, provider=_FakeTurnProvider(["final"], input_tokens=4242))  # type: ignore[arg-type]
        assert rt._last_response_input_tokens == 0
        rt.run_turn("hello")
        assert rt._last_response_input_tokens == 4242

    def test_event_payload_uses_trigger_input_tokens_key(self, workspace: Path, strong_config: AppConfig) -> None:
        from coding_agent.core.runtime import TurnSummary
        rt = AgentRuntime(workspace, strong_config)
        rt._auto_compact_threshold = 1000
        for i in range(8):
            rt.session.add_message(Message(role="user", content=f"msg {i} " + "x" * 200))
        rt._last_response_input_tokens = 1500
        events: list[dict] = []
        summary = TurnSummary(final_text="", iterations=1)
        rt._maybe_auto_compact(summary, events.append)
        auto_compact_events = [e for e in events if e["type"] == "auto_compaction"]
        assert len(auto_compact_events) == 1
        assert auto_compact_events[0]["trigger_input_tokens"] == 1500
        assert "cumulative_input_tokens" not in auto_compact_events[0]


# ---------------------------------------------------------------------------
# P2-1: AskUserQuestion bounded timeout
# ---------------------------------------------------------------------------

class TestAskUserQuestionTimeout:
    def test_normal_answer(self) -> None:
        import coding_agent.tools as tools_mod
        with patch("builtins.input", return_value="my answer"):
            out = tools_mod._run_ask_user({"question": "pick one", "options": ["a", "b"]})
        assert json.loads(out) == {"answer": "my answer", "question": "pick one"}

    def test_eof_returns_cancelled_not_hung(self) -> None:
        import coding_agent.tools as tools_mod

        def _raise_eof(*a, **kw):  # noqa: ANN002, ANN003
            raise EOFError()

        with patch("builtins.input", side_effect=_raise_eof):
            out = tools_mod._run_ask_user({"question": "pick one"})
        payload = json.loads(out)
        assert payload["cancelled"] is True
        assert payload["reason"] == "interrupted"

    def test_timeout_fires_instead_of_hanging_forever(self) -> None:
        import time

        import coding_agent.tools as tools_mod

        def _never_returns(*a, **kw):  # noqa: ANN002, ANN003
            time.sleep(5)
            return "too late"

        with patch("builtins.input", side_effect=_never_returns), \
                patch.object(tools_mod, "_ASK_USER_TIMEOUT_SECONDS", 0.2):
            start = time.monotonic()
            out = tools_mod._run_ask_user({"question": "pick one"})
            elapsed = time.monotonic() - start
        payload = json.loads(out)
        assert payload["cancelled"] is True
        assert payload["reason"] == "timeout"
        assert elapsed < 2


# ---------------------------------------------------------------------------
# P2-2: skill summary budget-tiered truncation
# ---------------------------------------------------------------------------

def _write_skill(root: Path, name: str, description: str) -> None:
    d = root / ".yucode" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody\n", encoding="utf-8")


class TestSkillSummaryTruncation:
    def test_large_budget_keeps_full_descriptions(self, workspace: Path) -> None:
        from coding_agent.memory.skills import skill_summaries_for_prompt
        long_desc = ("This is a fairly long description. " * 10).strip()
        for i in range(5):
            _write_skill(workspace, f"skill{i:02d}", long_desc)
        rendered = skill_summaries_for_prompt(workspace, max_chars=100_000)
        assert long_desc in rendered

    def test_medium_budget_shortens_descriptions_and_fits(self, workspace: Path) -> None:
        from coding_agent.memory.skills import skill_summaries_for_prompt
        long_desc = ("This is a fairly long description. " * 10).strip()
        for i in range(20):
            _write_skill(workspace, f"skill{i:02d}", long_desc)
        rendered = skill_summaries_for_prompt(workspace, max_chars=2000)
        assert len(rendered) <= 2000
        assert "skill00" in rendered

    def test_tiny_budget_degrades_to_count_only(self, workspace: Path) -> None:
        from coding_agent.memory.skills import skill_summaries_for_prompt
        long_desc = ("This is a fairly long description. " * 10).strip()
        for i in range(20):
            _write_skill(workspace, f"skill{i:02d}", long_desc)
        rendered = skill_summaries_for_prompt(workspace, max_chars=150)
        assert "20 skills available" in rendered

    def test_no_skills_is_empty_string(self, workspace: Path) -> None:
        from coding_agent.memory.skills import skill_summaries_for_prompt
        assert skill_summaries_for_prompt(workspace) == ""

    def test_default_budget_is_bounded_not_unbounded(self, workspace: Path) -> None:
        """Regression test for the pre-fix bug: unconditional injection of
        every skill's full description with no cap at all."""
        from coding_agent.memory.skills import DEFAULT_SKILLS_SUMMARY_MAX_CHARS, skill_summaries_for_prompt
        long_desc = "X" * 2000
        for i in range(30):
            _write_skill(workspace, f"skill{i:02d}", long_desc)
        rendered = skill_summaries_for_prompt(workspace)  # uses the default budget
        assert len(rendered) <= DEFAULT_SKILLS_SUMMARY_MAX_CHARS + 200  # small slack for fallback wording


# ---------------------------------------------------------------------------
# P2-3: skill dynamic discovery tracker
# ---------------------------------------------------------------------------

class TestSkillDiscoveryTracker:
    def test_discovers_nested_skill_near_accessed_path(self, workspace: Path, strong_config: AppConfig) -> None:
        sub = workspace / "packages" / "foo"
        _write_skill(sub, "linting", "Run the linter for this package.")
        (sub / "mod.py").write_text("x = 1\n", encoding="utf-8")
        rt = AgentRuntime(workspace, strong_config)
        reminder = rt._check_skill_discovery("read_file", json.dumps({"path": "packages/foo/mod.py"}))
        assert "<system-reminder>" in reminder
        assert "linting" in reminder
        assert "Run the linter" in reminder

    def test_does_not_repeat_for_same_skill(self, workspace: Path, strong_config: AppConfig) -> None:
        sub = workspace / "packages" / "foo"
        _write_skill(sub, "linting", "desc")
        rt = AgentRuntime(workspace, strong_config)
        first = rt._check_skill_discovery("read_file", json.dumps({"path": "packages/foo/x.py"}))
        second = rt._check_skill_discovery("read_file", json.dumps({"path": "packages/foo/y.py"}))
        assert "<system-reminder>" in first
        assert second == ""

    def test_disabled_via_config(self, workspace: Path) -> None:
        sub = workspace / "packages" / "foo"
        _write_skill(sub, "linting", "desc")
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(permission_mode="danger-full-access", skill_discovery_enabled=False),
        )
        rt = AgentRuntime(workspace, config)
        assert rt._check_skill_discovery("read_file", json.dumps({"path": "packages/foo/mod.py"})) == ""

    def test_noop_for_tools_without_path_argument(self, workspace: Path, strong_config: AppConfig) -> None:
        rt = AgentRuntime(workspace, strong_config)
        assert rt._check_skill_discovery("bash", json.dumps({"command": "ls"})) == ""

    def test_seeded_skills_from_initial_prompt_are_not_reannounced(self, workspace: Path, strong_config: AppConfig) -> None:
        _write_skill(workspace, "root-skill", "at root")
        rt = AgentRuntime(workspace, strong_config)
        from coding_agent.memory.skills import list_skills
        rt._announced_skill_files.update(s.path for s in list_skills(workspace))
        reminder = rt._check_skill_discovery("read_file", json.dumps({"path": str(workspace / "top.py")}))
        assert "root-skill" not in reminder


# ---------------------------------------------------------------------------
# P2-4: web_fetch overflow -> disk artifact
# ---------------------------------------------------------------------------

@pytest.fixture
def local_http_server():
    """Serve arbitrary HTML on 127.0.0.1 for real (non-mocked) web_fetch tests."""
    import http.server
    import threading

    servers: list[http.server.HTTPServer] = []

    def _start(html: str) -> str:
        body = html.encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a) -> None:  # noqa: ANN002
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}/"

    yield _start
    for s in servers:
        s.shutdown()


class TestWebFetchOverflow:
    def test_overflowing_page_persists_full_content_and_stays_recoverable(self, workspace: Path, strong_config: AppConfig, local_http_server) -> None:
        paragraphs = [f"<p>Paragraph {i} filler text repeated for bulk padding.</p>" for i in range(400)]
        paragraphs.insert(200, "<p>DEEP_MARKER_VALUE_12345</p>")
        html = f"<html><body><h1>Big Page</h1>{''.join(paragraphs)}</body></html>"
        url = local_http_server(html)

        registry = ToolRegistry(workspace, strong_config)
        out = registry.execute("web_fetch", {"url": url})
        payload = json.loads(out)

        assert "artifact_file" in payload
        assert "Full page content" in payload["result"]
        assert "DEEP_MARKER_VALUE_12345" not in payload["result"]

        artifact = Path(payload["artifact_file"])
        assert artifact.exists()
        assert "DEEP_MARKER_VALUE_12345" in artifact.read_text(encoding="utf-8")

    def test_small_page_gets_no_artifact(self, workspace: Path, strong_config: AppConfig, local_http_server) -> None:
        url = local_http_server("<html><body><p>tiny page</p></body></html>")
        registry = ToolRegistry(workspace, strong_config)
        out = registry.execute("web_fetch", {"url": url})
        payload = json.loads(out)
        assert "artifact_file" not in payload
        assert "tiny page" in payload["result"]


# ---------------------------------------------------------------------------
# P2-5: apply_patch multi-file atomic patch tool
# ---------------------------------------------------------------------------

class TestApplyPatch:
    def test_add_file_creates_new_file(self, workspace: Path, strong_config: AppConfig) -> None:
        registry = ToolRegistry(workspace, strong_config)
        patch = (
            "*** Begin Patch\n"
            "*** Add File: hello.py\n"
            "+def hello():\n"
            '+    print("hi")\n'
            "*** End Patch"
        )
        result = registry.execute("apply_patch", {"patch": patch})
        assert "A hello.py" in result
        assert (workspace / "hello.py").read_text() == 'def hello():\n    print("hi")\n'

    def test_update_file_applies_single_hunk(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "existing.py").write_text("def bar():\n    return 1\n\ndef baz():\n    return 2\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        patch = (
            "*** Begin Patch\n"
            "*** Update File: existing.py\n"
            "@@ def bar():\n"
            " def bar():\n"
            "-    return 1\n"
            "+    return 100\n"
            "*** End Patch"
        )
        result = registry.execute("apply_patch", {"patch": patch})
        assert "M existing.py" in result
        content = (workspace / "existing.py").read_text()
        assert "return 100" in content
        assert "return 2" in content  # unrelated function survives untouched

    def test_delete_file_removes_it(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "todelete.py").write_text("x = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        patch = "*** Begin Patch\n*** Delete File: todelete.py\n*** End Patch"
        result = registry.execute("apply_patch", {"patch": patch})
        assert "D todelete.py" in result
        assert not (workspace / "todelete.py").exists()

    def test_move_to_renames_while_editing(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "old_name.py").write_text("VALUE = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        patch = (
            "*** Begin Patch\n"
            "*** Update File: old_name.py\n"
            "*** Move to: new_name.py\n"
            "@@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
            "*** End Patch"
        )
        result = registry.execute("apply_patch", {"patch": patch})
        assert "old_name.py -> new_name.py" in result
        assert not (workspace / "old_name.py").exists()
        assert (workspace / "new_name.py").read_text() == "VALUE = 2\n"

    def test_multiple_operations_across_multiple_files(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "multi_a.py").write_text("A = 1\n", encoding="utf-8")
        (workspace / "gone.py").write_text("x = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        patch = (
            "*** Begin Patch\n"
            "*** Add File: multi_new.py\n"
            "+NEW = 1\n"
            "*** Update File: multi_a.py\n"
            "@@\n"
            "-A = 1\n"
            "+A = 2\n"
            "*** Delete File: gone.py\n"
            "*** End Patch"
        )
        registry.execute("apply_patch", {"patch": patch})
        assert (workspace / "multi_new.py").read_text() == "NEW = 1\n"
        assert (workspace / "multi_a.py").read_text() == "A = 2\n"
        assert not (workspace / "gone.py").exists()

    def test_atomicity_one_bad_hunk_writes_nothing(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "atomic_a.py").write_text("A = 1\n", encoding="utf-8")
        (workspace / "atomic_b.py").write_text("B = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        patch = (
            "*** Begin Patch\n"
            "*** Update File: atomic_a.py\n"
            "@@\n"
            "-A = 1\n"
            "+A = 999\n"
            "*** Update File: atomic_b.py\n"
            "@@\n"
            "-B = NOT_THERE\n"
            "+B = 999\n"
            "*** End Patch"
        )
        with pytest.raises(ValueError, match="atomic_b.py"):
            registry.execute("apply_patch", {"patch": patch})
        assert (workspace / "atomic_a.py").read_text() == "A = 1\n"
        assert (workspace / "atomic_b.py").read_text() == "B = 1\n"

    def test_multiple_sequential_hunks_in_one_update(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "seq.py").write_text("def a():\n    return 1\n\ndef b():\n    return 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        patch = (
            "*** Begin Patch\n"
            "*** Update File: seq.py\n"
            "@@ def a():\n"
            " def a():\n"
            "-    return 1\n"
            "+    return 10\n"
            "@@ def b():\n"
            " def b():\n"
            "-    return 1\n"
            "+    return 20\n"
            "*** End Patch"
        )
        registry.execute("apply_patch", {"patch": patch})
        content = (workspace / "seq.py").read_text()
        assert "return 10" in content and "return 20" in content

    def test_end_of_file_anchor_appends_correctly(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "eof.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        patch = (
            "*** Begin Patch\n"
            "*** Update File: eof.py\n"
            "@@\n"
            " line3\n"
            "+line4\n"
            "*** End of File\n"
            "*** End Patch"
        )
        registry.execute("apply_patch", {"patch": patch})
        assert (workspace / "eof.py").read_text() == "line1\nline2\nline3\nline4\n"

    def test_preserves_no_trailing_newline(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "no_trailing_nl.py").write_text("a = 1\nb = 2", encoding="utf-8")  # no trailing \n
        registry = ToolRegistry(workspace, strong_config)
        patch = (
            "*** Begin Patch\n"
            "*** Update File: no_trailing_nl.py\n"
            "@@\n"
            "-a = 1\n"
            "+a = 100\n"
            "*** End Patch"
        )
        registry.execute("apply_patch", {"patch": patch})
        assert (workspace / "no_trailing_nl.py").read_text() == "a = 100\nb = 2"

    def test_add_file_on_existing_path_rejected(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "already.py").write_text("existing content\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        patch = "*** Begin Patch\n*** Add File: already.py\n+new content\n*** End Patch"
        with pytest.raises(ValueError, match="already exists"):
            registry.execute("apply_patch", {"patch": patch})
        assert (workspace / "already.py").read_text() == "existing content\n"

    def test_malformed_patch_missing_begin_marker(self, workspace: Path, strong_config: AppConfig) -> None:
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(ValueError, match="Begin Patch"):
            registry.execute("apply_patch", {"patch": "*** Update File: x.py\n@@\n-a\n+b"})

    def test_failed_hunk_match_gets_nearest_match_hint(self, workspace: Path, strong_config: AppConfig) -> None:
        (workspace / "nomatch.py").write_text("result = calculate_total(price, quantity)\n", encoding="utf-8")
        registry = ToolRegistry(workspace, strong_config)
        patch = (
            "*** Begin Patch\n"
            "*** Update File: nomatch.py\n"
            "@@\n"
            "-outcome = calculate_total(price, quantity)\n"
            "+outcome = calculate_total(price, quantity, tax)\n"
            "*** End Patch"
        )
        with pytest.raises(ValueError, match="Nearest match"):
            registry.execute("apply_patch", {"patch": patch})

    def test_delete_nonexistent_file_rejected(self, workspace: Path, strong_config: AppConfig) -> None:
        registry = ToolRegistry(workspace, strong_config)
        patch = "*** Begin Patch\n*** Delete File: nope.py\n*** End Patch"
        with pytest.raises(ValueError, match="not found"):
            registry.execute("apply_patch", {"patch": patch})

    def test_update_nonexistent_file_rejected(self, workspace: Path, strong_config: AppConfig) -> None:
        registry = ToolRegistry(workspace, strong_config)
        patch = "*** Begin Patch\n*** Update File: nope.py\n@@\n-a\n+b\n*** End Patch"
        with pytest.raises(ValueError, match="not found"):
            registry.execute("apply_patch", {"patch": patch})

    def test_empty_patch_body_rejected(self, workspace: Path, strong_config: AppConfig) -> None:
        registry = ToolRegistry(workspace, strong_config)
        with pytest.raises(ValueError, match="no operations"):
            registry.execute("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"})

    def test_add_file_over_size_limit_rejected(self, workspace: Path, strong_config: AppConfig) -> None:
        from coding_agent.tools.filesystem import MAX_WRITE_SIZE
        registry = ToolRegistry(workspace, strong_config)
        huge_line = "+" + ("x" * (MAX_WRITE_SIZE + 1000))
        patch = f"*** Begin Patch\n*** Add File: huge.py\n{huge_line}\n*** End Patch"
        with pytest.raises(ValueError, match="exceeds limit"):
            registry.execute("apply_patch", {"patch": patch})
        assert not (workspace / "huge.py").exists()
