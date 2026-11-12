"""
Failure handling shared by every network call of the external-sources pipeline.

- `RetryPolicy`: bounded attempts, exponential backoff with full jitter, honouring
  `Retry-After` (seconds, fractional seconds such as "10.000", or an HTTP date), capped.
- `CircuitBreaker` / `BreakerRegistry`: one breaker per host. After `failure_threshold`
  consecutive failures the host is skipped for `reset_timeout` seconds (calls fail fast with
  `CircuitOpenError`), then one trial call is let through (half-open).
- `retry_call`: run any callable under a policy (used for whole "request + parse JSON" steps,
  so a truncated or malformed body is retried like a network error).
- `RetryingHTTP`: wraps a `send(method, url, **kwargs)` callable with a default timeout, the
  host's breaker and the retry policy. `ResilientSession` is a `requests.Session` whose
  `request()` goes through it, so `CachedFetcher` and `RobotsGate` get the same behaviour.

Nothing here logs request headers or query strings (they may carry keys).
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_TIMEOUT: tuple[float, float] = (10.0, 90.0)  # (connect, read) seconds
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class CircuitOpenError(requests.ConnectionError):
    """Raised instead of calling a host whose circuit is open. A `requests.ConnectionError`, so
    callers that already handle network errors (e.g. CachedFetcher serving its stale copy) treat
    it the same way; it is never retried."""

    def __init__(self, host: str, retry_in: float) -> None:
        super().__init__(f"circuit open for {host} (retry in {retry_in:.0f}s)")
        self.host = host
        self.retry_in = retry_in


class RetryableStatus(RuntimeError):
    """An HTTP response with a retryable status that exhausted its retries."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {_safe_url(url)}")
        self.status = status


_SECRET_PARAM = re.compile(r"([?&](?:api_?key|apikey|key|token|access_token|password)=)[^&\s'\"]+", re.IGNORECASE)


def redact(text: str) -> str:
    """Mask credential-like query parameters in an error message (urllib3 echoes full URLs)."""
    return _SECRET_PARAM.sub(r"\1***", text)


def _safe_url(url: str) -> str:
    """Scheme + host + path only: query strings may hold API keys."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def parse_retry_after(value: Any, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header; None when absent or unparsable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retries with exponential backoff and full jitter."""

    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 60.0
    max_retry_after: float = 300.0
    retry_statuses: frozenset[int] = RETRY_STATUSES

    def delay(self, attempt: int, retry_after: float | None = None,
              rng: Callable[[], float] = random.random) -> float:
        """Wait before attempt `attempt + 1` (attempt counts from 1)."""
        if retry_after is not None:
            return min(retry_after, self.max_retry_after)
        ceiling = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return rng() * ceiling


@dataclass
class CircuitBreaker:
    """Closed -> open after N consecutive failures -> half-open after a cool-down."""

    failure_threshold: int = 5
    reset_timeout: float = 120.0
    clock: Callable[[], float] = time.monotonic
    failures: int = 0
    opened_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.reset_timeout:
            return "half_open"
        return "open"

    def retry_in(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.reset_timeout - (self.clock() - self.opened_at))

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.state == "half_open" or self.failures >= self.failure_threshold:
                self.opened_at = self.clock()


class BreakerRegistry:
    """One `CircuitBreaker` per host."""

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 120.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, host: str) -> CircuitBreaker:
        with self._lock:
            if host not in self._breakers:
                self._breakers[host] = CircuitBreaker(self.failure_threshold, self.reset_timeout, self.clock)
            return self._breakers[host]

    def snapshot(self) -> dict[str, str]:
        """host -> state, for status reports."""
        return {host: b.state for host, b in self._breakers.items()}


NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError, ConnectionError, TimeoutError,
)


