"""Unit tests for external_sources.verification (per-field verdicts across layers)."""

from pathlib import Path

import pytest
import yaml

from external_sources.plugins.base import NormalizedRecord
from external_sources.verification import Verifier, load_verification_policy

NOW = "2026-10-14T00:00:00+00:00"
TOPIC = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/"
FICHE = "https://ec.europa.eu/info/funding-tenders/opportunities/docs/2021-2027/cerv/wp-call/2026/call-fiche_x.pdf"
DESCRIPTION = (
    "The call supports national authorities that protect victims of violence across the Union. "
    "Projects should build capacity of front-line professionals working with children and families. "
    "Actions are expected to improve cooperation between police, justice and support services."
)
FICHE_TEXT = (
    "<!-- page 3 -->\nCall: Protection of victims\nThe call supports national authorities that protect\n"
    "victims of violence across the Union. Projects should build capacity of front-line professionals\n"
    "working with children and families.\nDaphne protection call\n"
)


def record(**kwargs):
    base = dict(
        natural_key="CERV-2026-DAPHNE-PROTECTION", record_type="call", domain="projects",
        programme="Citizens, Equality, Rights and Values", title="Daphne protection call",
        source_url=TOPIC + "CERV-2026-DAPHNE-PROTECTION", raw={},
        field_sources={"title": "api", "description": "api", "deadline_date": "api",
                       "funding_amount": "api", "programme": "api", "start_date": "api"},
    )
    base.update(kwargs)
    return NormalizedRecord(**base)


@pytest.fixture
def verifier():
    return Verifier(load_verification_policy(), verdicts=[], now=NOW)


def docs(text=FICHE_TEXT, url=FICHE):
    return {"d1": (url, text)}


class TestPolicy:
    def test_default_policy_loads_and_names_every_key_field(self):
        policy = load_verification_policy()
        for field in ("title", "description", "funding_amount", "details.topic_budget_eur",
                      "details.call_budget_eur", "deadline_date", "start_date", "programme", "eligibility"):
            assert field in policy["fields"]

    def test_unknown_kind_is_rejected(self, tmp_path):
        path = tmp_path / "p.yaml"
        path.write_text(yaml.safe_dump({"fields": {"title": {"kind": "magic"}}}))
        with pytest.raises(ValueError, match="kind"):
            load_verification_policy(path)

    def test_verdict_without_evidence_is_rejected(self, tmp_path):
        from external_sources.verification import load_verdicts

        path = tmp_path / "v.yaml"
        path.write_text(yaml.safe_dump({"verdicts": [{"key": "X", "field": "deadline_date", "status": "resolved"}]}))
        with pytest.raises(ValueError, match="sources"):
            load_verdicts(path)

    def test_shipped_verdicts_file_is_valid(self):
        from external_sources.verification import load_verdicts

        assert isinstance(load_verdicts(), list)


