"""
Unit tests for external_sources.extraction — amount parsing, config-driven field
extraction from documents, and provenance-preserving merge.
"""

import pytest

from external_sources.extraction.amounts import EUR_AMOUNT, parse_eur
from external_sources.extraction.fields import (
    Extraction,
    extract_fields,
    load_families,
    match_topic_rows,
    merge_extractions,
    table_rows,
)
from external_sources.plugins.base import NormalizedRecord

FAMILIES = load_families()


class TestParseEur:
    """Test parse_eur() across the number formats seen in EU documents"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("The estimated available call budget is EUR 4 000 000.", 4_000_000.0),
            ("EUR 20.00 million", 20_000_000.0),
            ("is EUR 634 million.", 634_000_000.0),
            ("EUR 1,5 million", 1_500_000.0),
            ("EUR 1.000.000", 1_000_000.0),
            ("€ 2,500,000", 2_500_000.0),
            ("EUR 10 000", 10_000.0),
            ("EUR 6.50\nmillion would", 6_500_000.0),
            ("EUR 100M", 100_000_000.0),
            ("EUR 100 000 000", 100_000_000.0),
            ("The estimated available call budget is EUR 1. 000. 000.", 1_000_000.0),
            ("EUR 500,000.00", 500_000.0),
            ("EUR 1.500.000,50", 1_500_000.5),
            ("EUR 70,000", 70_000.0),
        ],
    )
    def test_formats(self, text, expected):
        """Each written form parses to the right number of euros"""
        assert parse_eur(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("EUR 1.5m", 1_500_000.0),
            ("EUR 2,5 Mio", 2_500_000.0),
            ("EUR 3 mln", 3_000_000.0),
            ("EUR 2 Mld", 2e9),
            ("EUR 1.5 BN", 1.5e9),
            ("EUR 5", None),
            ("EUR 999", None),
            ("EUR 1 000", 1000.0),
            ("EUR 200 billion", None),
            ("€ 3 000 000 100", 3_000_000_100.0),  # glue is handled by the row/call-budget filter
            ("EUR 5 months", None),
        ],
    )
    def test_multipliers_and_sanity_bounds(self, text, expected):
        """One multiplier vocabulary; amounts under EUR 1,000 or over EUR 100 billion are rejected"""
        assert parse_eur(text) == expected

    def test_number_followed_by_percent_is_not_absorbed(self):
        """A funding-rate column ('100 %') glued after the amount is not part of it"""
        assert parse_eur("EUR 2 000 000 100 % 24 months") == 2_000_000.0

    def test_no_amount_is_none(self):
        """Text without an amount yields None"""
        assert parse_eur("no amount here") is None

    def test_page_number_on_next_line_is_not_merged(self):
        """A following page number must not become part of the amount"""
        assert parse_eur("budget is EUR 4 000 000.\n13\nFurther information") == 4_000_000.0

    def test_year_after_amount_is_not_merged(self):
        """A trailing year is not swallowed into the amount"""
        assert parse_eur("EUR 4 000 000 2026") == 4_000_000.0

    def test_eur_amount_pattern_captures_currency_and_number(self):
        """EUR_AMOUNT exposes one capture group holding 'EUR <amount>'"""
        import re

        match = re.search(r"budget is\s*" + EUR_AMOUNT, "budget is EUR 4 000 000.\n13", re.IGNORECASE)

        assert match.group(1) == "EUR 4 000 000"


FICHE = """<!-- page 1 -->
## 0. Introduction
Intro.
## 1. Background
Food waste is a large problem in the Union and this call addresses it with concrete support
for stakeholders across the food supply chain, including measurement and prevention actions.
## 2. Objectives — Themes and priorities
The objective of the action is to support the implementation of effective food waste prevention
initiatives within organisations and throughout the food supply chain, including consumers.
## 3. Available budget
The estimated available call budget is EUR 4 000 000.
We reserve the right not to award all available funds.
## 4. Timetable and deadlines
Deadline: 15 October 2026.
"""

WORK_PROGRAMME = """<!-- page 9 -->
### HORIZON-X-2026-01-A-01: Some long topic title that wraps
Call: X CALL
Indicative budget The total indicative budget for the topic is EUR 20.00 million.
Type of Action Innovation Actions
### HORIZON-X-2026-01-A-02: Another topic
Indicative budget The total indicative budget for the topic is EUR 5.00 million.
"""


class TestLoadFamilies:
    """Test load_families() validation"""

    def test_unknown_until_section_is_rejected(self, tmp_path):
        """A topic_table whose `until` names no section fails at load time"""
        path = tmp_path / "f.yaml"
        path.write_text(
            "families:\n  f:\n    roles: [call_specific]\n    sections: {budget: 'Budget'}\n"
            "    fields:\n      t: {section: budget, until: nope, parse: topic_table}\n"
        )

        with pytest.raises(ValueError, match="until"):
            load_families(path)

    def test_topic_table_needs_a_section(self, tmp_path):
        """topic_table without a section has nothing to read"""
        path = tmp_path / "f.yaml"
        path.write_text(
            "families:\n  f:\n    roles: [call_specific]\n    sections: {}\n"
            "    fields:\n      t: {parse: topic_table}\n"
        )

        with pytest.raises(ValueError, match="section"):
            load_families(path)


class TestExtractFields:
    """Test extract_fields() with the shipped families"""

    def test_call_fiche_budget_and_description(self):
        """A call fiche yields the CALL budget (not a topic budget) and a description"""
        found, notes = extract_fields(
            FAMILIES["call_fiche"], FICHE, "SMP-FOOD-X", "d1", "https://x.eu/f.pdf",
            published_at="2026-09-29T07:39:00+00:00",
        )

        by_field = {e.field: e for e in found}
        assert by_field["details.call_budget_eur"].value == 4_000_000.0
        assert "EUR 4 000 000" in by_field["details.call_budget_eur"].evidence
        assert by_field["details.call_budget_eur"].section == "3. Available budget"
        assert by_field["details.call_budget_eur"].published_at == "2026-09-29T07:39:00+00:00"
        assert "funding_amount" not in by_field  # a call total is NOT a topic budget
        assert "details.topic_budget_table" not in by_field  # this fiche has no table
        assert "Food waste is a large problem" in by_field["description"].value
        assert "effective food waste prevention" in by_field["description"].value
        assert "<!--" not in by_field["description"].value
        assert notes == []

    def test_work_programme_uses_only_the_calls_own_topic(self):
        """The anchored family reads the topic's own budget, not its sibling's"""
        found, notes = extract_fields(
            FAMILIES["work_programme_topic"], WORK_PROGRAMME, "HORIZON-X-2026-01-A-01",
            "d2", "https://x.eu/wp.pdf",
        )

        assert [(e.field, e.value) for e in found if e.field == "funding_amount"] == [("funding_amount", 20_000_000.0)]
        assert "20.00 million" in {e.field: e.value for e in found}["details.budget_statement"]
        assert notes == []

    def test_missing_anchor_is_reported_not_guessed(self):
        """When the topic heading is absent nothing is extracted and a note says why"""
        found, notes = extract_fields(
            FAMILIES["work_programme_topic"], WORK_PROGRAMME, "HORIZON-X-2026-01-Z-99",
            "d2", "https://x.eu/wp.pdf",
        )

        assert found == [] and notes == ["anchor_not_found"]

    def test_short_description_is_rejected(self):
        """A description shorter than min_chars is not accepted"""
        tiny = "## 1. Background\nToo short.\n## 2. Objectives\nAlso short.\n"

        found, _ = extract_fields(FAMILIES["call_fiche"], tiny, "X", "d", "u")

        assert "description" not in {e.field for e in found}


LIFE_FICHE = """## 3. Available budget
The estimated available call budget is EUR 1. 000. 000.

