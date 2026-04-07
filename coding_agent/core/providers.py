from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..config import ProviderConfig
from .session import AssistantResponse, ToolCall, Usage

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

StreamCallback = Callable[[dict[str, Any]], None]


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
                    data = json.loads(response.read().decode("utf-8"))
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
        choice = payload.get("choices", [{}])[0]
        message = choice.get("message", {})
        text = message.get("content") or ""
        tool_calls = _tool_calls_from_payload(message.get("tool_calls", []))
        raw_usage = payload.get("usage", {})
        usage = Usage(
            input_tokens=int(raw_usage.get("prompt_tokens", 0)),
            output_tokens=int(raw_usage.get("completion_tokens", 0)),
            cache_creation_input_tokens=int(raw_usage.get("prompt_tokens_details", raw_usage).get("cached_tokens_creation", raw_usage.get("cache_creation_input_tokens", 0))),
            cache_read_input_tokens=int(raw_usage.get("prompt_tokens_details", raw_usage).get("cached_tokens", raw_usage.get("cache_read_input_tokens", 0))),
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
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload_text = line[5:].strip()
            if payload_text == "[DONE]":
                break
            payload = json.loads(payload_text)
            choice = payload.get("choices", [{}])[0]
            delta = choice.get("delta", {})
            content = delta.get("content")
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
            raw_usage = payload.get("usage", {})
            if stream_callback and raw_usage:
                stream_callback({"type": "usage", "usage": raw_usage})
            usage.input_tokens = max(usage.input_tokens, int(raw_usage.get("prompt_tokens", 0)))
            usage.output_tokens = max(usage.output_tokens, int(raw_usage.get("completion_tokens", 0)))
            prompt_details = raw_usage.get("prompt_tokens_details", raw_usage)
            usage.cache_creation_input_tokens = max(
                usage.cache_creation_input_tokens,
                int(prompt_details.get("cached_tokens_creation", raw_usage.get("cache_creation_input_tokens", 0))),
            )
            usage.cache_read_input_tokens = max(
                usage.cache_read_input_tokens,
                int(prompt_details.get("cached_tokens", raw_usage.get("cache_read_input_tokens", 0))),
            )
        tool_calls = [
            ToolCall(id=item["id"], name=item["name"], arguments=item["arguments"])
            for _, item in sorted(tool_call_accumulator.items())
        ]
        return AssistantResponse(text="".join(text_parts), tool_calls=tool_calls, usage=usage)


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
