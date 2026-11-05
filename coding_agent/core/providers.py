from __future__ import annotations

import json
import logging
import queue
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import uuid4

from ..config import ProviderConfig
from .errors import ProviderError, RetriesExhaustedError
from .session import AssistantResponse, ToolCall, Usage

_log = logging.getLogger("yucode.providers")

_MAX_RETRIES = 5
_RETRY_BACKOFF_BASE = 1.5
_RETRY_JITTER_FRACTION = 0.2
_RETRYABLE_STATUS_CODES = {
    429, 500, 502, 503, 504,
    # Cloudflare edge failures (origin down / connection timed out / empty
    # reply) and 529 "overloaded", which several hosted-model gateways return
    # under load. All of them mean "try again", not "your request is wrong".
    520, 521, 522, 523, 524, 529,
}
# Total wait across 5 attempts with base=1.5: 1.5 + 3 + 6 + 12 + 24 = ~46s
# matches typical provider-gateway recovery windows. Each wait carries
# +/-_RETRY_JITTER_FRACTION jitter so that concurrent coordinator workers which
# hit the same 429 do not retry in lockstep and collide again.

# Upper bound on an honoured `Retry-After`. The header is a hint from a server
# we do not control: a misconfigured or hostile gateway answering
# `Retry-After: 86400` would otherwise park the agent for a day. 60s is chosen
# because it exceeds the whole computed ladder (~46s), so a server asking for a
# genuine rate-limit window is still obeyed in full in the common case, while
# the worst case stays bounded at 4 waits x 60s and remains interruptible by
# the cancel event.
_MAX_RETRY_AFTER_SECONDS = 60.0

# Second streaming stall timer, alongside the transport-level one inside
# _parse_streaming_response. That one is reset by *any* line off the socket,
# including the SSE comment lines (": ping") gateways send to keep the
# connection warm. Without this timer, a gateway that keeps the socket busy and
# never produces output holds the turn open forever. 300s leaves room for a
# slow reasoning model to take minutes before its first chunk.
#
# What resets it is deliberately narrow: only a chunk that *produced
# something* — a text delta, a reasoning delta, a tool-call delta, or a
# finish_reason. Well-formed `data:` lines are not enough, because the two
# observed hang shapes both send those forever: an empty
# `{"choices":[{"delta":{}}]}` heartbeat, and an origin error page smuggled
# through as `data: <html>502...</html>`. Keying off the `data:` prefix left
# both invisible to *both* timers (the socket never goes quiet either).
# Usage-only chunks do not reset it: they arrive at end of stream, immediately
# before [DONE], so nothing is waiting on them.
_STREAM_CONTENT_STALL_SECONDS = 300.0

# A failure inside a live stream gets its own, much shorter, retry budget than
# the HTTP ladder. The arithmetic is why: the expensive stream failure is the
# content stall, which by construction costs a full
# _STREAM_CONTENT_STALL_SECONDS (300s) before it is even detected. Reusing the
# 5-attempt ladder would put a mute gateway at ~25 minutes of wall clock for
# one turn — worse for the user than the single 300s failure this retry is
# meant to improve on. Two retries keeps the worst case near 15 minutes while
# still covering the cheap case (a socket reset, which fails immediately).
_MAX_STREAM_RETRIES = 2

# Statuses where the edge answered but the *origin* did not: 521 (web server
# is down), 522 (TCP handshake to the origin timed out), 523 (origin
# unreachable), 524 (origin took the connection and never answered). These are
# the connection-level failure the circuit breaker exists for, wearing an HTTP
# status code — so they count as breaker failures rather than proof of a
# healthy gateway.
#
# Deliberately not here:
#   - 429 / 529: rate limit and overload. The gateway is alive and doing its
#     job correctly; failing other calls fast would be wrong.
#   - 500 / 520: the origin answered, badly. Application-level, not host-level.
#   - 502 / 503 / 504: any proxy emits these for a single slow or oversized
#     request as readily as for a dead upstream, so they are far weaker
#     evidence about the *host*. Opening the breaker on them would convert a
#     request-scoped failure into a 30s outage for every other call.
# All of them stay in _RETRYABLE_STATUS_CODES either way; this set only
# governs what the breaker believes.
_ORIGIN_DOWN_STATUS_CODES = {521, 522, 523, 524}