Specific budget information per topic can be found in the table below:

Topic Topic budget Maximum EU Expected project
contribution duration

1 — Technical Assistance for EUR 500,000.00 EUR 70,000 24 months
the preparation of SNAPs

2 — Technical Assistance for EUR 300,000.00 EUR 70,000 24 months
the preparation of ENV – SIPs

3 — Technical Assistance for EUR 200,000.00 EUR 70,000 24 months
the preparation of CLIMA – SIPs

We reserve the right not to award all available funds.

## 4. Timetable and deadlines
Call opening: 21 April 2026
"""

ISF_FICHE = """## 3. Available budget
The available call budget is EUR 6 500 000. Specific budget information per topic can be found in the table below.
Topic Topic budget
## 1. ISF-2026-TF2-AG-DIGITAL EUR 5 000 000
## 2. ISF-2026-TF2-AG-STANDARD EUR 1 500 000
We expect to fund four projects under topic 1 and one project under topic 2.
## 4. Timetable and deadlines
Call opening: 15 September 2026
"""


class TestTopicTable:
    """Test the per-topic budget table (call total and topic rows are separate levels)"""

    def test_rows_with_wrapped_labels_and_extra_columns(self):
        """Labels that wrap onto the next line join up; only the first amount is the topic budget"""
        found, _ = extract_fields(FAMILIES["call_fiche"], LIFE_FICHE, "X", "d", "u")

        by_field = {e.field: e.value for e in found}

        assert by_field["details.call_budget_eur"] == 1_000_000.0
        assert by_field["details.topic_budget_table"] == [
            {"topic_no": 1, "label": "Technical Assistance for the preparation of SNAPs", "budget_eur": 500_000.0},
            {"topic_no": 2, "label": "Technical Assistance for the preparation of ENV – SIPs", "budget_eur": 300_000.0},
            {"topic_no": 3, "label": "Technical Assistance for the preparation of CLIMA – SIPs", "budget_eur": 200_000.0},
        ]

    def test_rows_rendered_as_markdown_headings(self):
        """Rows that the converter turned into '## 1. …' headings are still read"""
        found, _ = extract_fields(FAMILIES["call_fiche"], ISF_FICHE, "X", "d", "u")

        by_field = {e.field: e.value for e in found}

        assert by_field["details.call_budget_eur"] == 6_500_000.0
        assert [(r["label"], r["budget_eur"]) for r in by_field["details.topic_budget_table"]] == [
            ("ISF-2026-TF2-AG-DIGITAL", 5_000_000.0),
            ("ISF-2026-TF2-AG-STANDARD", 1_500_000.0),
        ]

    def test_intro_sentences_are_not_rows(self):
        """Numbered prose without an amount never becomes a row"""
        assert table_rows("1. The call opens in June.\n2. Applicants apply online.\n") == []

    def test_no_table_means_no_extraction(self):
        """A budget section without a table yields no topic_budget_table"""
        found, _ = extract_fields(FAMILIES["call_fiche"], FICHE, "X", "d", "u")

        assert "details.topic_budget_table" not in {e.field for e in found}


def topic(key, title="T", call_id="", **kwargs):
    """A topic record of a call, for matching tests."""
    return NormalizedRecord(
        natural_key=key, record_type="call", domain="projects", title=title,
        source_url="https://x.eu/" + key, raw={}, details={"call_identifier": call_id}, **kwargs,
    )


def row(no, label, eur):
    """A table row."""
    return {"topic_no": no, "label": label, "budget_eur": eur}


class TestMatchTopicRows:
    """Test match_topic_rows(): topics of one call -> rows of its budget table"""

    def test_matches_by_distinctive_identifier_tokens(self):
        """Wrapped identifiers like 'EDF-2026-RA- MCBRN-DST' match the topic's own id"""
        rows = [row(1, "EDF-2026-RA- MCBRN-DST", 15e6), row(2, "EDF-2026-RA-SENS- MSDT", 20e6)]
        records = [topic("EDF-2026-RA-MCBRN-DST", call_id="EDF-2026-RA"),
                   topic("EDF-2026-RA-SENS-MSDT", call_id="EDF-2026-RA")]

        matches = match_topic_rows(records, rows)

        assert matches["EDF-2026-RA-MCBRN-DST"]["row"]["budget_eur"] == 15e6
        assert matches["EDF-2026-RA-SENS-MSDT"]["row"]["budget_eur"] == 20e6
        assert matches["EDF-2026-RA-MCBRN-DST"]["how"] == "identifier"

    def test_head_of_the_label_wins_over_words_in_the_description(self):
        """'NATURE' matches the row whose identifier part says NATURE, not a row that merely mentions nature"""
        rows = [row(1, "LIFE-2026-SAP-NAT- NATURE - Nature and Biodiversity", 166e6),
                row(2, "LIFE-2026-SAP-NAT- GOV - Nature Governance and Information", 7.5e6)]
        records = [topic("LIFE-2026-SAP-NAT-NATURE", call_id="LIFE-2026-SAP-NAT"),
                   topic("LIFE-2026-SAP-NAT-GOV", call_id="LIFE-2026-SAP-NAT")]

        matches = match_topic_rows(records, rows)

        assert matches["LIFE-2026-SAP-NAT-NATURE"]["row"]["budget_eur"] == 166e6
        assert matches["LIFE-2026-SAP-NAT-GOV"]["row"]["budget_eur"] == 7.5e6

    def test_falls_back_to_title_similarity(self):
        """Plain-title rows match by clear title similarity"""
        rows = [row(1, "Key electronic components", 122.25e6), row(2, "Platforms and end-products", 152.75e6)]
        records = [topic("EDF-P-KEC", title="Key electronic components", call_id="EDF-P"),
                   topic("EDF-P-PE", title="Platforms and end-products", call_id="EDF-P")]

        matches = match_topic_rows(records, rows)

        assert matches["EDF-P-KEC"]["row"]["budget_eur"] == 122.25e6
        assert matches["EDF-P-KEC"]["how"] == "title_similarity"

    def test_lone_leftover_topic_takes_the_lone_leftover_row(self):
        """After the others are matched, one topic and one row remaining are paired"""
        rows = [row(1, "Technical Assistance for the preparation of SNAPs", 500_000.0),
                row(2, "Technical Assistance for the preparation of ENV – SIPs", 300_000.0),
                row(3, "Technical Assistance for the preparation of CLIMA – SIPs", 200_000.0)]
        records = [topic("LIFE-2026-TA-PP-ENV-SIP", title="Technical Assistance", call_id="LIFE-2026-TA-PP"),
                   topic("LIFE-2026-TA-PP-CLIMA-SIP", title="Technical Assistance", call_id="LIFE-2026-TA-PP"),
                   topic("LIFE-2026-TA-PP-NAT-SNAP", title="Technical Assistance", call_id="LIFE-2026-TA-PP")]

        matches = match_topic_rows(records, rows)

        assert matches["LIFE-2026-TA-PP-NAT-SNAP"]["row"]["budget_eur"] == 500_000.0
        assert matches["LIFE-2026-TA-PP-NAT-SNAP"]["how"] == "elimination"
        assert matches["LIFE-2026-TA-PP-ENV-SIP"]["row"]["budget_eur"] == 300_000.0

    def test_single_topic_single_row(self):
        """One topic and one row are paired directly"""
        matches = match_topic_rows([topic("EDF-USI-CUXS", call_id="EDF-USI")],
                                   [row(1, "Unmanned systems", 82e6)])

        assert matches["EDF-USI-CUXS"]["how"] == "single_row"

    def test_no_confident_match_returns_nothing(self):
        """Two unrelated topics and two unrelated rows are not guessed"""
        rows = [row(1, "Alpha programme", 1.0), row(2, "Beta programme", 2.0)]
        records = [topic("X-1-ZZ", title="Gamma", call_id="X-1"), topic("X-1-YY", title="Delta", call_id="X-1")]

        assert match_topic_rows(records, rows) == {}

    def test_a_row_claimed_by_two_topics_is_dropped(self):
        """Ambiguity is not resolved by guessing: both claimants are left unmatched"""
        rows = [row(1, "Shared topic name", 1.0), row(2, "Other", 2.0)]
        records = [topic("C-1-A", title="Shared topic name", call_id="C-1"),
                   topic("C-1-B", title="Shared topic name", call_id="C-1")]

        assert match_topic_rows(records, rows) == {}


