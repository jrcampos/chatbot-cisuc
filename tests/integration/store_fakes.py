"""Shared fakes for the store integration tests (plugins and enrichers that never touch the network)."""

from __future__ import annotations

import time
from typing import Any

from external_sources.plugins.base import NormalizedRecord, SourcePlugin

PAGE = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{}"
WP = "https://ec.europa.eu/info/funding-tenders/opportunities/docs/wp-test.pdf"


def make_record(key: str, deadline: str = "2099-04-15T00:00:00+00:00", budget: float | None = 5_000_000.0,
                doc_deadline: str | None = None, doc_budget: float | None = None, **extra: Any) -> NormalizedRecord:
    verification: dict[str, Any] = {}
    if doc_deadline:
        verification["deadline_date"] = {
            "status": "conflicting", "value": deadline, "layers": {"api": [deadline[:10]], "document": doc_deadline},
            "sources": [{"layer": "api", "url": PAGE.format(key)}, {"layer": "document", "url": WP, "page": 37}],
            "agree_count": 1, "basis": "agreement", "checked_at": "2026-10-28T00:00:00+00:00",
            "range": {"min": min(deadline[:10], doc_deadline), "max": max(deadline[:10], doc_deadline),
                      "primary": deadline[:10], "settled": False,
                      "values": [{"value": deadline[:10], "layer": "api", "source": PAGE.format(key)},
                                 {"value": doc_deadline, "layer": "document", "source": WP, "page": 37}]}}
    if doc_budget is not None and budget is not None:
        verification["funding_amount"] = {
            "status": "conflicting", "value": budget, "layers": {"api": budget, "document": doc_budget},
            "sources": [{"layer": "api", "url": PAGE.format(key)}, {"layer": "document", "url": WP, "page": 40}],
            "agree_count": 1, "basis": "agreement", "checked_at": "2026-10-28T00:00:00+00:00",
            "range": {"min": min(budget, doc_budget), "max": max(budget, doc_budget), "primary": budget,
                      "settled": False, "values": [{"value": budget, "layer": "api", "source": PAGE.format(key)},
                                                    {"value": doc_budget, "layer": "document", "source": WP,
                                                     "page": 40}]}}
    details = {"call_identifier": key.rsplit("-", 1)[0], "call_title": f"Call of {key}",
               "keywords": ["quantum computing"] if "Q" in key else ["climate"],
               "deadline_dates": [deadline], "verification": verification,
               "sources": [{"n": 1, "kind": "topic_page", "url": PAGE.format(key)},
                           {"n": 2, "kind": "work_programme", "url": WP, "pages": [37]}]}
    details.update(extra.pop("details", {}))
    return NormalizedRecord(
        natural_key=key, record_type="call", domain="projects", programme="Horizon Europe", country_scope="EU",
        title=extra.pop("title", f"Topic {key}"), description=f"Expected outcome for {key}. Scope text.",
        status="open", funding_amount=budget, start_date="2026-09-01T00:00:00+00:00", deadline_date=deadline,
        details=details, source_url=PAGE.format(key),
        field_sources={"title": "api", "description": "api", "funding_amount": "api", "deadline_date": "api"},
        raw={"identifier": [key], "frameworkProgramme": ["43108390"], "title": [f"Topic {key}"]}, **extra)


class FakePlugin(SourcePlugin):
    name = "fake"

    def __init__(self, records: list[NormalizedRecord], partial: list[str] | None = None) -> None:
        self.records, self.partial = records, partial or []

    def fetch(self) -> list[NormalizedRecord]:
        self.partial_reasons.extend(self.partial)
        return list(self.records)


class _Pipeline:
    store = None

    def process(self, record: NormalizedRecord) -> list[Any]:
        return []


class SlowEnricher:
    """Enricher stand-in: `delay` seconds per record in phase 2 (to be killed mid-run)."""

    def __init__(self, delay: float = 0.0) -> None:
        self.pipeline = _Pipeline()
        self.delay = delay
        self.skipped: set[str] = set()

    def iter_enrich(self, records, pool=None, skip=None):
        self.skipped = set(skip or ())
        for record in records:
            self.pipeline.process(record)
        for record in records:
            if record.natural_key in self.skipped:
                continue
            time.sleep(self.delay)
            yield record, record, [], None
