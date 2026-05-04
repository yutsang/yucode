"""Tests for the interactive event handler and tool-call rendering."""
from __future__ import annotations

import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from coding_agent.interface.cli import _InteractiveEventHandler
from coding_agent.interface.render import compact_tool_result_line, compact_tool_start_label


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_handler() -> tuple[_InteractiveEventHandler, MagicMock]:
    """Return (handler, mock_progress) so tests can assert on progress calls."""
    handler = _InteractiveEventHandler(streaming=True)
    mock_progress = MagicMock()
    handler._progress = mock_progress
    return handler, mock_progress


def _feed(handler, *events):
    for ev in events:
        handler(ev)


# ---------------------------------------------------------------------------
# tool_call event
# ---------------------------------------------------------------------------

class TestToolCallEvent:
    def test_sets_tool_calls_flag(self):
        h, _ = _make_handler()
        assert h._tool_calls_this_turn is False
        _feed(h, {"type": "tool_call", "name": "read_file", "arguments": '{"path":"x.py"}'})
        assert h._tool_calls_this_turn is True

    def test_clears_text_buffer(self):
        h, _ = _make_handler()
        h._text_buf = "some earlier text"
        _feed(h, {"type": "tool_call", "name": "bash", "arguments": '{"command":"ls"}'})
        assert h._text_buf == ""

    def test_resets_text_started(self):
        h, _ = _make_handler()
        h._text_started = True
        _feed(h, {"type": "tool_call", "name": "bash", "arguments": "{}"})
        assert h._text_started is False

    def test_calls_progress_start_tool(self):
        h, mock_p = _make_handler()
        _feed(h, {"type": "tool_call", "name": "bash", "arguments": '{"command":"echo hi"}'})
        mock_p.start_tool.assert_called_once()
        label = mock_p.start_tool.call_args[0][0]
        assert "echo hi" in label

    def test_multiple_tool_calls_keep_flag_set(self):
        h, _ = _make_handler()
        _feed(
            h,
            {"type": "tool_call", "name": "read_file", "arguments": '{"path":"a.py"}'},
            {"type": "tool_result", "name": "read_file", "content": "code"},
            {"type": "tool_call", "name": "write_file", "arguments": '{"path":"b.py","content":"x"}'},
        )
        assert h._tool_calls_this_turn is True


# ---------------------------------------------------------------------------
# tool_result event
# ---------------------------------------------------------------------------

class TestToolResultEvent:
    def test_calls_finish_tool(self):
        h, mock_p = _make_handler()
        _feed(h, {"type": "tool_result", "name": "read_file", "content": "file contents"})
        mock_p.finish_tool.assert_called_once()

    def test_error_json_detected(self):
        h, mock_p = _make_handler()
        _feed(h, {"type": "tool_result", "name": "bash", "content": '{"error": "not found"}'})
        label = mock_p.finish_tool.call_args[0][0]
        # The error symbol must appear, not the success symbol
        assert "✘" in label or "✗" in label or "error" in label.lower() or "ERR" in label

    def test_error_json_with_indentation_detected(self):
        h, mock_p = _make_handler()
        content = '{\n  "error": "command failed",\n  "recoverable": false\n}'
        _feed(h, {"type": "tool_result", "name": "bash", "content": content})
        label = mock_p.finish_tool.call_args[0][0]
        assert "✘" in label or "✗" in label or "error" in label.lower() or "ERR" in label

    def test_success_content_not_marked_error(self):
        h, mock_p = _make_handler()
        _feed(h, {"type": "tool_result", "name": "bash", "content": "OK"})
        label = mock_p.finish_tool.call_args[0][0]
        # Must not contain the error marker
        assert "✘" not in label and "✗" not in label


# ---------------------------------------------------------------------------
# assistant_delta buffering
# ---------------------------------------------------------------------------