def call(**kwargs):
    """A call record for merge tests."""
    base = dict(
        natural_key="K1", record_type="call", domain="projects", title="T",
        source_url="https://x.eu/K1", raw={},
    )
    base.update(kwargs)
    return NormalizedRecord(**base)


def ex(field="funding_amount", value=4_000_000.0, doc_id="d1", **kwargs):
    """An Extraction fixture."""
    return Extraction(
        field, value, "…EUR 4 000 000…", doc_id, "https://x.eu/f.pdf", "3. Available budget", **kwargs
    )


class TestMergeExtractions:
    """Test merge_extractions()"""

    def test_fills_empty_field_with_provenance_and_evidence(self):
        """An empty field is filled; source and evidence are recorded"""
        merged = merge_extractions(call(), [ex()])

        assert merged.funding_amount == 4_000_000.0
        assert merged.field_sources["funding_amount"] == "document:d1"
        evidence = merged.details["evidence"]["funding_amount"]
        assert evidence["quote"] == "…EUR 4 000 000…"
        assert evidence["url"] == "https://x.eu/f.pdf"

    def test_never_overwrites_an_api_value(self):
        """An existing API value is kept, and an agreeing document adds no gap"""
        record = call(funding_amount=4_000_000.0, field_sources={"funding_amount": "api"})

        merged = merge_extractions(record, [ex()])

        assert merged.funding_amount == 4_000_000.0
        assert merged.field_sources["funding_amount"] == "api"
        assert merged.data_gaps == []

    def test_agreeing_document_is_recorded_as_corroboration(self):
        """A document value equal to the existing one is kept as corroboration (with page and quote)"""
        record = call(funding_amount=4_000_000.0, field_sources={"funding_amount": "api"})

        merged = merge_extractions(record, [ex(page=12)])

        entry = merged.details["corroboration"]["funding_amount"]
        assert entry == [{"value": 4_000_000.0, "doc_id": "d1", "url": "https://x.eu/f.pdf",
                          "quote": "…EUR 4 000 000…", "page": 12, "section": "3. Available budget"}]

    def test_evidence_carries_the_page(self):
        """The page of the value (converted PDF) is part of the evidence"""
        merged = merge_extractions(call(), [ex(page=7)])

        assert merged.details["evidence"]["funding_amount"]["page"] == 7

    def test_disagreement_is_recorded_as_discrepancy(self):
        """A document value that differs beyond tolerance becomes a data_gaps entry"""
        record = call(funding_amount=1_000_000.0, field_sources={"funding_amount": "api"})

        merged = merge_extractions(record, [ex(value=4_000_000.0)])

        assert merged.funding_amount == 1_000_000.0
        gap = merged.data_gaps[0]
        assert gap["kind"] == "discrepancy"
        assert gap["field"] == "funding_amount"
        assert gap["api"] == 1_000_000.0 and gap["document"] == 4_000_000.0

    def test_within_tolerance_is_not_a_discrepancy(self):
        """Values within 1% are treated as agreeing"""
        record = call(funding_amount=4_000_000.0)

        merged = merge_extractions(record, [ex(value=4_020_000.0)])

        assert merged.data_gaps == []

    def test_details_target_is_written_into_details(self):
        """A `details.` target lands in record.details"""
        merged = merge_extractions(call(), [ex(field="details.expected_grants", value=7.0)])

        assert merged.details["expected_grants"] == 7.0
        assert merged.field_sources["details.expected_grants"] == "document:d1"

    def test_input_record_is_not_mutated(self):
        """merge returns a new record"""
        record = call()

        merge_extractions(record, [ex()])

        assert record.funding_amount is None and record.field_sources == {}

    def test_newer_document_replaces_the_api_value_and_keeps_the_old_one(self):
        """A document published after the API indexed the record wins; the old value is kept"""
        record = call(funding_amount=980_000.0, source_updated_at="2026-03-02T09:54:54+00:00",
                      field_sources={"funding_amount": "api"})

        merged = merge_extractions(
            record, [ex(value=3_920_000.0, published_at="2026-09-29T13:02:31+00:00")]
        )

        assert merged.funding_amount == 3_920_000.0
        assert merged.field_sources["funding_amount"] == "document:d1"
        history = merged.details["value_history"][0]
        assert history["value"] == 980_000.0 and history["reason"] == "newer_document"
        assert history["source"] == "api"
        gap = merged.data_gaps[0]
        assert gap["kind"] == "superseded" and gap["previous"] == 980_000.0 and gap["new"] == 3_920_000.0

    def test_older_document_keeps_the_api_value_and_records_both_dates(self):
        """A document older than the API index does not win; a human gets both dates"""
        record = call(funding_amount=80_000_000.0, source_updated_at="2026-06-17T11:49:06+00:00")

        merged = merge_extractions(
            record, [ex(value=82_000_000.0, published_at="2026-05-21T09:01:00+00:00")]
        )

        assert merged.funding_amount == 80_000_000.0
        gap = merged.data_gaps[0]
        assert gap["kind"] == "discrepancy"
        assert gap["api_updated_at"] == "2026-06-17T11:49:06+00:00"
        assert gap["document_published_at"] == "2026-05-21T09:01:00+00:00"
        assert "value_history" not in merged.details

    def test_api_value_equal_to_the_call_total_is_replaced_by_the_topic_row(self):
        """An API topic budget that is really the call total repeated is replaced, whatever the dates"""
        record = call(funding_amount=150_000_000.0, source_updated_at="2026-06-17T00:00:00+00:00")

        merged = merge_extractions(
            record,
            [ex(value=60_000_000.0, published_at="2026-01-01T00:00:00+00:00", call_totals=(150_000_000.0,))],
        )

        assert merged.funding_amount == 60_000_000.0
        assert merged.details["value_history"][0]["reason"] == "api_is_call_total"

    def test_extraction_equal_to_the_call_total_does_not_replace_anything(self):
        """A document value that IS the call total cannot displace an API value by the call-total rule"""
        record = call(funding_amount=150_000_000.0)

        merged = merge_extractions(
            record, [ex(value=90_000_000.0, call_totals=(90_000_000.0,))]
        )

        assert merged.funding_amount == 150_000_000.0
        assert merged.data_gaps[0]["kind"] == "discrepancy"

    def test_table_and_call_budget_go_to_details(self):
        """Call-level and table values are stored under details, separate from funding_amount"""
        rows = [{"topic_no": 1, "label": "A", "budget_eur": 1.0}]

        merged = merge_extractions(
            call(),
            [ex(field="details.call_budget_eur", value=6_500_000.0),
             ex(field="details.topic_budget_table", value=rows)],
        )

        assert merged.funding_amount is None
        assert merged.details["call_budget_eur"] == 6_500_000.0
        assert merged.details["topic_budget_table"] == rows

    def test_first_extraction_wins_and_second_is_compared(self):
        """Two documents for one field: the first fills, the second is checked against it"""
        merged = merge_extractions(call(), [ex(doc_id="d1"), ex(value=9_000_000.0, doc_id="d2")])

        assert merged.funding_amount == 4_000_000.0
        assert merged.data_gaps[0]["kind"] == "discrepancy"


