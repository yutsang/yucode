#!/usr/bin/env python3
"""Probe multiple provider connection styles and print comparable results.

Usage examples:

  1. Edit the USER_EDITABLE_SETTINGS block below, then run:
     python test_connection.py

  2. Or override values on the command line:
     python test_connection.py --base-url https://api.deepseek.com --api-key sk-xxx --model deepseek-chat
     python test_connection.py --base-url https://api.deepseek.com/v1/chat/completions --api-key sk-xxx --model deepseek-chat
     python test_connection.py --base-url https://api.deepseek.com --api-key sk-xxx --model deepseek-chat --all-auth --include-stream --include-insecure

The raw HTTP probes are dependency-free. There is also an optional OpenAI SDK
probe mode that mimics the working pattern in python-pptx-hr
(`OpenAI(base_url=..., api_key=..., http_client=httpx.Client(verify=False))`)
when `openai` and `httpx` are installed.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_MESSAGE = "Reply with exactly OK."

# Edit these values directly if you want to run the script without any CLI args.
USER_EDITABLE_SETTINGS = {
    "base_url": "https://api.deepseek.com",
    "api_key": "PASTE_API_KEY_HERE",
    "model": "deepseek-chat",
    "chat_path": "/chat/completions",
    "timeout": 20.0,
    "message": DEFAULT_MESSAGE,
    "include_stream": True,
    "include_insecure": True,
    "all_auth": True,
    "run_sdk_probe": True,
}


@dataclass(frozen=True)
class Attempt:
    label: str
    url: str
    auth_mode: str
    stream: bool
    verify_tls: bool


@dataclass(frozen=True)
class SdkAttempt:
    label: str
    base_url: str
    verify_tls: bool


def _normalize_base_url(base_url: str) -> str:
    return str(base_url).strip().rstrip("/")


def _normalize_path(path: str) -> str:
    text = str(path).strip()
    if not text:
        return "/chat/completions"
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if not text.startswith("/"):
        return f"/{text}"
    return text


def _looks_like_openai_payload(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("choices", "usage", "model", "id", "object"))


def _candidate_urls(base_url: str, chat_path: str) -> list[tuple[str, str]]:
    base = _normalize_base_url(base_url)
    path = _normalize_path(chat_path)
    candidates: list[tuple[str, str]] = []

    def add(label: str, url: str) -> None:
        normalized = url.strip()
        if normalized and all(existing_url != normalized for _, existing_url in candidates):
            candidates.append((label, normalized))

    if path.startswith("http://") or path.startswith("https://"):
        add("absolute-chat-path", path)
    else:
        add("append-provided-chat_path", f"{base}{path}")

    add("base_url-as-final-endpoint", base)
    add("base_url-plus-chat-completions", f"{base}/chat/completions")
    add("base_url-plus-v1-chat-completions", f"{base}/v1/chat/completions")

    if base.endswith("/v1"):
        add("base_url-v1-plus-chat-completions", f"{base}/chat/completions")
    elif not base.endswith("/v1/chat/completions"):
        add("base_url-plus-v1", f"{base}/v1")

    return candidates


def _auth_headers(api_key: str, auth_mode: str) -> dict[str, str]:
    if auth_mode == "bearer":
        return {"Authorization": f"Bearer {api_key}"}
    if auth_mode == "x-api-key":
        return {"X-API-Key": api_key}
    if auth_mode == "api-key":
        return {"api-key": api_key}
    if auth_mode == "none":
        return {}
    raise ValueError(f"Unsupported auth mode: {auth_mode}")


def _build_attempts(
    *,
    base_url: str,
    chat_path: str,
    include_stream: bool,
    include_insecure: bool,
    all_auth: bool,
) -> list[Attempt]:
    urls = _candidate_urls(base_url, chat_path)
    auth_modes = ["bearer"]
    if all_auth:
        auth_modes.extend(["x-api-key", "api-key", "none"])

    attempts: list[Attempt] = []
    for label, url in urls:
        for auth_mode in auth_modes:
            attempts.append(
                Attempt(
                    label=label,
                    url=url,
                    auth_mode=auth_mode,
                    stream=False,
                    verify_tls=True,
                )
            )
            if include_stream:
                attempts.append(
                    Attempt(
                        label=label,
                        url=url,
                        auth_mode=auth_mode,
                        stream=True,
                        verify_tls=True,
                    )
                )
            if include_insecure:
                attempts.append(
                    Attempt(
                        label=label,
                        url=url,
                        auth_mode=auth_mode,
                        stream=False,
                        verify_tls=False,
                    )
                )
                if include_stream:
                    attempts.append(
                        Attempt(
                            label=label,
                            url=url,
                            auth_mode=auth_mode,
                            stream=True,
                            verify_tls=False,
                        )
                    )
    return attempts


def _request_context(verify_tls: bool) -> ssl.SSLContext | None:
    if verify_tls:
        return None
    return ssl._create_unverified_context()  # noqa: S323


def _parse_non_stream_response(raw_body: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "body_preview": raw_body[:300],
        "json_keys": [],
        "looks_openai": False,
        "text": "",
        "tool_calls": 0,
    }
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        result["parse_error"] = "non_json"
        return result

    if not isinstance(payload, dict):
        result["parse_error"] = "json_not_object"
        return result

    result["json_keys"] = sorted(payload.keys())
    result["looks_openai"] = _looks_like_openai_payload(payload)
    choices = payload.get("choices", [])
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        if isinstance(message, dict):
            content = message.get("content", "")
            if isinstance(content, str):
                result["text"] = content
            elif isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict) and item.get("text"):
                        parts.append(str(item["text"]))
                result["text"] = "".join(parts)
            tool_calls = message.get("tool_calls", [])
            if isinstance(tool_calls, list):
                result["tool_calls"] = len(tool_calls)
    return result


def _parse_stream_response(raw_body: bytes) -> dict[str, Any]:
    lines = raw_body.decode("utf-8", errors="replace").splitlines()
    chunks: list[dict[str, Any]] = []
    collected_text: list[str] = []
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload_text = line[5:].strip()
        if payload_text == "[DONE]":
            break
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            chunks.append(payload)
            choices = payload.get("choices", [])
            if isinstance(choices, list) and choices:
                choice = choices[0] if isinstance(choices[0], dict) else {}
                delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                if isinstance(delta, dict):
                    content = delta.get("content", "")
                    if isinstance(content, str) and content:
                        collected_text.append(content)
    first_chunk = chunks[0] if chunks else {}
    return {
        "chunk_count": len(chunks),
        "first_chunk_keys": sorted(first_chunk.keys()) if isinstance(first_chunk, dict) else [],
        "text": "".join(collected_text),
        "body_preview": raw_body.decode("utf-8", errors="replace")[:300],
    }


def _run_attempt(
    attempt: Attempt,
    *,
    api_key: str,
    model: str,
    timeout: float,
    message: str,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if attempt.stream else "application/json",
        **_auth_headers(api_key, attempt.auth_mode),
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "temperature": 0.0,
        "stream": attempt.stream,
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(attempt.url, data=data, headers=headers, method="POST")
    context = _request_context(attempt.verify_tls)

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read()
            raw_info: dict[str, Any]
            if attempt.stream:
                raw_info = _parse_stream_response(body)
            else:
                raw_info = _parse_non_stream_response(body.decode("utf-8", errors="replace"))
            return {
                "ok": True,
                "http_status": getattr(response, "status", 200),
                "content_type": response.headers.get("Content-Type", ""),
                **raw_info,
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "http_status": exc.code,
            "error_type": "http_error",
            "error": str(exc),
            "body_preview": detail[:300],
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "http_status": None,
            "error_type": "url_error",
            "error": str(exc.reason),
            "body_preview": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "http_status": None,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "body_preview": "",
        }


def _print_result(index: int, attempt: Attempt, result: dict[str, Any]) -> None:
    status = "OK" if result.get("ok") else "FAIL"
    print(f"[{index:02d}] {status}  {attempt.label}")
    print(f"     url:         {attempt.url}")
    print(f"     auth:        {attempt.auth_mode}")
    print(f"     stream:      {attempt.stream}")
    print(f"     verify_tls:  {attempt.verify_tls}")
    print(f"     http_status: {result.get('http_status')}")
    if result.get("content_type"):
        print(f"     content_type:{result['content_type']}")
    if "json_keys" in result:
        print(f"     json_keys:   {result.get('json_keys')}")
    if "looks_openai" in result:
        print(f"     looks_openai:{result.get('looks_openai')}")
    if result.get("text"):
        print(f"     text:        {result['text'][:120]!r}")
    if "tool_calls" in result:
        print(f"     tool_calls:  {result.get('tool_calls')}")
    if "chunk_count" in result:
        print(f"     chunk_count: {result.get('chunk_count')}")
    if result.get("error_type"):
        print(f"     error_type:  {result['error_type']}")
    if result.get("error"):
        print(f"     error:       {result['error']}")
    if result.get("body_preview"):
        print(f"     preview:     {result['body_preview'][:160]!r}")
    print()


def _run_openai_sdk_probe(
    attempt: SdkAttempt,
    *,
    api_key: str,
    model: str,
    timeout: float,
    message: str,
) -> dict[str, Any]:
    try:
        import httpx
        from openai import OpenAI
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error_type": "missing_sdk_dependency",
            "error": f"{exc}. Install openai and httpx to use SDK probe mode.",
        }

    try:
        http_client = httpx.Client(timeout=timeout, verify=attempt.verify_tls)
        client = OpenAI(base_url=attempt.base_url, api_key=api_key, http_client=http_client)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": message}],
            temperature=0.0,
        )
        body_preview = ""
        json_keys: list[str] = []
        looks_openai = False
        text = ""
        if hasattr(response, "model_dump"):
            payload = response.model_dump()
            if isinstance(payload, dict):
                body_preview = json.dumps(payload, ensure_ascii=False)[:300]
                json_keys = sorted(payload.keys())
                looks_openai = _looks_like_openai_payload(payload)
        choices = getattr(response, "choices", None)
        if isinstance(choices, list) and choices:
            message_obj = getattr(choices[0], "message", None)
            if message_obj is not None:
                content = getattr(message_obj, "content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts: list[str] = []
                    for item in content:
                        item_text = getattr(item, "text", None)
                        if item_text:
                            parts.append(str(item_text))
                    text = "".join(parts)
        return {
            "ok": True,
            "http_status": 200,
            "content_type": "openai-sdk",
            "json_keys": json_keys,
            "looks_openai": looks_openai,
            "text": text,
            "tool_calls": 0,
            "body_preview": body_preview,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "http_status": None,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "body_preview": "",
        }


def _print_sdk_result(index: int, attempt: SdkAttempt, result: dict[str, Any]) -> None:
    status = "OK" if result.get("ok") else "FAIL"
    print(f"[SDK {index:02d}] {status}  {attempt.label}")
    print(f"         base_url:   {attempt.base_url}")
    print(f"         verify_tls: {attempt.verify_tls}")
    print(f"         http_status:{result.get('http_status')}")
    if result.get("json_keys"):
        print(f"         json_keys:  {result['json_keys']}")
    if "looks_openai" in result:
        print(f"         looks_openai:{result.get('looks_openai')}")
    if result.get("text"):
        print(f"         text:       {result['text'][:120]!r}")
    if result.get("error_type"):
        print(f"         error_type: {result['error_type']}")
    if result.get("error"):
        print(f"         error:      {result['error']}")
    if result.get("body_preview"):
        print(f"         preview:    {result['body_preview'][:160]!r}")
    print()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe many likely provider connection methods.")
    parser.add_argument("--base-url", default=USER_EDITABLE_SETTINGS["base_url"], help="Provider base URL or final endpoint URL.")
    parser.add_argument("--api-key", default=USER_EDITABLE_SETTINGS["api_key"], help="Provider API key.")
    parser.add_argument("--model", default=USER_EDITABLE_SETTINGS["model"], help="Model name to send.")
    parser.add_argument("--chat-path", default=USER_EDITABLE_SETTINGS["chat_path"], help="Relative or absolute chat path.")
    parser.add_argument("--timeout", type=float, default=USER_EDITABLE_SETTINGS["timeout"], help="Per-request timeout in seconds.")
    parser.add_argument("--message", default=USER_EDITABLE_SETTINGS["message"], help="Prompt text used for the probe.")
    parser.add_argument("--include-stream", action="store_true", help="Also test stream=true requests.")
    parser.add_argument("--no-include-stream", action="store_true", help="Disable stream=true attempts.")
    parser.add_argument(
        "--include-insecure",
        action="store_true",
        help="Also test TLS verification disabled (helpful for proxy/cert issues).",
    )
    parser.add_argument("--no-include-insecure", action="store_true", help="Disable insecure TLS attempts.")
    parser.add_argument(
        "--all-auth",
        action="store_true",
        help="Also test X-API-Key, api-key, and no-auth header variants.",
    )
    parser.add_argument("--no-all-auth", action="store_true", help="Disable alternate auth header attempts.")
    parser.add_argument("--sdk-probe", action="store_true", help="Also run an OpenAI SDK style probe like python-pptx-hr.")
    parser.add_argument("--no-sdk-probe", action="store_true", help="Disable the OpenAI SDK style probe.")
    parser.set_defaults(
        include_stream=bool(USER_EDITABLE_SETTINGS["include_stream"]),
        include_insecure=bool(USER_EDITABLE_SETTINGS["include_insecure"]),
        all_auth=bool(USER_EDITABLE_SETTINGS["all_auth"]),
        run_sdk_probe=bool(USER_EDITABLE_SETTINGS["run_sdk_probe"]),
    )
    args = parser.parse_args(argv)
    if args.no_include_stream:
        args.include_stream = False
    if args.no_include_insecure:
        args.include_insecure = False
    if args.no_all_auth:
        args.all_auth = False
    if args.sdk_probe:
        args.run_sdk_probe = True
    if args.no_sdk_probe:
        args.run_sdk_probe = False
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.base_url or args.base_url == "PASTE_BASE_URL_HERE":
        print("Please set USER_EDITABLE_SETTINGS['base_url'] or pass --base-url.", file=sys.stderr)
        return 2
    if not args.api_key or args.api_key == "PASTE_API_KEY_HERE":
        print("Please set USER_EDITABLE_SETTINGS['api_key'] or pass --api-key.", file=sys.stderr)
        return 2
    if not args.model or args.model == "PASTE_MODEL_NAME_HERE":
        print("Please set USER_EDITABLE_SETTINGS['model'] or pass --model.", file=sys.stderr)
        return 2

    attempts = _build_attempts(
        base_url=args.base_url,
        chat_path=args.chat_path,
        include_stream=args.include_stream,
        include_insecure=args.include_insecure,
        all_auth=args.all_auth,
    )

    print("Testing candidate connection methods...\n")
    print(f"base_url:  {args.base_url}")
    print(f"chat_path: {args.chat_path}")
    print(f"model:     {args.model}")
    print(f"attempts:  {len(attempts)}")
    print(f"sdk_probe: {args.run_sdk_probe}")
    print()

    successful: list[tuple[Attempt, dict[str, Any]]] = []
    for index, attempt in enumerate(attempts, start=1):
        result = _run_attempt(
            attempt,
            api_key=args.api_key,
            model=args.model,
            timeout=args.timeout,
            message=args.message,
        )
        _print_result(index, attempt, result)
        if result.get("ok"):
            successful.append((attempt, result))

    sdk_successful: list[tuple[SdkAttempt, dict[str, Any]]] = []
    if args.run_sdk_probe:
        print("OpenAI SDK style probes")
        print("-----------------------")
        sdk_attempts = [SdkAttempt(label="openai-sdk-verify-true", base_url=args.base_url, verify_tls=True)]
        if args.include_insecure:
            sdk_attempts.append(SdkAttempt(label="openai-sdk-verify-false-hr-style", base_url=args.base_url, verify_tls=False))
        for index, attempt in enumerate(sdk_attempts, start=1):
            result = _run_openai_sdk_probe(
                attempt,
                api_key=args.api_key,
                model=args.model,
                timeout=args.timeout,
                message=args.message,
            )
            _print_sdk_result(index, attempt, result)
            if result.get("ok"):
                sdk_successful.append((attempt, result))

    print("Summary")
    print("-------")
    if not successful and not sdk_successful:
        print("No attempt completed successfully.")
        return 1

    summary_index = 1
    for attempt, result in successful:
        print(
            f"{summary_index}. raw-http | {attempt.label} | url={attempt.url} | auth={attempt.auth_mode} | "
            f"stream={attempt.stream} | verify_tls={attempt.verify_tls} | looks_openai={result.get('looks_openai')}"
        )
        summary_index += 1
    for attempt, result in sdk_successful:
        print(
            f"{summary_index}. openai-sdk | {attempt.label} | base_url={attempt.base_url} | "
            f"verify_tls={attempt.verify_tls} | looks_openai={result.get('looks_openai')}"
        )
        summary_index += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
