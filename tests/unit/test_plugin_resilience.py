"""
Chaos tests for the F&T plugins: network errors, 5xx, malformed JSON, empty responses, schema
drift, garbage records and interruption never abort a source; every failure is recorded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
import yaml

from external_sources.plugins.base import FetchOutcome, NormalizedRecord, SourcePlugin
from external_sources.plugins.ft_cascade import FTCascadePlugin
from external_sources.plugins.ft_europeaid import FTEuropeAidPlugin
from external_sources.plugins.ft_portal import FTPortalPlugin
from external_sources.resilience import BreakerRegistry, RetryingHTTP, RetryPolicy

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
DATA = Path(__file__).parent / "data"


class Resp:
    def __init__(self, data=None, status=200, raw: str | None = None, headers=None):
        self.status_code = status
        self._data = data
        self._raw = raw
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)  # type: ignore[arg-type]

    def json(self):
        if self._raw is not None:
            return json.loads(self._raw)
        return self._data

    def close(self):
        pass


def topic(identifier: str, deadline="2027-03-01T17:00:00.000+0000", **extra):
    metadata = {
        "identifier": [identifier], "title": [f"Title {identifier}"], "status": ["31094502"],
        "deadlineDate": [deadline], "startDate": ["2026-09-01T00:00:00.000+0000"],
        "url": [f"https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/{identifier}.json"],
        "frameworkProgramme": ["43108390"], "type": ["1"],
    }
    metadata.update(extra)
    return {"metadata": metadata}


def page(*results, total=None):
    return {"totalResults": len(results) if total is None else total, "results": list(results)}


class Router:
    """Fake transport: answers by programme id found in the multipart query."""

    def __init__(self, by_programme: dict[str, list], facet=None):
        self.by_programme = {k: list(v) for k, v in by_programme.items()}
        self.facet = facet
        self.calls = 0

    def __call__(self, method, url, **kwargs):
        self.calls += 1
        if url.endswith("/facet"):
            item = self.facet
        else:
            query = json.loads(kwargs["files"]["query"][1])
            must = query["bool"]["must"]
            ids = [t["terms"]["frameworkProgramme"][0] for t in must if "frameworkProgramme" in t.get("terms", {})]
            key = ids[0] if ids else "catch_all"
            queue = self.by_programme.get(key) or [Resp(page())]
            item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def two_programmes(tmp_path):
    config = yaml.safe_load((Path(__file__).parents[2] / "external_sources/config/eu_programmes.yaml").read_text())
    config["programmes"] = {"horizon": config["programmes"]["horizon"], "life": config["programmes"]["life"]}
    path = tmp_path / "programmes.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def plugin_with(router, config_path, cls=FTPortalPlugin, breaker_threshold=50, **kwargs):
    http = RetryingHTTP(router, RetryPolicy(max_attempts=3, base_delay=0), sleep=lambda s: None,
                        breakers=BreakerRegistry(failure_threshold=breaker_threshold))
    return cls(config_path=config_path, now=NOW, http=http, sleep=lambda s: None, **kwargs)


HORIZON, LIFE = "43108390", "43252405"


class TestIsolation:
    def test_failing_programme_does_not_stop_the_others(self, two_programmes):
        router = Router({
            HORIZON: [requests.ConnectionError("reset")],
            LIFE: [Resp(page(topic("LIFE-2026-A")))],
        })
        outcome = plugin_with(router, two_programmes).collect()
        assert [r.natural_key for r in outcome.records] == ["LIFE-2026-A"]
        assert not outcome.complete
        assert outcome.partial_reasons == ["horizon: page 1 failed (ConnectionError)"]
        assert outcome.failures[0].stage == "fetch" and outcome.failures[0].key == "horizon:page1"
        assert outcome.failures[0].payload["page"] == 1

    def test_host_outage_opens_the_circuit_and_fails_fast(self, two_programmes):
        router = Router({HORIZON: [requests.ConnectionError("down")], LIFE: [requests.ConnectionError("down")]})
        outcome = plugin_with(router, two_programmes, breaker_threshold=3).collect()
        assert outcome.records == [] and len(outcome.partial_reasons) == 2
        assert "CircuitOpenError" in outcome.failures[-1].error
        assert router.calls == 3  # the second programme never touched the network

    def test_5xx_then_success_is_complete(self, two_programmes):
        router = Router({HORIZON: [Resp(status=503), Resp(page(topic("HORIZON-X-01")))],
                         LIFE: [Resp(page())]})
        outcome = plugin_with(router, two_programmes).collect()
        assert outcome.complete and [r.natural_key for r in outcome.records] == ["HORIZON-X-01"]

    def test_malformed_json_is_retried(self, two_programmes):
        router = Router({HORIZON: [Resp(raw="{\"results\": [tru"), Resp(page(topic("HORIZON-X-02")))],
                         LIFE: [Resp(page())]})
        outcome = plugin_with(router, two_programmes).collect()
        assert outcome.complete and len(outcome.records) == 1

    def test_persistently_malformed_json_is_a_recorded_failure(self, two_programmes):
        router = Router({HORIZON: [Resp(raw="<html>maintenance</html>")], LIFE: [Resp(page(topic("LIFE-1")))]})
        outcome = plugin_with(router, two_programmes).collect()
        assert len(outcome.records) == 1 and not outcome.complete
        assert "JSONDecodeError" in outcome.failures[0].error

    def test_empty_body_object_is_schema_drift_not_a_crash(self, two_programmes):
        router = Router({HORIZON: [Resp({})], LIFE: [Resp(page(topic("LIFE-1")))]})
        outcome = plugin_with(router, two_programmes).collect()
        assert len(outcome.records) == 1
        assert "schema drift" in outcome.failures[0].error

    def test_non_object_json_is_a_failure(self, two_programmes):
        router = Router({HORIZON: [Resp([1, 2])], LIFE: [Resp(page())]})
        outcome = plugin_with(router, two_programmes).collect()
        assert outcome.failures and "not a JSON object" in outcome.failures[0].error

    def test_garbage_records_are_dead_lettered_with_payload(self, two_programmes):
        garbage = {"no_metadata": True}
        no_id = {"metadata": {"title": ["x"], "status": ["31094502"]}}
        router = Router({HORIZON: [Resp(page(garbage, no_id, topic("HORIZON-OK-1")))], LIFE: [Resp(page())]})
        outcome = plugin_with(router, two_programmes).collect()
        assert [r.natural_key for r in outcome.records] == ["HORIZON-OK-1"]
        stages = [(f.stage, f.payload) for f in outcome.failures]
        assert ("normalize", garbage) in stages and ("normalize", no_id) in stages

    def test_unknown_raw_keys_become_schema_drift_gaps(self, two_programmes):
        router = Router({HORIZON: [Resp(page(topic("HORIZON-D-1", brandNewField=["x"])))], LIFE: [Resp(page())]})
        record = plugin_with(router, two_programmes).collect().records[0]
        drift = [g for g in record.data_gaps if g["kind"] == "schema_drift"]
        assert drift == [{"field": "raw", "kind": "schema_drift", "reason": "unmapped raw keys",
                          "keys": ["brandNewField"]}]
        assert record.raw["brandNewField"] == ["x"]  # nothing lost

    def test_missing_title_keeps_the_record_with_a_gap(self, two_programmes):
        result = topic("HORIZON-NT-1")
        del result["metadata"]["title"]
        router = Router({HORIZON: [Resp(page(result))], LIFE: [Resp(page())]})
        record = plugin_with(router, two_programmes).collect().records[0]
        assert record.title == "HORIZON-NT-1"
        assert any(g["field"] == "title" for g in record.data_gaps)

    def test_stop_request_marks_partial(self, two_programmes):
        router = Router({HORIZON: [Resp(page(topic("HORIZON-1")))], LIFE: [Resp(page(topic("LIFE-1")))]})
        plugin = plugin_with(router, two_programmes)
        plugin.stop_requested = lambda: True
        outcome = plugin.collect()
        assert outcome.records == [] and any("interrupted" in r for r in outcome.partial_reasons)


class TestDiscoverAll:
    def test_catch_all_query_labels_unconfigured_programmes(self, two_programmes):
        facet = Resp({"facets": [{"name": "frameworkProgramme", "values": [
            {"value": "Euratom  Research (EURATOM)", "rawValue": "43298916"}]}]})
        router = Router({HORIZON: [Resp(page())], LIFE: [Resp(page())],
                         "catch_all": [Resp(page(topic("EURATOM-2026-1", frameworkProgramme=["43298916"])))]},
                        facet=facet)
        outcome = plugin_with(router, two_programmes, discover_all=True).collect()
        assert [(r.natural_key, r.programme) for r in outcome.records] == [
            ("EURATOM-2026-1", "Euratom Research (EURATOM)")]

    def test_facet_failure_falls_back_to_a_generic_label(self, two_programmes):
        router = Router({HORIZON: [Resp(page())], LIFE: [Resp(page())],
                         "catch_all": [Resp(page(topic("X-2026-1", frameworkProgramme=["999"])))]},
                        facet=Resp(status=500))
        outcome = plugin_with(router, two_programmes, discover_all=True).collect()
        assert outcome.records[0].programme == "EU programme 999"
        assert outcome.complete  # labels are decoration, not data
        assert outcome.failures[0].key.startswith("facet:")


class TestBaseCollect:
    def test_plugin_that_raises_becomes_a_failed_outcome(self):
        class Broken(SourcePlugin):
            name = "broken"

            def fetch(self):
                raise RuntimeError("boom")

        outcome = Broken().collect()
        assert isinstance(outcome, FetchOutcome) and outcome.records == []
        assert outcome.failures[0].error == "RuntimeError: boom"
        assert not outcome.complete


class TestEuropeAid:
    def make(self, two_programmes, facet=None):
        payload = json.loads((DATA / "ft_europeaid_search.json").read_text())

        def send(method, url, **kwargs):
            return facet if url.endswith("/facet") else Resp(payload)

        http = RetryingHTTP(send, RetryPolicy(max_attempts=1), sleep=lambda s: None)
        return FTEuropeAidPlugin(config_path=two_programmes, now=NOW, http=http, sleep=lambda s: None,
                                 discover_all=facet is not None)

    def test_records_in_scope_with_documents_and_spec_clean(self, two_programmes):
        plugin = self.make(two_programmes)
        records = plugin.fetch()
        keys = sorted(r.natural_key for r in records)
        assert "EuropeAid/177107/DD/ACT/LB" not in keys  # deadline 2023: closed by derivation
        assert "EuropeAid/187686/ID/ACT/MK" in keys
        mk = next(r for r in records if r.natural_key == "EuropeAid/187686/ID/ACT/MK")
        assert mk.funding_amount == 1_300_000 and mk.details["currency"] == "EUR"
        assert mk.details["publication_documents"][0]["text"] == "Twinning Fiche"
        assert mk.details["publication_documents"][0]["url"].startswith("https://webgate.ec.europa.eu/")
        assert mk.details["beneficiary_administration"].startswith("Ministry of Transport")
        assert any(g["field"] == "description" for g in mk.data_gaps)

    def test_facet_codes_are_decoded(self, two_programmes):
        facet = Resp({"facets": [
            {"name": "contractType", "values": [{"value": "Twinning", "rawValue": "42958121"}]},
            {"name": "geographicalZones", "values": [{"value": "North Macedonia", "rawValue": "20000952"}]},
            {"name": "frameworkProgramme", "values": [{"value": "EU External Action - Prospect", "rawValue": "111111"}]},
        ]})
        records = self.make(two_programmes, facet).fetch()
        mk = next(r for r in records if r.natural_key == "EuropeAid/187686/ID/ACT/MK")
        assert mk.details["contract_type"] == "Twinning"
        assert mk.details["geographical_zones"] == ["North Macedonia"]
        assert mk.programme == "EU External Action - Prospect"


def test_cascade_shares_the_isolation(two_programmes):
    router = Router({"catch_all": [requests.Timeout("slow")] * 3})
    http = RetryingHTTP(router, RetryPolicy(max_attempts=3, base_delay=0), sleep=lambda s: None)
    plugin = FTCascadePlugin(config_path=two_programmes, now=NOW, http=http, sleep=lambda s: None)
    outcome = plugin.collect()
    assert outcome.records == [] and not outcome.complete
    assert outcome.failures[0].stage == "fetch"


def test_normalized_record_shape_unchanged():
    assert "data_gaps" in NormalizedRecord.model_fields
