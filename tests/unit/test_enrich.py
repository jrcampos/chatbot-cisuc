"""
Unit tests for external_sources.enrich — orchestration of documents, extraction,
provenance, gaps, change tracking and quality reporting.
"""


import pytest

from external_sources.documents.pipeline import CallDocument
from external_sources.documents.store import DocumentStore
from external_sources.enrich import Enricher
from external_sources.extraction.fields import load_families
from external_sources.plugins.base import NormalizedRecord

FICHE_URL = "https://ec.europa.eu/info/funding-tenders/opportunities/docs/2021-2027/smp/wp-call/2026/call-fiche_x.html"
FICHE_MD = """## 1. Background
Food waste is a large problem in the Union and this call addresses it with concrete support for
stakeholders across the whole food supply chain, including measurement and prevention actions.
## 2. Objectives
The objective of the action is to support the implementation of effective food waste prevention
initiatives within organisations and throughout the food supply chain, including consumers.
## 3. Available budget
The estimated available call budget is EUR 4 000 000.
## 4. Timetable
Deadline in October.
"""
TABLE_FICHE_MD = """## 3. Available budget
The estimated available call budget is EUR 150 000 000.
Topic Topic budget
1 — Alpha system EUR 60 000 000
2 — Beta system EUR 60 000 000
3 — Gamma system EUR 30 000 000
## 4. Timetable
Call opening: 1 June 2026
"""
REQUIRED = ["description"]
REQUIRED_ANY = {"budget": ["funding_amount", "details.topic_budget_eur", "details.call_budget_eur"]}


def call(key="SMP-FOOD-X", **kwargs):
    """A call record; keyword arguments override the defaults."""
    base = dict(
        natural_key=key, record_type="call", domain="projects", programme="SMP",
        title="T", source_url="https://x.eu/" + key, raw={},
    )
    base.update(kwargs)
    return NormalizedRecord(**base)


class FakePipeline:
    """Pipeline double: fixed CallDocuments over a real store."""

    def __init__(self, tmp_path, docs_by_key):
        self.store = DocumentStore(tmp_path)
        self.docs_by_key = docs_by_key

    def process(self, record):
        return self.docs_by_key.get(record.natural_key, [])


def fiche_doc(store, markdown=FICHE_MD, role="call_specific", url=FICHE_URL):
    """Store a document and return its CallDocument."""
    doc_id = store.save(url, markdown, {"role": role})
    return CallDocument(url, role, "Call document", "fresh", doc_id, len(markdown))