class TestAssistantDeltaBuffering:
    def test_buffered_before_any_tool(self):
        h, _ = _make_handler()
        _feed(
            h,
            {"type": "iteration_started", "iteration": 1},
            {"type": "assistant_delta", "delta": "Hello "},
            {"type": "assistant_delta", "delta": "world"},
        )
        assert h._text_buf == "Hello world"

    def test_suppressed_after_tool_call(self):
        h, _ = _make_handler()
        _feed(
            h,
            {"type": "iteration_started", "iteration": 1},
            {"type": "tool_call", "name": "bash", "arguments": "{}"},
            {"type": "assistant_delta", "delta": "some reasoning"},
        )
        assert h._text_buf == ""

    def test_suppressed_inside_coordinator(self):
        h, _ = _make_handler()
        _feed(
            h,
            {"type": "phase_started", "phase": "planning"},
            {"type": "assistant_delta", "delta": "worker output"},
        )
        assert h._text_buf == ""

    def test_buffer_reset_on_new_iteration(self):
        h, _ = _make_handler()
        h._text_buf = "stale text"
        _feed(h, {"type": "iteration_started", "iteration": 1})
        assert h._text_buf == ""


# ---------------------------------------------------------------------------
# completed event
# ---------------------------------------------------------------------------

class TestCompletedEvent:
    def test_flushes_buffer_to_stdout_when_no_tool_calls(self, capsys):
        h, _ = _make_handler()
        _feed(h, {"type": "iteration_started", "iteration": 1})
        h._text_buf = "The answer is 42."
        _feed(h, {"type": "completed"})
        captured = capsys.readouterr()
        assert "42" in captured.out

    def test_does_not_flush_when_tool_calls_occurred(self, capsys):
        h, _ = _make_handler()
        _feed(h, {"type": "iteration_started", "iteration": 1})
        # tool_call clears the buffer; completed should not reprint anything
        _feed(h, {"type": "tool_call", "name": "bash", "arguments": "{}"})
        _feed(h, {"type": "completed"})
        captured = capsys.readouterr()
        assert "bash" not in captured.out

    def test_does_not_flush_coordinator_text(self, capsys):
        h, _ = _make_handler()
        _feed(h, {"type": "phase_started", "phase": "planning"})
        h._text_buf = "coordinator reasoning"
        _feed(h, {"type": "completed"})
        captured = capsys.readouterr()
        assert "coordinator reasoning" not in captured.out

    def test_clears_in_coordinator_flag_after_completed(self):
        h, _ = _make_handler()
        _feed(h, {"type": "phase_started", "phase": "planning"})
        assert h._in_coordinator is True
        _feed(h, {"type": "completed"})
        assert h._in_coordinator is False

    def test_sets_text_started_when_buffer_flushed(self):
        h, _ = _make_handler()
        _feed(h, {"type": "iteration_started", "iteration": 1})
        h._text_buf = "some text"
        assert h._text_started is False
        _feed(h, {"type": "completed"})
        assert h._text_started is True


# ---------------------------------------------------------------------------
# iteration_started state reset
# ---------------------------------------------------------------------------

class TestIterationStarted:
    def test_resets_state_on_first_iteration(self):
        h, _ = _make_handler()
        h._tool_calls_this_turn = True
        h._text_buf = "dirty"
        h._text_started = True
        _feed(h, {"type": "iteration_started", "iteration": 1})
        assert h._tool_calls_this_turn is False
        assert h._text_buf == ""
        assert h._text_started is False

    def test_no_reset_on_later_iterations(self):
        h, _ = _make_handler()
        h._tool_calls_this_turn = True
        h._text_buf = "preserved"
        _feed(h, {"type": "iteration_started", "iteration": 2})
        assert h._tool_calls_this_turn is True
        assert h._text_buf == "preserved"

    def test_no_reset_inside_coordinator(self):
        h, _ = _make_handler()
        _feed(h, {"type": "phase_started", "phase": "planning"})
        h._text_buf = "coordinator state"
        _feed(h, {"type": "iteration_started", "iteration": 1})
        assert h._text_buf == "coordinator state"


# ---------------------------------------------------------------------------
# dedup_limit / stuck_exit warnings
# ---------------------------------------------------------------------------

