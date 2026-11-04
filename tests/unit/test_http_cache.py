"""
Unit tests for external_sources.http_cache — the conditional-GET cache used by
detail enrichment and document capture.
"""

import json
import time

import requests

from external_sources.http_cache import USER_AGENT, CachedFetcher


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True


class ChunkedResponse(FakeResponse):
    """Streaming response: only `iter_content` exposes the body; counts chunks served."""

    def __init__(self, chunks, status_code=200, headers=None):
        super().__init__(status_code, b"", headers)
        self._chunks = chunks
        self.served = 0

    def iter_content(self, chunk_size=1):
        for chunk in self._chunks:
            self.served += 1
            yield chunk


class FakeSession:
    """Session double returning queued responses and recording requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append({"url": url, "headers": dict(headers or {}), "stream": stream})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fetcher(tmp_path, *responses, **kwargs):
    """Build a CachedFetcher over a fake session with no throttling."""
    session = FakeSession(*responses)
    return CachedFetcher(tmp_path, session=session, min_interval=0, **kwargs), session


URL = "https://example.eu/doc.pdf"
HEADERS = {"ETag": '"abc"', "Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT",
           "Content-Type": "application/pdf"}


class TestFirstFetch:
    """Test an uncached request"""

    def test_fresh_result_is_returned_and_cached(self, tmp_path):
        """A 200 is returned as fresh with its validators"""
        f, _ = fetcher(tmp_path, FakeResponse(200, b"BODY", HEADERS))

        result = f.get(URL)

        assert result.status == "fresh"
        assert result.body == b"BODY"
        assert result.etag == '"abc"'
        assert result.last_modified == HEADERS["Last-Modified"]
        assert result.from_cache is False

    def test_not_found_has_no_body(self, tmp_path):
        """A 404 is reported as not_found"""
        f, _ = fetcher(tmp_path, FakeResponse(404))

        result = f.get(URL)

        assert result.status == "not_found"
        assert result.usable is False

    def test_error_without_cache_has_no_body(self, tmp_path):
        """A network error with nothing cached yields status error and no body"""
        f, _ = fetcher(tmp_path, requests.ConnectionError("boom"))

        result = f.get(URL)

        assert result.status == "error"
        assert result.body is None
        assert "boom" in result.error


class TestRevalidation:
    """Test conditional requests against a cached copy"""

    def test_conditional_headers_are_sent(self, tmp_path):
        """The second request carries If-None-Match and If-Modified-Since"""
        f, session = fetcher(
            tmp_path, FakeResponse(200, b"BODY", HEADERS), FakeResponse(304)
        )
        f.get(URL)

        f.get(URL)

        sent = session.calls[1]["headers"]
        assert sent["If-None-Match"] == '"abc"'
        assert sent["If-Modified-Since"] == HEADERS["Last-Modified"]

    def test_304_returns_cached_body_as_not_modified(self, tmp_path):
        """A 304 serves the cached bytes"""
        f, _ = fetcher(tmp_path, FakeResponse(200, b"BODY", HEADERS), FakeResponse(304))
        f.get(URL)

        result = f.get(URL)

        assert result.status == "not_modified"
        assert result.body == b"BODY"
        assert result.from_cache is True

    def test_server_ignoring_conditionals_is_detected_by_content(self, tmp_path):
        """200 with identical bytes counts as not_modified (some endpoints ignore If-None-Match)"""
        f, _ = fetcher(
            tmp_path, FakeResponse(200, b"BODY", HEADERS), FakeResponse(200, b"BODY", HEADERS)
        )
        f.get(URL)

        result = f.get(URL)

        assert result.status == "not_modified"

    def test_changed_content_is_fresh(self, tmp_path):
        """200 with different bytes is fresh and replaces the cache"""
        f, _ = fetcher(
            tmp_path, FakeResponse(200, b"OLD", HEADERS), FakeResponse(200, b"NEW", HEADERS),
            FakeResponse(304),
        )
        f.get(URL)

        assert f.get(URL).status == "fresh"
        assert f.get(URL).body == b"NEW"

    def test_force_refresh_sends_no_conditional_headers(self, tmp_path):
        """force_refresh ignores validators"""
        f, session = fetcher(
            tmp_path, FakeResponse(200, b"BODY", HEADERS), FakeResponse(200, b"BODY", HEADERS),
            force_refresh=False,
        )
        f.get(URL)
        f.force_refresh = True

        f.get(URL)

        assert "If-None-Match" not in session.calls[1]["headers"]


class TestStaleFallback:
    """Test that a failed refresh never loses the previous good content"""

    def test_network_error_serves_stale_body_flagged_as_error(self, tmp_path):
        """A ConnectionError after a good fetch returns the cached body with status error"""
        f, _ = fetcher(
            tmp_path, FakeResponse(200, b"BODY", HEADERS), requests.ConnectionError("down")
        )
        f.get(URL)

        result = f.get(URL)

        assert result.status == "error"
        assert result.body == b"BODY"
        assert result.from_cache is True
        assert "down" in result.error

    def test_server_error_serves_stale_body(self, tmp_path):
        """HTTP 503 after a good fetch also falls back to the cached body"""
        f, _ = fetcher(tmp_path, FakeResponse(200, b"BODY", HEADERS), FakeResponse(503))
        f.get(URL)

        result = f.get(URL)

        assert result.status == "error"
        assert result.body == b"BODY"
        assert "503" in result.error


class TestRetryAfterAndRobots:
    """Test the Retry configuration and robots-disallowed handling"""

    def test_default_session_ignores_retry_after_header(self, tmp_path):
        """A non-integer Retry-After ("10.000") must not make urllib3 raise InvalidHeader"""
        session = CachedFetcher(tmp_path)._default_session()

        retry = session.get_adapter("https://x.eu/").max_retries

        assert retry.respect_retry_after_header is False
        assert retry.backoff_factor > 0

    def test_robots_403_is_blocked_not_error(self, tmp_path):
        """A 403 'Disallowed by robots.txt' (any case) becomes blocked_robots"""
        f, _ = fetcher(tmp_path, FakeResponse(403, b"<h1>DISALLOWED BY ROBOTS.TXT</h1>"))

        result = f.get(URL)

        assert result.status == "blocked_robots"
        assert result.error == "disallowed by robots.txt"
        assert result.body is None and result.usable is False

    def test_robots_403_is_never_cached_nor_served_stale(self, tmp_path):
        """A previously cached body is not served for a robots block, and nothing is written"""
        f, _ = fetcher(
            tmp_path, FakeResponse(200, b"OLD", HEADERS),
            FakeResponse(403, b"Disallowed by robots.txt"),
        )
        f.get(URL)

        blocked = f.get(URL)

        assert blocked.status == "blocked_robots" and blocked.body is None
        assert (tmp_path / next(p.name for p in tmp_path.glob("*.bin"))).read_bytes() == b"OLD"

    def test_other_403_stays_error(self, tmp_path):
        """A plain 403 is an ordinary error"""
        f, _ = fetcher(tmp_path, FakeResponse(403, b"Forbidden"))

        result = f.get(URL)

        assert result.status == "error" and result.error == "HTTP 403"


class TestSizeCap:
    """Test the download size cap"""

    def test_default_cap_is_60mb(self, tmp_path):
        """The constructor default matches the policy default"""
        assert CachedFetcher(tmp_path).max_bytes == 60_000_000

    def test_content_length_over_cap_is_rejected_without_reading(self, tmp_path):
        """A declared Content-Length above the cap aborts before any chunk is read"""
        resp = ChunkedResponse([b"x" * 5], headers={"Content-Length": "999"})
        f, session = fetcher(tmp_path, resp, max_bytes=100)

        result = f.get(URL)

        assert result.status == "error" and result.error == "too large (> 100 bytes)"
        assert result.body is None and resp.served == 0 and resp.closed
        assert session.calls[0]["stream"] is True
        assert list(tmp_path.glob("*")) == []

    def test_running_size_over_cap_aborts_and_is_not_cached(self, tmp_path):
        """Without Content-Length the download stops once the running size exceeds the cap"""
        resp = ChunkedResponse([b"x" * 60] * 10)
        f, _ = fetcher(tmp_path, resp, max_bytes=100)

        result = f.get(URL)

        assert result.status == "error" and "too large" in result.error
        assert resp.served == 2 and resp.closed
        assert list(tmp_path.glob("*")) == []

    def test_chunked_body_within_cap_is_joined(self, tmp_path):
        """Chunks under the cap are concatenated and cached"""
        f, _ = fetcher(tmp_path, ChunkedResponse([b"AB", b"CD"], headers=HEADERS), max_bytes=100)

        result = f.get(URL)

        assert result.status == "fresh" and result.body == b"ABCD"
        assert list(tmp_path.glob("*.bin"))

    def test_content_fallback_is_capped_too(self, tmp_path):
        """A response without iter_content is checked via .content"""
        f, _ = fetcher(tmp_path, FakeResponse(200, b"x" * 50), max_bytes=10)

        assert f.get(URL).error == "too large (> 10 bytes)"


def _robots(text, status=200):
    return FakeResponse(status, text.encode())


DOC = "https://example.eu/private/doc.pdf"
OK = "https://example.eu/public/doc.pdf"
DISALLOW = "User-agent: *\nDisallow: /private/\n"


class TestRobotsGate:
    """Test robots.txt compliance"""

    def test_disallowed_url_is_blocked_and_never_requested(self, tmp_path):
        """A disallowed path yields blocked_robots without requesting the document"""
        f, session = fetcher(tmp_path, _robots(DISALLOW), respect_robots=True)

        result = f.get(DOC)

        assert result.status == "blocked_robots" and result.error == "disallowed by robots.txt"
        assert [c["url"] for c in session.calls] == ["https://example.eu/robots.txt"]

    def test_allowed_url_passes(self, tmp_path):
        """An allowed path is fetched normally"""
        f, session = fetcher(tmp_path, _robots(DISALLOW), FakeResponse(200, b"B", HEADERS),
                             respect_robots=True)

        result = f.get(OK)

        assert result.status == "fresh"
        assert [c["url"] for c in session.calls] == ["https://example.eu/robots.txt", OK]

    def test_robots_fetched_once_per_host(self, tmp_path):
        """Two documents on one host trigger a single robots.txt request"""
        f, session = fetcher(tmp_path, _robots(DISALLOW), FakeResponse(200, b"A", HEADERS),
                             FakeResponse(200, b"B", HEADERS), respect_robots=True)

        f.get(OK)
        f.get(OK + "2")

        assert sum(c["url"].endswith("/robots.txt") for c in session.calls) == 1

    def test_missing_robots_means_allowed(self, tmp_path):
        """404 and 5xx robots.txt fail open"""
        f, _ = fetcher(tmp_path, _robots("", 404), FakeResponse(200, b"B", HEADERS),
                       respect_robots=True)
        assert f.get(DOC).status == "fresh"
        g, _ = fetcher(tmp_path / "b", _robots("", 503), FakeResponse(200, b"B", HEADERS),
                       requests.ConnectionError("x"), respect_robots=True)
        assert g.get(DOC).status == "fresh"
        assert g.robots.decisions[-1].note

    def test_unreachable_robots_means_allowed(self, tmp_path):
        """A network error fetching robots.txt is not an exception"""
        f, _ = fetcher(tmp_path, requests.ConnectionError("down"),
                       FakeResponse(200, b"B", HEADERS), respect_robots=True)

        assert f.get(DOC).status == "fresh"
        assert "down" in f.robots.decisions[-1].note

    def test_disk_cache_is_reused_across_fetchers(self, tmp_path):
        """A second fetcher reads robots_<hash>.json instead of the network"""
        f, _ = fetcher(tmp_path, _robots(DISALLOW), respect_robots=True)
        f.get(DOC)
        assert list(tmp_path.glob("robots_*.json"))

        g, session = fetcher(tmp_path, respect_robots=True)

        assert g.get(DOC).status == "blocked_robots"
        assert session.calls == []

    def test_stale_disk_cache_is_refetched(self, tmp_path):
        """Entries older than 24h are ignored"""
        f, _ = fetcher(tmp_path, _robots(DISALLOW), respect_robots=True)
        f.get(DOC)
        path = next(tmp_path.glob("robots_*.json"))
        data = json.loads(path.read_text())
        data["fetched_at"] = time.time() - 25 * 3600
        path.write_text(json.dumps(data))

        g, session = fetcher(tmp_path, _robots(""), FakeResponse(200, b"B", HEADERS),
                             respect_robots=True)

        assert g.get(DOC).status == "fresh"
        assert session.calls[0]["url"].endswith("/robots.txt")

    def test_specific_user_agent_group_applies(self, tmp_path):
        """Rules for our own agent token override the '*' group"""
        text = f"User-agent: {USER_AGENT.split('/')[0]}\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
        f, _ = fetcher(tmp_path, _robots(text), respect_robots=True)

        assert f.get(OK).status == "blocked_robots"

    def test_disabled_by_default(self, tmp_path):
        """Without respect_robots no robots.txt request is made"""
        f, session = fetcher(tmp_path, FakeResponse(200, b"B", HEADERS))

        f.get(DOC)

        assert [c["url"] for c in session.calls] == [DOC]


class TestCorruptMetadata:
    """Test that unreadable cache metadata means 'no cache'"""

    def test_corrupt_meta_is_treated_as_uncached(self, tmp_path):
        """Garbage JSON does not raise; the document is fetched fresh"""
        f, _ = fetcher(tmp_path, FakeResponse(200, b"BODY", HEADERS), FakeResponse(200, b"NEW", HEADERS))
        f.get(URL)
        meta = next(tmp_path.glob("*.json"))
        meta.write_text("{not json")

        result = f.get(URL)

        assert result.status == "fresh" and result.body == b"NEW"
