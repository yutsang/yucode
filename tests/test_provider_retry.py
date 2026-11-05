"""Retry-ladder and streaming-stall resilience for the hand-written provider
client (IMPROVEMENT_PLAN_2026-08 A10 and the three follow-ups in §6).

Covers:
  - +/-20% jitter on the exponential backoff, one-sided for `Retry-After`
  - `Retry-After` honoured (delta-seconds and HTTP-date) and clamped
  - Cloudflare 520-524 and 529 "overloaded" treated as retryable
  - A stream that fails before emitting anything is retried; one that fails
    after emitting is not
  - Origin-down edge statuses (52x) open the circuit breaker; 429/503 do not
  - The content-level stream stall timer, which only a chunk that actually
    produced something may reset

Every backoff sleep is stubbed; nothing here waits on a real retry ladder.
The stall tests do elapse real (sub-second) wall time, because the timer they
exercise is written against ``time.monotonic()``.
"""
from __future__ import annotations

import email.message
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from coding_agent.config import ProviderConfig
from coding_agent.core import providers as providers_mod
from coding_agent.core.errors import ProviderError
from coding_agent.core.providers import OpenAICompatibleProvider

_OK_PAYLOAD = {"choices": [{"message": {"content": "OK"}}]}


@pytest.fixture(autouse=True)
def _clean_breakers():
    providers_mod._reset_circuit_breakers()
    yield
    providers_mod._reset_circuit_breakers()


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