AMIF_FICHE = """## 1. Background
The Asylum, Migration and Integration Fund supports the effective management of migration flows
and this call funds integration measures for third-country nationals across the Member States.
## 2. Topics under this call for proposals
Applicants must address one of the following topics and show a clear European added value.1
12 See the Union legislation referred to above.
13 Further details are in the call document and in the guide for applicants.
## 3. Available budget
The estimated available call budget is EUR 3 000 000.
"""

LIFE_OG = """## 3. Available budget
The total indicative budget earmarked for operating grants under this call for
framework partnership agreement is EUR 14,500,000 per year.
## 4. Timetable and deadlines
Opening: 1 June 2026
"""

PRIZE = """## 3. Available prize budget
Each prize winner will receive EUR 0.1 million from a total prize budget of EUR 0.4 million.
## 4. Timetable and deadlines
Opening: 1 June 2026
"""

ERASMUS_NO_BUDGET = """## 3. Available budget
The budget will be allocated via specific calls for proposals under the framework agreement.
## 4. Timetable and deadlines
Opening: 1 June 2026
"""


class TestAdaptableFiche:
    """Test the call_fiche family against fiche variants seen in real F&T calls"""

    def test_amif_topics_section_feeds_the_description(self):
        """AMIF has no Objectives section; 'Topics under this call' stands in despite footnote lines"""
        found, _ = extract_fields(FAMILIES["call_fiche"], AMIF_FICHE, "X", "d", "u")

        by_field = {e.field: e for e in found}

        assert "integration measures" in by_field["description"].value
        assert "European added value" in by_field["description"].value

    def test_operating_grant_budget_sentence_spans_a_newline(self):
        """The LIFE-OG amount is read across the line break and the statement is kept"""
        found, _ = extract_fields(FAMILIES["call_fiche"], LIFE_OG, "X", "d", "u")

        by_field = {e.field: e.value for e in found}

        assert by_field["details.call_budget_eur"] == 14_500_000.0
        assert "framework partnership agreement is EUR 14,500,000" in by_field["details.budget_statement"]

    def test_prize_budget_is_the_total_not_the_first_amount(self):
        """The amount after 'budget of' wins over the per-winner amount"""
        found, _ = extract_fields(FAMILIES["call_fiche"], PRIZE, "X", "d", "u")

        assert {e.field: e.value for e in found}["details.call_budget_eur"] == 400_000.0

    def test_no_budget_keeps_the_statement_without_an_amount(self):
        """A section that states no amount still yields the document's own wording"""
        found, _ = extract_fields(FAMILIES["call_fiche"], ERASMUS_NO_BUDGET, "X", "d", "u")

        by_field = {e.field: e.value for e in found}

        assert "details.call_budget_eur" not in by_field
        assert by_field["details.budget_statement"].startswith("The budget will be allocated")


def write_families(tmp_path, body):
    path = tmp_path / "f.yaml"
    path.write_text("families:\n" + body)
    return load_families(path)


class TestSectionLists:
    """Test list-valued section patterns, joined text sections and max_chars"""

    def test_invalid_regex_is_rejected_at_load(self, tmp_path):
        """A bad section regex fails at load time, also inside a list"""
        with pytest.raises(ValueError, match="regex"):
            write_families(tmp_path, "  f:\n    roles: [x]\n    sections: {a: ['ok', '(bad']}\n")

    def test_text_joins_existing_sections_and_fails_only_when_none(self, tmp_path):
        """Missing sections are skipped; none present or too short means no value"""
        families = write_families(
            tmp_path,
            "  f:\n    roles: [x]\n    sections: {a: 'Alpha', b: 'Beta'}\n    fields:\n"
            "      d: {sections: [a, b], parse: text, min_chars: 10}\n",
        )

        only_a, _ = extract_fields(families["f"], "## Alpha\nAlpha body text here.\n", "X", "d", "u")
        neither, _ = extract_fields(families["f"], "## Gamma\nGamma body text here.\n", "X", "d", "u")
        short, _ = extract_fields(families["f"], "## Alpha\nHi.\n", "X", "d", "u")

        assert "Alpha body text here." in only_a[0].value
        assert neither == [] and short == []

    def test_max_chars_truncates_at_a_sentence_boundary(self, tmp_path):
        """Long text is cut at the last sentence end within the limit"""
        families = write_families(
            tmp_path,
            "  f:\n    roles: [x]\n    sections: {a: 'Alpha'}\n    fields:\n"
            "      d: {section: a, parse: text, max_chars: 40}\n",
        )

        found, _ = extract_fields(
            families["f"], "## Alpha\nFirst sentence here. Second sentence is long and runs on.\n", "X", "d", "u"
        )

        assert found[0].value == "First sentence here."

    def test_anchored_and_anchor_are_mutually_exclusive(self, tmp_path):
        """A family cannot use both anchoring modes"""
        with pytest.raises(ValueError, match="mutually exclusive"):
            write_families(
                tmp_path,
                "  f:\n    roles: [x]\n    anchored: true\n    anchor:\n      by_heading:\n"
                "        - {identifier: 'A', heading: 'B'}\n",
            )

    def test_invalid_anchor_regex_is_rejected(self, tmp_path):
        """Anchor regexes are validated at load time"""
        with pytest.raises(ValueError, match="regex"):
            write_families(
                tmp_path,
                "  f:\n    roles: [x]\n    anchor:\n      by_heading:\n        - {identifier: '(', heading: 'B'}\n",
            )