class TestEnricher:
    """Test Enricher.enrich_record()"""

    def _enricher(self, tmp_path, docs_by_key=None):
        pipeline = FakePipeline(tmp_path, docs_by_key or {})
        return Enricher(pipeline, load_families(), REQUIRED, REQUIRED_ANY), pipeline

    def test_fills_missing_fields_from_a_call_document(self, tmp_path):
        """An empty budget and description are filled from the call fiche with provenance"""
        enricher, pipeline = self._enricher(tmp_path)
        pipeline.docs_by_key["SMP-FOOD-X"] = [fiche_doc(pipeline.store)]

        record, docs = enricher.enrich_record(call())

        assert record.funding_amount is None  # a call total is not a topic budget
        assert record.details["call_budget_eur"] == 4_000_000.0
        assert "Food waste is a large problem" in record.description
        assert record.field_sources["details.call_budget_eur"].startswith("document:")
        assert record.details["evidence"]["details.call_budget_eur"]["url"] == FICHE_URL
        assert record.data_gaps == []  # budget information exists at call level
        assert docs[0].status == "fresh"

    def test_api_values_are_kept_and_the_call_total_is_reported_beside_them(self, tmp_path):
        """The API topic budget stays; the document's call total is added as its own field"""
        enricher, pipeline = self._enricher(tmp_path)
        pipeline.docs_by_key["SMP-FOOD-X"] = [fiche_doc(pipeline.store)]
        api = call(funding_amount=1_000_000.0, description="API description")

        record, _ = enricher.enrich_record(api)

        assert record.funding_amount == 1_000_000.0 and record.description == "API description"
        assert record.details["call_budget_eur"] == 4_000_000.0
        assert record.data_gaps == []

    def test_published_at_comes_from_the_documents_last_modified(self, tmp_path):
        """The evidence carries the document's own publication time in ISO UTC"""
        enricher, pipeline = self._enricher(tmp_path)
        doc_id = pipeline.store.save(FICHE_URL, FICHE_MD, {"role": "call_specific", "last_modified": "Tue, 29 Sep 2026 13:02:31 GMT"})
        pipeline.docs_by_key["SMP-FOOD-X"] = [CallDocument(FICHE_URL, "call_specific", "d", "fresh", doc_id, 1)]

        record, _ = enricher.enrich_record(call())

        evidence = record.details["evidence"]["details.call_budget_eur"]
        assert evidence["published_at"] == "2026-09-29T13:02:31+00:00"

    def test_topic_budgets_are_matched_per_topic_and_a_repeated_call_total_is_replaced(self, tmp_path):
        """Three topics each carrying the call total get their own row from the call document"""
        enricher, pipeline = self._enricher(tmp_path)
        doc = fiche_doc(pipeline.store, TABLE_FICHE_MD)
        topics = [
            call(f"EDF-X-{name.upper()}", title=f"{name} system", funding_amount=150e6,
                 details={"call_identifier": "EDF-X"})
            for name in ("Alpha", "Beta", "Gamma")
        ]
        for topic in topics:
            pipeline.docs_by_key[topic.natural_key] = [doc]

        enriched, _ = enricher.enrich(topics)

        by_key = {r.natural_key: r for r in enriched}
        assert by_key["EDF-X-ALPHA"].funding_amount == 60e6
        assert by_key["EDF-X-GAMMA"].funding_amount == 30e6
        assert by_key["EDF-X-GAMMA"].details["topic_budget_eur"] == 30e6
        assert by_key["EDF-X-GAMMA"].details["call_budget_eur"] == 150e6
        assert by_key["EDF-X-GAMMA"].details["topic_budget_match"]["how"] == "identifier"
        history = by_key["EDF-X-GAMMA"].details["value_history"][0]
        assert history["value"] == 150e6 and history["reason"] == "api_is_call_total"
        assert len(by_key["EDF-X-GAMMA"].details["topic_budget_table"]) == 3

    def test_a_sample_run_still_sees_all_siblings_through_the_pool(self, tmp_path):
        """Enriching one topic with the whole pool matches its row; without the pool it would not"""
        enricher, pipeline = self._enricher(tmp_path)
        doc = fiche_doc(pipeline.store, TABLE_FICHE_MD)
        topics = [
            call(f"EDF-X-{name.upper()}", title=f"{name} system", details={"call_identifier": "EDF-X"})
            for name in ("Alpha", "Beta", "Gamma")
        ]
        for topic in topics:
            pipeline.docs_by_key[topic.natural_key] = [doc]

        enriched, _ = enricher.enrich([topics[2]], pool=topics)

        assert enriched[0].funding_amount == 30e6

    def test_no_documents_records_a_gap_with_reason(self, tmp_path):
        """Nothing to read -> the still-empty required fields are logged, the record is kept"""
        enricher, _ = self._enricher(tmp_path)

        record, docs = enricher.enrich_record(call())

        assert docs == []
        assert {g["field"] for g in record.data_gaps} == {"description", "budget"}
        assert all(g["reason"] == "no_document_available" and g["kind"] == "missing_required" for g in record.data_gaps)

    def test_programme_level_document_without_the_call_section(self, tmp_path):
        """A work programme lacking the call's heading gives reason document_anchor_not_found"""
        enricher, pipeline = self._enricher(tmp_path)
        wp = fiche_doc(pipeline.store, "### OTHER-2026-01-A-01: Another topic\ntext\n", "programme_level", "https://x.eu/wp.html")
        pipeline.docs_by_key["SMP-FOOD-X"] = [wp]

        record, _ = enricher.enrich_record(call())

        assert {g["reason"] for g in record.data_gaps} == {"document_anchor_not_found"}

    def test_documents_read_but_value_absent(self, tmp_path):
        """Documents were read but hold nothing -> absent_in_documents"""
        enricher, pipeline = self._enricher(tmp_path)
        empty = fiche_doc(pipeline.store, "## 1. Background\nshort\n")
        pipeline.docs_by_key["SMP-FOOD-X"] = [empty]

        record, _ = enricher.enrich_record(call())

        assert {g["reason"] for g in record.data_gaps} == {"absent_in_documents"}

    def test_existing_gaps_are_kept_and_not_duplicated(self, tmp_path):
        """Earlier data_gaps survive; stale missing_required entries are recomputed, not stacked"""
        enricher, _ = self._enricher(tmp_path)
        start = call(data_gaps=[
            {"field": "detail", "kind": "detail_unavailable"},
            {"field": "description", "kind": "missing_required", "reason": "old"},
        ])

        record, _ = enricher.enrich_record(start)

        kinds = [g["kind"] for g in record.data_gaps]
        assert kinds.count("detail_unavailable") == 1
        assert kinds.count("missing_required") == 2  # description + funding_amount, recomputed

    def test_unusable_documents_do_not_count_as_read(self, tmp_path):
        """A not_found document has no doc_id: reason stays no_document_available"""
        enricher, pipeline = self._enricher(tmp_path)
        pipeline.docs_by_key["SMP-FOOD-X"] = [CallDocument(FICHE_URL, "call_specific", "d", "not_found")]

        record, _ = enricher.enrich_record(call())

        assert {g["reason"] for g in record.data_gaps} == {"no_document_available"}

    def test_equal_api_values_that_match_the_table_total_are_a_call_total(self, tmp_path):
        """EDF-EDIP-P shape: both topics carry 282.15M, rows sum to the 275M call budget"""
        md = (
            "## 3. Available budget\nThe estimated available call budget is EUR 275 000 000.\n"
            "Topic Topic budget\n"
            "1 — Alpha system EUR 122 250 000\n2 — Beta system EUR 152 750 000\n"
            "## 4. Timetable\nCall opening: 1 June 2026\n"
        )
        enricher, pipeline = self._enricher(tmp_path)
        doc = fiche_doc(pipeline.store, md)
        topics = [
            call(f"EDF-P-{n.upper()}", title=f"{n} system", funding_amount=282.15e6,
                 details={"call_identifier": "EDF-P"})
            for n in ("Alpha", "Beta")
        ]
        for t in topics:
            pipeline.docs_by_key[t.natural_key] = [doc]

        enriched, _ = enricher.enrich(topics)

        by_key = {r.natural_key: r for r in enriched}
        assert by_key["EDF-P-ALPHA"].funding_amount == 122.25e6
        assert by_key["EDF-P-BETA"].details["value_history"][0]["reason"] == "api_is_call_total"

    def test_equal_correct_api_values_with_a_missing_row_are_not_replaced(self, tmp_path):
        """Three topics with equal API values but only two parsed rows: a discrepancy, not a call total"""
        md = (
            "## 3. Available budget\nTopic Topic budget\n"
            "1 — Alpha system EUR 20 000 000\n2 — Beta system EUR 25 000 000\n"
            "## 4. Timetable\nCall opening: 1 June 2026\n"
        )
        enricher, pipeline = self._enricher(tmp_path)
        doc = fiche_doc(pipeline.store, md)
        topics = [
            call(f"EDF-Q-{n.upper()}", title=f"{n} system", funding_amount=100e6,
                 details={"call_identifier": "EDF-Q"})
            for n in ("Alpha", "Beta", "Gamma")
        ]
        for t in topics:
            pipeline.docs_by_key[t.natural_key] = [doc]

        enriched, _ = enricher.enrich(topics)

        alpha = {r.natural_key: r for r in enriched}["EDF-Q-ALPHA"]
        assert alpha.funding_amount == 100e6
        assert "value_history" not in alpha.details
        assert any(g["kind"] == "discrepancy" and g["field"] == "funding_amount" for g in alpha.data_gaps)

    def test_topic_evidence_is_the_verbatim_table_text_plus_the_row_label(self, tmp_path):
        """quote is the document's own text, row names the matched table row"""
        enricher, pipeline = self._enricher(tmp_path)
        doc = fiche_doc(pipeline.store, TABLE_FICHE_MD)
        topic = call("EDF-X-ALPHA", title="Alpha system", details={"call_identifier": "EDF-X"})
        pipeline.docs_by_key[topic.natural_key] = [doc]

        record, _ = enricher.enrich_record(topic)

        evidence = record.details["evidence"]["details.topic_budget_eur"]
        assert "EUR 60 000 000" in evidence["quote"] and "Topic budget" in evidence["quote"]
        assert "Alpha system" in evidence["row"]
        assert record.details["evidence"]["funding_amount"]["row"] == evidence["row"]


