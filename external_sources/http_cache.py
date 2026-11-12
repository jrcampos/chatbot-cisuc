"""
Conditional-GET HTTP cache.

Every enrichment fetch (per-call detail JSON, PDFs, web pages) goes through
`CachedFetcher`, which keeps the last good body on disk with its validators
(ETag / Last-Modified) and re-validates cheaply on the next run:

- 304 -> serve the cached body ("not_modified")
- 200 with byte-identical content -> also "not_modified" (some endpoints ignore
  If-None-Match; the content hash makes detection independent of server support)
- 200 with new content -> "fresh", cache replaced
- network error / HTTP 5xx -> the previous good body is returned with status
  "error" (stale-but-usable): a failed refresh never loses information.
- HTTP 403 whose body says "Disallowed by robots.txt" (the legacy participant
  portal) -> "blocked_robots": a policy outcome, never cached and never served
  from a stale copy. Any other 403 is an ordinary "error".
- bodies larger than `max_bytes` are rejected while streaming ("error", "too
  large"), never cached.
- URLs come from call texts (some written by third parties), so every request and every
  redirect hop must be http(s) to a host whose addresses are all public (`check_public_url`):
  the loader runs inside the application network and must not reach its services or the lab
  hosts (SSRF). A blocked URL is an "error" result, never served from cache.
- with `respect_robots=True` a `RobotsGate` consults each host's robots.txt
  (cached in memory and on disk for 24 h); disallowed URLs are "blocked_robots"
  and never requested. A missing/unreachable/5xx robots.txt means allowed.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

ROBOTS_TTL_SECONDS = 24 * 3600
CHUNK_BYTES = 64 * 1024
ROBOTS_MARKER = "disallowed by robots.txt"
USER_AGENT = "cisuc-chatbot-external-sources/1.0 (research use)"
MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)


def check_public_url(url: str) -> str | None:
    """None when `url` may be fetched, else why not: http(s) only, every resolved address public."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"blocked URL (scheme/host): {url[:200]}"
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError) as exc:
        return f"blocked URL (cannot resolve {parts.hostname}: {type(exc).__name__})"
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%", 1)[0]).is_global:
            return f"blocked URL ({parts.hostname} resolves to a non-public address)"
    return None


@dataclass
class FetchResult:
    """Outcome of one `CachedFetcher.get`."""

    status: str  # "fresh" | "not_modified" | "not_found" | "error" | "blocked_robots"
    body: bytes | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    from_cache: bool = False
    error: str | None = None

    @property
    def usable(self) -> bool:
        """True when a body is available (fresh, cached, or stale-on-error)."""
        return self.body is not None


@dataclass
class RobotsDecision:
    """Whether a URL may be fetched; `note` explains a fail-open (robots unavailable)."""

    allowed: bool
    note: str | None = None