def _jittered(seconds: float, *, never_shorter: bool = False) -> float:
    """Spread a backoff delay by +/-_RETRY_JITTER_FRACTION.

    ``never_shorter`` keeps the jitter one-sided (never below *seconds*), which
    is what a server-supplied ``Retry-After`` needs: still desynchronised from
    the other workers, but never retrying earlier than the server asked.
    """
    low = 1.0 if never_shorter else 1.0 - _RETRY_JITTER_FRACTION
    return seconds * random.uniform(low, 1.0 + _RETRY_JITTER_FRACTION)


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """Seconds to wait per the response's ``Retry-After``, or None if absent.

    Accepts both forms RFC 9110 allows (delta-seconds and HTTP-date) and
    clamps the result to _MAX_RETRY_AFTER_SECONDS.

    None also means "the header carries no usable wait" — a header of ``0``,
    a negative delta, a NaN (``float()`` parses "nan" happily), or an
    HTTP-date already in the past. Those must fall back to the exponential
    ladder rather than resolve to a delay of zero: the one-sided jitter
    cannot spread a zero, so honouring it literally would fire the whole
    remaining ladder as back-to-back requests at a gateway that is already
    rate-limiting or overloaded.
    """
    headers = getattr(exc, "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    # Written as `not (x > 0)` so NaN lands here too.
    if not (seconds > 0.0):
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)

# Shared circuit breaker, keyed by gateway host. It trips on "this host is not
# usable right now", which is two things:
#   1. Connection-level failure (URLError: refused / unreachable / timeout).
#   2. An _ORIGIN_DOWN_STATUS_CODES answer. The edge replied, so this is not a
#      connection failure in the socket sense — but the reply says the origin
#      is down, which is the same outage wearing an HTTP status. Every *other*
#      HTTP status still counts as success: the gateway answered from its own
#      logic, so the host is fine and the problem is the request.
# Without the breaker every call pays the full retry ladder: up to
# 5 × request_timeout_seconds + ~46s backoff ≈ 8 minutes on the default
# config. Concurrent coordinator workers and subagents each pay that
# independently. After _BREAKER_FAILURE_THRESHOLD consecutive failed attempts,
# subsequent calls to the same host fail fast for _BREAKER_COOLDOWN_SECONDS,
# then one probe call is let through (half-open): success closes the breaker,
# failure re-opens it.
_BREAKER_FAILURE_THRESHOLD = 3
_BREAKER_COOLDOWN_SECONDS = 30.0


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    opened_at: float | None = None


_circuit_states: dict[str, _CircuitState] = {}
_circuit_lock = threading.Lock()


def _breaker_remaining_cooldown(host: str) -> float | None:
    """Seconds of cooldown left if the breaker for *host* is open, else None.

    Returning None when the cooldown has elapsed is what implements the
    half-open probe: the caller proceeds with a real request, and the
    outcome (success/failure) closes or re-opens the breaker."""
    with _circuit_lock:
        state = _circuit_states.get(host)
        if state is None or state.opened_at is None:
            return None
        elapsed = time.monotonic() - state.opened_at
        if elapsed >= _BREAKER_COOLDOWN_SECONDS:
            return None
        return _BREAKER_COOLDOWN_SECONDS - elapsed


def _breaker_record_connection_failure(host: str) -> None:
    """Record one failed attempt against *host*.

    Named for the original connection-level case; it now also carries the
    origin-down HTTP statuses (see _ORIGIN_DOWN_STATUS_CODES), which are the
    same outage reported by an edge that is still up. The name is kept because
    `diagnose_env.py` and the hardening tests call it directly.
    """
    with _circuit_lock:
        state = _circuit_states.setdefault(host, _CircuitState())
        state.consecutive_failures += 1
        if state.consecutive_failures >= _BREAKER_FAILURE_THRESHOLD:
            state.opened_at = time.monotonic()


def _breaker_record_success(host: str) -> None:
    with _circuit_lock:
        if host in _circuit_states:
            del _circuit_states[host]


def _reset_circuit_breakers() -> None:
    """Test hook: clear all breaker state."""
    with _circuit_lock:
        _circuit_states.clear()

# Some gateways (e.g. Azure-fronted reasoning models) 400 on individual body
# params they don't support. _MAX_SELF_HEAL_ROUNDS bounds how many distinct
# params we'll drop/rename in response to consecutive 400s before giving up.
_MAX_SELF_HEAL_ROUNDS = 4
_MAX_TOKENS_ALIASES = ("max_tokens", "max_completion_tokens")
# A gateway refusing the *value* rather than the parameter, and naming the
# ceiling while it does. Measured from the configured DeepSeek endpoint:
#   {"error": {"message": "Invalid max_tokens value, the valid range of
#              max_tokens is [1, 393216]", "param": null, ...}}
# `param` is null and the prose matches none of the "unsupported parameter"
# spellings, so _extract_rejected_param finds nothing and the 400 surfaces as a
# hard failure. Since the safe ceiling is a property of the gateway and cannot
# be known from here, the only portable way to configure max_tokens at all is
# to ask for one and accept the correction.
_RE_MAX_TOKENS_CEILING = re.compile(
    r"max_(?:tokens|completion_tokens)\b[^[]*\[\s*\d+\s*,\s*(\d+)\s*\]"
)
_RE_REJECTED_PARAM = re.compile(
    r"[Uu]nsupported (?:parameter|value)[:\s]+['\"]([a-zA-Z_][a-zA-Z0-9_]*)['\"]"
    r"|['\"]([a-zA-Z_][a-zA-Z0-9_]*)['\"]\s+is not (?:supported|allowed)"
)

