"""Tests for provider response parsing — content extraction, usage
extraction, and streaming edge cases.

Covers:
  - Standard OpenAI-style payloads (string content, prompt/completion tokens)
  - Alternate content shapes (block arrays, None, non-string)
  - Alternate usage keys (input_tokens / output_tokens)
  - Empty-response detection and logging
  - Streaming with and without usage chunks
  - Malformed SSE lines
"""

from __future__ import annotations

import io
import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any

import pytest

from coding_agent.config import ProviderConfig
from coding_agent.core.errors import ProviderError
from coding_agent.core.providers import (
    OpenAICompatibleProvider,
    _extract_content_text,
    _extract_envelope_detail,
    _extract_usage,
    _looks_like_openai_response,
    _merge_usage_max,
)
from coding_agent.core.session import Usage

# ---------------------------------------------------------------------------
# _extract_content_text
# ---------------------------------------------------------------------------

class TestExtractContentText:
    def test_plain_string(self):
        assert _extract_content_text("hello") == "hello"

    def test_none_returns_empty(self):
        assert _extract_content_text(None) == ""

    def test_empty_string(self):
        assert _extract_content_text("") == ""

    def test_block_array_text_only(self):
        blocks = [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ]
        assert _extract_content_text(blocks) == "Hello world"

    def test_block_array_mixed_types(self):
        blocks = [
            {"type": "thinking", "text": "internal"},
            {"type": "text", "text": "visible"},
        ]
        assert _extract_content_text(blocks) == "visible"

    def test_block_array_with_text_key_no_type(self):
        blocks = [{"text": "fallback content"}]
        assert _extract_content_text(blocks) == "fallback content"

    def test_integer_returns_empty(self):
        assert _extract_content_text(42) == ""

    def test_empty_list_returns_empty(self):
        assert _extract_content_text([]) == ""


# ---------------------------------------------------------------------------
# _extract_usage
# ---------------------------------------------------------------------------

class TestExtractUsage:
    def test_openai_style(self):
        raw = {"prompt_tokens": 100, "completion_tokens": 50}
        usage = _extract_usage(raw)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50

    def test_anthropic_style(self):
        raw = {"input_tokens": 200, "output_tokens": 80}
        usage = _extract_usage(raw)
        assert usage.input_tokens == 200
        assert usage.output_tokens == 80

    def test_openai_takes_priority_when_both_present(self):
        raw = {"prompt_tokens": 100, "completion_tokens": 50,
               "input_tokens": 200, "output_tokens": 80}
        usage = _extract_usage(raw)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50

    def test_empty_dict_returns_zeros(self):
        usage = _extract_usage({})
        assert usage.total_tokens() == 0

    def test_none_returns_zeros(self):
        usage = _extract_usage({})
        assert usage.total_tokens() == 0

    def test_cache_tokens_openai_nested(self):
        raw = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {
                "cached_tokens_creation": 10,
                "cached_tokens": 20,
            },
        }
        usage = _extract_usage(raw)
        assert usage.cache_creation_input_tokens == 10
        assert usage.cache_read_input_tokens == 20

    def test_cache_tokens_flat_keys(self):
        raw = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cache_creation_input_tokens": 15,
            "cache_read_input_tokens": 25,
        }
        usage = _extract_usage(raw)
        assert usage.cache_creation_input_tokens == 15
        assert usage.cache_read_input_tokens == 25


# ---------------------------------------------------------------------------
# _merge_usage_max
# ---------------------------------------------------------------------------

class TestMergeUsageMax:
    def test_takes_maximum(self):
        target = Usage(input_tokens=50, output_tokens=30)
        _merge_usage_max(target, {"prompt_tokens": 100, "completion_tokens": 10})
        assert target.input_tokens == 100
        assert target.output_tokens == 30

    def test_empty_raw_is_noop(self):
        target = Usage(input_tokens=50, output_tokens=30)
        _merge_usage_max(target, {})
        assert target.input_tokens == 50
        assert target.output_tokens == 30


# ---------------------------------------------------------------------------
# _parse_response_payload
# ---------------------------------------------------------------------------

def _make_provider(**overrides: Any) -> OpenAICompatibleProvider:
    defaults = {
        "name": "test",
        "api_key": "test-key",
        "base_url": "http://localhost",
        "model": "test-model",
        "stream": False,
    }
    defaults.update(overrides)
    return OpenAICompatibleProvider(config=ProviderConfig(**defaults))