class TestNumeric:
    def test_api_value_confirmed_by_an_agreeing_document(self, verifier):
        rec = record(funding_amount=4e6, details={"corroboration": {"funding_amount": [
            {"value": 4e6, "doc_id": "d1", "url": FICHE, "quote": "EUR 4 000 000", "page": 5}]}})

        verdict = verifier.verify(rec, docs())["funding_amount"]

        assert verdict["status"] == "confirmed" and verdict["agree_count"] == 2
        assert {"layer": "document", "url": FICHE, "page": 5} in verdict["sources"]
        assert verdict["sources"][0] == {"layer": "api", "url": TOPIC + "CERV-2026-DAPHNE-PROTECTION"}

    def test_value_from_one_layer_only_is_single_source(self, verifier):
        rec = record(funding_amount=4e6)

        assert verifier.verify(rec, {})["funding_amount"]["status"] == "single_source"

    def test_discrepancy_is_conflicting(self, verifier):
        rec = record(funding_amount=4e6, data_gaps=[{"field": "funding_amount", "kind": "discrepancy",
                                                     "existing": 4e6, "document": 5e6, "url": FICHE}])

        verdict = verifier.verify(rec, docs())["funding_amount"]

        assert verdict["status"] == "conflicting"
        assert verdict["layers"] == {"api": 4e6, "document": 5e6}

    def test_pending_replacement_is_conflicting_and_structural_is_not(self, verifier):
        pending = record(funding_amount=5e6, field_sources={"funding_amount": "document:d1"}, details={
            "value_history": [{"field": "funding_amount", "value": 4e6, "source": "api",
                               "reason": "newer_document", "confirmation": "pending"}]})
        structural = record(funding_amount=5e6, field_sources={"funding_amount": "document:d1"}, details={
            "value_history": [{"field": "funding_amount", "value": 2e7, "source": "api",
                               "reason": "api_is_call_total", "confirmation": "structural"}]})

        assert verifier.verify(pending, docs())["funding_amount"]["status"] == "conflicting"
        assert verifier.verify(structural, docs())["funding_amount"]["status"] == "single_source"

    def test_call_budget_confirmed_when_the_api_repeated_it_as_the_topic_budget(self, verifier):
        rec = record(funding_amount=5e6, field_sources={"funding_amount": "document:d1",
                                                        "details.call_budget_eur": "document:d1"},
                     details={"call_budget_eur": 2e7, "value_history": [
                         {"field": "funding_amount", "value": 2e7, "source": "api",
                          "reason": "api_is_call_total", "confirmation": "structural"}]})

        assert verifier.verify(rec, docs())["details.call_budget_eur"]["status"] == "confirmed"

    def test_absent_field(self, verifier):
        assert verifier.verify(record(), {})["details.topic_budget_eur"]["status"] == "absent"


class TestDates:
    def test_same_calendar_day_confirms_and_notes_the_time(self, verifier):
        rec = record(deadline_date="2026-11-05T00:00:00+00:00",
                     details={"document_deadline": "2026-11-05T16:00:00+00:00"})

        verdict = verifier.verify(rec, docs())["deadline_date"]

        assert verdict["status"] == "confirmed"
        assert "17:00 Brussels" in verdict["note"]

    def test_different_day_conflicts(self, verifier):
        rec = record(deadline_date="2026-11-05T00:00:00+00:00",
                     details={"document_deadline": "2026-11-03T16:00:00+00:00"})

        verdict = verifier.verify(rec, docs())["deadline_date"]

        assert verdict["status"] == "conflicting"
        assert verdict["layers"] == {"api": ["2026-11-05"], "document": "2026-11-03"}

    def test_any_cut_off_matches(self, verifier):
        rec = record(deadline_date="2026-03-01T00:00:00+00:00", details={
            "deadline_dates": ["2026-03-01T00:00:00+00:00", "2026-09-15T00:00:00+00:00"],
            "document_deadline": "2026-09-15T15:00:00+00:00"})

        assert verifier.verify(rec, docs())["deadline_date"]["status"] == "confirmed"

    def test_opening_date_uses_the_document_opening(self, verifier):
        rec = record(start_date="2026-06-16T00:00:00+00:00", details={"document_opening_date": "2026-06-16"})

        assert verifier.verify(rec, docs())["start_date"]["status"] == "confirmed"