def default_is_retryable(exc: BaseException) -> bool:
    """Network errors, timeouts, retryable statuses and malformed/truncated JSON."""
    if isinstance(exc, (CircuitOpenError,)):
        return False
    if isinstance(exc, NETWORK_ERRORS + (RetryableStatus,)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        return isinstance(status, int) and status in RETRY_STATUSES
    return isinstance(exc, ValueError)  # json.JSONDecodeError and requests' subclass


def retry_call(
    fn: Callable[[], T],
    policy: RetryPolicy = RetryPolicy(),
    is_retryable: Callable[[BaseException], bool] = default_is_retryable,
    sleep: Callable[[float], None] = time.sleep,
    describe: str = "call",
) -> T:
    """Call `fn` until it succeeds, a non-retryable error is raised, or attempts run out."""
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= policy.max_attempts or not is_retryable(exc):
                raise
            retry_after = getattr(exc, "retry_after", None)
            response = getattr(exc, "response", None)
            if retry_after is None and response is not None:
                retry_after = parse_retry_after((getattr(response, "headers", None) or {}).get("Retry-After"))
            wait = policy.delay(attempt, retry_after)
            logger.warning("%s failed (attempt %d/%d: %s: %s); retrying in %.1fs", describe, attempt,
                           policy.max_attempts, type(exc).__name__, redact(str(exc))[:200], wait)
            sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


class RetryingHTTP:
    """Retries, timeouts and a per-host circuit breaker around a `send` callable."""

    def __init__(
        self,
        send: Callable[..., Any],
        policy: RetryPolicy = RetryPolicy(),
        breakers: BreakerRegistry | None = None,
        timeout: Any = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.send = send
        self.policy = policy
        self.breakers = breakers or BreakerRegistry()
        self.timeout = timeout
        self.sleep = sleep
        self.attempts = 0  # total transport calls, for run statistics

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Send with retries. Returns the final response (possibly an error status)."""
        host = urlsplit(url).netloc
        breaker = self.breakers.get(host)
        kwargs.setdefault("timeout", self.timeout)
        for attempt in range(1, self.policy.max_attempts + 1):
            if not breaker.allow():
                raise CircuitOpenError(host, breaker.retry_in())
            self.attempts += 1
            try:
                response = self.send(method, url, **kwargs)
            except NETWORK_ERRORS as exc:
                breaker.record_failure()
                if attempt >= self.policy.max_attempts:
                    raise
                wait = self.policy.delay(attempt)
                logger.warning("%s %s: %s (attempt %d/%d); retrying in %.1fs", method, _safe_url(url),
                               type(exc).__name__, attempt, self.policy.max_attempts, wait)
                self.sleep(wait)
                continue
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and status in self.policy.retry_statuses:
                breaker.record_failure()
                if attempt >= self.policy.max_attempts:
                    return response
                headers = getattr(response, "headers", None) or {}
                wait = self.policy.delay(attempt, parse_retry_after(headers.get("Retry-After")))
                logger.warning("%s %s: HTTP %d (attempt %d/%d); retrying in %.1fs", method, _safe_url(url),
                               status, attempt, self.policy.max_attempts, wait)
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                self.sleep(wait)
                continue
            breaker.record_success()
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)


class ResilientSession(requests.Session):
    """A `requests.Session` whose every request goes through `RetryingHTTP`."""

    def __init__(self, policy: RetryPolicy = RetryPolicy(), breakers: BreakerRegistry | None = None,
                 timeout: Any = DEFAULT_TIMEOUT, sleep: Callable[[float], None] = time.sleep,
                 transport: Callable[..., Any] | None = None, user_agent: str | None = None) -> None:
        super().__init__()
        if user_agent:
            self.headers["User-Agent"] = user_agent
        send = transport or (lambda method, url, **kw: requests.Session.request(self, method, url, **kw))
        self.http = RetryingHTTP(send, policy, breakers, timeout, sleep)

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        if args:
            raise TypeError("ResilientSession.request takes keyword arguments only")
        return self.http.request(method, url, **kwargs)