class TestBuildUrl:
    def test_appends_chat_path_by_default(self):
        provider = _make_provider(base_url="https://api.example.com", chat_path="/chat/completions")
        assert provider._build_url() == "https://api.example.com/chat/completions"

    def test_uses_base_url_when_append_chat_path_disabled(self):
        provider = _make_provider(
            base_url="https://api.example.com/v1/chat/completions",
            chat_path="/chat/completions",
            append_chat_path=False,
        )
        assert provider._build_url() == "https://api.example.com/v1/chat/completions"

    def test_absolute_chat_path_takes_precedence(self):
        provider = _make_provider(
            base_url="https://api.example.com",
            chat_path="https://gateway.example.com/custom/chat",
            append_chat_path=False,
        )
        assert provider._build_url() == "https://gateway.example.com/custom/chat"


class _FakeHTTPResponse:
    def __init__(self, body: str, *, content_type: str = "application/json", status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body


class TestTlsVerification:
    def test_verify_tls_true_uses_default_context(self, monkeypatch):
        provider = _make_provider(verify_tls=True)
        seen_contexts: list[Any] = []

        def fake_urlopen(request, timeout=90, context=None):
            seen_contexts.append(context)
            return _FakeHTTPResponse(
                '{"choices":[{"message":{"content":"OK","role":"assistant"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        response = provider._do_complete([{"role": "user", "content": "hi"}], [], stream=False)
        assert response.text == "OK"
        assert seen_contexts == [None]

    def test_verify_tls_false_uses_unverified_context(self, monkeypatch):
        provider = _make_provider(verify_tls=False)
        seen_contexts: list[Any] = []

        def fake_urlopen(request, timeout=90, context=None):
            seen_contexts.append(context)
            return _FakeHTTPResponse(
                '{"choices":[{"message":{"content":"OK","role":"assistant"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        response = provider._do_complete([{"role": "user", "content": "hi"}], [], stream=False)
        assert response.text == "OK"
        assert seen_contexts
        assert seen_contexts[0] is not None

    def test_tls_verification_error_suggests_verify_tls_false(self, monkeypatch):
        provider = _make_provider(verify_tls=True)

        def fake_urlopen(request, timeout=90, context=None):
            raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(ProviderError, match="provider.verify_tls: false"):
            provider._do_complete([{"role": "user", "content": "hi"}], [], stream=False)


class TestParseResponsePayload:
    def test_standard_openai_payload(self):
        provider = _make_provider()
        payload = {
            "choices": [{
                "message": {
                    "content": "Hello!",
                    "role": "assistant",
                },
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        resp = provider._parse_response_payload(payload, None)
        assert resp.text == "Hello!"
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 5
        assert resp.tool_calls == []

    def test_block_array_content(self):
        provider = _make_provider()
        payload = {
            "choices": [{
                "message": {
                    "content": [{"type": "text", "text": "block response"}],
                    "role": "assistant",
                },
            }],
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }
        resp = provider._parse_response_payload(payload, None)
        assert resp.text == "block response"
        assert resp.usage.input_tokens == 20

    def test_empty_choices_logs_warning(self, caplog):
        provider = _make_provider()
        payload = {"id": "xxx", "object": "chat.completion"}
        with caplog.at_level(logging.WARNING, logger="yucode.providers"):
            resp = provider._parse_response_payload(payload, None)
        assert resp.text == ""
        assert any("no choices" in r.message.lower() for r in caplog.records)

    def test_null_content_with_tool_calls(self):
        provider = _make_provider()
        payload = {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                    }],
                },
            }],
            "usage": {"prompt_tokens": 15, "completion_tokens": 8},
        }
        resp = provider._parse_response_payload(payload, None)
        assert resp.text == ""
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read_file"

    def test_empty_response_logs_warning(self, caplog):
        provider = _make_provider()
        payload = {
            "choices": [{"message": {"content": "", "role": "assistant"}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        with caplog.at_level(logging.WARNING, logger="yucode.providers"):
            resp = provider._parse_response_payload(payload, None)
        assert resp.text == ""
        assert any("empty response" in r.message.lower() for r in caplog.records)

    def test_stream_callback_receives_delta(self):
        provider = _make_provider()
        payload = {
            "choices": [{"message": {"content": "hi", "role": "assistant"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        events: list[dict] = []
        provider._parse_response_payload(payload, events.append)
        assert any(e["type"] == "assistant_delta" for e in events)


# ---------------------------------------------------------------------------
# _parse_streaming_response
# ---------------------------------------------------------------------------

def _sse_lines(*chunks: str) -> io.BytesIO:
    """Build a fake SSE byte stream from a sequence of JSON payloads."""
    lines: list[bytes] = []
    for chunk in chunks:
        lines.append(f"data: {chunk}\n\n".encode())
    lines.append(b"data: [DONE]\n\n")
    return io.BytesIO(b"".join(lines))


class TestParseStreamingResponse:
    def test_standard_streaming(self):
        provider = _make_provider(stream=True)
        chunks = [
            json.dumps({"choices": [{"delta": {"content": "Hel"}}]}),
            json.dumps({"choices": [{"delta": {"content": "lo!"}}]}),
            json.dumps({"choices": [{"delta": {}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
        ]
        resp = provider._parse_streaming_response(_sse_lines(*chunks), None)
        assert resp.text == "Hello!"
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 5

    def test_streaming_without_usage(self):
        provider = _make_provider(stream=True)
        chunks = [
            json.dumps({"choices": [{"delta": {"content": "No usage"}}]}),
        ]
        resp = provider._parse_streaming_response(_sse_lines(*chunks), None)
        assert resp.text == "No usage"
        assert resp.usage.total_tokens() == 0

    def test_streaming_with_anthropic_usage_keys(self):
        provider = _make_provider(stream=True)
        chunks = [
            json.dumps({"choices": [{"delta": {"content": "ok"}}]}),
            json.dumps({"choices": [{"delta": {}}], "usage": {"input_tokens": 30, "output_tokens": 12}}),
        ]
        resp = provider._parse_streaming_response(_sse_lines(*chunks), None)
        assert resp.usage.input_tokens == 30
        assert resp.usage.output_tokens == 12

    def test_zero_chunks_logs_warning(self, caplog):
        provider = _make_provider(stream=True)
        stream = io.BytesIO(b"data: [DONE]\n\n")
        with caplog.at_level(logging.WARNING, logger="yucode.providers"):
            resp = provider._parse_streaming_response(stream, None)
        assert resp.text == ""
        assert any("zero sse data chunks" in r.message.lower() for r in caplog.records)

    def test_empty_stream_logs_warning(self, caplog):
        provider = _make_provider(stream=True)
        stream = io.BytesIO(b"")
        with caplog.at_level(logging.WARNING, logger="yucode.providers"):
            resp = provider._parse_streaming_response(stream, None)
        assert resp.text == ""
        assert any("zero sse data chunks" in r.message.lower() for r in caplog.records)

    def test_malformed_json_in_sse_skipped(self, caplog):
        provider = _make_provider(stream=True)
        raw = (
            b"data: {bad json}\n\n"
            b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        with caplog.at_level(logging.DEBUG, logger="yucode.providers"):
            resp = provider._parse_streaming_response(io.BytesIO(raw), None)
        assert resp.text == "ok"

    def test_streaming_tool_calls(self):
        provider = _make_provider(stream=True)
        chunks = [
            json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "tc1", "function": {"name": "read_file", "arguments": ""}}]}}]}),
            json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path":"x"}'}}]}}]}),
        ]
        resp = provider._parse_streaming_response(_sse_lines(*chunks), None)
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read_file"
        assert '"path"' in resp.tool_calls[0].arguments

    def test_block_content_in_streaming_delta(self):
        provider = _make_provider(stream=True)
        chunks = [
            json.dumps({"choices": [{"delta": {"content": [{"type": "text", "text": "block"}]}}]}),
        ]
        resp = provider._parse_streaming_response(_sse_lines(*chunks), None)
        assert resp.text == "block"

    def test_stream_callback_invoked(self):
        provider = _make_provider(stream=True)
        chunks = [
            json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
        ]
        events: list[dict] = []
        provider._parse_streaming_response(_sse_lines(*chunks), events.append)
        assert any(e["type"] == "assistant_delta" for e in events)

    def test_non_openai_sse_payload_warns_clearly(self):
        provider = _make_provider(stream=True)
        raw = (
            b'data: {"flag": false, "code": 400, "msg": "406 NOT_ACCEPTABLE"}\n\n'
            b"data: [DONE]\n\n"
        )
        events: list[dict] = []
        resp = provider._parse_streaming_response(io.BytesIO(raw), events.append)
        warnings = [e["warning"] for e in events if e.get("type") == "warning"]
        assert resp.text == ""
        assert any("non-OpenAI SSE payload" in warning for warning in warnings)
        assert not any("zero SSE data chunks" in warning for warning in warnings)


class TestHybridFallback:
    """Test the hybrid streaming_mode behaviour in OpenAICompatibleProvider."""

    def test_hybrid_retries_after_empty_stream(self, monkeypatch):
        provider = OpenAICompatibleProvider(
            config=ProviderConfig(
                base_url="https://example.com",
                model="test",
                api_key="k",
                stream=True,
                streaming_mode="hybrid",
            )
        )
        call_count = 0

        def fake_do_complete(self, messages, tools, *, stream, stream_callback=None, cancel_event=None):
            nonlocal call_count
            call_count += 1
            from coding_agent.core.session import AssistantResponse, Usage
            if stream:
                return AssistantResponse(text="", tool_calls=[], usage=Usage())
            return AssistantResponse(text="fallback ok", tool_calls=[], usage=Usage(input_tokens=5, output_tokens=3))

        monkeypatch.setattr(OpenAICompatibleProvider, "_do_complete", fake_do_complete)
        events: list[dict] = []
        resp = provider.complete([], [], stream_callback=events.append)
        assert resp.text == "fallback ok"
        assert call_count == 2
        assert any(e.get("type") == "fallback" for e in events)

    def test_hybrid_does_not_retry_when_stream_succeeds(self, monkeypatch):
        provider = OpenAICompatibleProvider(
            config=ProviderConfig(
                base_url="https://example.com",
                model="test",
                api_key="k",
                stream=True,
                streaming_mode="hybrid",
            )
        )
        call_count = 0

        def fake_do_complete(self, messages, tools, *, stream, stream_callback=None, cancel_event=None):
            nonlocal call_count
            call_count += 1
            from coding_agent.core.session import AssistantResponse, Usage
            return AssistantResponse(text="streamed", tool_calls=[], usage=Usage(input_tokens=10, output_tokens=5))

        monkeypatch.setattr(OpenAICompatibleProvider, "_do_complete", fake_do_complete)
        resp = provider.complete([], [])
        assert resp.text == "streamed"
        assert call_count == 1

    def test_no_stream_mode_skips_streaming(self, monkeypatch):
        provider = OpenAICompatibleProvider(
            config=ProviderConfig(
                base_url="https://example.com",
                model="test",
                api_key="k",
                stream=True,
                streaming_mode="no_stream",
            )
        )
        streams_used: list[bool] = []

        def fake_do_complete(self, messages, tools, *, stream, stream_callback=None, cancel_event=None):
            streams_used.append(stream)
            from coding_agent.core.session import AssistantResponse, Usage
            return AssistantResponse(text="non-stream", tool_calls=[], usage=Usage(input_tokens=1))

        monkeypatch.setattr(OpenAICompatibleProvider, "_do_complete", fake_do_complete)
        resp = provider.complete([], [])
        assert resp.text == "non-stream"
        assert streams_used == [False]

    def test_stream_mode_does_not_fallback(self, monkeypatch):
        provider = OpenAICompatibleProvider(
            config=ProviderConfig(
                base_url="https://example.com",
                model="test",
                api_key="k",
                stream=True,
                streaming_mode="stream",
            )
        )
        call_count = 0

        def fake_do_complete(self, messages, tools, *, stream, stream_callback=None, cancel_event=None):
            nonlocal call_count
            call_count += 1
            from coding_agent.core.session import AssistantResponse, Usage
            return AssistantResponse(text="", tool_calls=[], usage=Usage())

        monkeypatch.setattr(OpenAICompatibleProvider, "_do_complete", fake_do_complete)
        resp = provider.complete([], [])
        assert resp.text == ""
        assert call_count == 1

    def test_hybrid_retries_after_non_openai_stream_payload(self, monkeypatch):
        provider = OpenAICompatibleProvider(
            config=ProviderConfig(
                base_url="https://example.com",
                model="test",
                api_key="k",
                stream=True,
                streaming_mode="hybrid",
            )
        )
        call_count = 0

        def fake_do_complete(self, messages, tools, *, stream, stream_callback=None, cancel_event=None):
            nonlocal call_count
            call_count += 1
            from coding_agent.core.session import AssistantResponse, Usage

            if stream:
                raw = io.BytesIO(
                    b'data: {"flag": false, "code": 400, "msg": "406 NOT_ACCEPTABLE"}\n\n'
                    b"data: [DONE]\n\n"
                )
                return self._parse_streaming_response(raw, stream_callback)
            return AssistantResponse(text="fallback ok", tool_calls=[], usage=Usage(input_tokens=5, output_tokens=3))

        monkeypatch.setattr(OpenAICompatibleProvider, "_do_complete", fake_do_complete)
        events: list[dict] = []
        resp = provider.complete([], [], stream_callback=events.append)
        warnings = [e.get("warning", "") for e in events if e.get("type") == "warning"]
        assert resp.text == "fallback ok"
        assert call_count == 2
        assert any("non-OpenAI SSE payload" in warning for warning in warnings)
        assert any(e.get("type") == "fallback" for e in events)


# ---------------------------------------------------------------------------
# _looks_like_openai_response / _extract_envelope_detail
# ---------------------------------------------------------------------------


class TestLooksLikeOpenaiResponse:
    def test_standard_openai_payload(self):
        assert _looks_like_openai_response({"id": "x", "choices": [], "usage": {}})

    def test_minimal_choices_only(self):
        assert _looks_like_openai_response({"choices": [{}]})

    def test_gateway_envelope_not_openai(self):
        assert not _looks_like_openai_response({"flag": 1, "code": 0, "msg": "ok", "ts": 123})

    def test_empty_payload_not_openai(self):
        assert not _looks_like_openai_response({})

    def test_random_keys_not_openai(self):
        assert not _looks_like_openai_response({"status": "healthy", "version": "1.0"})


class TestExtractEnvelopeDetail:
    def test_msg_key(self):
        assert _extract_envelope_detail({"msg": "invalid token"}) == "invalid token"

    def test_message_key(self):
        assert _extract_envelope_detail({"message": "not found"}) == "not found"

    def test_error_dict_with_message(self):
        assert _extract_envelope_detail({"error": {"message": "bad key"}}) == "bad key"

    def test_error_string(self):
        assert _extract_envelope_detail({"error": "something broke"}) == "something broke"

    def test_no_detail(self):
        assert _extract_envelope_detail({"flag": 1, "code": 0}) == ""


# ---------------------------------------------------------------------------
# Non-OpenAI envelope detection in _parse_response_payload
# ---------------------------------------------------------------------------


class TestNonOpenaiEnvelopeDetection:
    def test_gateway_envelope_raises(self):
        provider = _make_provider()
        payload = {"flag": 1, "code": 0, "msg": "invalid api key", "ts": 1234567890}
        with pytest.raises(ProviderError, match="non-OpenAI response"):
            provider._parse_response_payload(payload, None)

    def test_gateway_envelope_includes_server_message(self):
        provider = _make_provider()
        payload = {"flag": 1, "code": 403, "msg": "auth failed", "ts": 99}
        with pytest.raises(ProviderError, match="auth failed"):
            provider._parse_response_payload(payload, None)

    def test_gateway_envelope_includes_url(self):
        provider = _make_provider(base_url="https://my-api.example.com", chat_path="/v1/chat")
        payload = {"status": "error", "detail": "not found"}
        with pytest.raises(ProviderError, match="https://my-api.example.com/v1/chat"):
            provider._parse_response_payload(payload, None)

    def test_empty_choices_with_openai_keys_warns_not_raises(self, caplog):
        """An OpenAI-shaped payload with empty choices warns but does not raise."""
        provider = _make_provider()
        payload = {"id": "chatcmpl-xxx", "object": "chat.completion", "choices": []}
        with caplog.at_level(logging.WARNING, logger="yucode.providers"):
            resp = provider._parse_response_payload(payload, None)
        assert resp.text == ""
        assert any("no choices" in r.message.lower() for r in caplog.records)

    def test_gateway_envelope_via_stream_callback(self):
        provider = _make_provider()
        payload = {"code": -1, "msg": "rate limited"}
        events: list[dict] = []
        with pytest.raises(ProviderError, match="rate limited"):
            provider._parse_response_payload(payload, events.append)


class TestStreamingZeroChunksDiagnostics:
    def test_zero_chunks_warning_includes_url(self):
        provider = _make_provider(
            base_url="https://my-provider.com",
            chat_path="/chat/completions",
            stream=True,
        )
        stream = io.BytesIO(b"data: [DONE]\n\n")
        events: list[dict] = []
        provider._parse_streaming_response(stream, events.append)
        warnings = [e for e in events if e.get("type") == "warning"]
        assert any("https://my-provider.com/chat/completions" in w["warning"] for w in warnings)

    def test_zero_chunks_warning_uses_base_url_when_append_disabled(self):
        provider = _make_provider(
            base_url="https://my-provider.com/v1/chat/completions",
            chat_path="/chat/completions",
            append_chat_path=False,
            stream=True,
        )
        stream = io.BytesIO(b"data: [DONE]\n\n")
        events: list[dict] = []
        provider._parse_streaming_response(stream, events.append)
        warnings = [e for e in events if e.get("type") == "warning"]
        assert any("https://my-provider.com/v1/chat/completions" in w["warning"] for w in warnings)


class TestTextToolCallFallback:
    """Models that emit tool calls as <tool_call> tags in text content."""

    def test_extract_tool_call_tag(self):
        from coding_agent.core.providers import _extract_text_tool_calls
        text = (
            'Here is the result:\n<tool_call>\n'
            '{"name": "write_file", "arguments": {"path": "test.md", "content": "hello"}}\n'
            '</tool_call>\n'
        )
        cleaned, calls = _extract_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "write_file"
        assert '"path": "test.md"' in calls[0].arguments
        assert "<tool_call>" not in cleaned

    def test_extract_function_call_tag(self):
        from coding_agent.core.providers import _extract_text_tool_calls
        text = '<function_call>\n{"name": "bash", "arguments": {"command": "ls"}}\n</function_call>'
        cleaned, calls = _extract_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "bash"

    def test_no_tags_returns_original(self):
        from coding_agent.core.providers import _extract_text_tool_calls
        text = "Just a normal response with no tool calls."
        cleaned, calls = _extract_text_tool_calls(text)
        assert cleaned == text
        assert calls == []

    def test_multiple_tool_calls(self):
        from coding_agent.core.providers import _extract_text_tool_calls
        text = (
            '<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>\n'
            '<tool_call>{"name": "read_file", "arguments": {"path": "b.py"}}</tool_call>'
        )
        cleaned, calls = _extract_text_tool_calls(text)
        assert len(calls) == 2
        assert calls[0].name == "read_file"
        assert calls[1].name == "read_file"

    def test_malformed_json_skipped(self):
        from coding_agent.core.providers import _extract_text_tool_calls
        text = '<tool_call>{not valid json}</tool_call>\n<tool_call>{"name": "bash", "arguments": {"command": "ls"}}</tool_call>'
        cleaned, calls = _extract_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "bash"

    def test_payload_integration_fallback(self):
        """Non-streaming response with tool calls in text triggers fallback."""
        provider = _make_provider()
        payload = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": '<tool_call>\n{"name": "write_file", "arguments": {"path": "out.txt", "content": "hi"}}\n</tool_call>',
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        response = provider._parse_response_payload(payload, None)
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "write_file"
        assert response.text == ""  # tag content stripped


class TestTextToolCallFilter:
    """The live streaming filter that hides <tool_call> blocks from the terminal."""

    def test_plain_text_emits_without_holdback(self):
        from coding_agent.core.providers import _TextToolCallFilter
        f = _TextToolCallFilter()
        assert f.push("Hello world! ") == "Hello world! "
        assert f.push("no tags here.") == "no tags here."

    def test_holds_back_only_real_tag_prefix(self):
        from coding_agent.core.providers import _TextToolCallFilter
        f = _TextToolCallFilter()
        # Ends with "<" — a valid prefix of "<tool_call>", must be withheld.
        out = f.push("Hello <")
        assert out == "Hello "
        # Completing into a non-tag emits the held-back chars.
        out2 = f.push("NOT>")
        assert out2 == "<NOT>"

    def test_suppresses_tool_call_block(self):
        from coding_agent.core.providers import _TextToolCallFilter
        f = _TextToolCallFilter()
        out = ""
        out += f.push('before <tool_call>{"name":"x","arguments":{}}</tool_call> after')
        out += f.flush()
        assert "<tool_call>" not in out
        assert "before " in out
        assert "after" in out

    def test_flush_emits_partial_tag_prefix(self):
        from coding_agent.core.providers import _TextToolCallFilter
        f = _TextToolCallFilter()
        # "<tool" is a partial prefix; on EOF it should be flushed verbatim.
        assert f.push("<tool") == ""
        assert f.flush() == "<tool"