class RaisingPipeline(FakePipeline):
    """Pipeline whose process() fails for one record."""

    def process(self, record):
        if record.natural_key == "BAD":
            raise RuntimeError("boom")
        return super().process(record)


class TestIsolation:
    """One failing record never aborts the run"""

    def test_a_failing_record_is_kept_with_an_enrichment_error_gap(self, tmp_path):
        pipeline = RaisingPipeline(tmp_path, {})
        enricher = Enricher(pipeline, load_families(), REQUIRED, REQUIRED_ANY)
        good, bad = call("GOOD", description="d", funding_amount=1.0), call("BAD", title="orig")

        enriched, index = enricher.enrich([bad, good])

        assert [r.natural_key for r in enriched] == ["BAD", "GOOD"]
        assert enriched[0].title == "orig"
        assert {"field": "enrichment", "kind": "enrichment_error", "error": "RuntimeError: boom"} in enriched[0].data_gaps
        assert index["BAD"] == [] and "GOOD" in index


STUB_FAMILIES = """
families:
  fam_text:
    roles: [call_specific]
    sections:
      objectives: '^\\s*(?:\\d+\\.\\s*)?Objectives'
    fields:
      description: {parse: text, sections: [objectives], min_chars: 20}
  fam_budget:
    roles: [call_specific]
    sections:
      budget: '^\\s*(?:\\d+\\.\\s*)?Available budget'
    fields:
      details.call_budget_eur: {parse: eur, sections: [budget]}
"""


