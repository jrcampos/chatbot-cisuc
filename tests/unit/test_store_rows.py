"""Unit tests for external_sources.store.rows / chunks / batch (no database)."""

from __future__ import annotations

import copy

from external_sources.plugins.base import NormalizedRecord
from external_sources.store.batch import LoadBatch
from external_sources.store.chunks import chunk_markdown
from external_sources.store.rows import (
    LONG_VALUE_CHARS,
    norm,
    programme_key,
    provenance_rows,
    record_row,
    resolved_rows,
)

PAGE = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/T-1"
WP = "https://ec.europa.eu/docs/wp.pdf"


def make_record(**overrides) -> NormalizedRecord:
    data = {
        "natural_key": "T-1", "record_type": "call", "domain": "projects", "programme": "Horizon Europe",
        "title": "Topic one", "description": "x" * (LONG_VALUE_CHARS + 50), "status": "forthcoming",
        "funding_amount": 5_000_000.0, "start_date": "2026-12-15T00:00:00+00:00",
        "deadline_date": "2027-04-15T00:00:00+00:00", "source_url": PAGE,
        "details": {
            "call_identifier": "CALL-1", "call_title": "Call one", "keywords": ["quantum", "AI"],
            "deadline_dates": ["2027-01-10T17:00:00+00:00", "2027-04-15T00:00:00+00:00"],
            "call_budget_eur": 20_000_000.0,
            "evidence": {"details.call_budget_eur": {"quote": "HORIZON CALL-1 20.00", "url": WP, "doc_id": "d1",
                                                     "page": 34, "published_at": "2026-09-29T12:58:26+00:00"}},
            "verification": {
                "deadline_date": {
                    "status": "conflicting", "value": "2027-04-15T00:00:00+00:00",
                    "layers": {"api": ["2027-04-15"], "document": "2027-04-13"},
                    "sources": [{"layer": "api", "url": PAGE}, {"layer": "document", "url": WP, "page": 37}],
                    "agree_count": 1, "basis": "agreement", "checked_at": "2026-10-14T00:00:00+00:00",
                    "range": {"min": "2027-04-13", "max": "2027-04-15", "primary": "2027-04-15", "settled": False,
                              "values": [{"value": "2027-04-15", "layer": "api", "source": PAGE},
                                         {"value": "2027-04-13", "layer": "document", "source": WP, "page": 37}]},
                },
                "funding_amount": {
                    "status": "confirmed", "value": 5_000_000.0, "layers": {"api": 5_000_000.0, "document": 4_700_000.0},
                    "sources": [{"layer": "api", "url": PAGE}, {"layer": "document", "url": WP, "page": 40}],
                    "agree_count": 2, "basis": "agreement", "checked_at": "2026-10-14T00:00:00+00:00",
                },
                "eligibility": {"status": "confirmed", "value": None, "layers": {"api": True, "document": True},
                                "sources": [{"layer": "document", "url": WP, "page": 41}], "basis": "official_document"},
            },
            "corroboration": {"funding_amount": [{"value": 5_000_000.0, "doc_id": "d1", "url": WP, "quote": "EUR 5.00 million",
                                                  "page": 40}]},
            "value_history": [{"field": "funding_amount", "value": 20_000_000.0, "source": "api",
                               "replaced_by": "document:d1", "reason": "api_is_call_total"}],
            "sources": [{"n": 1, "url": PAGE}],
        },
        "field_sources": {"title": "api", "description": "api", "funding_amount": "document:d1",
                          "deadline_date": "api", "details.call_budget_eur": "document:d1", "details.keywords": "api"},
        "raw": {"identifier": ["T-1"], "frameworkProgramme": ["43108390"]},
    }
    data.update(overrides)
    return NormalizedRecord(**data)


def by(rows, **match):
    return [r for r in rows if all(r.get(k) == v for k, v in match.items())]


class TestProvenance:
    def test_every_layer_value_is_kept_including_disagreements(self):
        rows = provenance_rows(make_record(), "2026-10-28T00:00:00+00:00", {"d1": "2026-10-27T00:00:00+00:00"})
        deadline = by(rows, field="deadline_date")
        assert {(r["layer"], r["role"], r["value"]) for r in deadline} == {
            ("api", "value", "2027-04-15T00:00:00+00:00"), ("document", "alternative", "2027-04-13")}
        alt = by(deadline, role="alternative")[0]
        assert alt["source_url"] == WP and alt["page"] == 37 and alt["value_ts"] == "2027-04-13"

    def test_document_value_carries_evidence_and_retrieval_time(self):
        rows = provenance_rows(make_record(), "2026-10-28T00:00:00+00:00", {"d1": "2026-10-27T00:00:00+00:00"})
        [budget] = by(rows, field="details.call_budget_eur", role="value")
        assert budget["layer"] == "document" and budget["doc_id"] == "d1" and budget["page"] == 34
        assert budget["evidence_quote"] == "HORIZON CALL-1 20.00" and budget["retrieved_at"] == "2026-10-27T00:00:00+00:00"
        assert budget["value_num"] == 20_000_000.0

    def test_corroboration_superseded_and_checks(self):
        rows = provenance_rows(make_record(), "t")
        assert by(rows, field="funding_amount", role="corroboration")[0]["evidence_quote"] == "EUR 5.00 million"
        superseded = by(rows, field="funding_amount", role="superseded")[0]
        assert superseded["value"] == 20_000_000.0 and "api_is_call_total" in superseded["note"]
        assert {r["layer"] for r in by(rows, field="eligibility", role="check")} == {"api", "document"}
        # the API's 4.7M-vs-5M disagreement inside the verdict is an alternative row
        assert by(rows, field="funding_amount", role="alternative", value=4_700_000.0)

    def test_long_values_are_hashed_not_duplicated(self):
        [row] = by(provenance_rows(make_record(), "t"), field="description")
        assert row["value"] is None and len(row["value_text"]) == 300 and row["content_hash"]
        assert "full value in external_record" in row["note"]

    def test_annotations_are_not_fields(self):
        fields = {r["field"] for r in provenance_rows(make_record(), "t")}
        assert not fields & {"details.verification", "details.evidence", "details.sources", "details.value_history"}

    def test_unlabelled_field_is_layer_other(self):
        record = make_record(field_sources={})
        assert by(provenance_rows(record, "t"), field="title")[0]["layer"] == "other"


