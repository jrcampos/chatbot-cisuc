"""
Unit tests for external_sources.enrichment.detail — per-call detail endpoint.
"""

import json

from external_sources.enrichment.detail import DetailEnricher
from external_sources.http_cache import FetchResult


class StubFetcher:
    """Fetcher double returning a canned FetchResult and recording URLs."""

    def __init__(self, result):
        self.result = result
        self.urls: list[str] = []

    def get(self, url):
        self.urls.append(url)
        return self.result


TEMPLATE = "https://x.eu/topicDetails/{identifier_lower}.json"


def enricher(result, **kwargs):
    """Build an enricher over a stub fetcher."""
    return DetailEnricher(StubFetcher(result), TEMPLATE, **kwargs)


def ok(payload):
    """A fresh FetchResult carrying `payload` as JSON."""
    return FetchResult("fresh", json.dumps(payload).encode(), "application/json")


class TestDetailEnricher:
    """Test DetailEnricher.fetch()"""

    def test_url_uses_lowercase_identifier(self):
        """The portal only serves lowercase identifiers"""
        e = enricher(ok({}))

        assert e.url_for("HORIZON-CL5-2026-10-D6-06") == (
            "https://x.eu/topicDetails/horizon-cl5-2026-10-d6-06.json"
        )

    def test_unwraps_root_key(self):
        """root_key selects the payload inside the response"""
        e = enricher(ok({"TopicDetails": {"sme": False}}), root_key="TopicDetails")

        result = e.fetch("A-1")

        assert result.status == "fresh"
        assert result.payload == {"sme": False}

    def test_keep_keys_filters_payload(self):
        """Only keep_keys survive (avoids duplicating large text already in the search payload)"""
        e = enricher(
            ok({"TopicDetails": {"sme": True, "description": "big", "topicMGAs": []}}),
            root_key="TopicDetails", keep_keys=["sme", "topicMGAs"],
        )

        assert e.fetch("A-1").payload == {"sme": True, "topicMGAs": []}

    def test_not_found_has_no_payload(self):
        """A 404 propagates as not_found without a payload"""
        e = enricher(FetchResult("not_found", error="HTTP 404"))

        result = e.fetch("A-1")

        assert result.status == "not_found"
        assert result.payload is None

    def test_invalid_json_is_reported(self):
        """Non-JSON bodies become status invalid"""
        e = enricher(FetchResult("fresh", b"<html>", "text/html"))

        result = e.fetch("A-1")

        assert result.status == "invalid"
        assert result.payload is None

    def test_wrong_shape_is_reported(self):
        """A JSON list where a dict is expected is invalid"""
        e = enricher(ok([1, 2]))

        assert e.fetch("A-1").status == "invalid"

    def test_stale_error_still_yields_payload(self):
        """A stale cached body (status error) is still parsed and returned"""
        stale = FetchResult("error", json.dumps({"sme": True}).encode(), None,
                            from_cache=True, error="down")
        e = enricher(stale)

        result = e.fetch("A-1")

        assert result.status == "error"
        assert result.payload == {"sme": True}
        assert result.error == "down"