PAD = "Filler sentence about the scheme and its applicants. " * 30
EIC = """Contents
II. EIC Pathfinder ........ 10
II.1 EIC Pathfinder Open ........ 11
II.2 EIC Pathfinder Challenges ........ 20
V. EIC Accelerator ........ 40
V.1 EIC Accelerator Open ........ 41
V.2 EIC Accelerator Challenges ........ 60
VI. EIC Strategic Technologies for Europe Platform ........ 70
VII. EIC STEP Scale Up Defence ........ 80
IX. EIC Prizes ........ 90
<!-- page 10 -->
II. EIC Pathfinder
Pathfinder intro.
II.1 EIC Pathfinder Open
""" + PAD + """
What support will you receive if your proposal is funded?

The total indicative budget for this call is EUR 166 million.

II.2 EIC Pathfinder Challenges
""" + PAD + """
The total indicative budget for this call is EUR 96 million which is expected to be
allocated in approximately equal shares across the Challenges.

II.2.1 Advanced Materials for Miniaturised Energy Harvesting Systems
Challenge text.
<!-- page 40 -->
V. EIC Accelerator
""" + PAD + """
What support will you receive if your proposal is funded?

The total indicative budget for this call is EUR 634 million. EUR 414 million of the total
indicative budget will be allocated to EIC Accelerator Open and EUR 220 million to EIC
Accelerator Challenges. The breakdown of the indicative budget for the Accelerator
Challenges is provided in Challenge descriptions below.

V.1 EIC Accelerator Open
The EIC Accelerator Open supports start-ups and SMEs.
V.2 EIC Accelerator Challenges
The total indicative budget for EIC Accelerator Challenges is EUR 220 million. However, more.
<!-- page 70 -->
VI. EIC Strategic Technologies for Europe Platform
""" + PAD + """
The EIC STEP Scale Up call offers a total indicative budget of EUR 300 million for 2026,
which is expected to rise to EUR 900 million for the period 2025-2027.

VII. EIC STEP Scale Up Defence194
""" + PAD + """
The total indicative budget for the EIC STEP Scale Up Defence call is EUR 200 million for
the period 2026-2027, comprising EUR 100 million in 2026 and EUR 100 million in 2027,

IX. EIC Prizes
Prizes text.
"""


def _eic(identifier):
    found, notes = extract_fields(FAMILIES["eic_work_programme"], EIC, identifier, "d", "u")
    return {e.field: e.value for e in found}, notes, found


class TestHeadingAnchor:
    """Test heading anchors for programme documents without topic identifiers"""

    def test_accelerator_call_reads_the_parent_accelerator_section(self):
        """The short-application Accelerator call gets the call total stated in 'V. EIC Accelerator'"""
        by_field, notes, _ = _eic("HORIZON-EIC-2026-ACCELERATOR-01")

        assert by_field["details.call_budget_eur"] == 634_000_000.0
        assert by_field["description"].startswith("V. EIC Accelerator")
        assert "defence" not in by_field["description"].lower()
        assert len(by_field["description"]) <= 3000
        assert notes == []

    def test_accelerator_call_type_budgets_are_labelled(self):
        """Open / Challenges allocations are kept as call-TYPE budgets, never as a topic budget"""
        by_field, _, _ = _eic("HORIZON-EIC-2026-ACCELERATOR-01")

        assert by_field["details.call_type_budgets"] == [
            {"label": "EIC Accelerator Open", "budget_eur": 414_000_000.0, "level": "call_type"},
            {"label": "EIC Accelerator Challenges", "budget_eur": 220_000_000.0, "level": "call_type"},
        ]
        assert "funding_amount" not in by_field

    def test_pathfinder_challenges_do_not_get_the_pathfinder_open_budget(self):
        """PATHFINDERCHALLENGES maps to II.2, not to II.1 Pathfinder Open"""
        by_field, _, _ = _eic("HORIZON-EIC-2026-PATHFINDERCHALLENGES-01-02")

        assert by_field["details.call_budget_eur"] == 96_000_000.0
        assert "approximately equal shares" in by_field["details.budget_statement"]

    def test_accelerator_challenges_call_reads_its_subsection(self):
        """A full-application Challenges call takes 'total indicative budget for EIC Accelerator Challenges'"""
        by_field, _, _ = _eic("HORIZON-EIC-2026-ACCELERATORCHALLENGES-01")

        assert by_field["details.call_budget_eur"] == 220_000_000.0

    def test_pathfinder_open_gets_its_own_budget(self):
        by_field, _, _ = _eic("HORIZON-EIC-2026-PATHFINDEROPEN-01")

        assert by_field["details.call_budget_eur"] == 166_000_000.0

    def test_step_scale_up_budget(self):
        """'offers a total indicative budget of' is the STEP wording"""
        by_field, _, _ = _eic("HORIZON-EIC-2026-STEP")

        assert by_field["details.call_budget_eur"] == 300_000_000.0

    def test_defence_call_gets_this_years_amount(self):
        """STEP Defence states 2026-2027; the call budget is the amount for the work-programme year"""
        for identifier in ("HORIZON-EIC-2026-DEFENCE-01", "HORIZON-EIC-2026-STEP-DEFENCE-01"):
            by_field, _, _ = _eic(identifier)

            assert by_field["details.call_budget_eur"] == 100_000_000.0

    def test_evidence_carries_the_page(self):
        """Values from a converted PDF cite the page they were read on"""
        _, _, found = _eic("HORIZON-EIC-2026-ACCELERATOR-01")

        assert {e.field: e.page for e in found}["details.call_budget_eur"] == 40

    def test_unmatched_identifier_gives_nothing_and_no_note(self):
        """A call outside every rule is simply not this family's business"""
        assert extract_fields(FAMILIES["eic_work_programme"], EIC, "HORIZON-CL4-2026-01", "d", "u") == ([], [])

    def test_missing_heading_is_reported(self):
        """A matching rule whose heading is absent reports anchor_not_found"""
        found, notes = extract_fields(
            FAMILIES["eic_work_programme"], "Nothing relevant here.", "HORIZON-EIC-2026-TRANSITION-01", "d", "u"
        )

        assert found == [] and notes == ["anchor_not_found"]

    def test_markdown_headings_slice_to_the_next_same_level_heading(self):
        """With real Markdown headings the slice ends at the next heading of the same or higher level"""
        doc = (
            "## EIC Transition\n### Detail\nThe total indicative budget for this call is EUR 5 million.\n"
            + "Text. " * 60 + "\n## EIC Other\nThe total indicative budget for this call is EUR 9 million.\n"
        )

        found, _ = extract_fields(FAMILIES["eic_work_programme"], doc, "HORIZON-EIC-2026-TRANSITION-01", "d", "u")

        assert {e.field: e.value for e in found}["details.call_budget_eur"] == 5_000_000.0


class TestPatternListsAndBreakdowns:
    """Test the loader's support for pattern lists and eur_breakdown"""

    def test_first_matching_pattern_wins(self, tmp_path):
        families = write_families(tmp_path, (
            "  f:\n    roles: [x]\n    fields:\n      call_budget_eur:\n        target: details.call_budget_eur\n"
            "        parse: eur\n        pattern: ['budget of {EUR}', 'comprising {EUR}']\n"
        ))
        found, _ = extract_fields(families["f"], "It is comprising EUR 3 million.", "X", "d", "u")

        assert found[0].value == 3_000_000.0

    def test_breakdown_items_need_label_and_one_eur(self, tmp_path):
        with pytest.raises(ValueError, match="eur_breakdown"):
            write_families(tmp_path, (
                "  f:\n    roles: [x]\n    fields:\n      b:\n        parse: eur_breakdown\n"
                "        items: [{label: A, pattern: 'no amount here'}]\n"
            ))


