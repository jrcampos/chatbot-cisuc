"""
Unit tests for strip_corpus_boilerplate in the content_extractor module.
Tests removal of site-wide nav/menu lines repeated across scraped pages.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("WORKSPACE", "/tmp/cisuc-test-workspace")

from preprocessing.ingestion.cisuc_scraper.extractors.content_extractor import (
    strip_corpus_boilerplate,
)


class TestStripCorpusBoilerplate:
    def test_removes_line_repeated_across_most_files(self, tmp_path):
        menu_line = "The CentreOrganisationPeopleResearchAdvanced Training"

        for i in range(5):
            content = f"# Page {i}\n\n{menu_line}\n\nUnique content for page {i}.\n"
            (tmp_path / f"page{i}.md").write_text(content, encoding="utf-8")

        strip_corpus_boilerplate(tmp_path, min_frequency=0.2)

        for i in range(5):
            text = (tmp_path / f"page{i}.md").read_text(encoding="utf-8")
            assert menu_line not in text
            assert f"Unique content for page {i}." in text

    def test_keeps_lines_below_threshold(self, tmp_path):
        for i in range(5):
            content = f"# Page {i}\n\nUnique content for page {i}.\n"
            (tmp_path / f"page{i}.md").write_text(content, encoding="utf-8")

        strip_corpus_boilerplate(tmp_path, min_frequency=0.2)

        for i in range(5):
            text = (tmp_path / f"page{i}.md").read_text(encoding="utf-8")
            assert f"Unique content for page {i}." in text

    def test_noop_with_fewer_than_two_files(self, tmp_path):
        (tmp_path / "only.md").write_text("Solo content\n", encoding="utf-8")

        strip_corpus_boilerplate(tmp_path, min_frequency=0.2)

        assert (tmp_path / "only.md").read_text(encoding="utf-8") == "Solo content\n"