class TestUpdateNotices:
    def test_official_deadline_notice_resolves_a_conflict_for_the_api(self, verifier):
        rec = record(deadline_date="2026-11-05T00:00:00+00:00", details={
            "document_deadline": "2026-11-03T16:00:00+00:00",
            "evidence": {"details.document_deadline": {"doc_id": "d1", "url": FICHE, "page": 4,
                                                       "published_at": "2026-07-01T00:00:00+00:00"}},
            "latest_updates": [{"date": "2026-08-17T14:23:57+00:00",
                                "text": "Submission deadline update\nThe new deadline for submissions is 05 November 2026, 17:00 CET"}]})

        verdict = verifier.verify(rec, docs())["deadline_date"]

        assert verdict["status"] == "resolved" and verdict["computed_status"] == "conflicting"
        assert verdict["check"]["decision"] == "api" and verdict["check"]["basis"] == "update_notice"
        assert "05 November 2026" in verdict["check"]["sources"][0]["quote"]
        assert verdict["check"]["sources"][0]["url"].startswith(TOPIC)

    def test_notice_without_year_matches_day_and_month(self, verifier):
        rec = record(deadline_date="2026-11-10T00:00:00+00:00", details={
            "document_deadline": "2026-10-13T15:00:00+00:00",
            "latest_updates": [{"date": "2026-09-30T14:33:36+00:00",
                                "text": "The deadline for this call has been extended from 13 October to 10 November"}]})

        assert verifier.verify(rec, docs())["deadline_date"]["status"] == "resolved"

    def test_notice_older_than_the_document_does_not_resolve(self, verifier):
        rec = record(deadline_date="2026-11-05T00:00:00+00:00", details={
            "document_deadline": "2026-11-03T16:00:00+00:00",
            "evidence": {"details.document_deadline": {"url": FICHE, "published_at": "2026-09-01T00:00:00+00:00"}},
            "latest_updates": [{"date": "2026-08-17T00:00:00+00:00", "text": "New deadline: 05 November 2026"}]})

        assert verifier.verify(rec, docs())["deadline_date"]["status"] == "conflicting"

    def test_notice_about_something_else_does_not_resolve(self, verifier):
        rec = record(deadline_date="2026-11-05T00:00:00+00:00", details={
            "document_deadline": "2026-11-03T16:00:00+00:00",
            "latest_updates": [{"date": "2026-08-17T00:00:00+00:00", "text": "Info session on 05 November 2026"}]})

        assert verifier.verify(rec, docs())["deadline_date"]["status"] == "conflicting"


class TestCallTotalOnTopic:
    def test_topic_value_equal_to_the_documented_call_budget_is_labelled_call_level(self, verifier):
        rec = record(funding_amount=21e6, field_sources={"funding_amount": "api",
                                                         "details.call_budget_eur": "document:d1"},
                     details={"call_budget_eur": 21e6,
                              "evidence": {"details.call_budget_eur": {"doc_id": "d1", "url": FICHE, "page": 29}}})

        verdict = verifier.verify(rec, docs())["funding_amount"]

        assert verdict["status"] == "confirmed" and verdict["level"] == "call_total"
        assert {"layer": "document", "url": FICHE, "page": 29} in verdict["sources"]


class TestText:
    def test_title_found_in_the_document_confirms(self, verifier):
        assert verifier.verify(record(), docs())["title"]["status"] == "confirmed"

    def test_title_missing_from_documents_is_single_source(self, verifier):
        assert verifier.verify(record(title="Something else entirely"), docs())["title"]["status"] == "single_source"

    def test_description_overlap_confirms(self, verifier):
        verdict = verifier.verify(record(description=DESCRIPTION), docs())["description"]

        assert verdict["status"] == "confirmed" and verdict["probes_found"] >= 2

    def test_description_from_a_document_is_single_source(self, verifier):
        rec = record(description=DESCRIPTION, field_sources={"description": "document:d1"})

        assert verifier.verify(rec, docs())["description"]["status"] == "single_source"

    def test_eligibility_stated_by_the_call_document_is_confirmed(self, verifier):
        rec = record(details={"conditions": "Eligible countries: EU Member States.",
                              "document_eligibility": "Applicants must be legal entities.",
                              "evidence": {"details.document_eligibility": {"doc_id": "d1", "url": FICHE, "page": 9}}})

        verdict = verifier.verify(rec, docs())["eligibility"]

        assert verdict["status"] == "confirmed" and verdict["basis"] == "official_document"
        assert {"layer": "document", "url": FICHE, "page": 9} in verdict["sources"]

    def test_work_programme_specific_conditions_also_state_eligibility(self, verifier):
        """Horizon topics: the WP's 'Specific conditions' is the official document text"""
        rec = record(details={"conditions": "General conditions...",
                              "document_specific_conditions": "Specific conditions: consortia must include...",
                              "evidence": {"details.document_specific_conditions": {"url": FICHE, "page": 40}}})

        verdict = verifier.verify(rec, docs())["eligibility"]

        assert verdict["status"] == "confirmed"
        assert {"layer": "document", "url": FICHE, "page": 40} in verdict["sources"]

    def test_eligibility_from_the_api_only_is_single_source(self, verifier):
        rec = record(details={"conditions": "Eligible countries: EU Member States."})

        assert verifier.verify(rec, {})["eligibility"]["status"] == "single_source"