DEADLINE_FICHE = """## 3. Available budget

The total budget is EUR 10 000 000.

## 4. Timetable and deadlines

Timetable and deadlines (indicative)
Call opening: 17 June 2026
Deadline for submission: 16 February 2027 – 17:00:00 CET (Brussels)
Evaluation: February-May 2027
"""

NO_DEADLINE_FICHE = """## 4. Timetable and deadlines

Call opening: 17 June 2026
Evaluation: February-May 2027
"""


class TestDocumentDeadline:
    """The call fiche's submission deadline is extracted as a date field"""

    def test_deadline_is_extracted_as_utc(self):
        found, _ = extract_fields(FAMILIES["call_fiche"], DEADLINE_FICHE, "X", "d", "u")

        by_field = {e.field: e for e in found}

        assert by_field["details.document_deadline"].value == "2027-02-16T16:00:00+00:00"
        assert "Deadline for submission" in by_field["details.document_deadline"].evidence

    def test_opening_date_is_extracted(self):
        """'Call opening: 17 June 2026' becomes details.document_opening_date (date only)"""
        found, _ = extract_fields(FAMILIES["call_fiche"], DEADLINE_FICHE, "X", "d", "u")

        assert {e.field: e.value for e in found}["details.document_opening_date"] == "2026-06-17"

    def test_no_deadline_line_yields_nothing(self):
        found, _ = extract_fields(FAMILIES["call_fiche"], NO_DEADLINE_FICHE, "X", "d", "u")

        assert "details.document_deadline" not in {e.field for e in found}

    def test_first_deadline_line_wins(self):
        text = DEADLINE_FICHE + "Deadline for submission: 1 March 2028\n"
        found, _ = extract_fields(FAMILIES["call_fiche"], text, "X", "d", "u")

        assert {e.field: e.value for e in found}["details.document_deadline"] == "2027-02-16T16:00:00+00:00"

    def test_merge_puts_deadline_in_details_with_provenance(self):
        found, _ = extract_fields(FAMILIES["call_fiche"], DEADLINE_FICHE, "X", "doc1", "u")

        merged = merge_extractions(call(), found)

        assert merged.details["document_deadline"] == "2027-02-16T16:00:00+00:00"
        assert merged.field_sources["details.document_deadline"] == "document:doc1"
        assert merged.details["evidence"]["details.document_deadline"]["doc_id"] == "doc1"

    def test_date_parse_requires_a_pattern(self, tmp_path):
        with pytest.raises(ValueError, match="pattern"):
            write_families(tmp_path, "  f:\n    fields:\n      d: {parse: date}\n")


FULL_FICHE = """## 1. Background
Background text about the call that is long enough. """ + "Context sentence. " * 15 + """
## 2. Objectives
Objective text that explains what the call wants to achieve. """ + "More detail here. " * 15 + """
## 3. Available budget
The estimated available call budget is EUR 4 000 000.
| Topic | Budget |
| A | EUR 1 000 000 |
## 4. Timetable
Call opening: 1 June 2026. Deadline for submission: 15 October 2026 - 17:00 CET. Evaluation follows.
## 5. Admissibility and documents
Proposals must be submitted via the Funding & Tenders Portal before the deadline stated above.
## 6. Eligibility
Applicants must be legal entities established in an eligible country. Consortia need three partners.
## 7. Financial capacity and exclusion
Financial capacity text that must not leak into eligibility at all.
## 8. Evaluation and award procedure
Evaluation procedure text.
## 9. Award criteria
Proposals are evaluated against the following criteria, each scored out of five points.
## 1. Relevance
Relevance sub-criterion text on policy fit.
## 2. Quality
Quality sub-criterion text on the work plan.
## 10. Legal and financial set-up of the grants
Legal text that is outside the award criteria.
## 11. How to submit an application
Create an account in the portal and submit the proposal electronically before the deadline.
## 12. Help
Contact the helpdesk.
"""


class TestCallFicheSections:
    """Test the researcher-facing document sections kept as retrievable text"""

    @staticmethod
    def _values(markdown):
        found, _ = extract_fields(FAMILIES["call_fiche"], markdown, "X", "d", "u")
        return {e.field: e.value for e in found}

    def test_all_sections_present_with_correct_boundaries(self):
        values = self._values(FULL_FICHE)

        assert "legal entities" in values["details.document_eligibility"]
        assert "Financial capacity" not in values["details.document_eligibility"]
        assert "Funding & Tenders Portal" in values["details.document_admissibility"]
        assert "Legal entities" not in values["details.document_admissibility"]
        assert "Create an account" in values["details.document_how_to_apply"]
        assert "Contact the helpdesk" not in values["details.document_how_to_apply"]
        assert "15 October 2026" in values["details.document_timetable"]

    def test_award_criteria_runs_through_numbered_sub_criteria(self):
        criteria = self._values(FULL_FICHE)["details.document_award_criteria"]

        assert "scored out of five" in criteria
        assert "Relevance sub-criterion" in criteria and "Quality sub-criterion" in criteria
        assert "Legal text" not in criteria

    def test_award_criteria_end_at_how_to_submit_when_no_legal_section(self):
        text = FULL_FICHE.replace("## 10. Legal and financial set-up of the grants\nLegal text that is outside the award criteria.\n", "")
        criteria = self._values(text)["details.document_award_criteria"]

        assert "Quality sub-criterion" in criteria and "Create an account" not in criteria

    def test_missing_sections_yield_only_present_ones(self):
        values = self._values("## 6. Eligibility\n" + "Eligible applicants are universities. " * 3 + "\n")

        assert set(values) == {"details.document_eligibility"}

    def test_numbering_is_irrelevant(self):
        values = self._values("## Eligibility\n" + "Eligible applicants are universities. " * 3 + "\n")

        assert "details.document_eligibility" in values


