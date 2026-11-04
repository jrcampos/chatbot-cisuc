"""
Per-call detail endpoint enrichment.

Many portals expose a richer per-item JSON than their search API. The URL
template and payload root live in config (see `detail:` in
config/eu_programmes.yaml); this class only fetches, unwraps and filters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from external_sources.http_cache import CachedFetcher


@dataclass
class DetailResult:
    """Outcome of one detail lookup."""

    status: str  # fresh | not_modified | not_found | error | invalid
    payload: dict[str, Any] | None
    error: str | None = None


class DetailEnricher:
    """Fetch and unwrap one item's detail payload."""

    def __init__(
        self,
        fetcher: CachedFetcher,
        url_template: str,
        root_key: str | None = None,
        keep_keys: list[str] | None = None,
    ) -> None:
        """
        Args:
            fetcher: cached HTTP fetcher.
            url_template: URL with `{identifier}` and/or `{identifier_lower}`.
            root_key: key wrapping the payload in the response (e.g. "TopicDetails").
            keep_keys: if given, only these payload keys are kept (avoids storing
                large text that the search payload already carries).
        """
        self.fetcher = fetcher
        self.url_template = url_template
        self.root_key = root_key
        self.keep_keys = keep_keys

    def url_for(self, identifier: str) -> str:
        """Build the detail URL for `identifier`."""
        return self.url_template.format(
            identifier=identifier, identifier_lower=identifier.lower()
        )

    def fetch(self, identifier: str) -> DetailResult:
        """Fetch the detail payload for `identifier`; never raises."""
        result = self.fetcher.get(self.url_for(identifier))
        if result.body is None:
            return DetailResult(result.status, None, result.error)
        try:
            data = json.loads(result.body)
        except ValueError as exc:
            return DetailResult("invalid", None, f"invalid JSON: {exc}")
        payload = data.get(self.root_key) if self.root_key and isinstance(data, dict) else data
        if not isinstance(payload, dict):
            return DetailResult("invalid", None, "unexpected payload shape")
        if self.keep_keys is not None:
            payload = {k: payload[k] for k in self.keep_keys if k in payload}
        return DetailResult(result.status, payload, result.error)