class TestProgramme:
    def test_programme_confirmed_by_a_document_of_that_programme(self, verifier):
        assert verifier.verify(record(), docs())["programme"]["status"] == "confirmed"

    def test_programme_single_source_without_documents(self, verifier):
        assert verifier.verify(record(), {})["programme"]["status"] == "single_source"


class TestVerdicts:
    def test_checked_verdict_resolves_a_conflict_and_keeps_both_values(self):
        verdict_entry = {
            "key_pattern": "^CERV-2026-DAPHNE-", "field": "deadline_date", "status": "resolved",
            "decision": "api", "value": "2026-11-05",
            "sources": [{"url": TOPIC + "CERV-2026-DAPHNE-PROTECTION", "quote": "05 November 2026"}],
            "checked_at": "2026-10-14", "note": "Deadline extended; the call document predates it.",
        }
        verifier = Verifier(load_verification_policy(), verdicts=[verdict_entry], now=NOW)
        rec = record(deadline_date="2026-11-05T00:00:00+00:00",
                     details={"document_deadline": "2026-11-03T16:00:00+00:00"})

        verdict = verifier.verify(rec, docs())["deadline_date"]

        assert verdict["status"] == "resolved" and verdict["computed_status"] == "conflicting"
        assert verdict["check"]["decision"] == "api"
        assert verdict["layers"] == {"api": ["2026-11-05"], "document": "2026-11-03"}

    def test_verdict_for_another_record_does_not_apply(self):
        entry = {"key": "OTHER", "field": "deadline_date", "status": "resolved", "decision": "api",
                 "sources": [{"url": "https://x.eu"}], "checked_at": "2026-10-14"}
        verifier = Verifier(load_verification_policy(), verdicts=[entry], now=NOW)
        rec = record(deadline_date="2026-11-05T00:00:00+00:00",
                     details={"document_deadline": "2026-11-03T16:00:00+00:00"})

        assert verifier.verify(rec, docs())["deadline_date"]["status"] == "conflicting"

    def test_every_verdict_carries_checked_at(self, verifier):
        verdicts = verifier.verify(record(funding_amount=1e6), {})

        assert all(v["checked_at"] == NOW for v in verdicts.values())


def test_policy_file_documents_the_verdict_levels():
    text = (Path(__file__).parents[2] / "external_sources" / "config" / "verification_policy.yaml").read_text()
    for word in ("confirmed", "single_source", "conflicting", "resolved", "absent"):
        assert word in text


