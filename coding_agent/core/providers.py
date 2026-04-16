from __future__ import annotations

import json
import logging
import queue
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..config import ProviderConfig
from .errors import ProviderError, RetriesExhaustedError
from .session import AssistantResponse, ToolCall, Usage

_log = logging.getLogger("yucode.providers")

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

StreamCallback = Callable[[dict[str, Any]], None]

_OPENAI_TOPLEVEL_KEYS = {"id", "object", "choices", "model", "usage", "created"}


def _emit_warning(
    stream_callback: StreamCallback | None,
    message: str,
    *,
    category: str,
) -> None:
    if stream_callback:
        stream_callback({"type": "warning", "warning": message, "category": category})
        return
    _log.warning(message)


def _extract_envelope_detail(payload: dict[str, Any]) -> str:
    """Pull a human-readable hint from a non-OpenAI JSON envelope."""
    for key in ("msg", "message", "error", "detail", "reason"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            inner = val.get("message") or val.get("msg") or val.get("detail")
            if isinstance(inner, str) and inner:
                return inner
    return ""


def _looks_like_openai_response(payload: dict[str, Any]) -> bool:
    """Return True if *payload* has at least one typical chat-completions key."""
    return bool(payload.keys() & _OPENAI_TOPLEVEL_KEYS)


def _extract_content_text(content: Any) -> str:
    """Extract plain text from ``message.content`` regardless of shape.

    Handles:
    - ``str``: returned as-is (standard OpenAI format).
    - ``list[dict]``: concatenates ``text`` values from blocks whose
      ``type`` is ``"text"`` (Anthropic / multi-block format).
    - ``None`` / other: returns ``""``.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type == "text" or (block_type is None and "text" in block):
                    parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def _extract_usage(raw_usage: dict[str, Any]) -> Usage:
    """Build a ``Usage`` from provider-returned usage dicts.

    Recognises both OpenAI-style keys (``prompt_tokens`` /
    ``completion_tokens``) and Anthropic/generic keys (``input_tokens``
    / ``output_tokens``).
    """
    if not raw_usage:
        return Usage()

    input_tokens = int(
        raw_usage.get("prompt_tokens", 0)
        or raw_usage.get("input_tokens", 0)
    )
    output_tokens = int(
        raw_usage.get("completion_tokens", 0)
        or raw_usage.get("output_tokens", 0)
    )

    prompt_details = raw_usage.get("prompt_tokens_details", raw_usage)
    cache_create = int(
        prompt_details.get("cached_tokens_creation", 0)
        or raw_usage.get("cache_creation_input_tokens", 0)
    )
    cache_read = int(
        prompt_details.get("cached_tokens", 0)
        or raw_usage.get("cache_read_input_tokens", 0)
    )

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_create,
        cache_read_input_tokens=cache_read,
    )


def _merge_usage_max(target: Usage, raw_usage: dict[str, Any]) -> None:
    """Update *target* with the element-wise maximum from *raw_usage*."""
    incoming = _extract_usage(raw_usage)
    target.input_tokens = max(target.input_tokens, incoming.input_tokens)
    target.output_tokens = max(target.output_tokens, incoming.output_tokens)
    target.cache_creation_input_tokens = max(
        target.cache_creation_input_tokens, incoming.cache_creation_input_tokens,
    )
    target.cache_read_input_tokens = max(
        target.cache_read_input_tokens, incoming.cache_read_input_tokens,
    )


@dataclass
class OpenAICompatibleProvider:
    config: ProviderConfig

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream_callback: StreamCallback | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> AssistantResponse:
        use_stream = self.config.streaming_mode != "no_stream"
        response = self._do_complete(
            messages, tools, stream=use_stream,
            stream_callback=stream_callback, cancel_event=cancel_event,
        )

        if (
            self.config.streaming_mode == "hybrid"
            and use_stream
            and not response.text
            and not response.tool_calls
        ):
            _log.info("Hybrid mode: streaming returned empty response, retrying without streaming.")
            if stream_callback:
                stream_callback({
                    "type": "fallback",
                    "message": "Streaming returned no usable response; retrying without streaming.",
                })
            response = self._do_complete(
                messages, tools, stream=False,
                stream_callback=stream_callback, cancel_event=cancel_event,
            )

        return response

    def _do_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool,
        stream_callback: StreamCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AssistantResponse:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "stream": stream,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.config.extra_body:
            body.update(self.config.extra_body)
        payload = json.dumps(body).encode("utf-8")
        url = self._build_url()
        headers = self._headers(stream=stream)
        context = self._ssl_context()

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            request = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds, context=context) as response:
                    if stream:
                        return self._parse_streaming_response(
                            response, stream_callback, cancel_event=cancel_event,
                        )
                    raw_body = response.read().decode("utf-8")
                    try:
                        data = json.loads(raw_body)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            f"Provider returned non-JSON response (first 200 chars): "
                            f"{raw_body[:200]!r}"
                        ) from exc
                    return self._parse_response_payload(data, stream_callback)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF_BASE * (2 ** attempt)
                    time.sleep(wait)
                    continue
                detail = exc.read().decode("utf-8", errors="replace")
                raise ProviderError(f"Provider request failed with {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if self._is_tls_verification_error(exc):
                    raise ProviderError(
                        "Provider TLS verification failed. "
                        "If your provider uses an enterprise proxy or custom certificate, "
                        "try setting `provider.verify_tls: false`.",
                        recoverable=False,
                    ) from exc
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF_BASE * (2 ** attempt)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"Provider request failed: {exc.reason}") from exc
        raise RetriesExhaustedError(
            f"Provider request failed after {_MAX_RETRIES} attempts",
            attempts=_MAX_RETRIES,
        ) from last_error

    def _build_url(self) -> str:
        if self.config.chat_path.startswith("http://") or self.config.chat_path.startswith("https://"):
            return self.config.chat_path
        if not self.config.append_chat_path:
            return self.config.base_url
        return f"{self.config.base_url}{self.config.chat_path}"

    def _headers(self, *, stream: bool | None = None) -> dict[str, str]:
        use_stream = stream if stream is not None else self.config.stream
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if use_stream else "application/json",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.extra_headers)
        return headers

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self.config.verify_tls:
            return None
        return ssl._create_unverified_context()  # noqa: S323

    def _is_tls_verification_error(self, exc: urllib.error.URLError) -> bool:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return True
        if isinstance(reason, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(reason):
            return True
        return "CERTIFICATE_VERIFY_FAILED" in str(reason)

    def _parse_response_payload(
        self,
        payload: dict[str, Any],
        stream_callback: StreamCallback | None,
    ) -> AssistantResponse:
        choices = payload.get("choices", [])
        if not choices:
            if not _looks_like_openai_response(payload):
                detail = _extract_envelope_detail(payload)
                hint = f" Server message: {detail}" if detail else ""
                raise ProviderError(
                    f"The provider endpoint ({self._build_url()}) returned a non-OpenAI "
                    f"response. Payload keys: {sorted(payload.keys())}.{hint} "
                    "This usually means base_url, chat_path, or append_chat_path is pointing at a "
                    "gateway or wrong endpoint instead of the chat completions API."
                )
            _emit_warning(
                stream_callback,
                "Provider response contained no choices. "
                f"Payload keys: {list(payload.keys())}. Check that base_url, chat_path, append_chat_path, and model are correct.",
                category="provider_no_choices",
            )
        choice = choices[0] if choices else {}
        message = choice.get("message", {})
        text = _extract_content_text(message.get("content"))
        tool_calls = _tool_calls_from_payload(message.get("tool_calls", []))
        usage = _extract_usage(payload.get("usage", {}))

        # Fallback: some models emit tool calls as <tool_call> tags in text
        if text and not tool_calls:
            text, tool_calls = _extract_text_tool_calls(text)

        if not text and not tool_calls:
            _emit_warning(
                stream_callback,
                "Provider returned an empty response (no text, no tool calls). "
                "This usually means the provider, model, or API key is misconfigured. "
                "Run `yucode doctor --workspace .` to diagnose.",
                category="provider_empty_response",
            )

        if stream_callback and text:
            stream_callback({"type": "assistant_delta", "delta": text})
        return AssistantResponse(text=text, tool_calls=tool_calls, usage=usage)

    def _parse_streaming_response(
        self,
        response: Any,
        stream_callback: StreamCallback | None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> AssistantResponse:
        text_parts: list[str] = []
        tool_call_accumulator: dict[int, dict[str, str]] = {}
        usage = Usage()
        chunk_count = 0
        saw_non_openai_stream_payload = False
        # Filter ensures <tool_call> blocks are never forwarded to the terminal
        # while still being collected in text_parts for end-of-stream parsing.
        stream_filter = _TextToolCallFilter()

        # Run the blocking socket read in a daemon thread so the main thread
        # stays responsive to Ctrl+C (KeyboardInterrupt) and stall timeouts.
        _q: queue.Queue[tuple[str, Any]] = queue.Queue()

        def _reader() -> None:
            try:
                for raw_line in response:
                    _q.put(("line", raw_line))
                _q.put(("done", None))
            except Exception as exc:  # noqa: BLE001
                _q.put(("error", exc))

        threading.Thread(target=_reader, daemon=True).start()

        _POLL = 0.1          # seconds between cancel/stall checks
        _STALL = 120.0       # seconds of silence before giving up
        idle = 0.0

        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderError("Cancelled by user", recoverable=True)
            try:
                kind, data = _q.get(timeout=_POLL)
            except queue.Empty:
                idle += _POLL
                if idle >= _STALL:
                    raise ProviderError(
                        f"Stream stalled: no data received for {_STALL:.0f}s. "
                        "The provider may have hung. Try again or set "
                        "provider.streaming_mode: no_stream.",
                        recoverable=True,
                    )
                continue
            idle = 0.0
            if kind == "done":
                break
            if kind == "error":
                exc = data
                if isinstance(exc, OSError):
                    raise ProviderError(
                        f"Stream read error: {exc}. "
                        "Try increasing provider.request_timeout_seconds or "
                        "set provider.streaming_mode: no_stream.",
                        recoverable=True,
                    ) from exc
                raise exc
            # kind == "line"
            raw_line = data
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload_text = line[5:].strip()
            if payload_text == "[DONE]":
                break
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                _log.debug("Skipping non-JSON SSE chunk: %s", payload_text[:120])
                continue
            if not isinstance(payload, dict):
                continue
            if not _looks_like_openai_response(payload):
                if not saw_non_openai_stream_payload:
                    detail = _extract_envelope_detail(payload)
                    hint = f" Server message: {detail}" if detail else ""
                    _emit_warning(
                        stream_callback,
                        f"Streaming response from {self._build_url()} returned a non-OpenAI "
                        f"SSE payload. Payload keys: {sorted(payload.keys())}.{hint} "
                        "This provider may not support OpenAI-style streaming; "
                        "try setting `provider.streaming_mode: no_stream`.",
                        category="provider_streaming_non_openai",
                    )
                    saw_non_openai_stream_payload = True
                continue
            chunk_count += 1
            choice = payload.get("choices", [{}])[0]
            delta = choice.get("delta", {})
            content = _extract_content_text(delta.get("content"))
            if content:
                text_parts.append(content)
                to_emit = stream_filter.push(content)
                if to_emit and stream_callback:
                    stream_callback({"type": "assistant_delta", "delta": to_emit})
            for tool_call_delta in delta.get("tool_calls", []):
                index = int(tool_call_delta.get("index", 0))
                function_delta = tool_call_delta.get("function", {})
                slot = tool_call_accumulator.setdefault(
                    index,
                    {
                        "id": tool_call_delta.get("id") or f"tool_{uuid4().hex[:8]}",
                        "name": "",
                        "arguments": "",
                    },
                )
                if tool_call_delta.get("id"):
                    slot["id"] = tool_call_delta["id"]
                slot["name"] += function_delta.get("name", "")
                slot["arguments"] += function_delta.get("arguments", "")
            raw_usage = payload.get("usage") or {}
            if stream_callback and raw_usage:
                stream_callback({"type": "usage", "usage": raw_usage})
            if raw_usage:
                _merge_usage_max(usage, raw_usage)

        if chunk_count == 0 and not saw_non_openai_stream_payload:
            _emit_warning(
                stream_callback,
                f"Streaming response from {self._build_url()} contained zero SSE "
                "data chunks. Either the endpoint does not support streaming, or "
                "base_url/chat_path/append_chat_path is pointing at a non-chat-completions URL. "
                "Try setting provider.streaming_mode to no_stream, or check that "
                "your base_url, chat_path, and append_chat_path are correct.",
                category="provider_empty_stream",
            )

        # Flush any text still in the filter buffer (e.g. content after the
        # last </tool_call> tag that didn't fill a full safe-emit window).
        tail = stream_filter.flush()
        if tail and stream_callback:
            stream_callback({"type": "assistant_delta", "delta": tail})

        tool_calls = [
            ToolCall(id=item["id"], name=item["name"], arguments=item["arguments"])
            for _, item in sorted(tool_call_accumulator.items())
        ]
        final_text = "".join(text_parts)
        # Fallback: some models emit tool calls as <tool_call> tags in streamed text
        if final_text and not tool_calls:
            final_text, tool_calls = _extract_text_tool_calls(final_text)
        if not final_text and not tool_calls and not saw_non_openai_stream_payload:
            _emit_warning(
                stream_callback,
                "Provider streaming completed with no text and no tool calls. "
                "Run `yucode doctor --workspace .` to check your configuration.",
                category="provider_streaming_empty_response",
            )
        return AssistantResponse(text=final_text, tool_calls=tool_calls, usage=usage)


def _tool_calls_from_payload(raw_tool_calls: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in raw_tool_calls:
        function = item.get("function", {})
        calls.append(
            ToolCall(
                id=str(item.get("id", f"tool_{uuid4().hex[:8]}")),
                name=str(function.get("name", "")),
                arguments=str(function.get("arguments", "{}")),
            )
        )
    return calls


_RE_TOOL_CALL_TAG = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)
_RE_FUNCTION_CALL_TAG = re.compile(
    r"<function_call>\s*(\{.*?\})\s*</function_call>",
    re.DOTALL,
)


def _fix_json_control_chars(text: str) -> str:
    """Escape literal control characters inside JSON string values.

    Local/quantised models (e.g. Qwen3) sometimes embed raw newlines inside
    JSON string values — invalid JSON that ``json.loads`` rejects.  This
    walks the text character-by-character, tracks string context, and escapes
    ``\\n``, ``\\r``, ``\\t`` only when they appear inside a string literal.
    """
    result: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and in_string:
            # Keep existing escape sequence intact
            result.append(c)
            i += 1
            if i < len(text):
                result.append(text[i])
                i += 1
            continue
        if c == '"':
            in_string = not in_string
            result.append(c)
        elif in_string:
            if c == "\n":
                result.append("\\n")
            elif c == "\r":
                result.append("\\r")
            elif c == "\t":
                result.append("\\t")
            else:
                result.append(c)
        else:
            result.append(c)
        i += 1
    return "".join(result)


class _TextToolCallFilter:
    """Buffers streaming text so ``<tool_call>`` / ``<function_call>`` blocks
    are never forwarded to the ``assistant_delta`` callback.

    The full text (including tool-call blocks) is still accumulated in
    ``text_parts`` so that ``_extract_text_tool_calls`` can parse them at the
    end of the stream.  This filter only governs what reaches the terminal.
    """

    _OPEN_TAGS: tuple[str, ...] = ("<tool_call>", "<function_call>")
    _CLOSE: dict[str, str] = {
        "<tool_call>": "</tool_call>",
        "<function_call>": "</function_call>",
    }
    _MAX_OPEN_LEN: int = max(len(t) for t in _OPEN_TAGS)

    def __init__(self) -> None:
        self._buf = ""
        self._open_tag = ""  # empty = not inside a suppressed block

    def push(self, chunk: str) -> str:
        """Accept a new streaming chunk; return text safe to emit."""
        self._buf += chunk
        return self._drain(flush_all=False)

    def flush(self) -> str:
        """Call at end of stream; return any remaining safe text."""
        return self._drain(flush_all=True)

    def _drain(self, flush_all: bool) -> str:
        parts: list[str] = []
        while True:
            if not self._open_tag:
                nearest, found = len(self._buf), ""
                for tag in self._OPEN_TAGS:
                    pos = self._buf.find(tag)
                    if pos != -1 and pos < nearest:
                        nearest, found = pos, tag
                if found:
                    parts.append(self._buf[:nearest])
                    self._buf = self._buf[nearest:]
                    self._open_tag = found
                    # continue to look for the close tag
                else:
                    safe = len(self._buf) if flush_all else max(0, len(self._buf) - self._MAX_OPEN_LEN)
                    parts.append(self._buf[:safe])
                    self._buf = self._buf[safe:]
                    break
            else:
                close = self._CLOSE[self._open_tag]
                cpos = self._buf.find(close)
                if cpos != -1:
                    self._buf = self._buf[cpos + len(close):]
                    self._open_tag = ""
                    # continue to handle text after the close tag
                else:
                    break  # close tag hasn't arrived yet
        return "".join(parts)


def _extract_text_tool_calls(text: str) -> tuple[str, list[ToolCall]]:
    """Parse tool calls embedded as text (e.g. ``<tool_call>{...}</tool_call>``).

    Some models (especially local/quantised ones) don't use the OpenAI
    function-calling API and instead emit tool calls as XML-wrapped JSON in the
    assistant message content.  This function extracts them, returning the
    cleaned text and a list of ToolCall objects.

    If the embedded JSON contains literal newlines inside string values
    (a common Qwen3/local-model artefact), ``_fix_json_control_chars`` is
    tried as a second-chance parse before giving up.
    """
    calls: list[ToolCall] = []
    cleaned = text

    for pattern in (_RE_TOOL_CALL_TAG, _RE_FUNCTION_CALL_TAG):
        for match in pattern.finditer(text):
            raw_json = match.group(1)
            try:
                data = json.loads(raw_json)
            except json.JSONDecodeError:
                # Second chance: escape literal control chars inside strings
                try:
                    data = json.loads(_fix_json_control_chars(raw_json))
                except json.JSONDecodeError:
                    continue
            name = data.get("name", "")
            if not name:
                continue
            arguments = data.get("arguments", data.get("parameters", {}))
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments)
            calls.append(ToolCall(
                id=f"tool_{uuid4().hex[:8]}",
                name=str(name),
                arguments=str(arguments),
            ))
            cleaned = cleaned.replace(match.group(0), "")

    return cleaned.strip(), calls