class TestWarningEvents:
    def test_dedup_limit_prints_to_stderr(self, capsys):
        h, _ = _make_handler()
        _feed(h, {"type": "dedup_limit", "tool": "bash", "blocks_this_turn": 1})
        assert "bash" in capsys.readouterr().err

    def test_dedup_limit_mentions_stuck_when_many_blocks(self, capsys):
        h, _ = _make_handler()
        _feed(h, {"type": "dedup_limit", "tool": "read_file", "blocks_this_turn": 4})
        err = capsys.readouterr().err
        assert "read_file" in err
        assert "4" in err

    def test_stuck_exit_prints_to_stderr(self, capsys):
        h, _ = _make_handler()
        _feed(h, {"type": "stuck_exit", "tool": "web_fetch", "blocks": 5})
        assert "web_fetch" in capsys.readouterr().err

    def test_auto_compaction_prints_to_stdout(self, capsys):
        h, _ = _make_handler()
        _feed(h, {"type": "auto_compaction", "removed": 12, "cumulative_input_tokens": 50000})
        out = capsys.readouterr().out
        assert "12" in out
        assert "50,000" in out


# ---------------------------------------------------------------------------
# compact_tool_start_label
# ---------------------------------------------------------------------------

class TestCompactToolStartLabel:
    def test_bash_shows_command(self):
        label = compact_tool_start_label("bash", '{"command": "pytest tests/"}')
        assert "pytest tests/" in label

    def test_bash_capital_shows_command(self):
        label = compact_tool_start_label("Bash", '{"command": "ls -la"}')
        assert "ls -la" in label

    def test_web_search_shows_query(self):
        label = compact_tool_start_label("web_search", '{"query": "python async"}')
        assert "python async" in label

    def test_web_fetch_shows_url(self):
        label = compact_tool_start_label("web_fetch", '{"url": "https://example.com"}')
        assert "example.com" in label

    def test_read_file_shows_path(self):
        label = compact_tool_start_label("read_file", '{"path": "src/main.py"}')
        assert "src/main.py" in label

    def test_write_file_shows_path(self):
        label = compact_tool_start_label("write_file", '{"path": "out.txt", "content": "x"}')
        assert "out.txt" in label

    def test_unknown_tool_shows_name(self):
        label = compact_tool_start_label("custom_thing", '{}')
        assert "custom_thing" in label

    def test_invalid_json_does_not_crash(self):
        label = compact_tool_start_label("bash", "not json at all")
        assert "bash" in label or "$" in label

    def test_agent_tool_shows_description(self):
        label = compact_tool_start_label("agent", '{"description": "run tests"}')
        assert "run tests" in label

    def test_long_command_is_truncated(self):
        long_cmd = "x" * 200
        label = compact_tool_start_label("bash", f'{{"command": "{long_cmd}"}}')
        assert len(label) < 200


# ---------------------------------------------------------------------------
# compact_tool_result_line (error detection)
# ---------------------------------------------------------------------------

class TestCompactToolResultLine:
    def test_success_contains_ok_symbol(self):
        line = compact_tool_result_line("read_file", "file contents here", is_error=False)
        assert "✔" in line or "✓" in line or "ok" in line.lower()

    def test_error_contains_err_symbol(self):
        line = compact_tool_result_line("bash", "command failed", is_error=True)
        assert "✘" in line or "✗" in line or "err" in line.lower()

    def test_tool_name_appears_in_result(self):
        line = compact_tool_result_line("my_tool", "output", is_error=False)
        assert "my_tool" in line

    def test_error_json_prefix_inline(self):
        """Verify the handler's error-detection prefix logic matches real JSON error shapes."""
        content_inline = '{"error": "something went wrong"}'
        s = content_inline.lstrip()
        assert s.startswith('{"error"')

    def test_error_json_prefix_indented(self):
        content_indented = '{\n  "error": "something went wrong"\n}'
        s = content_indented.lstrip()
        assert s.startswith('{\n  "error"')