class TestResolved:
    def test_conflicting_deadline_exposes_min_max(self):
        [row] = by(resolved_rows(make_record()), field="deadline_date")
        assert row["status"] == "conflicting" and row["settled"] is False
        assert (row["value_min_ts"], row["value_max_ts"]) == ("2027-04-13", "2027-04-15")
        assert len(row["contributing"]) == 2 and row["sources"][1]["page"] == 37

    def test_confirmed_numbers_still_keep_the_spread(self):
        [row] = by(resolved_rows(make_record()), field="funding_amount")
        assert row["status"] == "confirmed"
        assert (row["value_min_num"], row["value_max_num"]) == (4_700_000.0, 5_000_000.0)

    def test_single_source_and_absent(self):
        record = make_record(start_date=None)
        rows = {r["field"]: r for r in resolved_rows(record)}
        assert rows["title"]["status"] == "single_source" and rows["title"]["value"] == "Topic one"
        assert rows["start_date"]["status"] == "absent" and rows["start_date"]["value"] is None
        assert rows["details.call_budget_eur"]["value_min_num"] == 20_000_000.0


class TestRecordRow:
    def test_columns(self):
        row, raw_sha, raw = record_row(make_record(), "ft_portal", "2026-10-28T00:00:00+00:00")
        assert row["record_kind"] == "topic" and row["programme_key"] == "43108390"
        assert row["deadline_dates"] == ["2027-01-10T17:00:00+00:00", "2027-04-15T00:00:00+00:00"]
        assert "quantum" in row["search_extra"] and row["call_identifier"] == "CALL-1"
        assert raw == {"identifier": ["T-1"], "frameworkProgramme": ["43108390"]} and len(raw_sha) == 64

    def test_programme_key_falls_back_to_label_slug(self):
        assert programme_key(make_record(raw={})) == "label:horizon-europe"

    def test_norm_treats_dates_by_day_and_numbers_by_value(self):
        assert norm("2027-04-15") == norm("2027-04-15T00:00:00+00:00")
        assert norm(5_000_000) == norm(5_000_000.0)
        assert norm("Text  A") == norm("text a")


class TestChunks:
    def test_sections_pages_and_size(self):
        markdown = "\n".join([
            "<!-- page 1 -->", "# Call one", "Intro paragraph.", "", "## Budget",
            "The indicative budget is EUR 5 million.", "<!-- page 2 -->", "More budget text " * 200,
            "[^1]: a footnote that is dropped", "## Eligibility", "Who can apply.",
        ])
        chunks = chunk_markdown(markdown, max_chars=1000, min_chars=10)
        assert all(len(c.text) <= 1000 for c in chunks)
        assert chunks[0].heading == "Call one" and chunks[0].page_start == 1
        budget = [c for c in chunks if c.heading == "Budget"]
        assert budget and budget[-1].page_end == 2
        assert not any("footnote" in c.text for c in chunks)
        assert chunks[-1].heading == "Eligibility"
        assert [c.chunk_no for c in chunks] == list(range(len(chunks)))

    def test_empty_document(self):
        assert chunk_markdown("") == []


class TestBatch:
    def make(self, records, partial=None):
        return LoadBatch("ft_portal", "r1", "2026-10-28T00:00:00+00:00", "2026-10-28T00:00:00+00:00",
                         [r.model_dump() for r in records], partial_reasons=partial or [])

    def test_sha_is_stable_and_content_sensitive(self, tmp_path):
        record = make_record()
        a = self.make([record])
        assert a.batch_sha256 == self.make([record]).batch_sha256
        changed = copy.deepcopy(record)
        changed.title = "Topic one (updated)"
        assert self.make([changed]).batch_sha256 != a.batch_sha256
        assert self.make([record], ["horizon: page 2 failed"]).batch_sha256 != a.batch_sha256
        other_run = self.make([record])
        other_run.run_id = "r2"
        assert other_run.batch_sha256 != a.batch_sha256  # a new run is a new snapshot
        path = a.write(tmp_path / "batch.json")
        assert LoadBatch.read(path).batch_sha256 == a.batch_sha256
        assert not list(tmp_path.glob("*.tmp"))


def test_nul_characters_are_stripped_everywhere():
    from external_sources.store.loader import strip_nul
    assert strip_nul({"a\x00": ["x\x00y", {"k": "\x00"}], "n": 1}) == {"a": ["xy", {"k": ""}], "n": 1}