class TestSliceMarkers:
    """Test slice_from / slice_to and until for parse: text"""

    FAMILY = (
        "  f:\n    roles: [x]\n    fields:\n"
        "      d: {parse: text, slice_from: '^Start here', slice_to: '^Stop here', min_chars: 5}\n"
    )

    def test_slice_is_inclusive_of_start_and_exclusive_of_end(self, tmp_path):
        families = write_families(tmp_path, self.FAMILY)
        found, _ = extract_fields(families["f"], "pre\nStart here\nbody\nStop here\npost\n", "X", "d", "u")

        assert found[0].value == "Start here\nbody"

    def test_slice_runs_to_the_end_without_an_end_marker(self, tmp_path):
        families = write_families(tmp_path, self.FAMILY)
        found, _ = extract_fields(families["f"], "Start here\nbody\n", "X", "d", "u")

        assert found[0].value == "Start here\nbody"

    def test_no_start_marker_no_value(self, tmp_path):
        families = write_families(tmp_path, self.FAMILY)

        assert extract_fields(families["f"], "nothing\n", "X", "d", "u")[0] == []

    @pytest.mark.parametrize("spec", [
        "{parse: text, slice_from: '(bad'}",
        "{parse: text, slice_from: 'a', slice_to: '(bad'}",
        "{parse: eur, slice_from: 'a'}",
        "{parse: text, slice_to: 'a'}",
    ])
    def test_bad_slice_config_is_rejected(self, tmp_path, spec):
        with pytest.raises(ValueError):
            write_families(tmp_path, f"  f:\n    fields:\n      d: {spec}\n")

    def test_slice_with_sections_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="slice"):
            write_families(
                tmp_path,
                "  f:\n    sections: {a: 'A'}\n    fields:\n      d: {section: a, parse: text, slice_from: 'x'}\n",
            )

    def test_until_for_text_joins_across_headings(self, tmp_path):
        families = write_families(
            tmp_path,
            "  f:\n    sections: {a: 'Alpha', z: 'Zeta'}\n    fields:\n"
            "      d: {section: a, until: z, parse: text}\n",
        )
        found, _ = extract_fields(families["f"], "## Alpha\none\n## 1. Sub\ntwo\n## Zeta\nthree\n", "X", "d", "u")

        assert found[0].value == "one\n1. Sub\ntwo"

    def test_work_programme_specific_conditions(self):
        wp = (
            "### HORIZON-X-2026-01-A-01: Topic title\nCall: X CALL\nSome intro text.\n"
            "Specific conditions\n"
            "Expected EU contribution per project   EUR 3.00 million   Indicative budget   EUR 20.00 million\n"
            "Type of Action   Innovation Actions   Eligibility conditions   see General Annex B\n"
            "Technology Readiness Level   TRL 5-6\n"
            "### HORIZON-X-2026-01-A-02: Another topic\nSpecific conditions\nOther topic conditions that must not leak in here.\n"
        )
        found, _ = extract_fields(FAMILIES["work_programme_topic"], wp, "HORIZON-X-2026-01-A-01", "d", "u")
        values = {e.field: e.value for e in found}

        conditions = values["details.document_specific_conditions"]
        assert conditions.startswith("Specific conditions")
        assert "Technology Readiness Level" in conditions and "Other topic" not in conditions


class TestReviewFixes:
    """Regression tests from the independent review"""

    def test_extract_fields_keeps_the_multiplier(self):
        """call_budget_eur uses the same multiplier vocabulary as parse_eur"""
        for text, expected in (("EUR 1.5m", 1_500_000.0), ("EUR 2,5 Mio", 2_500_000.0)):
            doc = f"## 3. Available budget\nThe available call budget is {text}.\n## 4. Timetable\nx\n"

            found, _ = extract_fields(FAMILIES["call_fiche"], doc, "X", "d", "u")

            assert {e.field: e.value for e in found}["details.call_budget_eur"] == expected

    def test_table_row_is_case_insensitive(self):
        """'Million' capitalised is a multiplier"""
        assert table_rows("Topic Topic budget\n1. Topic A EUR 5 Million")[0]["budget_eur"] == 5e6

    def test_glued_column_row_is_dropped_above_call_budget(self):
        """A neighbouring column glued onto the amount exceeds the call budget and drops the row"""
        text = "Topic Topic budget\n1 - Topic A EUR 1 000 000 24 months\n3 - Topic C € 2 000 000 100 3"

        rows = table_rows(text, call_budget=3_000_000.0)

        assert [r["topic_no"] for r in rows] == [1]

    def test_percent_column_not_absorbed_in_row(self):
        """'EUR 2 000 000 100 %' keeps 2,000,000"""
        rows = table_rows("Topic Topic budget\n2 \u2014 Topic B EUR 2 000 000 100 % 24 months")

        assert rows[0]["budget_eur"] == 2_000_000.0

    def test_extract_fields_filters_rows_by_call_budget(self):
        """extract_fields passes the section's call budget to the row filter"""
        doc = (
            "## 3. Available budget\nThe available call budget is EUR 3 000 000.\n"
            "Topic Topic budget\n1 - A EUR 1 000 000 24 months\n3 - C EUR 2 000 000 100 3\n"
            "## 4. Timetable\nx\n"
        )

        found, _ = extract_fields(FAMILIES["call_fiche"], doc, "X", "d", "u")

        table = {e.field: e.value for e in found}["details.topic_budget_table"]
        assert [r["topic_no"] for r in table] == [1]

    def test_heading_with_available_budget_is_not_a_row(self):
        """'## 3. Available budget EUR 10 000 000' is not topic 3"""
        text = "## 3. Available budget EUR 10 000 000\nTopic Topic budget\n1 - A EUR 5 000 000"

        assert [r["topic_no"] for r in table_rows(text)] == [1]
        assert table_rows("## 3. Available budget EUR 10 000 000\n1 - A EUR 5 000 000") == []

    def test_budget_label_row_after_header_is_skipped(self):
        """Even after the header, a 'total budget' line is not a topic"""
        text = "Topic Topic budget\n## 3. Total budget EUR 10 000 000\n1 - A EUR 5 000 000"

        assert [r["topic_no"] for r in table_rows(text)] == [1]

    def test_elimination_requires_contiguous_topic_numbers(self):
        """Rows 1 and 3 (a gap) never pair by elimination"""
        rows = [row(1, "XX-2026-RA-BETA", 1e6), row(3, "Gamma", 2e6)]
        records = [topic("XX-2026-RA-ALPHA", title="Alpha", call_id="XX-2026-RA"),
                   topic("XX-2026-RA-BETA", title="Beta", call_id="XX-2026-RA")]

        matches = match_topic_rows(records, rows)

        assert list(matches) == ["XX-2026-RA-BETA"]

    def test_elimination_requires_equal_counts(self):
        """More rows than records: no elimination"""
        rows = [row(1, "XX-2026-RA-BETA", 1e6), row(2, "Gamma", 2e6), row(3, "Delta", 3e6)]
        records = [topic("XX-2026-RA-ALPHA", title="Alpha", call_id="XX-2026-RA"),
                   topic("XX-2026-RA-BETA", title="Beta", call_id="XX-2026-RA")]

        assert list(match_topic_rows(records, rows)) == ["XX-2026-RA-BETA"]

    def test_newer_document_is_pending_confirmation(self):
        """newer_document replacements are flagged pending; api_is_call_total structural"""
        record = call(funding_amount=980_000.0, source_updated_at="2026-03-02T09:54:54+00:00")

        merged = merge_extractions(record, [ex(value=3_920_000.0, published_at="2026-09-29T13:02:31+00:00")])

        assert merged.details["value_history"][0]["confirmation"] == "pending"
        assert merged.data_gaps[0]["confirmation"] == "pending"
        record = call(funding_amount=150e6)

        merged = merge_extractions(record, [ex(value=60e6, call_totals=(150e6,))])

        assert merged.details["value_history"][0]["confirmation"] == "structural"
        assert merged.data_gaps[0]["confirmation"] == "structural"

    def test_discrepancy_names_the_real_source_of_the_existing_value(self):
        """The existing value's source is reported; `api` key only when it IS the api"""
        record = call(funding_amount=1e6, field_sources={"funding_amount": "document:old"})

        gap = merge_extractions(record, [ex(value=4e6)]).data_gaps[0]

        assert gap["existing"] == 1e6 and gap["existing_source"] == "document:old"
        assert "api" not in gap
        gap = merge_extractions(call(funding_amount=1e6), [ex(value=4e6)]).data_gaps[0]
        assert gap["existing_source"] == "api" and gap["api"] == 1e6 and gap["existing"] == 1e6

    def test_later_document_compared_with_earlier_document_date(self):
        """Two documents: newer-than-the-current-document replaces, otherwise a discrepancy between them"""
        record = call(source_updated_at="2026-01-01T00:00:00+00:00")
        d1 = ex(value=4e6, doc_id="d1", published_at="2026-05-01T00:00:00+00:00")

        newer = merge_extractions(record, [d1, ex(value=9e6, doc_id="d2", published_at="2026-06-01T00:00:00+00:00")])
        older = merge_extractions(record, [d1, ex(value=9e6, doc_id="d2", published_at="2026-04-01T00:00:00+00:00")])

        assert newer.funding_amount == 9e6
        assert newer.details["value_history"][0]["source"] == "document:d1"
        assert older.funding_amount == 4e6
        assert older.data_gaps[0]["kind"] == "discrepancy"
        assert older.data_gaps[0]["existing_source"] == "document:d1"

    def test_eur_pattern_must_contain_placeholder_or_one_group(self, tmp_path):
        """A malformed eur pattern is rejected at load"""
        with pytest.raises(ValueError, match="eur"):
            write_families(tmp_path, FAMILY_YAML.format(pattern="'budget is'"))

    def test_eur_pattern_with_trailing_optional_group_still_works(self, tmp_path):
        """The amount is read through the named group, not the last group"""
        fam = write_families(tmp_path, FAMILY_YAML.format(pattern=r"'budget is\s*{EUR}(\s+net)?'"))["f"]

        found, _ = extract_fields(fam, "## Budget\nbudget is EUR 4 000 000 net\n", "X", "d", "u")

        assert found[0].value == 4_000_000.0


