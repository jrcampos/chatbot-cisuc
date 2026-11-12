"""Unit tests for the F&T cascade-funding plugin (financial support to third parties; no network)."""

import json
from datetime import UTC, datetime
from pathlib import Path

from external_sources.plugins.ft_cascade import FTCascadePlugin
from external_sources.sources import topic_page_url

FIXTURE = json.loads((Path(__file__).parent / "data" / "ft_cascade_search.json").read_text())
NOW = datetime(2026, 5, 1, tzinfo=UTC)
PAGE = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/competitive-calls-cs/46786367"


def plugin(results=FIXTURE, now=NOW):
    p = FTCascadePlugin(now=now)
    calls = []

    def fake(status_codes, page_number):
        calls.append((tuple(status_codes), page_number))
        return results if page_number == 1 else {"results": [], "totalResults": results["totalResults"]}

    p._fetch_cascade_page = fake
    return p, calls


def test_query_asks_for_cascade_calls_only():
    query = FTCascadePlugin()._build_cascade_query(["31094502"])
    assert {"terms": {"type": ["8"]}} in query["bool"]["must"]
    assert {"terms": {"status": ["31094502"]}} in query["bool"]["must"]


def test_record_is_the_cascade_call_not_its_parent_topic():
    p, _ = plugin()
    [record] = p.fetch()
    assert record.natural_key == "FT-CASCADE-46786367"
    assert record.title.startswith("Enhancing the Cybersecurity Capabilities of the Domestic SME Sector")
    assert record.details["parent_topic"] == "DIGITAL-ECCC-2024-DEPLOY-NCC-06-MS-COORDINATION"
    assert record.details["organiser_project_acronym"] == "NCC-HU"
    assert record.programme == "Digital Europe Programme"
    assert record.funding_amount == 2_000_000.0
    assert record.deadline_date == "2026-06-28T22:59:00+00:00"
    assert record.status == "open" and record.source_url == PAGE
    assert record.description and "<" not in record.description
    assert record.domain == "projects" and record.record_type == "call"


def test_every_raw_key_is_mapped_or_ignored():
    p, _ = plugin()
    keys = set(FIXTURE["results"][0]["metadata"])
    assert keys <= set(p.field_spec.rules) | set(p.field_spec.ignored)


def test_expired_cascade_calls_are_dropped():
    p, _ = plugin(now=datetime(2026, 10, 14, tzinfo=UTC))
    assert p.fetch() == []


def test_portal_screen_pages_are_their_own_topic_page():
    p, _ = plugin()
    [record] = p.fetch()
    assert topic_page_url(record) == PAGE


def test_empty_description_falls_back_to_further_information_with_a_label():
    import copy

    results = copy.deepcopy(FIXTURE)
    results["results"][0]["metadata"]["description"] = ["<p>&nbsp;</p>"]
    p, _ = plugin(results)
    [record] = p.fetch()
    assert record.description.startswith("The purpose of this call for proposals")
    assert record.details["description_from"] == "furtherInformation"
    assert record.field_sources["description"] == "api"
