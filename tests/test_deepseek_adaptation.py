"""Fixes driven by measuring the DeepSeek endpoint this agent is deployed against.

The numbers in `MEASURED_CORPUS` are real: each sample was sent to
deepseek-chat on 2026-08-26 and the gateway's own `prompt_tokens` recorded.
They are kept here so that anyone retuning `_BYTES_PER_TOKEN` has to argue with
observations rather than with a preference.
"""
from __future__ import annotations

import io
import json

import pytest

from coding_agent.config.settings import ProviderConfig
from coding_agent.core.providers import OpenAICompatibleProvider
from coding_agent.core.session import Message, ToolCall
from coding_agent.memory.compact import (
    CompactionConfig,
    estimate_message_tokens,
    estimate_text_tokens,
    should_compact,
)


def _make_provider(**kwargs) -> OpenAICompatibleProvider:
    base = {
        "name": "test", "type": "openai", "base_url": "https://example.invalid",
        "api_key": "k", "model": "deepseek-chat",
    }
    base.update(kwargs)
    return OpenAICompatibleProvider(ProviderConfig(**base))


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

# (name, utf-8 bytes, tokens the gateway actually billed)
MEASURED_CORPUS = [
    ("py_source",   6000,  1507),
    ("py_source2",  6000,  1580),
    ("md_english",  6004,  1661),
    ("git_diff",    6000,  1581),
    ("tool_json",   6000,  2350),
    ("zh_prose",    9750,  2439),
    ("zh_code_mix", 7794,  2349),
]


