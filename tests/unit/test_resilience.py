"""Tests for external_sources.resilience: retries, backoff, Retry-After, circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests

from external_sources.resilience import (
    BreakerRegistry,
    CircuitBreaker,
    CircuitOpenError,
    ResilientSession,
    RetryingHTTP,
    RetryPolicy,
    parse_retry_after,
    retry_call,
)


class FakeResponse:
    def __init__(self, status: int = 200, headers: dict | None = None, body: bytes = b"{}") -> None:
        self.status_code = status
        self.headers = headers or {}
        self.content = body
        self.closed = False

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def scripted(*outcomes):
    """A send() that returns/raises the given outcomes in order and records calls."""
    calls: list[tuple[str, str, dict]] = []
    items = list(outcomes)

    def send(method, url, **kwargs):
        calls.append((method, url, kwargs))
        item = items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    send.calls = calls  # type: ignore[attr-defined]
    return send


class TestRetryAfter:
    def test_integer_and_fractional_seconds(self):
        assert parse_retry_after("10") == 10.0
        assert parse_retry_after("10.000") == 10.0

    def test_http_date(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        header = format_datetime(now + timedelta(seconds=30), usegmt=True)
        assert parse_retry_after(header, now=now) == pytest.approx(30.0)

    def test_past_date_and_negative_are_zero(self):
        now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        assert parse_retry_after(format_datetime(now - timedelta(minutes=1), usegmt=True), now=now) == 0.0
        assert parse_retry_after("-5") == 0.0

    @pytest.mark.parametrize("value", [None, "", "soon", "  "])
    def test_garbage_is_none(self, value):
        assert parse_retry_after(value) is None


class TestPolicy:
    def test_exponential_ceiling_with_full_jitter(self):
        policy = RetryPolicy(base_delay=2, max_delay=10)
        assert policy.delay(1, rng=lambda: 1.0) == 2
        assert policy.delay(3, rng=lambda: 1.0) == 8
        assert policy.delay(5, rng=lambda: 1.0) == 10  # capped
        assert policy.delay(3, rng=lambda: 0.5) == 4

    def test_retry_after_wins_but_is_capped(self):
        policy = RetryPolicy(max_retry_after=60)
        assert policy.delay(1, retry_after=5) == 5
        assert policy.delay(1, retry_after=3600) == 60


class TestBreaker:
    def test_opens_after_threshold_then_half_opens(self):
        clock = Clock()
        breaker = CircuitBreaker(failure_threshold=2, reset_timeout=60, clock=clock)
        breaker.record_failure()
        assert breaker.state == "closed"
        breaker.record_failure()
        assert breaker.state == "open" and not breaker.allow()
        clock.t += 61
        assert breaker.state == "half_open" and breaker.allow()
        breaker.record_failure()  # trial failed: open again
        assert breaker.state == "open"
        clock.t += 61
        breaker.record_success()
        assert breaker.state == "closed" and breaker.failures == 0

    def test_registry_is_per_host(self):
        registry = BreakerRegistry(failure_threshold=1)
        registry.get("a.example").record_failure()
        assert registry.get("a.example").state == "open"
        assert registry.get("b.example").state == "closed"
        assert registry.snapshot() == {"a.example": "open", "b.example": "closed"}


class TestRetryingHTTP:
    def make(self, send, attempts=3, **kw):
        sleeps: list[float] = []
        http = RetryingHTTP(send, RetryPolicy(max_attempts=attempts, base_delay=1), sleep=sleeps.append, **kw)
        return http, sleeps

    def test_retries_network_errors_then_succeeds(self):
        send = scripted(requests.ConnectionError("reset"), requests.Timeout("slow"), FakeResponse(200))
        http, sleeps = self.make(send)
        assert http.get("https://x.example/a").status_code == 200
        assert len(send.calls) == 3 and len(sleeps) == 2

    def test_default_timeout_is_always_set(self):
        send = scripted(FakeResponse(200))
        http, _ = self.make(send)
        http.post("https://x.example/a")
        assert send.calls[0][2]["timeout"] == http.timeout

    def test_retry_after_header_is_honoured(self):
        send = scripted(FakeResponse(429, {"Retry-After": "7"}), FakeResponse(200))
        http, sleeps = self.make(send)
        http.get("https://x.example/a")
        assert sleeps == [7.0]

    def test_exhausted_retryable_status_returns_last_response(self):
        send = scripted(FakeResponse(503), FakeResponse(503), FakeResponse(503))
        http, _ = self.make(send)
        assert http.get("https://x.example/a").status_code == 503

    def test_exhausted_network_errors_raise(self):
        send = scripted(*[requests.ConnectionError("down")] * 3)
        http, _ = self.make(send)
        with pytest.raises(requests.ConnectionError):
            http.get("https://x.example/a")

    def test_non_retryable_status_returns_immediately(self):
        send = scripted(FakeResponse(404))
        http, sleeps = self.make(send)
        assert http.get("https://x.example/a").status_code == 404 and sleeps == []

    def test_open_circuit_fails_fast(self):
        registry = BreakerRegistry(failure_threshold=2, reset_timeout=600)
        send = scripted(requests.ConnectionError("a"), requests.ConnectionError("b"))
        http, _ = self.make(send, attempts=5, breakers=registry)
        with pytest.raises(CircuitOpenError):
            http.get("https://down.example/a")
        assert len(send.calls) == 2  # the third attempt never reached the network

    def test_mock_like_status_is_not_retried(self):
        class Odd:
            status_code = object()

        http, _ = self.make(scripted(Odd()))
        assert isinstance(http.get("https://x.example/a"), Odd)


class TestRetryCall:
    def test_retries_malformed_json(self):
        outcomes = [ValueError("Expecting value"), {"ok": 1}]

        def fn():
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        sleeps: list[float] = []
        assert retry_call(fn, RetryPolicy(max_attempts=3), sleep=sleeps.append) == {"ok": 1}
        assert len(sleeps) == 1

    def test_non_retryable_raises_at_once(self):
        calls = []

        def fn():
            calls.append(1)
            raise KeyError("x")

        with pytest.raises(KeyError):
            retry_call(fn, RetryPolicy(max_attempts=5), sleep=lambda s: None)
        assert calls == [1]

    def test_http_error_retry_after(self):
        response = FakeResponse(429, {"Retry-After": "3"})
        error = requests.HTTPError("429", response=response)  # type: ignore[arg-type]
        outcomes = [error, "done"]

        def fn():
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        sleeps: list[float] = []
        assert retry_call(fn, RetryPolicy(max_attempts=2), sleep=sleeps.append) == "done"
        assert sleeps == [3.0]


def test_resilient_session_routes_get_through_transport():
    send = scripted(requests.ConnectionError("x"), FakeResponse(200))
    session = ResilientSession(RetryPolicy(max_attempts=2), sleep=lambda s: None, transport=send,
                               user_agent="ua-test")
    assert session.get("https://x.example/doc.pdf", headers={}).status_code == 200
    assert session.headers["User-Agent"] == "ua-test"
    assert len(send.calls) == 2


def test_redact_masks_credential_parameters():
    from external_sources.plugins.base import Failure
    from external_sources.resilience import redact
    text = "Max retries exceeded with url: /rest/facet?apiKey=SEDIA&text=*** and ?token=abc&x=1"
    assert redact(text) == "Max retries exceeded with url: /rest/facet?apiKey=***&text=*** and ?token=***&x=1"
    failure = Failure.from_exception("fetch", "k", RuntimeError("GET /x?api_key=s3cr3t"))
    assert "s3cr3t" not in failure.error
