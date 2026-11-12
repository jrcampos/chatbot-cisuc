"""Tests for the F&T detail-as-fallback layer (fill_from_detail + plugin integration)."""

import copy
import json
from datetime import UTC, datetime

from external_sources.enrichment.detail import DetailResult
from external_sources.plugins.ft_portal import FTPortalPlugin
from external_sources.plugins.ft_portal_detail import fill_from_detail

FIXED_NOW = datetime(2026, 9, 1, tzinfo=UTC)
TOPIC = "HORIZON-A-01"


def thin_metadata(**overrides):
    metadata = {
        "identifier": [TOPIC],
        "title": ["A title"],
        "status": ["31094502"],
        "deadlineDate": ["2027-04-06T00:00:00.000+0000"],
        "url": ["https://ec.europa.eu/topic-details/HORIZON-A-01.json"],
    }
    metadata.update(overrides)
    return metadata


def detail_payload():
    budget = {
        "budgetTopicActionMap": {
            "1": [{"action": f"{TOPIC} - Type X", "budgetYearMap": {"2026": "1000000"}}],
            "2": [{"action": "HORIZON-B-02 - Type Y", "budgetYearMap": {"2026": "777"}}],
        }
    }
    return {
        "description": "<p>Detail   description</p>",
        "conditions": "<div><h4>Conditions</h4><p>Page limit</p></div>",
        "supportInfo": "<p>Help desk</p>",
        "sepTemplate": "<p>Submit via SEP</p>",
        "budgetOverviewJSONItem": budget,
        "allowPartnerSearch": True,
        "keywords": ["ai", "robotics"],
        "publicationDateLong": 1765497600000,
        "sme": False,
    }


class StubEnricher:
    def __init__(self, payload):
        self.payload = payload

    def fetch(self, identifier):
        return DetailResult("fresh", copy.deepcopy(self.payload))


class TestFillFromDetail:
    def test_injects_missing_keys_in_search_shape(self):
        filled, injected = fill_from_detail(thin_metadata(), detail_payload())
        assert filled["descriptionByte"] == ["<p>Detail   description</p>"]
        assert filled["topicConditions"] == ["<div><h4>Conditions</h4><p>Page limit</p></div>"]
        assert filled["keywords"] == ["ai", "robotics"]
        assert {"descriptionByte", "topicConditions", "supportInfo", "budgetOverview"} <= set(injected)

    def test_does_not_overwrite_present_values(self):
        meta = thin_metadata(descriptionByte=["<p>search</p>"])
        filled, injected = fill_from_detail(meta, detail_payload())
        assert filled["descriptionByte"] == ["<p>search</p>"]
        assert "descriptionByte" not in injected

    def test_ignores_empty_detail_values(self):
        payload = {"description": "", "conditions": None, "links": [], "actions": {}, "keywords": []}
        filled, injected = fill_from_detail(thin_metadata(), payload)
        assert injected == []
        assert filled == thin_metadata()

    def test_bracket_string_and_empty_items_count_as_empty(self):
        meta = thin_metadata(links=["[]"], actions=[""], tags=[None])
        payload = {"links": [{"url": "https://x.eu"}], "actions": [{"a": 1}], "tags": ["t"]}
        filled, injected = fill_from_detail(meta, payload)
        assert set(injected) == {"links", "actions", "tags"}
        assert filled["links"] == [json.dumps([{"url": "https://x.eu"}])]
        assert filled["tags"] == ["t"]

    def test_inputs_are_not_mutated(self):
        meta, payload = thin_metadata(), detail_payload()
        meta_copy, payload_copy = copy.deepcopy(meta), copy.deepcopy(payload)
        fill_from_detail(meta, payload)
        assert meta == meta_copy
        assert payload == payload_copy

    def test_bool_and_json_wrapping(self):
        filled, _ = fill_from_detail(thin_metadata(), detail_payload())
        assert filled["allowPartnerSearch"] == ["true"]
        assert json.loads(filled["budgetOverview"][0])["budgetTopicActionMap"]["1"]


class TestPluginDetailFallback:
    """fetch() completes thin search results from the detail endpoint (FTPortalPlugin._add_detail)."""

    @staticmethod
    def build(plugin, metadata, label="Horizon Europe"):
        records = [plugin._record_from_result({"metadata": metadata}, label)]
        return plugin._add_detail(records) if plugin.detail_enricher else records

    def _plugin(self, payload):
        plugin = FTPortalPlugin(now=FIXED_NOW)
        plugin.detail_enricher = StubEnricher(payload)
        return plugin

    def test_thin_search_result_is_completed_from_detail(self):
        [record] = self.build(self._plugin(detail_payload()), thin_metadata())
        assert record.description == "Detail description"
        assert record.funding_amount == 1000000.0
        assert record.details["conditions"] == "Conditions\nPage limit"
        assert record.details["support"] == "Help desk"
        assert record.field_sources["description"] == "detail"
        assert record.field_sources["funding_amount"] == "detail"
        assert record.field_sources["title"] == "api"
        assert record.details["sme"] is False
        assert "descriptionByte" in record.raw["detail"]["_filled_from_detail"]

    def test_present_search_value_is_not_overwritten(self):
        meta = thin_metadata(descriptionByte=["<p>From search</p>"])
        [record] = self.build(self._plugin(detail_payload()), meta)
        assert record.description == "From search"
        assert record.field_sources["description"] == "api"

    def test_without_detail_enricher_fields_stay_empty(self):
        [record] = self.build(FTPortalPlugin(now=FIXED_NOW), thin_metadata(), "P")
        assert record.description is None
        assert record.field_sources["title"] == "api"