def stub_registry(tmp_path, call_specific):
    """Families (stub YAML) and a one-programme registry whose call_specific families are given."""
    import yaml

    from external_sources.documents.profiles import load_profiles

    (tmp_path / "fam.yaml").write_text(STUB_FAMILIES)
    (tmp_path / "prog.yaml").write_text(yaml.safe_dump({"programmes": {"smp": {"id": "1", "label": "SMP"}}}))
    (tmp_path / "prof.yaml").write_text(yaml.safe_dump(
        {"profiles": {"smp": {"extractor": "x", "families": {"call_specific": call_specific}}}}))
    return load_families(tmp_path / "fam.yaml"), load_profiles(tmp_path / "prof.yaml", tmp_path / "prog.yaml")


class TestEnricherProfiles:
    """Test Enricher family selection by programme profile"""

    def test_profile_selects_only_its_families(self, tmp_path):
        """Only the profile's families read the document, though both accept the role"""
        families, registry = stub_registry(tmp_path, ["fam_text"])
        pipeline = FakePipeline(tmp_path / "docs", {})
        pipeline.docs_by_key["SMP-FOOD-X"] = [fiche_doc(pipeline.store)]
        enricher = Enricher(pipeline, families, REQUIRED, REQUIRED_ANY, profiles=registry)

        record, _ = enricher.enrich_record(call())

        assert record.description
        assert "call_budget_eur" not in record.details

    def test_without_profiles_every_matching_family_runs(self, tmp_path):
        """Old behaviour: every family whose roles include the role"""
        families, _ = stub_registry(tmp_path, ["fam_text"])
        pipeline = FakePipeline(tmp_path / "docs", {})
        pipeline.docs_by_key["SMP-FOOD-X"] = [fiche_doc(pipeline.store)]

        record, _ = Enricher(pipeline, families, REQUIRED, REQUIRED_ANY).enrich_record(call())

        assert record.description and record.details["call_budget_eur"] == 4_000_000

    def test_empty_family_list_reads_nothing(self, tmp_path):
        """A profile with no families for the role extracts nothing"""
        families, registry = stub_registry(tmp_path, [])
        pipeline = FakePipeline(tmp_path / "docs", {})
        pipeline.docs_by_key["SMP-FOOD-X"] = [fiche_doc(pipeline.store)]

        record, _ = Enricher(pipeline, families, REQUIRED, profiles=registry).enrich_record(call())

        assert not record.description

    def test_unknown_family_name_fails_at_construction(self, tmp_path):
        """A profile naming a family that does not exist raises ValueError in __init__"""
        families, registry = stub_registry(tmp_path, ["nope"])

        with pytest.raises(ValueError, match="nope"):
            Enricher(FakePipeline(tmp_path / "docs", {}), families, REQUIRED, profiles=registry)


WP_URL = "https://x.eu/wp-2026-2027.pdf"
WP_OLD_URL = "https://x.eu/wp-2025.pdf"
WP_SECTION = (
    "### HORIZON-CL3-2027-01-BM-03: Border topic\n"
    "The indicative budget for the topic is EUR 8 000 000.\n"
)
OTHER_SECTION = "### HORIZON-CL3-2025-01-ZZ-01: Other topic\nThe indicative budget for the topic is EUR 1 000 000.\n"


