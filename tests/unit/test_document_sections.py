"""
Unit tests for external_sources.documents.sections.
"""

from external_sources.documents.sections import (
    clean_text,
    find_topic_slice,
    split_sections,
)

MARKDOWN = """<!-- page 1 -->
Contents
### HORIZON-X-2026-01-A-01: Some long topic title that wraps
onto a second line ........................ 12
### HORIZON-X-2026-01-A-02: Another topic ........ 14

<!-- page 5 -->
## Overview table
### HORIZON-X-2026-01-A-01: Some long topic title IA 20.00 Around 3
### HORIZON-X-2026-01-A-02: Another topic IA 5.00

<!-- page 9 -->
### HORIZON-X-2026-01-A-01: Some long topic title that wraps
Call: X CALL

Specific conditions
Indicative budget The total indicative budget for the topic is EUR 20.00 million.
Expected outcome text that goes on for a while so this slice is clearly the longest one
in the document, unlike the contents line and the summary table row above it.

### HORIZON-X-2026-01-A-02: Another topic
Indicative budget The total indicative budget for the topic is EUR 5.00 million.
"""


class TestSplitSections:
    """Test split_sections()"""

    def test_sections_follow_markdown_headings(self):
        """Each heading starts a section that runs to the next heading"""
        text = "## 1. Background\nfoo\n## 2. Objectives\nbar\nbaz\n"

        sections = split_sections(text)

        assert [s.heading for s in sections] == ["1. Background", "2. Objectives"]
        assert sections[0].text == "foo"
        assert sections[1].text == "bar\nbaz"
        assert sections[0].level == 2

    def test_no_headings_gives_no_sections(self):
        """Plain text has no sections"""
        assert split_sections("just text") == []


class TestFindTopicSlice:
    """Test find_topic_slice()"""

    def test_picks_the_real_section_not_toc_or_table_row(self):
        """The contents entry and summary table row are shorter than the real section"""
        start, end = find_topic_slice(MARKDOWN, "HORIZON-X-2026-01-A-01")

        piece = MARKDOWN[start:end]

        assert "total indicative budget for the topic is EUR 20.00 million" in piece
        assert "EUR 5.00 million" not in piece  # the next topic is excluded
        assert piece.startswith("### HORIZON-X-2026-01-A-01: Some long topic title that wraps")

    def test_last_topic_runs_to_the_end(self):
        """The final topic slice ends at the end of the document"""
        start, end = find_topic_slice(MARKDOWN, "HORIZON-X-2026-01-A-02")

        assert "EUR 5.00 million" in MARKDOWN[start:end]
        assert end == len(MARKDOWN)

    def test_unknown_identifier_returns_none(self):
        """No heading for the identifier -> None"""
        assert find_topic_slice(MARKDOWN, "HORIZON-X-2026-01-Z-99") is None

    def test_identifier_match_is_case_insensitive(self):
        """Identifiers match regardless of case"""
        assert find_topic_slice(MARKDOWN, "horizon-x-2026-01-a-01") is not None

    def test_max_chars_caps_the_slice(self):
        """A runaway slice is capped"""
        start, end = find_topic_slice(MARKDOWN, "HORIZON-X-2026-01-A-01", max_chars=100)

        assert end - start <= 100


class TestCleanText:
    """Test clean_text()"""

    def test_removes_page_markers_and_heading_hashes(self):
        """Page markers disappear and ## prefixes are dropped"""
        text = "<!-- page 3 -->\n## 1. Background\n\n\n\nBody text.\n"

        assert clean_text(text) == "1. Background\n\nBody text."