# From gpt-5.6 onwards, a chat-completions request that carries function tools
# is rejected outright unless reasoning_effort is explicitly "none". Verbatim:
#
#   Function tools with reasoning_effort are not supported for gpt-5.6-terra
#   in /v1/chat/completions. To use function tools, use /v1/responses or set
#   reasoning_effort to 'none'.
#
# The model reasons by default, so *omitting* the param fails too — which is
# why the two existing self-heal actions (drop, or rename) cannot fix this
# one. The only correction that works is to set a value.
#
# THIS IS A STOPGAP, NOT THE FIX. It buys a working session at the cost of the
# model's reasoning; the actual remedy is to speak the Responses API, which
# yucode does not. Do not read the presence of this heal as the problem being
# solved.
_RE_TOOLS_VS_REASONING = re.compile(
    r"reasoning_effort", re.IGNORECASE,
)
_RE_TOOLS_VS_REASONING_REMEDY = re.compile(
    r"function tools?|/v1/responses", re.IGNORECASE,
)

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


def _error_message_text(detail_text: str) -> str:
    """The human-readable message out of an error body, or the body itself."""
    try:
        payload = json.loads(detail_text)
    except json.JSONDecodeError:
        return detail_text
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message", "")) or detail_text
        if isinstance(error, str) and error:
            return error
    return detail_text


def _is_tools_vs_reasoning_400(detail_text: str, param: str) -> bool:
    """True if this 400 is the gpt-5.6 "function tools + reasoning" refusal.

    Deliberately narrow. The message has to name ``reasoning_effort`` *and*
    the remedy the gateway itself offers (function tools / the Responses API),
    and a structured ``error.param``, if the gateway sent one, has to agree.
    A plain "unsupported parameter: 'reasoning_effort'" — a gateway that
    simply does not know the param — must keep taking the ordinary drop path:
    forcing ``none`` there would put back the very field it rejected, on every
    later request.
    """
    if param and param != "reasoning_effort":
        return False
    message = _error_message_text(detail_text)
    return bool(
        _RE_TOOLS_VS_REASONING.search(message)
        and _RE_TOOLS_VS_REASONING_REMEDY.search(message)
    )


def _extract_max_tokens_ceiling(detail_text: str) -> int:
    """Pull the upper bound out of a 400 that refuses a max_tokens *value*.

    Returns 0 when the message says nothing about a range, which is the common
    case -- this only fires on the one error shape that names its own limit.
    """
    match = _RE_MAX_TOKENS_CEILING.search(detail_text)
    if not match:
        return 0
    try:
        ceiling = int(match.group(1))
    except ValueError:
        return 0
    return ceiling if ceiling > 0 else 0


def _extract_rejected_param(detail_text: str) -> str:
    """Best-effort extraction of the offending body param from a 400 error.

    Tries the structured ``error.param`` field first (OpenAI/Azure gateway
    convention), then falls back to a regex over the message text for
    gateways that only describe the problem in prose.
    """
    message = detail_text
    try:
        payload = json.loads(detail_text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            param = error.get("param")
            if isinstance(param, str) and param:
                return param
            message = str(error.get("message", "")) or message
        elif isinstance(error, str) and error:
            message = error
    match = _RE_REJECTED_PARAM.search(message)
    if match:
        return match.group(1) or match.group(2) or ""
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


# A model that stopped because it ran out of output budget, rather than
# because it was done. Both spellings are in the wild: OpenAI and DeepSeek say
# "length", some gateways relay Anthropic's "max_tokens".
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})