def topic(key="HORIZON-CL3-2027-01-BM-03", programme="Horizon Europe", **kwargs):
    """A Horizon topic record."""
    return call(key, programme=programme, **kwargs)


class TestProgrammePool:
    """Test Enricher.enrich() anchoring topics against programme-level documents of the programme"""

    def _setup(self, tmp_path):
        pipeline = FakePipeline(tmp_path, {})
        families = load_families()
        return Enricher(pipeline, families, REQUIRED, REQUIRED_ANY), pipeline

    def test_pool_document_of_the_programme_fills_a_topic_whose_own_wp_lacks_the_section(self, tmp_path):
        """Own (older) WP has no section; the newer WP linked by a sibling has it"""
        enricher, pipeline = self._setup(tmp_path)
        old = fiche_doc(pipeline.store, OTHER_SECTION, "programme_level", WP_OLD_URL)
        new = fiche_doc(pipeline.store, WP_SECTION, "programme_level", WP_URL)
        pipeline.docs_by_key = {"HORIZON-CL3-2027-01-BM-03": [old], "SIBLING-1": [new]}

        records, index = enricher.enrich([topic(), topic("SIBLING-1")])

        first = records[0]
        assert first.funding_amount == 8_000_000
        assert WP_URL in str(first.details["evidence"]["funding_amount"])
        assert index["HORIZON-CL3-2027-01-BM-03"] == [old]

    def test_pool_document_of_another_programme_is_never_used(self, tmp_path):
        """A programme-level document linked only by another programme's record is ignored"""
        enricher, pipeline = self._setup(tmp_path)
        new = fiche_doc(pipeline.store, WP_SECTION, "programme_level", WP_URL)
        pipeline.docs_by_key = {"OTHER-1": [new]}

        records, _ = enricher.enrich([topic(), topic("OTHER-1", programme="Digital Europe")])

        assert records[0].funding_amount is None

    def test_own_document_extraction_wins_over_the_pool(self, tmp_path):
        """A field an own document yielded is not proposed again from the pool (no discrepancy)"""
        enricher, pipeline = self._setup(tmp_path)
        own = fiche_doc(pipeline.store, WP_SECTION, "programme_level", WP_OLD_URL)
        pooled = fiche_doc(
            pipeline.store, WP_SECTION.replace("8 000 000", "9 000 000"), "programme_level", WP_URL
        )
        pipeline.docs_by_key = {"HORIZON-CL3-2027-01-BM-03": [own], "SIBLING-1": [pooled]}

        records, _ = enricher.enrich([topic(), topic("SIBLING-1")])

        assert records[0].funding_amount == 8_000_000
        assert not [g for g in records[0].data_gaps if g.get("kind") == "discrepancy"]

    def test_pool_documents_are_not_in_the_returned_index(self, tmp_path):
        """The index lists only the record's own documents"""
        enricher, pipeline = self._setup(tmp_path)
        new = fiche_doc(pipeline.store, WP_SECTION, "programme_level", WP_URL)
        pipeline.docs_by_key = {"SIBLING-1": [new]}

        _, index = enricher.enrich([topic(), topic("SIBLING-1")])

        assert index["HORIZON-CL3-2027-01-BM-03"] == []
        assert index["SIBLING-1"] == [new]

    def test_non_anchoring_families_do_not_run_on_pool_documents(self, tmp_path):
        """A call-specific-style family without an anchor must not read a document the call does not link"""
        families, _ = stub_registry(tmp_path, ["fam_text"])
        pipeline = FakePipeline(tmp_path / "docs", {})
        doc = fiche_doc(pipeline.store, FICHE_MD, "programme_level", WP_URL)
        pipeline.docs_by_key = {"SIBLING-1": [doc]}
        for fam in families.values():
            object.__setattr__(fam, "roles", ("programme_level",))

        records, _ = Enricher(pipeline, families, REQUIRED).enrich([topic(), topic("SIBLING-1")])

        assert not records[0].description

    def test_enrich_record_without_documents_processes_the_record(self, tmp_path):
        """documents=None keeps the old behaviour of calling the pipeline"""
        enricher, pipeline = self._setup(tmp_path)
        pipeline.docs_by_key["SMP-FOOD-X"] = [fiche_doc(pipeline.store)]

        record, documents = enricher.enrich_record(call(), documents=None)

        assert record.description and len(documents) == 1

    def test_each_document_is_tried_once_per_identifier(self, tmp_path):
        """The (doc_id, identifier) cache stops a pool document being re-read for the same topic"""
        enricher, pipeline = self._setup(tmp_path)
        new = fiche_doc(pipeline.store, WP_SECTION, "programme_level", WP_URL)
        tried: set = set()
        record = topic()

        enricher.enrich_record(record, documents=[], pool_documents=[new], tried=tried)

        assert tried == {(new.doc_id, record.natural_key)}

    def test_iter_enrich_yields_every_record_in_order_with_pool(self, tmp_path):
        """iter_enrich streams (record, enriched, own docs, error) and still uses the programme pool"""
        enricher, pipeline = self._setup(tmp_path)
        new = fiche_doc(pipeline.store, WP_SECTION, "programme_level", WP_URL)
        pipeline.docs_by_key = {"SIBLING-1": [new]}

        rows = list(enricher.iter_enrich([topic(), topic("SIBLING-1")]))

        assert [r[0].natural_key for r in rows] == ["HORIZON-CL3-2027-01-BM-03", "SIBLING-1"]
        first, enriched, docs, error = rows[0]
        assert error is None and docs == [] and enriched.funding_amount == 8_000_000

    def test_iter_enrich_reports_a_failing_record_without_stopping(self, tmp_path):
        """A pipeline failure becomes (record, None, [], message); the next record is still enriched"""
        enricher, pipeline = self._setup(tmp_path)
        original = pipeline.process

        def process(record):
            if record.natural_key == "BAD":
                raise RuntimeError("kaput")
            return original(record)

        pipeline.process = process
        rows = list(enricher.iter_enrich([topic("BAD"), topic()]))

        assert rows[0][1] is None and rows[0][3] == "RuntimeError: kaput"
        assert rows[1][1] is not None and rows[1][3] is None


