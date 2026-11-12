"""
Unit tests for external_sources.context.
"""

import pytest

from external_sources.context import (
    DocSection,
    build_card,
    build_context,
    load_context_policy,
    truncate_text,
)
from external_sources.plugins.base import NormalizedRecord

LONG = ("Sentence about the call. " * 30 + "\n\n") * 20  # ~15k chars with paragraph breaks


def call(**kwargs):
    """A realistic call record."""
    base = dict(
        natural_key="HORIZON-CL5-2026-10-D6-06", record_type="call", domain="projects",
        programme="Horizon Europe", country_scope="EU",
        title="Increasing competitiveness and resilience of multimodal freight transport",
        description="Expected Outcome: multimodal freight.\n\nScope: build and demonstrate tools.",
        status="open", funding_amount=20_000_000.0,
        start_date="2026-05-26T00:00:00+00:00", deadline_date="2026-10-08T00:00:00+00:00",
        source_url="https://ec.europa.eu/topic-details/horizon-cl5-2026-10-d6-06",
        raw={},
        details={
            "budget": {"per_year": {"2026": 20_000_000.0}, "expected_grants": 3,
                       "min_contribution": 5_000_000.0, "max_contribution": 7_000_000.0,
                       "planned_opening_date": "2026-05-26T00:00:00+00:00"},
            "deadline_dates": ["2026-10-08T00:00:00+00:00"],
            "conditions": "General conditions. Eligible countries are described in Annex B.",
            "support": "Online Manual is your guide.",
            "submission_notice": "The submission system opens on the date on the topic header.",
            "destination_description": "Multimodal transport", "destination_details": "Theme text.",
            "latest_updates": [{"date": "2026-06-04T00:01:03+00:00", "text": "Session now open."}],
            "description_links": [{"text": "Work Programme", "url": "https://x.eu/wp.pdf"}],
        },
    )
    base.update(kwargs)
    return NormalizedRecord(**base)


class TestTruncateText:
    """Test truncate_text()"""

    def test_short_text_is_unchanged(self):
        """Text within the limit is returned as-is"""
        assert truncate_text("abc", 10, "…") == "abc"

    def test_result_never_exceeds_limit(self):
        """Truncated text including the marker fits the limit"""
        assert len(truncate_text(LONG, 500, " [cut]")) <= 500

    def test_prefers_paragraph_boundary_and_adds_marker(self):
        """The cut lands on a paragraph boundary when one is close enough"""
        out = truncate_text(LONG, 800, " [cut]")

        assert out.endswith(" [cut]")
        assert out[: -len(" [cut]")].endswith("call.")


class TestBuildCard:
    """Test build_card()"""

    def test_card_is_short_and_has_the_essentials(self):
        """A card carries title, id, programme, status, deadline, budget and URL in < 500 chars"""
        card = build_card(call())

        assert len(card) < 500
        for expected in ("HORIZON-CL5-2026-10-D6-06", "Horizon Europe", "open", "2026-10-08",
                         "EUR 20,000,000", "https://ec.europa.eu/topic-details"):
            assert expected in card

    def test_card_labels_a_verified_call_total_on_a_topic_as_call_level(self):
        """A topic value verified as the call total is not presented as the topic's own budget"""
        record = call(funding_amount=21_000_000, details={
            **call().details, "verification": {"funding_amount": {"status": "confirmed", "level": "call_total"}}})

        card = build_card(record)

        assert "Call budget (shared by all topics of the call): EUR 21,000,000" in card
        assert "\nBudget: " not in card
        assert "Topic budget" not in build_context(record, "funding").text

    def test_card_omits_missing_budget(self):
        """No amount -> no Budget line"""
        assert "Budget" not in build_card(call(funding_amount=None))

    def test_card_labels_a_call_level_budget_as_such(self):
        """With only a call total, the card says so instead of calling it the topic budget"""
        record = call(funding_amount=None, details={"call_budget_eur": 4_000_000.0})

        card = build_card(record)

        assert "Call budget (covers all topics of the call): EUR 4,000,000" in card
        assert "\nBudget:" not in card