def _warn_if_truncated(finish_reason: str, stream_callback: StreamCallback | None) -> None:
    """Say so when the gateway cut the reply off at the output cap.

    Measured against the configured DeepSeek endpoint on 2026-08-26: it accepts
    ``max_tokens`` anywhere in [1, 393216] but defaults to **8192** when the
    request omits it, and yucode omitted it entirely — ``ProviderConfig`` had no
    such field. At the measured 3.4 UTF-8 bytes per token that ceiling lands at
    roughly 28KB of generated text, which a single large ``write_file`` clears
    easily.

    Nothing downstream could see it happen. Both parse paths read
    ``finish_reason`` off the wire -- the streaming one only to decide the chunk
    counted as output -- and neither kept it, so a tool call whose JSON arguments
    were severed mid-string was handed on as a finished response. The visible
    symptom is a file written with its tail missing, or a tool call dropped for
    unparseable arguments, with nothing in the transcript explaining either.
    """
    if finish_reason in _TRUNCATED_FINISH_REASONS:
        _emit_warning(
            stream_callback,
            "The model stopped because it hit the output token cap, not because "
            "it finished (finish_reason="
            f"{finish_reason!r}). Anything it was writing — file contents, a tool "
            "call's arguments — is cut off mid-way. Raise `provider.max_tokens` "
            "in settings.yml, or ask for the work in smaller pieces.",
            category="provider_output_truncated",
        )


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

    # The two vocabularies disagree about what the cache counts mean. OpenAI's
    # `prompt_tokens_details.cached_tokens` is a *breakdown* of prompt_tokens,
    # already inside it; Anthropic's `cache_read_input_tokens` /
    # `cache_creation_input_tokens` are separate buckets counted *alongside*
    # `input_tokens`. `Usage` keeps the OpenAI meaning throughout, which is
    # what lets `Usage.total_tokens()` be input+output — so an Anthropic-shaped
    # body has to be normalised here, or a reply of
    # {"input_tokens": 12, "cache_read_input_tokens": 48000} is reported as 12
    # prompt tokens in /cost and in the headless JSON usage block.
    #
    # The discriminator is the key the *totals* arrived under, not the cache
    # keys: a body carrying `prompt_tokens` (or an OpenAI details block) is
    # speaking OpenAI, so its cache counts stay a breakdown even when spelled
    # with the flat names, which some OpenAI-compatible gateways do.
    if "prompt_tokens" not in raw_usage and "prompt_tokens_details" not in raw_usage:
        input_tokens += cache_create + cache_read

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
    # Params a 400 has taught us this gateway/model rejects, keyed by name
    # (dropped) or renamed (e.g. max_tokens -> max_completion_tokens).
    # Learned once, applied to every subsequent call for the life of this
    # provider instance (one instance per session) so only the first request
    # pays the extra round-trip.
    _learned_omit_params: set[str] = field(default_factory=set)
    _learned_renames: dict[str, str] = field(default_factory=dict)
    # Set once a 400 proves this gateway refuses function tools alongside
    # reasoning on /v1/chat/completions (see _is_tools_vs_reasoning_400).
    # Unlike the two above this one *adds* a value rather than removing one,
    # because omitting reasoning_effort does not help: gpt-5.6 reasons by
    # default, so carrying tools is enough to trigger the refusal.
    _learned_reasoning_effort_none: bool = False
    # The gateway's own max_tokens ceiling, learned from a 400 that named it.
    # 0 means nothing has been learned and the configured value stands.
    _learned_max_tokens_ceiling: int = 0

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream_callback: StreamCallback | None = None,
        *,
        cancel_event: threading.Event | None = None,
        body_overrides: dict[str, Any] | None = None,
    ) -> AssistantResponse:
        use_stream = self.config.streaming_mode != "no_stream"
        response = self._do_complete(
            messages, tools, stream=use_stream,
            stream_callback=stream_callback, cancel_event=cancel_event,
            body_overrides=body_overrides,
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
                body_overrides=body_overrides,
            )

        return response

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool,
        body_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "stream": stream,
        }
        if self.config.max_tokens > 0:
            # Set before extra_body so an existing config that spells it there
            # keeps winning, and before the omit/rename learning below picks it
            # up if this gateway calls the field max_completion_tokens.
            body["max_tokens"] = self.config.max_tokens
            if self._learned_max_tokens_ceiling:
                body["max_tokens"] = min(
                    body["max_tokens"], self._learned_max_tokens_ceiling,
                )
        if stream:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.config.extra_body:
            body.update(self.config.extra_body)
        # A concrete `provider.reasoning_effort` applies to every request, not
        # just the ones run_turn() routes through body_overrides -- the
        # coordinator's planner/validator/synthesis calls don't pass overrides,
        # and before this field existed they picked the value up from
        # extra_body. "auto" is a yucode-level sentinel meaning "let the
        # per-prompt heuristic decide", so it is never sent to the gateway.
        #
        # The sentinel check reads through configured_reasoning_effort() so
        # both configuration paths behave identically: an "auto" that arrived
        # via the legacy extra_body key was already merged into the body above
        # and has to be taken back out, or every call that passes no
        # body_overrides ships a literal "auto" and takes a 400. Writing back
        # stays keyed off the first-class field only, so any other extra_body
        # value passes through untouched.
        if self.config.configured_reasoning_effort().lower() == "auto":
            body.pop("reasoning_effort", None)
        elif (configured_effort := (self.config.reasoning_effort or "").strip()):
            body["reasoning_effort"] = configured_effort
        if body_overrides:
            body.update(body_overrides)
        if self._learned_reasoning_effort_none:
            # Deliberately applied *after* body_overrides. run_turn() sends a
            # per-turn reasoning_effort on every turn; if that won here, every
            # turn the heuristic rated above "none" would 400 again and the
            # correction would never stick. A hard refusal measured from the
            # gateway outranks any local preference, config or per-turn.
            body["reasoning_effort"] = "none"
        for renamed_from, renamed_to in self._learned_renames.items():
            if renamed_from in body:
                body[renamed_to] = body.pop(renamed_from)
        for param in (*self.config.omit_params, *self._learned_omit_params):
            body.pop(param, None)
        return body

    def _apply_self_heal(self, body: dict[str, Any], detail: str) -> bool:
        """Try to fix *body* in place after a 400 naming an unsupported param.

        Returns True if a correction was applied (worth retrying immediately),
        False if the rejected param couldn't be identified or isn't present
        in the body. Corrections are also remembered on the instance (see
        ``_learned_omit_params`` / ``_learned_renames``) so later calls apply
        them up front via ``_build_body``.
        """
        param = _extract_rejected_param(detail)
        if _is_tools_vs_reasoning_400(detail, param):
            if self._learned_reasoning_effort_none:
                # Already pinned to "none" and still refused — nothing else
                # this heal can try, so let the 400 surface.
                return False
            self._learned_reasoning_effort_none = True
            # The two learned states are contradictory, so establishing one
            # has to clear the other. A gateway can teach both lessons in one
            # session (a plain "unsupported parameter: 'reasoning_effort'"
            # 400 first, then this one after a deployment upgrade or a
            # reworded refusal). This retry would succeed either way — it
            # edits `body` in place — but every later _build_body writes
            # "none" and then the stale omit entry pops it straight back out,
            # so the request ships without the param, 400s again, and lands
            # here with the pin already set: nothing left to try, session
            # unrecoverable. Cleared here rather than by reordering
            # _build_body so the pin never overrides a user's explicit
            # `provider.omit_params`, only what a previous 400 taught us.
            self._learned_omit_params.discard("reasoning_effort")
            body["reasoning_effort"] = "none"
            _log.warning(
                "Gateway refuses function tools together with reasoning on "
                "/v1/chat/completions, so reasoning_effort is pinned to 'none' "
                "for the rest of this session — the model will no longer "
                "reason. This is a stopgap; the real fix is the Responses API "
                "(/v1/responses), which yucode does not speak. Gateway said: %s",
                _error_message_text(detail).strip()[:300],
            )
            return True
        ceiling = _extract_max_tokens_ceiling(detail)
        if ceiling:
            for alias in _MAX_TOKENS_ALIASES:
                current = body.get(alias)
                if isinstance(current, int) and current > ceiling:
                    body[alias] = ceiling
                    self._learned_max_tokens_ceiling = ceiling
                    _log.warning(
                        "Gateway caps %s at %d; the configured %d was refused. "
                        "Using the cap for the rest of this session — set "
                        "provider.max_tokens to %d or less to avoid the extra "
                        "round trip.", alias, ceiling, current, ceiling,
                    )
                    return True
            return False
        if not param:
            return False
        if param in _MAX_TOKENS_ALIASES:
            other = "max_completion_tokens" if param == "max_tokens" else "max_tokens"
            if param in body:
                body[other] = body.pop(param)
                self._learned_renames[param] = other
                return True
            return False
        if param in body:
            del body[param]
            self._learned_omit_params.add(param)
            if param == "reasoning_effort":
                # Same contradiction, other order: the gateway no longer knows
                # the param at all, so a pin left over from an earlier
                # tools-vs-reasoning 400 would write it back on every later
                # request and 400 forever.
                self._learned_reasoning_effort_none = False
            return True
        return False

    def _do_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool,
        stream_callback: StreamCallback | None = None,
        cancel_event: threading.Event | None = None,
        body_overrides: dict[str, Any] | None = None,
    ) -> AssistantResponse:
        body = self._build_body(messages, tools, stream, body_overrides)
        url = self._build_url()
        headers = self._headers(stream=stream)
        context = self._ssl_context()

        host = urllib.parse.urlsplit(url).netloc or url

        def _check_breaker() -> None:
            remaining = _breaker_remaining_cooldown(host)
            if remaining is not None:
                raise ProviderError(
                    f"Provider gateway {host} is marked unavailable after "
                    f"{_BREAKER_FAILURE_THRESHOLD}+ consecutive unreachable-host "
                    f"failures (connection refused/timed out, or an edge status "
                    f"reporting the origin is down) — failing fast instead of "
                    f"re-paying the full retry ladder. "
                    f"Next probe allowed in {remaining:.0f}s.",
                    recoverable=True,
                )

        def _backoff(seconds: float) -> None:
            # A plain time.sleep here would ignore a user cancellation for the
            # whole backoff window (up to 24s on the last retry) — wait on the
            # cancel event instead so Esc/stop takes effect immediately.
            if cancel_event is None:
                time.sleep(seconds)
            elif cancel_event.wait(seconds):
                raise ProviderError("Cancelled by user", recoverable=True)

        # A streamed turn can only be replayed while nothing from it has
        # reached the user: assistant_delta events go straight to the terminal
        # (interface/cli.py), so retrying after one has been emitted would
        # print the reply twice. Wrapping the caller's callback is the only
        # place that fact is knowable — the parse itself has already handed
        # the text off by the time it fails.
        emitted_to_user = False
        stream_retries = 0
        tracked_callback: StreamCallback | None = None
        if stream_callback is not None:
            def tracked_callback(event: dict[str, Any]) -> None:  # noqa: F811
                nonlocal emitted_to_user
                if event.get("type") == "assistant_delta" and event.get("delta"):
                    emitted_to_user = True
                stream_callback(event)

        last_error: Exception | None = None
        self_heal_rounds = 0
        for attempt in range(_MAX_RETRIES):
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderError("Cancelled by user", recoverable=True)
            # Checked per attempt, not just per call: failures are recorded
            # per connection attempt below, so a dead gateway aborts this
            # call's remaining retry ladder as soon as the threshold trips
            # (and a half-open probe call stops after a single failed
            # attempt re-opens the breaker).
            _check_breaker()
            payload = json.dumps(body).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
                method="POST",
            )
            stream_retry = False
            try:
                with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds, context=context) as response:
                    _breaker_record_success(host)
                    if stream:
                        try:
                            return self._parse_streaming_response(
                                response, tracked_callback, cancel_event=cancel_event,
                            )
                        except ProviderError as exc:
                            # Failures inside the stream (stall, socket reset)
                            # are raised from *inside* this try, where neither
                            # the HTTPError nor the URLError handler below can
                            # see them — so before this they cost the whole
                            # turn on the first hiccup, with no retry at all.
                            cancelled = cancel_event is not None and cancel_event.is_set()
                            if (
                                not exc.recoverable
                                or cancelled
                                or emitted_to_user
                                or stream_retries >= _MAX_STREAM_RETRIES
                                or attempt >= _MAX_RETRIES - 1
                            ):
                                raise
                            last_error = exc
                            stream_retries += 1
                            _emit_warning(
                                stream_callback,
                                f"Streaming response failed before producing output "
                                f"({exc}); retrying ({stream_retries} of "
                                f"{_MAX_STREAM_RETRIES}).",
                                category="provider_stream_retry",
                            )
                            stream_retry = True
                    else:
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
                if exc.code in _ORIGIN_DOWN_STATUS_CODES:
                    # The edge answered; the origin behind it did not. Counting
                    # this as a healthy gateway is what made the breaker
                    # unopenable against a dead origin — a permanent 522 used
                    # to cost the full ladder on every call, forever.
                    _breaker_record_connection_failure(host)
                elif exc.code in _RETRYABLE_STATUS_CODES:
                    # 429 and 503 were being counted as proof of health, which
                    # left an overloaded gateway costing the full ladder on
                    # every call with nothing ever tripping. Excluding them from
                    # the origin-down set was right — a proxy returns them for
                    # one slow request too — but that only argues a *single*
                    # one proves nothing, not that a run of them proves nothing.
                    #
                    # The ladder itself separates the two cases, so nothing new
                    # has to: a transient overload clears on retry and reaches
                    # the success path below, while one that outlasts all five
                    # attempts is a host that cannot serve us. Intermediate
                    # attempts stay neutral — neither clearing the count nor
                    # adding to it — so a retry that succeeds is still recorded
                    # as the success it was.
                    if attempt >= _MAX_RETRIES - 1:
                        _breaker_record_connection_failure(host)
                else:
                    # The gateway answered from its own logic — connection-wise
                    # it's healthy, so the breaker resets even though the HTTP
                    # status is an error.
                    _breaker_record_success(host)
                last_error = exc
                if exc.code == 400:
                    detail = exc.read().decode("utf-8", errors="replace")
                    if self_heal_rounds < _MAX_SELF_HEAL_ROUNDS and self._apply_self_heal(body, detail):
                        self_heal_rounds += 1
                        continue
                    raise ProviderError(f"Provider request failed with {exc.code}: {detail}") from exc
                if exc.code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                    # Abort before sleeping if this failure just tripped the
                    # breaker — no point backing off into a fail-fast.
                    _check_breaker()
                    # A server that tells us how long to wait knows better than
                    # our ladder does — but only within the clamp.
                    server_delay = _retry_after_seconds(exc)
                    if server_delay is None:
                        _backoff(_jittered(_RETRY_BACKOFF_BASE * (2 ** attempt)))
                    else:
                        # Clamp again *after* jitter. The Retry-After jitter is
                        # one-sided, so it can only lengthen the wait — clamping
                        # only inside _retry_after_seconds would turn the
                        # documented 60s bound into a 72s sleep.
                        _backoff(min(
                            _jittered(server_delay, never_shorter=True),
                            _MAX_RETRY_AFTER_SECONDS,
                        ))
                    # The error body is never read on this path, so the socket
                    # it is holding lives until the exception is collected.
                    # Refcounting usually does that promptly; saying so
                    # explicitly costs nothing and does not depend on it.
                    exc.close()
                    continue
                detail = exc.read().decode("utf-8", errors="replace")
                raise ProviderError(f"Provider request failed with {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if self._is_tls_verification_error(exc):
                    raise ProviderError(
                        "Provider TLS verification failed. "
                        "If your provider sits behind an enterprise proxy that re-signs "
                        "TLS, point `provider.ca_bundle` at your company's CA certificate "
                        "file (preferred), or as a last resort set "
                        "`provider.verify_tls: false`.",
                        recoverable=False,
                    ) from exc
                _breaker_record_connection_failure(host)
                if attempt < _MAX_RETRIES - 1:
                    # Abort before sleeping if this failure just tripped the
                    # breaker — no point backing off into a fail-fast.
                    _check_breaker()
                    _backoff(_jittered(_RETRY_BACKOFF_BASE * (2 ** attempt)))
                    continue
                raise ProviderError(f"Provider request failed: {exc.reason}") from exc
            if stream_retry:
                # Out here rather than inside the `with`, so the stalled socket
                # is closed before we sit out the backoff.
                _backoff(_jittered(_RETRY_BACKOFF_BASE * (2 ** attempt)))
                continue
        raise RetriesExhaustedError(
            f"Provider request failed after {_MAX_RETRIES} attempts",
            attempts=_MAX_RETRIES,
        ) from last_error

    def _build_url(self) -> str:
        if self.config.api_version:
            # Azure-OpenAI-style deployment URL; chat_path/append_chat_path
            # are not used in this mode.
            model = urllib.parse.quote(self.config.model, safe="")
            api_version = urllib.parse.quote(self.config.api_version, safe="")
            return f"{self.config.base_url}/deployments/{model}/chat/completions?api-version={api_version}"
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
            if self.config.api_version:
                # Azure convention: subscription/API key goes in `api-key`,
                # not `Authorization: Bearer`.
                headers["api-key"] = self.config.api_key
            else:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.extra_headers)
        return headers

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.config.verify_tls:
            return ssl._create_unverified_context()  # noqa: S323
        if self.config.ca_bundle:
            # Enterprise proxies that re-sign TLS need their CA trusted
            # without disabling verification wholesale.
            try:
                return ssl.create_default_context(cafile=self.config.ca_bundle)
            except (OSError, ssl.SSLError) as exc:
                raise ProviderError(
                    f"Could not load provider.ca_bundle ({self.config.ca_bundle!r}): {exc}",
                    recoverable=False,
                ) from exc
        return None

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
                if detail and detail.lower() in ("bad request", "unauthorized", "forbidden", "invalid request"):
                    raise ProviderError(
                        f"The provider gateway ({self._build_url()}) rejected the request "
                        f"with: {detail!r}.{hint} "
                        "The gateway URL is reachable but the request payload was refused. "
                        "Check: (1) provider.model is a valid model name on this gateway, "
                        "(2) your API key is current, "
                        "(3) any required extra_body or extra_headers fields your gateway needs."
                    )
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
        reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
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
        finish_reason = str(choice.get("finish_reason") or "")
        _warn_if_truncated(finish_reason, stream_callback)
        return AssistantResponse(
            text=text, tool_calls=tool_calls, usage=usage, reasoning=reasoning,
            finish_reason=finish_reason,
        )

    def _parse_streaming_response(
        self,
        response: Any,
        stream_callback: StreamCallback | None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> AssistantResponse:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = ""
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
        _STALL = 120.0       # transport-level: seconds of total silence
        # Content-level stall is the second timer; see
        # _STREAM_CONTENT_STALL_SECONDS for why one timer is not enough.
        content_stall = _STREAM_CONTENT_STALL_SECONDS
        idle = 0.0
        last_content_at = time.monotonic()

        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderError("Cancelled by user", recoverable=True)
            if time.monotonic() - last_content_at >= content_stall:
                raise ProviderError(
                    f"Stream stalled: no output for {content_stall:.0f}s "
                    "(connection alive — keepalives, empty deltas or "
                    "unparseable lines only). The provider may be holding the "
                    "connection open without producing anything. Try again or "
                    "set provider.streaming_mode: no_stream.",
                    recoverable=True,
                ) from None
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
                    ) from None
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
                # Blank line or SSE comment (keepalive). Proves the socket is
                # alive — which `idle` above already recorded — but carries no
                # payload, so it must not reset the content timer.
                continue
            # Set only where this chunk actually yields output; see
            # _STREAM_CONTENT_STALL_SECONDS for why the `data:` prefix alone is
            # not evidence of progress.
            produced_output = False
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
            # When stream_options.include_usage is on, providers send a final
            # chunk with `choices: []` carrying only usage — skip delta parsing
            # for that chunk but still process usage below.
            choices = payload.get("choices") or []
            if choices:
                choice = choices[0]
                delta = choice.get("delta", {})
                reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(reasoning_delta, str) and reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    produced_output = True
                content = _extract_content_text(delta.get("content"))
                if content:
                    text_parts.append(content)
                    produced_output = True
                    to_emit = stream_filter.push(content)
                    if to_emit and stream_callback:
                        stream_callback({"type": "assistant_delta", "delta": to_emit})
                if choice.get("finish_reason"):
                    # The model saying why it stopped is progress, even on a
                    # chunk whose delta is empty.
                    produced_output = True
                    # Last one wins: a stream carries at most one non-empty
                    # finish_reason, but a gateway that repeats it on the
                    # usage-only trailer should not have the value dropped.
                    finish_reason = str(choice["finish_reason"])
                for tool_call_delta in delta.get("tool_calls", []):
                    produced_output = True
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
            if produced_output:
                last_content_at = time.monotonic()

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
        _warn_if_truncated(finish_reason, stream_callback)
        return AssistantResponse(text=final_text, tool_calls=tool_calls, usage=usage,
                                 reasoning="".join(reasoning_parts),
                                 finish_reason=finish_reason)


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
    """Buffers streaming text so ``<tool_call>`` / ``<function_call>`` /
    ``<think>`` / ``<thinking>`` / ``<reasoning>`` blocks are never forwarded
    to the ``assistant_delta`` callback.

    The full text (including tool-call blocks) is still accumulated in
    ``text_parts`` so that ``_extract_text_tool_calls`` can parse them at the
    end of the stream.  This filter only governs what reaches the terminal.

    Thinking-style tags are stripped because users see them as noise — the
    streaming display prefixes intermediate text with "💭" already, so a
    leaked "<think>" tag produces "💭 <think>..." which is doubly confusing.
    """

    _OPEN_TAGS: tuple[str, ...] = (
        "<tool_call>", "<function_call>",
        "<think>", "<thinking>", "<reasoning>",
    )
    _CLOSE: dict[str, str] = {
        "<tool_call>": "</tool_call>",
        "<function_call>": "</function_call>",
        "<think>": "</think>",
        "<thinking>": "</thinking>",
        "<reasoning>": "</reasoning>",
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

    def _holdback(self) -> int:
        """Length of buffer tail that might still complete into an open tag.

        Only the suffix that is a proper prefix of any open tag needs to be
        withheld — the rest of the buffer can be safely emitted. Returning 0
        when the tail clearly isn't a tag prefix avoids per-chunk streaming
        latency on text that happens to be shorter than _MAX_OPEN_LEN.
        """
        tail_max = min(len(self._buf), self._MAX_OPEN_LEN - 1)
        for size in range(tail_max, 0, -1):
            suffix = self._buf[-size:]
            for tag in self._OPEN_TAGS:
                if tag.startswith(suffix):
                    return size
        return 0

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
                    safe = len(self._buf) if flush_all else len(self._buf) - self._holdback()
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