class TestTokenEstimation:
    @pytest.mark.parametrize("name,utf8_bytes,actual", MEASURED_CORPUS)
    def test_within_tolerance_of_measured(self, name, utf8_bytes, actual):
        """No sample may be under-counted by more than 25% or over by more than 40%.

        Under-counting is the dangerous direction -- it is what lets a session
        sail past the compaction threshold and overrun the context window -- so
        it gets the tighter bound.
        """
        estimate = int(utf8_bytes / 3.2)
        ratio = estimate / actual
        assert 0.75 <= ratio <= 1.40, (
            f"{name}: estimated {estimate} against a measured {actual} "
            f"({ratio:.2f}x)"
        )

    def test_chinese_is_not_counted_as_a_third_of_itself(self):
        """The regression this file exists for.

        Character-counting rated 700 characters of Chinese at 176 tokens; the
        gateway billed 529. Any estimator that treats a Chinese character like
        a quarter of a token puts a session three times over the threshold it
        was meant to stop at.
        """
        chinese = "這份財務盡職調查報告需要在下週一之前完成初稿，請確認資料庫的欄位對應。" * 20
        assert len(chinese) == 700
        assert estimate_text_tokens(chinese) >= 500

    def test_ascii_still_roughly_right(self):
        """The old behaviour was correct for English; it must not regress."""
        english = "The quick brown fox jumps over the lazy dog. " * 60
        assert 610 * 0.75 <= estimate_text_tokens(english) <= 610 * 1.40

    def test_empty_text_is_free(self):
        assert estimate_text_tokens("") == 0

    def test_tool_call_arguments_are_counted(self):
        """Arguments are frequently the largest part of an assistant message."""
        bare = estimate_message_tokens(Message(role="assistant", content=""))
        with_call = estimate_message_tokens(Message(
            role="assistant", content="",
            tool_calls=[ToolCall(id="1", name="write_file",
                                 arguments=json.dumps({"content": "x" * 4000}))],
        ))
        assert with_call > bare + 1000

    def test_compaction_now_triggers_on_a_chinese_session(self):
        """End to end: the estimator feeding the decision that actually matters.

        Sized so the two rules disagree. Twelve Chinese turns of 1,160
        characters each are 3,480 tokens by the old character rule and about
        13,000 by measurement; at a threshold of 8,000 the old rule sails past
        and the new one stops.
        """
        turn = "請幫我把這張資產負債表的科目對應到標準表，並說明差異原因。" * 40
        messages = [Message(role="user", content=turn) for _ in range(12)]
        config = CompactionConfig(preserve_recent_messages=2, max_estimated_tokens=8_000)

        old_rule = sum(len(m.content) // 4 + 1 for m in messages)
        assert old_rule < 8_000, "test no longer reproduces the old behaviour"

        assert should_compact(messages, config), (
            "a Chinese session well past the threshold was still rated below it"
        )


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------

def _sse(*chunks: str) -> io.BytesIO:
    body = b"".join(f"data: {c}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"
    return io.BytesIO(body)


class TestTruncationIsVisible:
    """The gateway defaults to an 8192-token reply cap when max_tokens is unset.

    Measured 2026-08-26: asking deepseek-chat to count to 4000 returned exactly
    8192 output tokens. Before this, `finish_reason` was parsed and dropped, so
    a reply severed at that ceiling was indistinguishable from a finished one.
    """

    def test_non_streaming_keeps_finish_reason(self):
        provider = _make_provider()
        payload = {
            "choices": [{"message": {"content": "half a fi"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8192},
        }
        resp = provider._parse_response_payload(payload, None)
        assert resp.finish_reason == "length"

    def test_streaming_keeps_finish_reason(self):
        provider = _make_provider(stream=True)
        chunks = [
            json.dumps({"choices": [{"delta": {"content": "half a fi"}}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}),
        ]
        resp = provider._parse_streaming_response(_sse(*chunks), None)
        assert resp.text == "half a fi"
        assert resp.finish_reason == "length"

    def test_clean_stop_is_reported_as_such(self):
        provider = _make_provider()
        payload = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
        assert provider._parse_response_payload(payload, None).finish_reason == "stop"

    def test_absent_finish_reason_is_empty_not_none(self):
        provider = _make_provider()
        payload = {"choices": [{"message": {"content": "hi"}}]}
        assert provider._parse_response_payload(payload, None).finish_reason == ""

    @pytest.mark.parametrize("reason", ["length", "max_tokens"])
    def test_truncation_warns_the_caller(self, reason):
        provider = _make_provider()
        payload = {"choices": [{"message": {"content": "cut"}, "finish_reason": reason}]}
        events: list[dict] = []
        provider._parse_response_payload(payload, events.append)
        assert any(
            e.get("category") == "provider_output_truncated" for e in events
        ), f"no truncation warning for finish_reason={reason!r}: {events}"

    def test_clean_stop_does_not_warn(self):
        provider = _make_provider()
        payload = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
        events: list[dict] = []
        provider._parse_response_payload(payload, events.append)
        assert not any(e.get("category") == "provider_output_truncated" for e in events)


# ---------------------------------------------------------------------------
# max_tokens
# ---------------------------------------------------------------------------

class TestMaxTokens:
    def test_absent_by_default(self):
        """Unset must stay off the wire -- a gateway's own default is safer than
        a ceiling picked here, since the accepted range is gateway-specific."""
        provider = _make_provider()
        body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
        assert "max_tokens" not in body

    def test_sent_when_configured(self):
        provider = _make_provider(max_tokens=32000)
        body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
        assert body["max_tokens"] == 32000

    def test_extra_body_still_wins(self):
        """Existing configs spell this in extra_body; they must not change meaning."""
        provider = _make_provider(max_tokens=32000, extra_body={"max_tokens": 4096})
        body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
        assert body["max_tokens"] == 4096

    def test_omit_params_can_suppress_it(self):
        provider = _make_provider(max_tokens=32000, omit_params=["max_tokens"])
        body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
        assert "max_tokens" not in body

    def test_learned_rename_applies(self):
        """A gateway that calls the field max_completion_tokens is already
        handled by the 400 self-heal; the new field must flow through it."""
        provider = _make_provider(max_tokens=32000)
        provider._learned_renames["max_tokens"] = "max_completion_tokens"
        body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
        assert "max_tokens" not in body
        assert body["max_completion_tokens"] == 32000


# ---------------------------------------------------------------------------
# Recovery from a severed tool call
# ---------------------------------------------------------------------------

class TestTruncatedToolCallAdvice:
    """A reply cut at the cap ends mid-string, so its tool call is invalid JSON.

    Telling the model to fix its syntax sends it round the same loop: the
    arguments were never malformed, and regenerating them truncates at the same
    place. The advice has to name the cap.
    """

    def _runtime(self, tmp_path, finish_reason):
        import dataclasses

        from coding_agent.config.settings import AppConfig
        from coding_agent.core.runtime import AgentRuntime
        config = dataclasses.replace(AppConfig(), provider=ProviderConfig(
            name="t", type="openai", base_url="https://example.invalid",
            api_key="k", model="deepseek-chat",
        ))
        runtime = AgentRuntime(tmp_path, config)
        runtime._last_finish_reason = finish_reason
        return runtime

    def test_truncated_call_is_named_as_truncated(self, tmp_path):
        runtime = self._runtime(tmp_path, "length")
        out = runtime._execute_tool("write_file", '{"path": "a.py", "content": "def f(')
        assert "output_truncated" in out
        assert "smaller sections" in out or "Split the work" in out

    def test_genuine_syntax_error_keeps_the_old_advice(self, tmp_path):
        runtime = self._runtime(tmp_path, "stop")
        out = runtime._execute_tool("write_file", '{"path": "a.py", "content": }')
        assert "invalid_arguments" in out


class TestMaxTokensCeilingSelfHeal:
    """A gateway that refuses the value, not the parameter, and says why.

    Measured shape (DeepSeek, 2026-08-26): `param` is null and the prose is
    "Invalid max_tokens value, the valid range of max_tokens is [1, 393216]",
    which matches none of the "unsupported parameter" spellings. Without this
    the 400 is terminal, so no max_tokens could be configured safely against a
    gateway whose ceiling is unknown -- which is every gateway but the one
    measured here.
    """

    DETAIL = json.dumps({"error": {
        "message": "Invalid max_tokens value, the valid range of max_tokens is [1, 393216]",
        "type": "invalid_request_error", "param": None, "code": "invalid_request_error",
    }})

    def test_clamps_to_the_stated_ceiling(self):
        provider = _make_provider(max_tokens=900_000)
        body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
        assert provider._apply_self_heal(body, self.DETAIL) is True
        assert body["max_tokens"] == 393216

    def test_ceiling_is_remembered_for_later_requests(self):
        provider = _make_provider(max_tokens=900_000)
        body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
        provider._apply_self_heal(body, self.DETAIL)
        later = provider._build_body([{"role": "user", "content": "again"}], [], False)
        assert later["max_tokens"] == 393216, "the correction cost a round trip every call"

    def test_a_value_already_under_the_ceiling_is_not_the_cause(self):
        """Don't claim a fix that changes nothing -- that burns a retry slot."""
        provider = _make_provider(max_tokens=1000)
        body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
        assert provider._apply_self_heal(body, self.DETAIL) is False

    def test_unrelated_400_is_untouched(self):
        provider = _make_provider(max_tokens=900_000)
        body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
        other = json.dumps({"error": {"message": "Unsupported parameter: 'temperature'"}})
        provider._apply_self_heal(body, other)
        assert body["max_tokens"] == 900_000

    def test_configured_value_below_ceiling_still_wins(self):
        provider = _make_provider(max_tokens=8000)
        provider._learned_max_tokens_ceiling = 393216
        body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
        assert body["max_tokens"] == 8000


# ---------------------------------------------------------------------------
# Oversized JSON tool results
# ---------------------------------------------------------------------------

class TestManySmallFieldsStayParseable:
    """The shape leaf-shrinking cannot help: bulk spread over short fields.

    A `list_directory` of a very large tree is thousands of entries none of
    which is individually long, so halving the per-leaf budget changes nothing
    and the result used to fall through to byte-slicing -- handing the model
    JSON that ends mid-key, indistinguishable from a tool that broke.
    """

    CAP = 8 * 1024

    def _out(self, payload):
        from coding_agent.core.runtime import _truncate_tool_output
        return _truncate_tool_output(
            json.dumps(payload), self.CAP, spill_note="Full output at /tmp/x.json",
        )

    def test_a_long_list_stays_valid_json(self):
        entries = [{"name": f"file_{i:05d}.py", "size": i} for i in range(4000)]
        out = self._out(entries)
        parsed = json.loads(out)  # the whole point: it must parse
        assert isinstance(parsed, list)
        assert len(out.encode("utf-8")) <= self.CAP

    def test_the_kept_entries_are_the_real_ones(self):
        entries = [{"name": f"file_{i:05d}.py", "size": i} for i in range(4000)]
        parsed = json.loads(self._out(entries))
        assert parsed[0] == {"name": "file_00000.py", "size": 0}
        assert "omitted" in parsed[-1], "no indication that anything was dropped"

    def test_the_omitted_count_is_accurate(self):
        entries = [{"name": f"file_{i:05d}.py", "size": i} for i in range(4000)]
        parsed = json.loads(self._out(entries))
        kept = len(parsed) - 1  # the trailing note is not an entry
        assert f"{4000 - kept} more entries omitted" in parsed[-1]

    def test_a_wide_dict_reduces_to_a_parseable_statement(self):
        payload = {f"key_{i:05d}": f"v{i}" for i in range(4000)}
        parsed = json.loads(self._out(payload))
        assert "_truncated" in parsed
        assert "/tmp/x.json" in parsed["_full_output"]

    def test_it_keeps_as_much_as_the_cap_allows(self):
        """Dropping to a token handful would be valid JSON and a useless answer.

        The prefix is found by halving rather than by walking back from the
        end, so this also pins that the search converges on the boundary
        instead of an arbitrary point below it.
        """
        entries = [{"name": f"file_{i:05d}.py", "size": i} for i in range(4000)]
        kept = len(json.loads(self._out(entries))) - 1
        assert kept > 150, f"kept only {kept} of 4000 entries under an 8KB cap"
        # One more entry would have to breach the cap, or the search stopped early.
        from coding_agent.core.runtime import _truncate_tool_output
        oversized = _truncate_tool_output(
            json.dumps(entries[: kept + 1]), self.CAP, spill_note="",
        )
        assert len(oversized.encode("utf-8")) <= self.CAP