class TestBuildContext:
    """Test build_context()"""

    def test_find_calls_is_just_the_card(self):
        """The discovery intent returns only the card"""
        pack = build_context(call(), "find_calls")

        assert pack.blocks_used == ["card"]
        assert pack.text == build_card(call())

    def test_explain_call_includes_description_budget_deadlines(self):
        """The explain intent adds description, budget and deadline blocks"""
        pack = build_context(call(), "explain_call")

        assert {"card", "description", "budget", "deadlines"} <= set(pack.blocks_used)
        assert "multimodal freight" in pack.text
        assert "Expected grants: 3" in pack.text
        assert "All cut-off dates" not in pack.text  # a single deadline is not repeated

    def test_budget_block_keeps_topic_and_call_levels_apart(self):
        """The budget block reports the topic budget, the document's topic row and the call total separately"""
        details = {**call().details, "topic_budget_eur": 6_500_000.0, "call_budget_eur": 150_000_000.0}

        pack = build_context(call(details=details), "funding")

        assert "Topic budget: EUR 20,000,000" in pack.text
        assert "Topic budget per call document: EUR 6,500,000" in pack.text
        assert "Call budget (all topics of the call): EUR 150,000,000" in pack.text

    def test_replaced_funding_amount_hides_the_api_call_total_per_year(self):
        """After a replacement the API per-year call total is not printed beside the topic budget"""
        details = {
            **call().details, "topic_budget_eur": 6_500_000.0,
            "value_history": [{"field": "funding_amount", "value": 20_000_000.0,
                               "reason": "api_is_call_total"}],
        }

        pack = build_context(call(funding_amount=6_500_000.0, details=details), "funding")

        assert "Per year" not in pack.text
        assert "API budget table lists the call total; topic budget taken from the call document" in pack.text
        assert "Topic budget: EUR 6,500,000" in pack.text

    def test_per_year_is_kept_when_nothing_was_replaced(self):
        pack = build_context(call(), "funding")

        assert "Per year: 2026: EUR 20,000,000" in pack.text
        assert "API budget table lists the call total" not in pack.text

    def test_budget_is_respected_and_truncation_is_marked(self):
        """A huge description is cut to fit, flagged, and points to the source URL"""
        record = call(description=LONG)

        pack = build_context(record, "explain_call", max_chars=2000)

        assert pack.chars <= 2000
        assert "description" in pack.truncated
        assert "https://ec.europa.eu/topic-details" in pack.text.split("truncated")[-1]

    def test_empty_blocks_are_omitted_with_reason(self):
        """A record without conditions omits that block instead of printing an empty header"""
        record = call(details={})

        pack = build_context(record, "eligibility")

        assert {"block": "conditions", "reason": "empty"} in pack.omitted
        assert "conditions" not in pack.blocks_used

    def test_unknown_intent_raises_with_valid_choices(self):
        """Unknown intents fail loudly"""
        with pytest.raises(ValueError, match="explain_call"):
            build_context(call(), "chit_chat")

    def test_document_section_block_is_used_and_cited(self):
        """A supplied doc:<name> section is included and its URL cited"""
        section = DocSection("eligibility", "Only entities in eligible countries may apply.", "https://x.eu/wp.pdf")

        pack = build_context(call(), "eligibility", doc_sections=[section])

        assert "doc:eligibility" in pack.blocks_used
        assert "Only entities in eligible countries" in pack.text
        assert "https://x.eu/wp.pdf" in pack.citations

    def test_boilerplate_documents_are_excluded_by_default(self):
        """Boilerplate sections never enter a pack unless the intent opts in"""
        legal = DocSection("eligibility", "Terms and conditions text.", "https://x.eu/tc.pdf", role="boilerplate")

        pack = build_context(call(), "eligibility", doc_sections=[legal])

        assert "Terms and conditions text." not in pack.text
        assert "https://x.eu/tc.pdf" not in pack.citations

    def test_boilerplate_included_when_intent_opts_in(self):
        """An intent with include_boilerplate may use boilerplate sections"""
        policy = {
            "default_max_chars": 3000, "truncation_marker": " [cut]",
            "blocks": {"doc": {"max_chars": 1000}},
            "intents": {"legal": {"max_chars": 3000, "include_boilerplate": True,
                                  "blocks": ["card", "doc:terms"]}},
        }
        legal = DocSection("terms", "Terms and conditions text.", "https://x.eu/tc.pdf", role="boilerplate")

        pack = build_context(call(), "legal", policy=policy, doc_sections=[legal])

        assert "Terms and conditions text." in pack.text

    def test_citations_start_with_the_call_url(self):
        """The call's own URL is always the first citation"""
        assert build_context(call(), "explain_call").citations[0] == call().source_url

    def test_every_answering_intent_has_a_sources_section(self):
        """Packs that answer about a call end with numbered, citable sources (document pages included)"""
        fiche = "https://ec.europa.eu/x/call-fiche.pdf"
        record = call(details={**call().details, "sources": [
            {"kind": "topic_page", "title": "EU Funding & Tenders Portal topic page", "url": call().source_url},
            {"kind": "call_document", "title": "Call document", "url": fiche, "pages": [8]}]})
        policy = load_context_policy()

        for intent in policy["intents"]:
            if intent in ("find_calls", "list_calls"):   # list items: the card's URL is the citation
                continue
            pack = build_context(record, intent, policy=policy)
            assert "[Sources]\n[1]" in pack.text, intent
            assert fiche in pack.citations, intent
        assert "[2] Call document — " + fiche + " (p. 8)" in build_context(record, "funding").text

    def test_list_calls_item_is_bounded_and_cites_the_call_page(self):
        """One call of a chatbot list: card + deadlines + budget (+ summary if room), within its cap"""
        pack = build_context(call(), "list_calls")
        assert pack.blocks_used[0] == "card" and call().source_url in pack.text
        assert pack.chars <= load_context_policy()["intents"]["list_calls"]["max_chars"]

    def test_conflicting_deadline_shows_both_dates(self):
        """An unresolved API-vs-document deadline conflict is shown, never hidden"""
        record = call(details={**call().details, "verification": {"deadline_date": {
            "status": "conflicting", "layers": {"api": ["2027-04-15"], "document": "2027-04-13"}}}})

        text = build_context(record, "deadlines").text

        assert "sources disagree" in text and "2027-04-13" in text

    def test_value_ranges_reach_the_model_with_their_sources(self):
        """A budget or date range (sources disagree) is shown with min..max and each cited value"""
        fiche = "https://ec.europa.eu/x/call-fiche.pdf"
        record = call(details={**call().details, "value_range": {
            "funding_amount": {"min": 180_000_000, "max": 184_500_000, "primary": 180_000_000, "settled": True,
                               "status": "resolved", "values": [
                                   {"value": 180_000_000, "layer": "api", "source": call().source_url},
                                   {"value": 184_500_000, "layer": "document", "source": fiche, "page": 10}]},
            "deadline_date": {"min": "2027-04-13", "max": "2027-04-15", "primary": "2027-04-15", "settled": False,
                              "status": "conflicting", "values": [
                                  {"value": "2027-04-15", "layer": "api", "source": call().source_url},
                                  {"value": "2027-04-13", "layer": "document", "source": fiche}]}}})

        funding = build_context(record, "funding")
        deadlines = build_context(record, "deadlines")

        assert "Budget range across sources: EUR 180,000,000 – EUR 184,500,000" in funding.text
        assert "EUR 184,500,000 (document, " + fiche + ", p. 10)" in funding.text
        assert "settled by the official source" in funding.text
        assert "Deadline range across sources: 2027-04-13 – 2027-04-15 (sources disagree" in deadlines.text
        assert fiche in deadlines.citations

    def test_latest_updates_are_included_for_deadlines(self):
        """The deadlines intent surfaces recent call updates"""
        pack = build_context(call(), "deadlines")

        assert "Session now open." in pack.text

    def test_all_shipped_intents_fit_their_budget_for_a_large_record(self):
        """Every shipped intent stays within its own max_chars for a very large record"""
        policy = load_context_policy()
        big = call(description=LONG, details={**call().details, "conditions": LONG, "support": LONG})

        for intent, spec in policy["intents"].items():
            pack = build_context(big, intent, policy=policy)

            assert pack.chars <= spec["max_chars"], intent