class TestValueRange:
    """Disagreeing sources are exposed as a range with every value and its source, never one picked value"""

    def test_numeric_discrepancy_gives_a_range_with_every_source(self, verifier):
        rec = record(funding_amount=4e6, data_gaps=[{"field": "funding_amount", "kind": "discrepancy",
                                                     "existing": 4e6, "document": 5e6, "url": FICHE}])

        verdict = verifier.verify(rec, docs())["funding_amount"]

        assert verdict["status"] == "conflicting"
        rng = verdict["range"]
        assert (rng["min"], rng["max"], rng["primary"]) == (4e6, 5e6, 4e6)
        assert {"value": 4e6, "layer": "api", "source": TOPIC + "CERV-2026-DAPHNE-PROTECTION"} in rng["values"]
        assert {"value": 5e6, "layer": "document", "source": FICHE} in rng["values"]
        assert rng["settled"] is False

    def test_pending_replacement_lists_both_values(self, verifier):
        rec = record(funding_amount=5e6, field_sources={"funding_amount": "document:d1"}, details={
            "evidence": {"funding_amount": {"doc_id": "d1", "url": FICHE, "page": 3}},
            "value_history": [{"field": "funding_amount", "value": 4e6, "source": "api",
                               "reason": "newer_document", "confirmation": "pending"}]})

        rng = verifier.verify(rec, docs())["funding_amount"]["range"]

        assert (rng["min"], rng["max"]) == (4e6, 5e6)
        assert {"value": 5e6, "layer": "document", "source": FICHE, "page": 3} in rng["values"]

    def test_agreement_has_no_range(self, verifier):
        rec = record(funding_amount=4e6, details={"corroboration": {"funding_amount": [
            {"value": 4e6, "doc_id": "d1", "url": FICHE, "page": 5}]}})

        assert "range" not in verifier.verify(rec, docs())["funding_amount"]

    def test_date_conflict_gives_a_date_range(self, verifier):
        rec = record(deadline_date="2027-04-15T00:00:00+00:00",
                     details={"document_deadline": "2027-04-13", "evidence": {
                         "details.document_deadline": {"url": FICHE, "page": 21}}})

        verdict = verifier.verify(rec, docs())["deadline_date"]

        assert verdict["status"] == "conflicting"
        assert (verdict["range"]["min"], verdict["range"]["max"]) == ("2027-04-13", "2027-04-15")
        assert {"value": "2027-04-13", "layer": "document", "source": FICHE, "page": 21} in verdict["range"]["values"]

    def test_official_notice_settles_the_range_and_keeps_its_value_primary(self, verifier):
        rec = record(deadline_date="2026-11-05T00:00:00+00:00", details={
            "document_deadline": "2026-11-03T16:00:00+00:00",
            "latest_updates": [{"date": "2026-08-17T00:00:00+00:00",
                                "text": "Submission deadline update: the new deadline is 05 November 2026"}]})

        verdict = verifier.verify(rec, docs())["deadline_date"]

        assert verdict["status"] == "resolved"
        rng = verdict["range"]
        assert rng["settled"] is True and rng["primary"] == "2026-11-05"
        assert (rng["min"], rng["max"]) == ("2026-11-03", "2026-11-05")

    def test_checked_verdict_that_stays_open_keeps_conflicting_and_the_range(self):
        entry = {"key": "CERV-2026-DAPHNE-PROTECTION", "field": "deadline_date", "status": "conflicting",
                 "decision": "open", "sources": [{"url": FICHE}], "checked_at": "2026-10-14"}
        verifier = Verifier(load_verification_policy(), verdicts=[entry], now=NOW)
        rec = record(deadline_date="2027-04-15T00:00:00+00:00", details={"document_deadline": "2027-04-13"})

        verdict = verifier.verify(rec, docs())["deadline_date"]

        assert verdict["status"] == "conflicting" and verdict["range"]["settled"] is False

    def test_checked_verdict_with_a_value_settles_and_sets_primary(self):
        entry = {"key": "CERV-2026-DAPHNE-PROTECTION", "field": "funding_amount", "status": "resolved",
                 "decision": "both", "value": 4e6, "sources": [{"url": FICHE}], "checked_at": "2026-10-14"}
        verifier = Verifier(load_verification_policy(), verdicts=[entry], now=NOW)
        rec = record(funding_amount=4e6, data_gaps=[{"field": "funding_amount", "kind": "discrepancy",
                                                     "existing": 4e6, "document": 4.1e6, "url": FICHE}])

        rng = verifier.verify(rec, docs())["funding_amount"]["range"]

        assert rng["settled"] is True and rng["primary"] == 4e6 and rng["max"] == 4.1e6

    def test_value_ranges_helper_collects_ranges_by_field(self, verifier):
        from external_sources.verification import value_ranges

        rec = record(funding_amount=4e6, data_gaps=[{"field": "funding_amount", "kind": "discrepancy",
                                                     "existing": 4e6, "document": 5e6, "url": FICHE}])

        ranges = value_ranges(verifier.verify(rec, docs()))

        assert list(ranges) == ["funding_amount"] and ranges["funding_amount"]["status"] == "conflicting"