class _FakeResponse:
    """Minimal stand-in for the object urlopen() returns (non-streaming)."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(
    code: int,
    *,
    retry_after: str | None = None,
    body: bytes = b'{"error": {"message": "transient"}}',
) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    import io

    return urllib.error.HTTPError(
        url="http://localhost",
        code=code,
        msg="err",
        hdrs=headers,
        fp=io.BytesIO(body),
    )


def _record_sleeps(monkeypatch) -> list[float]:
    """Stub out the backoff sleep and collect the requested durations."""
    slept: list[float] = []
    monkeypatch.setattr(providers_mod.time, "sleep", slept.append)
    return slept


def _run_with_responses(provider, monkeypatch, responses: list[Any]) -> Any:
    """Drive _do_complete against a scripted sequence of urlopen outcomes."""
    remaining = list(responses)

    def fake_urlopen(request, timeout=90, context=None):
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return provider._do_complete([{"role": "user", "content": "hi"}], [], stream=False)


# ---------------------------------------------------------------------------
# Jitter
# ---------------------------------------------------------------------------

class TestJitter:
    def test_stays_within_plus_minus_fraction(self):
        fraction = providers_mod._RETRY_JITTER_FRACTION
        values = [providers_mod._jittered(10.0) for _ in range(200)]
        assert all(10.0 * (1 - fraction) <= v <= 10.0 * (1 + fraction) for v in values)

    def test_actually_varies(self):
        values = {providers_mod._jittered(10.0) for _ in range(50)}
        assert len(values) > 1

    def test_never_shorter_is_one_sided(self):
        fraction = providers_mod._RETRY_JITTER_FRACTION
        values = [providers_mod._jittered(10.0, never_shorter=True) for _ in range(200)]
        assert all(10.0 <= v <= 10.0 * (1 + fraction) for v in values)

    def test_zero_backoff_stays_zero(self):
        assert providers_mod._jittered(0.0) == 0.0

    def test_ladder_backoff_is_jittered_end_to_end(self, monkeypatch):
        slept = _record_sleeps(monkeypatch)
        provider = _make_provider()
        with pytest.raises(ProviderError):
            _run_with_responses(
                provider, monkeypatch,
                [_http_error(503) for _ in range(providers_mod._MAX_RETRIES)],
            )
        # 5 attempts => 4 backoffs, each within +/-20% of base * 2**attempt.
        assert len(slept) == providers_mod._MAX_RETRIES - 1
        fraction = providers_mod._RETRY_JITTER_FRACTION
        expected = [providers_mod._RETRY_BACKOFF_BASE * (2 ** i) for i in range(4)]
        for actual, base in zip(slept, expected, strict=True):
            assert base * (1 - fraction) <= actual <= base * (1 + fraction)
        # Jitter, not the bare ladder.
        assert slept != expected


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------

class TestRetryAfterParsing:
    def test_missing_header_returns_none(self):
        assert providers_mod._retry_after_seconds(_http_error(429)) is None

    def test_delta_seconds(self):
        assert providers_mod._retry_after_seconds(_http_error(429, retry_after="7")) == 7.0

    def test_clamped_to_max(self):
        parsed = providers_mod._retry_after_seconds(_http_error(429, retry_after="86400"))
        assert parsed == providers_mod._MAX_RETRY_AFTER_SECONDS

    def test_http_date_form(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=20)
        stamp = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
        parsed = providers_mod._retry_after_seconds(_http_error(429, retry_after=stamp))
        assert parsed is not None
        assert 15.0 <= parsed <= 21.0

    def test_http_date_in_the_past_falls_back_to_the_ladder(self):
        stamp = "Wed, 21 Oct 2015 07:28:00 GMT"
        assert providers_mod._retry_after_seconds(_http_error(429, retry_after=stamp)) is None

    def test_unparseable_header_returns_none(self):
        assert providers_mod._retry_after_seconds(_http_error(429, retry_after="soon")) is None

    def test_negative_delta_falls_back_to_the_ladder(self):
        assert providers_mod._retry_after_seconds(_http_error(429, retry_after="-5")) is None

    def test_zero_delta_falls_back_to_the_ladder(self):
        assert providers_mod._retry_after_seconds(_http_error(429, retry_after="0")) is None

    def test_nan_falls_back_to_the_ladder(self):
        """float() happily parses 'nan'; min/max then collapse it to 0.0."""
        assert providers_mod._retry_after_seconds(_http_error(429, retry_after="nan")) is None

    def test_infinite_delta_is_clamped_not_dropped(self):
        parsed = providers_mod._retry_after_seconds(_http_error(429, retry_after="1e400"))
        assert parsed == providers_mod._MAX_RETRY_AFTER_SECONDS


class TestRetryAfterNeverSleepsZero:
    """A server answering `Retry-After: 0` (or a stale date) must not turn the
    ladder into four back-to-back requests with no pause at all — that is the
    exact hammering the backoff exists to prevent, and the one-sided jitter
    cannot spread a delay of zero."""

    @pytest.mark.parametrize(
        "header", ["0", "-5", "nan", "Wed, 21 Oct 2015 07:28:00 GMT"],
    )
    def test_degenerate_header_still_backs_off(self, header: str, monkeypatch):
        slept = _record_sleeps(monkeypatch)
        provider = _make_provider()
        with pytest.raises(ProviderError):
            _run_with_responses(
                provider, monkeypatch,
                [
                    _http_error(429, retry_after=header)
                    for _ in range(providers_mod._MAX_RETRIES)
                ],
            )
        assert len(slept) == providers_mod._MAX_RETRIES - 1
        assert all(s > 0.0 for s in slept), f"zero-delay retry storm: {slept}"


class TestRetryAfterHonoured:
    def test_server_delay_replaces_computed_backoff(self, monkeypatch):
        slept = _record_sleeps(monkeypatch)
        provider = _make_provider()
        response = _run_with_responses(
            provider, monkeypatch,
            [_http_error(429, retry_after="7"), _FakeResponse(_OK_PAYLOAD)],
        )
        assert response.text == "OK"
        # First ladder step would have been 1.5s; the server said 7s.
        assert len(slept) == 1
        fraction = providers_mod._RETRY_JITTER_FRACTION
        assert 7.0 <= slept[0] <= 7.0 * (1 + fraction)

    def test_absurd_retry_after_is_clamped(self, monkeypatch):
        slept = _record_sleeps(monkeypatch)
        provider = _make_provider()
        response = _run_with_responses(
            provider, monkeypatch,
            [_http_error(429, retry_after="86400"), _FakeResponse(_OK_PAYLOAD)],
        )
        assert response.text == "OK"
        cap = providers_mod._MAX_RETRY_AFTER_SECONDS
        assert slept[0] <= cap

    def test_clamp_holds_after_jitter_not_before(self, monkeypatch):
        """_MAX_RETRY_AFTER_SECONDS is documented as the bound on a single
        honoured wait ("worst case stays bounded at 4 waits x 60s"). Clamping
        first and jittering afterwards breaks that: the one-sided jitter can
        only push the clamped value up."""
        slept = _record_sleeps(monkeypatch)
        # Pin the jitter to its maximum so the bound is checked, not sampled.
        monkeypatch.setattr(
            providers_mod.random, "uniform", lambda low, high: high,
        )
        provider = _make_provider()
        _run_with_responses(
            provider, monkeypatch,
            [_http_error(503, retry_after="86400"), _FakeResponse(_OK_PAYLOAD)],
        )
        assert slept[0] <= providers_mod._MAX_RETRY_AFTER_SECONDS

    def test_retry_after_on_503_is_also_honoured(self, monkeypatch):
        slept = _record_sleeps(monkeypatch)
        provider = _make_provider()
        _run_with_responses(
            provider, monkeypatch,
            [_http_error(503, retry_after="9"), _FakeResponse(_OK_PAYLOAD)],
        )
        assert 9.0 <= slept[0] <= 9.0 * (1 + providers_mod._RETRY_JITTER_FRACTION)

    def test_unparseable_header_falls_back_to_ladder(self, monkeypatch):
        slept = _record_sleeps(monkeypatch)
        provider = _make_provider()
        _run_with_responses(
            provider, monkeypatch,
            [_http_error(429, retry_after="whenever"), _FakeResponse(_OK_PAYLOAD)],
        )
        base = providers_mod._RETRY_BACKOFF_BASE
        fraction = providers_mod._RETRY_JITTER_FRACTION
        assert base * (1 - fraction) <= slept[0] <= base * (1 + fraction)

    def test_cancel_event_still_interrupts_a_retry_after_wait(self, monkeypatch):
        """The clamp bounds the damage; the cancel event must still cut it short."""
        provider = _make_provider()
        cancel_event = threading.Event()
        cancel_event.set()

        def fake_urlopen(request, timeout=90, context=None):
            raise _http_error(429, retry_after="60")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        started = time.monotonic()
        with pytest.raises(ProviderError, match="Cancelled"):
            provider._do_complete(
                [{"role": "user", "content": "hi"}], [],
                stream=False, cancel_event=cancel_event,
            )
        assert time.monotonic() - started < 1.0


# ---------------------------------------------------------------------------
# Retryable status codes
# ---------------------------------------------------------------------------

class TestRetryableStatusCodes:
    @pytest.mark.parametrize("code", [520, 521, 522, 523, 524, 529])
    def test_new_codes_are_retried(self, code: int, monkeypatch):
        _record_sleeps(monkeypatch)
        provider = _make_provider()
        response = _run_with_responses(
            provider, monkeypatch, [_http_error(code), _FakeResponse(_OK_PAYLOAD)],
        )
        assert response.text == "OK"

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_existing_codes_still_retried(self, code: int, monkeypatch):
        _record_sleeps(monkeypatch)
        provider = _make_provider()
        response = _run_with_responses(
            provider, monkeypatch, [_http_error(code), _FakeResponse(_OK_PAYLOAD)],
        )
        assert response.text == "OK"

    @pytest.mark.parametrize("code", [401, 403, 404, 422])
    def test_client_errors_are_not_retried(self, code: int, monkeypatch):
        slept = _record_sleeps(monkeypatch)
        provider = _make_provider()
        with pytest.raises(ProviderError, match=str(code)):
            _run_with_responses(provider, monkeypatch, [_http_error(code)])
        assert slept == []


# ---------------------------------------------------------------------------
# Streaming content-stall timer
# ---------------------------------------------------------------------------

class _ScriptedStream:
    """SSE stream yielding pre-built lines with a pause between each.

    Bounded so that a regression (content timer reset by keepalives) fails the
    test instead of hanging the suite.
    """

    def __init__(self, lines: list[bytes], *, interval: float = 0.02, repeat: int = 1) -> None:
        self._lines = lines
        self._interval = interval
        self._repeat = repeat
        self.stop = threading.Event()

    def __iter__(self):
        for _ in range(self._repeat):
            for line in self._lines:
                if self.stop.is_set():
                    return
                yield line
                time.sleep(self._interval)


def _content_line(text: str) -> bytes:
    return f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n".encode()


def _empty_delta_line() -> bytes:
    """The shape a gateway emits when it is producing nothing at all."""
    return b'data: {"choices":[{"delta":{}}]}\n'


def _reasoning_line(text: str) -> bytes:
    payload = {"choices": [{"delta": {"reasoning_content": text}}]}
    return f"data: {json.dumps(payload)}\n".encode()


def _tool_call_line() -> bytes:
    payload = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "t1", "function": {"name": "", "arguments": "x"}},
    ]}}]}
    return f"data: {json.dumps(payload)}\n".encode()


def _finish_line() -> bytes:
    payload = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    return f"data: {json.dumps(payload)}\n".encode()


class _KeepaliveStream:
    """SSE stream that yields *prefix* then keepalives until stopped.

    Waits on an Event rather than ``time.sleep`` so the backoff-sleep stub
    (which patches the shared ``time`` module) cannot turn this into a
    zero-delay loop that ends before the stall timer can fire.
    """

    def __init__(self, prefix: list[bytes] | None = None, *, tick: float = 0.02) -> None:
        self._prefix = list(prefix or [])
        self._tick = tick
        self.stop = threading.Event()

    def __iter__(self):
        yield from self._prefix
        while not self.stop.wait(self._tick):
            yield b": keepalive\n"


class _FakeStreamResponse:
    """Context-manager wrapper matching what urlopen() returns when streaming."""

    def __init__(self, source: Any) -> None:
        self._source = source

    def __enter__(self) -> _FakeStreamResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        stop = getattr(self._source, "stop", None)
        if stop is not None:
            stop.set()
        return False

    def __iter__(self):
        return iter(self._source)


def _run_streaming(provider, monkeypatch, responses: list[Any], callback=None) -> Any:
    """Drive _do_complete(stream=True) against scripted urlopen outcomes.

    Returns the counter dict so tests can assert how many HTTP attempts the
    call actually made.
    """
    remaining = list(responses)
    calls = {"n": 0}

    def fake_urlopen(request, timeout=90, context=None):
        calls["n"] += 1
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls, lambda: provider._do_complete(
        [{"role": "user", "content": "hi"}], [],
        stream=True, stream_callback=callback,
    )


class TestStreamContentStall:
    def test_keepalive_only_stream_trips_the_content_timer(self, monkeypatch):
        monkeypatch.setattr(providers_mod, "_STREAM_CONTENT_STALL_SECONDS", 0.4)
        provider = _make_provider(stream=True)
        # ~10s of keepalives available; the fix must give up at 0.4s.
        stream = _ScriptedStream([b": keepalive\n"], interval=0.02, repeat=500)
        started = time.monotonic()
        try:
            with pytest.raises(ProviderError, match="Stream stalled"):
                provider._parse_streaming_response(stream, None)
        finally:
            stream.stop.set()
        assert 0.4 <= time.monotonic() - started < 5.0

    def test_real_chunks_keep_the_stream_alive(self, monkeypatch):
        monkeypatch.setattr(providers_mod, "_STREAM_CONTENT_STALL_SECONDS", 0.5)
        provider = _make_provider(stream=True)
        # A keepalive between every content chunk: total runtime exceeds the
        # 0.5s window, but no gap between data chunks ever does.
        stream = _ScriptedStream(
            [b": keepalive\n", _content_line("x")], interval=0.05, repeat=10,
        )
        try:
            response = provider._parse_streaming_response(stream, None)
        finally:
            stream.stop.set()
        assert response.text == "x" * 10

    def test_keepalives_are_not_counted_as_chunks(self, monkeypatch):
        """A keepalive-only stream that ends cleanly is still an empty stream."""
        monkeypatch.setattr(providers_mod, "_STREAM_CONTENT_STALL_SECONDS", 30.0)
        provider = _make_provider(stream=True)
        events: list[dict[str, Any]] = []
        stream = _ScriptedStream([b": keepalive\n"], interval=0.0, repeat=5)
        provider._parse_streaming_response(stream, events.append)
        assert any(e.get("category") == "provider_empty_stream" for e in events)


# ---------------------------------------------------------------------------
# The content-stall timer must key off content, not off the `data:` prefix
# ---------------------------------------------------------------------------

class TestStallTimerKeysOffContent:
    """A gateway can hold a turn open forever while sending well-formed
    `data:` lines that carry nothing. Resetting the timer on the prefix alone
    made every one of these invisible to both timers."""

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(_empty_delta_line(), id="empty_delta"),
            pytest.param(b"data: <html>502 Bad Gateway</html>\n", id="html_error_body"),
            pytest.param(b'data: {"msg":"upstream error"}\n', id="non_openai_envelope"),
        ],
    )
    def test_contentless_data_lines_trip_the_timer(self, line: bytes, monkeypatch):
        monkeypatch.setattr(providers_mod, "_STREAM_CONTENT_STALL_SECONDS", 0.4)
        provider = _make_provider(stream=True)
        # ~10s of contentless chunks available; the fix must give up at 0.4s.
        stream = _ScriptedStream([line], interval=0.02, repeat=500)
        started = time.monotonic()
        try:
            with pytest.raises(ProviderError, match="Stream stalled"):
                provider._parse_streaming_response(stream, None)
        finally:
            stream.stop.set()
        assert 0.4 <= time.monotonic() - started < 5.0

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(_content_line("x"), id="text_delta"),
            pytest.param(_reasoning_line("x"), id="reasoning_delta"),
            pytest.param(_tool_call_line(), id="tool_call_delta"),
            pytest.param(_finish_line(), id="finish_reason"),
        ],
    )
    def test_a_chunk_that_produces_something_resets_the_timer(self, line: bytes, monkeypatch):
        """Guard against over-tightening: each of these four is real output,
        so a stream carrying them at a steady rate must survive — even with
        contentless chunks interleaved between them."""
        monkeypatch.setattr(providers_mod, "_STREAM_CONTENT_STALL_SECONDS", 0.35)
        provider = _make_provider(stream=True)
        # 18 lines at 0.06s = ~1.08s total, far past the 0.35s window, but the
        # gap between two producing chunks is only 0.18s.
        stream = _ScriptedStream(
            [_empty_delta_line(), _empty_delta_line(), line], interval=0.06, repeat=6,
        )
        try:
            provider._parse_streaming_response(stream, None)
        finally:
            stream.stop.set()


# ---------------------------------------------------------------------------
# Streaming failures are retried, but only while nothing has been shown
# ---------------------------------------------------------------------------

class TestStreamingRetry:
    def test_stall_before_any_output_is_retried(self, monkeypatch):
        _record_sleeps(monkeypatch)
        monkeypatch.setattr(providers_mod, "_STREAM_CONTENT_STALL_SECONDS", 0.2)
        provider = _make_provider(stream=True)
        calls, run = _run_streaming(provider, monkeypatch, [
            _FakeStreamResponse(_KeepaliveStream()),
            _FakeStreamResponse([_content_line("OK"), b"data: [DONE]\n"]),
        ])
        response = run()
        assert response.text == "OK"
        assert calls["n"] == 2

    def test_stream_read_error_before_output_is_retried(self, monkeypatch):
        """Not just stalls: a socket that dies mid-stream is the same case."""
        _record_sleeps(monkeypatch)
        provider = _make_provider(stream=True)

        class _DyingStream:
            def __iter__(self):
                raise OSError("connection reset by peer")
                yield  # pragma: no cover

        calls, run = _run_streaming(provider, monkeypatch, [
            _FakeStreamResponse(_DyingStream()),
            _FakeStreamResponse([_content_line("OK"), b"data: [DONE]\n"]),
        ])
        assert run().text == "OK"
        assert calls["n"] == 2

    def test_retry_works_without_a_stream_callback(self, monkeypatch):
        """Coordinator workers pass no callback, so nothing can have been
        shown — the widest case the gate lets through."""
        _record_sleeps(monkeypatch)
        monkeypatch.setattr(providers_mod, "_STREAM_CONTENT_STALL_SECONDS", 0.2)
        provider = _make_provider(stream=True)
        calls, run = _run_streaming(provider, monkeypatch, [
            _FakeStreamResponse(_KeepaliveStream()),
            _FakeStreamResponse([_content_line("OK"), b"data: [DONE]\n"]),
        ], callback=None)
        assert run().text == "OK"
        assert calls["n"] == 2

    def test_stall_after_output_is_not_retried(self, monkeypatch):
        """The limit of the fix: replaying a stream that already reached the
        terminal would print its text twice, so it is left to fail."""
        _record_sleeps(monkeypatch)
        monkeypatch.setattr(providers_mod, "_STREAM_CONTENT_STALL_SECONDS", 0.2)
        provider = _make_provider(stream=True)
        events: list[dict[str, Any]] = []
        calls, run = _run_streaming(provider, monkeypatch, [
            _FakeStreamResponse(_KeepaliveStream([_content_line("Hello")])),
            _FakeStreamResponse([_content_line("Hello world"), b"data: [DONE]\n"]),
        ], callback=events.append)
        with pytest.raises(ProviderError, match="Stream stalled"):
            run()
        assert calls["n"] == 1
        deltas = [e["delta"] for e in events if e.get("type") == "assistant_delta"]
        assert deltas == ["Hello"]

    def test_cancellation_is_never_retried(self, monkeypatch):
        _record_sleeps(monkeypatch)
        provider = _make_provider(stream=True)
        cancel_event = threading.Event()
        remaining = [
            _FakeStreamResponse(_KeepaliveStream()),
            _FakeStreamResponse([_content_line("OK"), b"data: [DONE]\n"]),
        ]
        calls = {"n": 0}

        def fake_urlopen(request, timeout=90, context=None):
            calls["n"] += 1
            # Cancel once the stream is live, so the abort comes out of
            # _parse_streaming_response rather than the pre-attempt check.
            cancel_event.set()
            return remaining.pop(0)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(ProviderError, match="Cancelled"):
            provider._do_complete(
                [{"role": "user", "content": "hi"}], [],
                stream=True, cancel_event=cancel_event,
            )
        assert calls["n"] == 1

    def test_retries_are_capped_well_below_the_http_ladder(self, monkeypatch):
        """Each stall costs a full stall window before it is even noticed, so
        reusing the 5-attempt ladder would make a mute gateway cost ~25 real
        minutes — worse than the single failure this retry replaces."""
        slept = _record_sleeps(monkeypatch)
        monkeypatch.setattr(providers_mod, "_STREAM_CONTENT_STALL_SECONDS", 0.1)
        provider = _make_provider(stream=True)
        attempts = providers_mod._MAX_STREAM_RETRIES + 1
        assert attempts < providers_mod._MAX_RETRIES
        calls, run = _run_streaming(provider, monkeypatch, [
            _FakeStreamResponse(_KeepaliveStream()) for _ in range(attempts)
        ])
        with pytest.raises(ProviderError, match="Stream stalled"):
            run()
        assert calls["n"] == attempts
        assert len(slept) == providers_mod._MAX_STREAM_RETRIES


# ---------------------------------------------------------------------------
# Origin-down statuses and the circuit breaker
# ---------------------------------------------------------------------------

def _drive_repeated_failure(provider, monkeypatch, code: int, *, calls: int = 3) -> dict[str, int]:
    attempts = {"n": 0}

    def fake_urlopen(request, timeout=90, context=None):
        attempts["n"] += 1
        raise _http_error(code)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    for _ in range(calls):
        with pytest.raises(ProviderError):
            provider._do_complete([{"role": "user", "content": "hi"}], [], stream=False)
    return attempts


class TestOriginDownOpensTheBreaker:
    """A permanently-522 gateway used to cost 5 attempts on every call
    forever, because every HTTP status — including the ones that say the
    origin is dead — was recorded as breaker success."""

    @pytest.mark.parametrize("code", [521, 522, 523, 524])
    def test_persistent_origin_failure_trips_the_breaker(self, code: int, monkeypatch):
        _record_sleeps(monkeypatch)
        provider = _make_provider()
        attempts = _drive_repeated_failure(provider, monkeypatch, code)
        # The first call trips the breaker on attempt 3 and aborts its own
        # ladder; calls 2 and 3 fail fast without touching the socket.
        assert attempts["n"] == providers_mod._BREAKER_FAILURE_THRESHOLD
        assert providers_mod._circuit_states != {}

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504, 520, 529])
    def test_a_transient_gateway_failure_keeps_the_breaker_closed(
        self, code: int, monkeypatch,
    ):
        """A rate limit or an overloaded upstream means the gateway is alive
        and answering from its own logic. Failing other calls fast on it would
        turn a request-scoped problem into a host-scoped one."""
        _record_sleeps(monkeypatch)
        provider = _make_provider()
        response = _run_with_responses(provider, monkeypatch, [
            _http_error(code), _http_error(code), _FakeResponse(_OK_PAYLOAD),
        ])
        assert response.text == "OK"
        assert providers_mod._circuit_states == {}

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504, 520, 529])
    def test_sustained_gateway_failure_eventually_opens_the_breaker(
        self, code: int, monkeypatch,
    ):
        """The other half of the same rule.

        Treating these as proof of health meant a gateway that was down in
        every way that mattered — answering nothing but 429 or 503, forever —
        never tripped anything, and every call paid the whole ladder. One of
        them proves nothing; a run of them outlasting all five attempts is a
        host that cannot serve us, and the ladder is what tells the two apart.
        """
        _record_sleeps(monkeypatch)
        provider = _make_provider()
        attempts = _drive_repeated_failure(provider, monkeypatch, code)
        # Each exhausted ladder records one failure, so the breaker opens on
        # the third call -- and only then, which is what keeps a single slow
        # request from taking the host out.
        assert attempts["n"] == providers_mod._MAX_RETRIES * 3
        assert providers_mod._circuit_states != {}

    def test_a_recovered_overload_does_not_count_against_the_host(self, monkeypatch):
        """Intermediate attempts stay neutral: a retry that works is a success.

        Recording a failure per 429 seen — rather than per ladder exhausted —
        would trip the breaker on a gateway that was rate-limiting normally and
        serving everything on the second try.
        """
        _record_sleeps(monkeypatch)
        provider = _make_provider()
        for _ in range(4):
            response = _run_with_responses(provider, monkeypatch, [
                _http_error(429), _http_error(429), _FakeResponse(_OK_PAYLOAD),
            ])
            assert response.text == "OK"
        assert providers_mod._circuit_states == {}

    def test_a_success_clears_accumulated_origin_failures(self, monkeypatch):
        _record_sleeps(monkeypatch)
        provider = _make_provider()
        response = _run_with_responses(provider, monkeypatch, [
            _http_error(522), _http_error(522), _FakeResponse(_OK_PAYLOAD),
        ])
        assert response.text == "OK"
        assert providers_mod._circuit_states == {}

    def test_open_breaker_reports_itself(self, monkeypatch):
        _record_sleeps(monkeypatch)
        provider = _make_provider()
        _drive_repeated_failure(provider, monkeypatch, 522, calls=1)
        with pytest.raises(ProviderError, match="marked unavailable"):
            provider._do_complete([{"role": "user", "content": "hi"}], [], stream=False)


# ---------------------------------------------------------------------------
# gpt-5.6: function tools + reasoning are refused on /v1/chat/completions
# ---------------------------------------------------------------------------

# Verbatim from the gateway. Reproduced here because the self-heal keys off
# this exact wording, so a test built on a paraphrase would prove nothing.
_TOOLS_VS_REASONING_BODY = json.dumps({
    "error": {
        "message": (
            "Function tools with reasoning_effort are not supported for "
            "gpt-5.6-terra in /v1/chat/completions. To use function tools, use "
            "/v1/responses or set reasoning_effort to 'none'."
        ),
        "type": "invalid_request_error",
        "param": "reasoning_effort",
        "code": None,
    },
}).encode()

_TOOLS = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]


def _capture_bodies(monkeypatch, outcomes: list[Any]) -> list[dict[str, Any]]:
    """Record the JSON body of every request the ladder actually sends."""
    sent: list[dict[str, Any]] = []
    remaining = list(outcomes)

    def fake_urlopen(request, timeout=90, context=None):
        sent.append(json.loads(request.data.decode("utf-8")))
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent


class TestToolsVersusReasoningSelfHeal:
    """gpt-5.6 rejects every tool-carrying chat-completions request unless
    reasoning_effort is explicitly 'none' — and it reasons by default, so
    dropping the param (the only thing the old self-heal could do) does not
    help. The heal has to *set* a value."""

    def test_the_400_is_healed_by_setting_none(self, monkeypatch):
        _record_sleeps(monkeypatch)
        provider = _make_provider(reasoning_effort="medium")
        sent = _capture_bodies(monkeypatch, [
            _http_error(400, body=_TOOLS_VS_REASONING_BODY),
            _FakeResponse(_OK_PAYLOAD),
        ])
        response = provider._do_complete(
            [{"role": "user", "content": "hi"}], _TOOLS, stream=False,
        )
        assert response.text == "OK"
        assert sent[0]["reasoning_effort"] == "medium"
        assert sent[1]["reasoning_effort"] == "none"

    def test_the_correction_is_learned_for_later_calls(self, monkeypatch):
        _record_sleeps(monkeypatch)
        provider = _make_provider(reasoning_effort="medium")
        _capture_bodies(monkeypatch, [
            _http_error(400, body=_TOOLS_VS_REASONING_BODY),
            _FakeResponse(_OK_PAYLOAD),
        ])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        # A fresh call must not re-pay the round trip.
        sent = _capture_bodies(monkeypatch, [_FakeResponse(_OK_PAYLOAD)])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        assert len(sent) == 1
        assert sent[0]["reasoning_effort"] == "none"

    def test_the_learned_none_outranks_a_per_turn_override(self, monkeypatch):
        """run_turn() passes a per-turn effort in body_overrides. If that won,
        every reasoning turn would 400 again."""
        _record_sleeps(monkeypatch)
        provider = _make_provider(reasoning_effort="medium")
        _capture_bodies(monkeypatch, [
            _http_error(400, body=_TOOLS_VS_REASONING_BODY),
            _FakeResponse(_OK_PAYLOAD),
        ])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        sent = _capture_bodies(monkeypatch, [_FakeResponse(_OK_PAYLOAD)])
        provider._do_complete(
            [{"role": "user", "content": "hi"}], _TOOLS, stream=False,
            body_overrides={"reasoning_effort": "high"},
        )
        assert sent[0]["reasoning_effort"] == "none"

    def test_it_is_reported_not_silent(self, monkeypatch, caplog):
        """Turning reasoning off is a real capability downgrade — it must not
        happen without a trace."""
        _record_sleeps(monkeypatch)
        provider = _make_provider(reasoning_effort="medium")
        _capture_bodies(monkeypatch, [
            _http_error(400, body=_TOOLS_VS_REASONING_BODY),
            _FakeResponse(_OK_PAYLOAD),
        ])
        with caplog.at_level("WARNING", logger="yucode.providers"):
            provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        assert any("reasoning_effort" in r.message for r in caplog.records)
        assert any("responses" in r.message.lower() for r in caplog.records)

    def test_a_plain_unsupported_reasoning_effort_400_is_still_a_drop(self, monkeypatch):
        """Narrowness check: same param, no function-tools wording, so this
        must take the old path (drop it) rather than force 'none'."""
        _record_sleeps(monkeypatch)
        provider = _make_provider(reasoning_effort="medium")
        body = json.dumps({"error": {
            "message": "Unsupported parameter: 'reasoning_effort'",
            "param": "reasoning_effort",
        }}).encode()
        sent = _capture_bodies(monkeypatch, [
            _http_error(400, body=body), _FakeResponse(_OK_PAYLOAD),
        ])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        assert "reasoning_effort" not in sent[1]
        assert "reasoning_effort" in provider._learned_omit_params

    def test_a_learned_drop_does_not_outlive_the_pin(self, monkeypatch):
        """One gateway can teach both lessons in one session: first a plain
        "unsupported parameter" 400 (drop it), then — after a deployment
        upgrade, or the same refusal reworded — the function-tools one (pin it
        to 'none'). The retry that pins succeeds because it edits `body` in
        place, so the breakage only shows on the call *after*: _build_body
        writes 'none' and the stale omit entry pops it straight back out, the
        request goes out without the param, the gateway 400s again, and
        _apply_self_heal has nothing left to try (already pinned) — the
        session hard-fails with no way back."""
        _record_sleeps(monkeypatch)
        provider = _make_provider(reasoning_effort="medium")
        plain = json.dumps({"error": {
            "message": "Unsupported parameter: 'reasoning_effort'",
            "param": "reasoning_effort",
        }}).encode()
        _capture_bodies(monkeypatch, [
            _http_error(400, body=plain), _FakeResponse(_OK_PAYLOAD),
        ])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        assert "reasoning_effort" in provider._learned_omit_params

        _capture_bodies(monkeypatch, [
            _http_error(400, body=_TOOLS_VS_REASONING_BODY),
            _FakeResponse(_OK_PAYLOAD),
        ])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        assert provider._learned_reasoning_effort_none

        sent = _capture_bodies(monkeypatch, [_FakeResponse(_OK_PAYLOAD)])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        assert sent[0]["reasoning_effort"] == "none"

    def test_a_later_plain_drop_does_not_outlive_the_pin(self, monkeypatch):
        """The same contradiction in the other order: once the gateway stops
        recognising the param at all, a stale pin would put it back on every
        later request and 400 forever."""
        _record_sleeps(monkeypatch)
        provider = _make_provider(reasoning_effort="medium")
        _capture_bodies(monkeypatch, [
            _http_error(400, body=_TOOLS_VS_REASONING_BODY),
            _FakeResponse(_OK_PAYLOAD),
        ])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        assert provider._learned_reasoning_effort_none

        plain = json.dumps({"error": {
            "message": "Unsupported parameter: 'reasoning_effort'",
            "param": "reasoning_effort",
        }}).encode()
        _capture_bodies(monkeypatch, [
            _http_error(400, body=plain), _FakeResponse(_OK_PAYLOAD),
        ])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        assert not provider._learned_reasoning_effort_none

        sent = _capture_bodies(monkeypatch, [_FakeResponse(_OK_PAYLOAD)])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        assert "reasoning_effort" not in sent[0]

    def test_an_unrelated_400_is_untouched(self, monkeypatch):
        _record_sleeps(monkeypatch)
        provider = _make_provider(reasoning_effort="medium")
        body = json.dumps({"error": {
            "message": "Unsupported parameter: 'temperature'",
            "param": "temperature",
        }}).encode()
        sent = _capture_bodies(monkeypatch, [
            _http_error(400, body=body), _FakeResponse(_OK_PAYLOAD),
        ])
        provider._do_complete([{"role": "user", "content": "hi"}], _TOOLS, stream=False)
        assert "temperature" not in sent[1]
        assert sent[1]["reasoning_effort"] == "medium"