class RobotsGate:
    """Per-host robots.txt check: fetched once, cached in memory and on disk for 24 h."""

    def __init__(
        self, session: requests.Session, cache_dir: Path, timeout: float = 60.0,
        user_agent: str = USER_AGENT, ttl: float = ROBOTS_TTL_SECONDS,
    ) -> None:
        self.session = session
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.user_agent = user_agent
        self.ttl = ttl
        self._parsers: dict[str, tuple[robotparser.RobotFileParser | None, str | None]] = {}
        self.decisions: list[RobotsDecision] = []

    def _disk_path(self, origin: str) -> Path:
        key = hashlib.sha1(origin.encode("utf-8"), usedforsecurity=False).hexdigest()[:24]
        return self.cache_dir / f"robots_{key}.json"

    @staticmethod
    def _parser(lines: list[str]) -> robotparser.RobotFileParser:
        parser = robotparser.RobotFileParser()
        parser.parse(lines)
        return parser

    def _load(self, origin: str) -> tuple[robotparser.RobotFileParser | None, str | None]:
        path = self._disk_path(origin)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(data["fetched_at"]) < self.ttl:
                lines = data.get("lines")
                parser = self._parser(lines) if isinstance(lines, list) else None
                return parser, data.get("note")
        except (OSError, ValueError, KeyError, TypeError):
            pass
        parser, note, lines = None, None, None
        try:
            response = self.session.get(
                f"{origin}/robots.txt", headers={}, timeout=self.timeout, allow_redirects=False
            )
            if response.status_code == 200:
                lines = (response.content or b"").decode("utf-8", errors="replace").splitlines()
                parser = self._parser(lines)
            else:
                note = f"robots.txt HTTP {response.status_code}: treated as allowed"
        except requests.RequestException as exc:
            note = f"robots.txt unreachable ({type(exc).__name__}: {exc}): treated as allowed"
        try:
            path.write_text(
                json.dumps({"fetched_at": time.time(), "lines": lines, "note": note}),
                encoding="utf-8",
            )
        except OSError:
            pass
        return parser, note

    def check(self, url: str) -> RobotsDecision:
        """Decide whether `url` may be requested; never raises."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        first = origin not in self._parsers
        if first:
            self._parsers[origin] = self._load(origin)
        parser, note = self._parsers[origin]
        allowed = parser is None or parser.can_fetch(self.user_agent, url)
        decision = RobotsDecision(allowed, note)
        if note and first:  # a fail-open is reported once per host, not once per URL
            logger.warning("%s: %s", origin, note)
            self.decisions.append(decision)
        return decision


class CachedFetcher:
    """GET with an on-disk cache and conditional revalidation."""

    def __init__(
        self,
        cache_dir: str | Path,
        session: requests.Session | None = None,
        timeout: float = 60.0,
        min_interval: float = 0.25,
        force_refresh: bool = False,
        max_bytes: int = 60_000_000,
        respect_robots: bool = False,
        fresh_for: float = 0.0,
        url_check: Callable[[str], str | None] | None = check_public_url,
    ) -> None:
        """
        Args:
            cache_dir: directory for cached bodies (`<key>.bin`) and metadata (`<key>.json`).
            session: HTTP session (tests inject a fake); defaults to one with retries.
            timeout: per-request timeout in seconds.
            min_interval: minimum seconds between network requests (politeness;
                the portal drops connections when hammered).
            force_refresh: ignore cached validators and always re-download.
            max_bytes: reject (without caching) bodies larger than this.
            respect_robots: consult robots.txt per host before every request.
            fresh_for: seconds a cached body is served without any request (0 = always
                revalidate). Used by resumed and repeated runs to stay polite.
            url_check: returns why a URL must not be requested (None = allowed); checked for
                the URL and every redirect hop. Tests with fake sessions pass None.
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or self._default_session()
        self.timeout = timeout
        self.min_interval = min_interval
        self.force_refresh = force_refresh
        self.max_bytes = max_bytes
        self.robots = RobotsGate(self.session, self.cache_dir, timeout) if respect_robots else None
        self.fresh_for = fresh_for
        self.url_check = url_check
        self._last_request = 0.0

    @staticmethod
    def _default_session() -> requests.Session:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        retry = Retry(
            total=5, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
            # Some servers send a non-integer Retry-After ("10.000"), which urllib3
            # rejects with InvalidHeader; keep the exponential backoff instead.
            respect_retry_after_header=False,
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.mount("http://", HTTPAdapter(max_retries=retry))
        return session

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()[:24]
        return self.cache_dir / f"{key}.json", self.cache_dir / f"{key}.bin"

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _stale(self, meta: dict | None, body_path: Path, error: str) -> FetchResult:
        if meta and body_path.exists():
            return FetchResult(
                "error", body_path.read_bytes(), meta.get("content_type"), meta.get("etag"),
                meta.get("last_modified"), from_cache=True, error=error,
            )
        return FetchResult("error", error=error)

    def get(self, url: str) -> FetchResult:
        """Fetch `url`, revalidating any cached copy. See the module docstring."""
        meta_path, body_path = self._paths(url)
        meta: dict | None = None
        if meta_path.exists() and body_path.exists():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                meta = loaded if isinstance(loaded, dict) else None
            except (ValueError, OSError):
                meta = None  # unreadable metadata == no cache

        if meta and self.fresh_for > 0 and not self.force_refresh and self._is_fresh(meta):
            return FetchResult(
                "not_modified", body_path.read_bytes(), meta.get("content_type"), meta.get("etag"),
                meta.get("last_modified"), from_cache=True,
            )

        blocked = self.url_check(url) if self.url_check else None
        if blocked:
            return FetchResult("error", error=blocked)

        if self.robots is not None and not self.robots.check(url).allowed:
            return FetchResult("blocked_robots", error="disallowed by robots.txt")

        headers: dict[str, str] = {}
        if meta and not self.force_refresh:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        self._throttle()
        try:
            response, blocked = self._get_following_redirects(url, headers)
        except requests.RequestException as exc:
            return self._stale(meta, body_path, f"{type(exc).__name__}: {exc}")
        if blocked:
            return FetchResult("error", error=blocked)
        try:
            return self._handle(url, response, meta, meta_path, body_path)
        finally:
            close = getattr(response, "close", None)
            if close:
                close()

    def _get_following_redirects(self, url: str, headers: dict[str, str]) -> tuple[Any, str | None]:
        """GET with redirects followed here, so every hop passes `url_check`."""
        for _ in range(MAX_REDIRECTS + 1):
            response = self.session.get(url, headers=headers, timeout=self.timeout, stream=True,
                                        allow_redirects=False)
            location = response.headers.get("Location") if response.status_code in _REDIRECTS else None
            if not location:
                return response, None
            response.close()
            url = urljoin(url, location)
            blocked = self.url_check(url) if self.url_check else None
            if blocked:
                return None, blocked
        raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")

    def _is_fresh(self, meta: dict) -> bool:
        try:
            fetched = datetime.fromisoformat(str(meta.get("validated_at") or meta.get("fetched_at")))
        except ValueError:
            return False
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        return (datetime.now(UTC) - fetched).total_seconds() < self.fresh_for

    def _read_body(self, response) -> bytes | None:
        """Read the body in chunks; None when it exceeds `max_bytes`."""
        if not hasattr(response, "iter_content"):
            body = response.content or b""
            return body if len(body) <= self.max_bytes else None
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
            size += len(chunk)
            if size > self.max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def _handle(self, url: str, response, meta, meta_path: Path, body_path: Path) -> FetchResult:

        if response.status_code == 304 and meta:
            try:  # remember the successful revalidation (used by `fresh_for`)
                meta_path.write_text(json.dumps({**meta, "validated_at": datetime.now(UTC).isoformat()}),
                                     encoding="utf-8")
            except OSError:
                pass
            return FetchResult(
                "not_modified", body_path.read_bytes(), meta.get("content_type"),
                meta.get("etag"), meta.get("last_modified"), from_cache=True,
            )
        if response.status_code == 404:
            return FetchResult("not_found", error="HTTP 404")
        if response.status_code == 403 and ROBOTS_MARKER in (
            response.content or b""
        ).decode("utf-8", errors="replace").lower():
            return FetchResult("blocked_robots", error="disallowed by robots.txt")
        if response.status_code != 200:
            return self._stale(meta, body_path, f"HTTP {response.status_code}")

        too_large = FetchResult("error", error=f"too large (> {self.max_bytes} bytes)")
        try:
            declared = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > self.max_bytes:
            return too_large
        try:
            body = self._read_body(response)
        except requests.RequestException as exc:
            return self._stale(meta, body_path, f"{type(exc).__name__}: {exc}")
        if body is None:
            return too_large
        digest = hashlib.sha256(body).hexdigest()
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        content_type = response.headers.get("Content-Type")
        unchanged = bool(meta) and meta.get("sha256") == digest
        body_path.write_bytes(body)
        meta_path.write_text(
            json.dumps(
                {
                    "url": url, "etag": etag, "last_modified": last_modified,
                    "content_type": content_type, "sha256": digest,
                    "fetched_at": datetime.now(UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        return FetchResult(
            "not_modified" if unchanged else "fresh", body, content_type, etag, last_modified
        )
