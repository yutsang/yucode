from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..config import ProviderConfig
from .session import AssistantResponse, ToolCall, Usage

_log = logging.getLogger("yucode.providers")

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

StreamCallback = Callable[[dict[str, Any]], None]


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
    ) -> AssistantResponse:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "stream": self.config.stream,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.config.extra_body:
            body.update(self.config.extra_body)
        payload = json.dumps(body).encode("utf-8")
        url = self._build_url()

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            request = urllib.request.Request(
                url,
                data=payload,
                headers=self._headers(),
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    if self.config.stream:
                        return self._parse_streaming_response(response, stream_callback)
                    raw_body = response.read().decode("utf-8")
                    try:
                        data = json.loads(raw_body)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
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
                raise RuntimeError(f"Provider request failed with {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF_BASE * (2 ** attempt)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Provider request failed: {exc.reason}") from exc
        raise RuntimeError(f"Provider request failed after {_MAX_RETRIES} attempts") from last_error

    def _build_url(self) -> str:
        if self.config.chat_path.startswith("http://") or self.config.chat_path.startswith("https://"):
            return self.config.chat_path
        return f"{self.config.base_url}{self.config.chat_path}"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if self.config.stream else "application/json",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.extra_headers)
        return headers

    def _parse_response_payload(
        self,
        payload: dict[str, Any],
        stream_callback: StreamCallback | None,
    ) -> AssistantResponse:
        choices = payload.get("choices", [])
        if not choices:
            _emit_warning(
                stream_callback,
                "Provider response contained no choices. "
                f"Payload keys: {list(payload.keys())}. Check that base_url, chat_path, and model are correct.",
                category="provider_no_choices",
            )
        choice = choices[0] if choices else {}
        message = choice.get("message", {})
        text = _extract_content_text(message.get("content"))
        tool_calls = _tool_calls_from_payload(message.get("tool_calls", []))
        usage = _extract_usage(payload.get("usage", {}))

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
    ) -> AssistantResponse:
        text_parts: list[str] = []
        tool_call_accumulator: dict[int, dict[str, str]] = {}
        usage = Usage()
        chunk_count = 0
        for raw_line in response:
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
            chunk_count += 1
            choice = payload.get("choices", [{}])[0]
            delta = choice.get("delta", {})
            content = _extract_content_text(delta.get("content"))
            if content:
                text_parts.append(content)
                if stream_callback:
                    stream_callback({"type": "assistant_delta", "delta": content})
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

        if chunk_count == 0:
            _emit_warning(
                stream_callback,
                "Streaming response contained zero data chunks. "
                "The provider may not support streaming, or the stream was empty. "
                "Try setting provider.stream to false in your config.",
                category="provider_empty_stream",
            )

        tool_calls = [
            ToolCall(id=item["id"], name=item["name"], arguments=item["arguments"])
            for _, item in sorted(tool_call_accumulator.items())
        ]
        final_text = "".join(text_parts)
        if not final_text and not tool_calls:
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