FAMILY_YAML = """  f:
    roles: [x]
    sections: {{budget: 'budget'}}
    fields:
      amount:
        section: budget
        parse: eur
        pattern: {pattern}
"""


class TestWorkProgrammeBudgetStatement:
    """The WP topic's 'Indicative budget' line is kept as budget_statement (proof even when it is EUR 0.00)"""

    WP = (
        "### HORIZON-CL4-2027-SPACE-07-83: Space critical EEE components for EU non-\ndependence\n\n"
        "Specific conditions\n\nExpected EU The Commission estimates that an EU contribution of between EUR 3.00\n"
        "contribution per and 4.00 million would allow these outcomes to be addressed\n\n"
        "Indicative budget The total indicative budget for the topic is EUR 0.00 million.\n\n"
        "Type of Action Research and Innovation Actions\n\n"
        "### HORIZON-CL4-2027-SPACE-07-84: Other\nThe indicative budget for the topic is EUR 5.00 million.\n"
    )

    def test_zero_budget_statement_is_kept_and_no_amount_is_invented(self):
        found, _ = extract_fields(FAMILIES["work_programme_topic"], self.WP, "HORIZON-CL4-2027-SPACE-07-83", "d", "u")
        by_field = {e.field: e.value for e in found}

        assert by_field["details.budget_statement"] == (
            "Indicative budget The total indicative budget for the topic is EUR 0.00 million.")
        assert "funding_amount" not in by_field


class TestCallTableRow:
    """Horizon WPs list each CALL with its budget in EUR million: 'HORIZON-MSCA-2026-DN-01 628.03 24 Nov 2026'"""

    WP = (
        "Call Budgets (EUR million) Deadline(s)\n\n2026 2027\n\n"
        "HORIZON-MSCA-2026-DN-01 628.03 24 Nov 2026\n\nHORIZON-MSCA-2027-DN-01 645.99 23 Nov 2027\n\n"
        "HORIZON-NEB-2027-01-REGEN-03- IA Around 0\n"
    )

    def test_the_calls_own_row_gives_the_call_budget(self):
        found, _ = extract_fields(FAMILIES["work_programme_call_row"], self.WP, "HORIZON-MSCA-2027-DN-01-01",
                                  "d", "u", call_identifier="HORIZON-MSCA-2027-DN-01")

        assert {e.field: e.value for e in found} == {"details.call_budget_eur": 645_990_000.0}

    def test_year_columns_are_summed(self):
        """'HORIZON-CL6-2026-04 23.00 37.00' = 2026 + 2027 budgets: the call total is EUR 60 million"""
        found, _ = extract_fields(FAMILIES["work_programme_call_row"], "HORIZON-CL6-2026-04 23.00 37.00\n",
                                  "T", "d", "u", call_identifier="HORIZON-CL6-2026-04")

        assert found[0].value == 60_000_000.0

    def test_no_call_identifier_means_no_extraction(self):
        assert extract_fields(FAMILIES["work_programme_call_row"], self.WP, "X", "d", "u") == ([], [])

    def test_a_topic_id_that_extends_the_call_id_is_not_a_call_row(self):
        found, _ = extract_fields(FAMILIES["work_programme_call_row"], self.WP, "T", "d", "u",
                                  call_identifier="HORIZON-NEB-2027-01")
        assert found == []

    def test_call_anchored_family_must_use_the_call_placeholder(self, tmp_path):
        with pytest.raises(ValueError, match="CALL"):
            write_families(tmp_path, (
                "  f:\n    roles: [x]\n    call_anchored: true\n    fields:\n      b:\n        parse: eur\n"
                "        pattern: 'budget {EUR}'\n"
            ))


class TestCallConditions:
    """Horizon WP call blocks: 'HORIZON-CL4-2027-SPACE-07 ... Opening: 07 Oct 2027 / Deadline(s): 20 Jan 2028'"""

    WP = (
        "Call - SPACE\n\nHORIZON-CL4-2027-SPACE-07\n\nConditions for the Call\n\nIndicative budget(s)\n\n"
        "Opening: 07 Oct 2027\nDeadline(s): 20 Jan 2028\n\n"
        "Call - DIGITAL\n\nHORIZON-CL4-2027-DIGITAL-08\n\nConditions for the Call\n\n"
        "Opening: 01 Sep 2027\nDeadline(s): 10 Dec 2027\n"
    )

    def _found(self, call):
        found, _ = extract_fields(FAMILIES["work_programme_call_conditions"], self.WP, "T", "d", "u",
                                  call_identifier=call)
        return {e.field: e.value for e in found}

    def test_the_calls_own_dates(self):
        assert self._found("HORIZON-CL4-2027-SPACE-07") == {
            "details.document_deadline": "2028-01-20", "details.document_opening_date": "2027-10-07"}

    def test_never_reads_the_next_calls_block(self):
        assert self._found("HORIZON-CL4-2027-DIGITAL-08")["details.document_deadline"] == "2027-12-10"
        doc = "HORIZON-X-01\n\nConditions\n\nHORIZON-X-02\n\nDeadline(s): 10 Dec 2027\n"
        found, _ = extract_fields(FAMILIES["work_programme_call_conditions"], doc, "T", "d", "u",
                                  call_identifier="HORIZON-X-01")
        assert found == []


def test_section_page_is_the_sections_page_not_the_table_of_contents():
    """A section whose first line also appears in the contents page cites the section's own page"""
    doc = (
        "<!-- page 3 -->\nContents\nEligible participants (eligible countries) .......... 18\n\n"
        "<!-- page 18 -->\n## 6. Eligibility\nEligible participants (eligible countries)\n"
        "In order to be eligible, the applicants must be legal entities established in an eligible country.\n"
    )
    found, _ = extract_fields(FAMILIES["call_fiche"], doc, "X", "d", "u")

    assert {e.field: e.page for e in found}["details.document_eligibility"] == 18