class TestSourcesAndVerification:
    """Every enriched record carries details.sources and details.verification"""

    def test_record_gets_sources_and_verdicts(self, tmp_path):
        pipeline = FakePipeline(tmp_path, {})
        enricher = Enricher(pipeline, load_families(), REQUIRED, REQUIRED_ANY)
        doc = fiche_doc(pipeline.store)
        pipeline.docs_by_key["SMP-FOOD-X"] = [doc]
        portal = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/SMP-FOOD-X"

        record, _ = enricher.enrich_record(call(source_url=portal, field_sources={"title": "api"}))

        kinds = [s["kind"] for s in record.details["sources"]]
        assert kinds == ["topic_page", "call_document"]
        assert record.details["sources"][1]["fields"]  # the fiche supplied budget/description
        verification = record.details["verification"]
        assert verification["details.call_budget_eur"]["status"] == "single_source"
        assert verification["description"]["status"] == "single_source"
        assert set(verification) >= {"title", "deadline_date", "programme", "eligibility"}

    def test_pool_document_that_supplied_a_value_is_cited(self, tmp_path):
        pipeline = FakePipeline(tmp_path, {})
        enricher = Enricher(pipeline, load_families(), REQUIRED, REQUIRED_ANY)
        new = fiche_doc(pipeline.store, WP_SECTION, "programme_level", WP_URL)
        pipeline.docs_by_key = {"SIBLING-1": [new]}

        records, index = enricher.enrich([topic(), topic("SIBLING-1")])

        urls = [s["url"] for s in records[0].details["sources"]]
        assert WP_URL in urls and index["HORIZON-CL3-2027-01-BM-03"] == []

    def test_disagreeing_values_are_stored_as_a_range(self, tmp_path):
        """details.value_range keeps min/max and every value with its source"""
        pipeline = FakePipeline(tmp_path, {})
        enricher = Enricher(pipeline, load_families(), REQUIRED, REQUIRED_ANY)
        pipeline.docs_by_key["SMP-FOOD-X"] = [fiche_doc(pipeline.store)]
        record = call(details={"call_budget_eur": 3_000_000.0},
                      field_sources={"details.call_budget_eur": "api"})

        enriched, _ = enricher.enrich_record(record)

        rng = enriched.details["value_range"]["details.call_budget_eur"]
        assert (rng["min"], rng["max"], rng["status"]) == (3_000_000.0, 4_000_000.0, "conflicting")
        assert {v["layer"] for v in rng["values"]} == {"api", "document"}
