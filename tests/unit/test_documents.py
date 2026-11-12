"""
Unit tests for external_sources.documents — link discovery, role classification,
conversion to Markdown, storage and the fetch pipeline.
"""

import dataclasses
import shutil

import pytest

from external_sources.documents.convert import (
    UnsupportedContentType,
    convert,
    pdf_to_text,
    text_to_markdown,
)
from external_sources.documents.pipeline import DocumentPipeline
from external_sources.documents.policy import DEFAULT_POLICY_PATH, load_policy
from external_sources.documents.roles import classify_url, discover_links
from external_sources.documents.store import DocumentStore
from external_sources.http_cache import FetchResult
from external_sources.plugins.base import NormalizedRecord

POLICY = load_policy()
BASE = "https://ec.europa.eu/info/funding-tenders/opportunities/docs/2021-2027"


def rec(key="SMP-FOOD-2026-FW-STAKEHOLDERS-PJ", **details):
    """A call record with the given details."""
    return NormalizedRecord(
        natural_key=key, record_type="call", domain="projects", title="T",
        source_url="https://x.eu/" + key, raw={}, details=details,
    )


class TestClassifyUrl:
    """Test classify_url() against real URL shapes"""

    def test_terms_and_conditions_are_boilerplate(self):
        """The generic T&C PDF (linked by ~all calls) is boilerplate"""
        url = f"{BASE}/common/ftp/tc_en.pdf"
        assert classify_url(url, "A-1", "A", POLICY) == "boilerplate"

    def test_call_fiche_is_call_specific(self):
        """A call fiche under wp-call is call-specific, not programme-level"""
        url = f"{BASE}/smp/wp-call/2026/call-fiche_smp-food-2026-fw-stakeholders-pj_en.pdf"
        assert classify_url(url, "SMP-FOOD-2026-FW-STAKEHOLDERS-PJ", "X", POLICY) == "call_specific"

    def test_identifier_in_url_is_call_specific(self):
        """A URL containing the call's identifier is call-specific"""
        url = f"{BASE}/x/doc-horizon-cl5-2026-10-d6-06.pdf"
        assert classify_url(url, "HORIZON-CL5-2026-10-D6-06", "", POLICY) == "call_specific"

    def test_work_programme_is_programme_level(self):
        """A shared Work Programme part is programme-level"""
        url = f"{BASE}/horizon/wp-call/2026-2027/wp-8-climate-energy-and-mobility_horizon-2026-2027_en.pdf"
        assert classify_url(url, "A-1", "A", POLICY) == "programme_level"

    def test_eic_work_programme_is_programme_level(self):
        """The EIC work programme (different host) is recognised by name"""
        url = "https://eic.ec.europa.eu/document/download/x_en?filename=EIC-Work-Programme-2026.pdf"
        assert classify_url(url, "A-1", "A", POLICY) == "programme_level"

    def test_submission_link_is_ignored_even_with_identifier(self):
        """Submission-system links are not documents"""
        url = "https://ec.europa.eu/research/participants/submission/manage/screen/submission/create-draft/1?topic=A-1"
        assert classify_url(url, "A-1", "A", POLICY) == "ignore"

    def test_short_identifiers_never_match_a_url(self):
        """A one-letter call id must not turn every URL into call_specific"""
        url = f"{BASE}/common/ftp/tc_en.pdf"

        assert classify_url(url, "A-1", "A", POLICY) == "boilerplate"
        assert classify_url("https://a.eu/a/a.pdf", "X", "A", POLICY) == "other"

    def test_identifier_must_be_a_whole_token(self):
        """An identifier embedded inside a longer word does not match"""
        url = f"{BASE}/x/notlife-2026-sap-env-guide.pdf"

        assert classify_url(url, "LIFE-2026-SAP-ENV", "", POLICY) == "other"

    def test_random_web_page_is_other(self):
        """Unrecognised pages are role other"""
        assert classify_url("https://cordis.europa.eu/project/id/1", "A-1", "A", POLICY) == "other"

    def test_eur_lex_is_boilerplate(self):
        """Legal texts are boilerplate"""
        assert classify_url("https://eur-lex.europa.eu/eli/reg/2024/2509/oj", "A-1", "A", POLICY) == "boilerplate"


class TestDiscoverLinks:
    """Test discover_links()"""

    def test_collects_links_from_configured_fields_deduped(self):
        """Links come from the policy's link fields, once per URL, in order"""
        record = rec(
            conditions_links=[{"text": "A", "url": "https://a.eu/1"}, {"text": "B", "url": "https://a.eu/2"}],
            description_links=[{"text": "A again", "url": "https://a.eu/1"}],
            links=[{"url": "https://submit.eu/x"}],
        )

        links = discover_links(record, POLICY)

        assert [link.url for link in links] == ["https://a.eu/1", "https://a.eu/2"]

    def test_missing_fields_yield_no_links(self):
        """A record without link fields has no documents"""
        assert discover_links(rec(), POLICY) == []

    def test_malformed_entries_are_skipped(self):
        """Entries without a usable url are ignored"""
        record = rec(conditions_links=[{"text": "no url"}, "junk", {"url": ""}])

        assert discover_links(record, POLICY) == []


class TestConvert:
    """Test conversion to Markdown"""

    def test_html_becomes_text(self):
        """HTML pages convert to readable text"""
        body = b"<html><body><h1>Title</h1><p>Some <b>text</b>.</p></body></html>"

        markdown = convert(body, "text/html; charset=utf-8", "https://x.eu/p")

        assert "Title" in markdown and "Some text." in markdown
        assert "<p>" not in markdown

    def test_html_headings_become_markdown_headings(self):
        """h1..h6 become '#' headings so split_sections can slice the page"""
        from external_sources.documents.sections import split_sections

        body = (b"<html><body><h2>Background</h2><p>Intro.</p>"
                b"<h2>3. Available budget</h2><p>EUR 5 million.</p><h4>Sub</h4></body></html>")

        markdown = convert(body, "text/html", "https://x.eu/p")

        assert "\n## 3. Available budget\n" in "\n" + markdown
        assert "#### Sub" in markdown
        titles = [s.heading for s in split_sections(markdown)]
        assert any("Available budget" in str(t) for t in titles)

    def test_unsupported_type_raises(self):
        """Spreadsheets and archives are not converted"""
        with pytest.raises(UnsupportedContentType):
            convert(b"PK\x03\x04", "application/zip", "https://x.eu/a.zip")

    def test_numbered_headings_and_pages_become_markdown(self):
        """Numbered headings become ## and page breaks become page markers"""
        text = "0. Introduction\nIntro text.\n\f3. Available budget\nThe budget is EUR 4 000 000.\n"

        markdown = text_to_markdown(text)

        assert "<!-- page 1 -->" in markdown and "<!-- page 2 -->" in markdown
        assert "## 0. Introduction" in markdown
        assert "## 3. Available budget" in markdown

    def test_toc_lines_are_not_headings(self):
        """Table-of-contents lines with dot leaders stay plain text"""
        markdown = text_to_markdown("3. Available budget ........................ 7\n")

        assert "## " not in markdown

    def test_topic_headings_are_marked(self):
        """Work-programme topic headings (ID: title) become ### headings"""
        markdown = text_to_markdown("HORIZON-CL5-2026-10-D6-06: Increasing competitiveness\nbody\n")

        assert "\n### HORIZON-CL5-2026-10-D6-06: Increasing competitiveness\n" in markdown

    def test_sentence_lines_are_not_headings(self):
        """A numbered line ending with a period is a sentence, not a heading"""
        assert "## " not in text_to_markdown("1. This is a sentence that ends here.\n")

    def _paged(self, bodies):
        header = "Call: SMP-FOOD-2026-X \u2014 Food Waste\nEU Grants: Call document (SMP): V1.0 \u2013 01.06.2026\n"
        return "\f".join(header + body for body in bodies)

    def test_running_headers_and_page_numbers_are_removed(self):
        """Header lines repeated at the top of most pages and bare page numbers are page furniture"""
        text = self._paged(["Intro line one.\n\n2\n", "## body\nMore prose here.\n\n3\n", "Last page.\n\n4\n"])

        markdown = text_to_markdown(text)

        assert "Call: SMP-FOOD-2026-X" not in markdown and "EU Grants: Call document" not in markdown
        assert "\n2\n" not in markdown and "\n3\n" not in markdown
        assert "Intro line one." in markdown and "More prose here." in markdown
        assert markdown.count("<!-- page") == 3

    def test_a_header_seen_on_few_pages_is_kept(self):
        """A line repeated on only two pages is not treated as a running header"""
        text = "Same line\nA\f" + "Same line\nB\f" + "Other\nC\f" + "Other2\nD\f" + "Other3\nE"

        assert text_to_markdown(text).count("Same line") == 2

    def test_footnotes_at_the_page_tail_become_footnote_lines(self):
        """'13 / text / 14 / url' at the bottom of a page are footnotes, kept as [^n]: lines"""
        page = (
            "## 3. Available budget\nThe estimated available call budget is EUR 4 000 000.\n\n"
            "We reserve the right not to award all available funds.\n\n"
            "13\nFurther information can be found under section Milestones\n"
            "14\nhttps://ec.europa.eu/food/x\n\n8\n"
        )

        markdown = text_to_markdown(page)

        assert "[^13]: Further information can be found under section Milestones" in markdown
        assert "[^14]: https://ec.europa.eu/food/x" in markdown
        assert "\n8\n" not in markdown

    def test_clean_text_drops_footnote_lines(self):
        """Extracted prose (e.g. budget_statement) carries no footnote bodies"""
        from external_sources.documents.sections import clean_text

        text = "Budget is EUR 4 000 000.\n\n[^13]: Further information here\n[^14]: https://x.eu\n"

        assert clean_text(text) == "Budget is EUR 4 000 000."

    def test_numbered_list_in_the_middle_of_a_page_is_not_a_footnote(self):
        """Bare numbers far from the page tail stay (e.g. table cells)"""
        body = "1\nAlpha row\n2\nBeta row\n" + "".join(f"Line {i} of prose.\n" for i in range(40))

        markdown = text_to_markdown(body)

        assert "[^" not in markdown and "Alpha row" in markdown

    def test_docx_becomes_markdown(self):
        """Word documents are read with the standard library: headings, paragraphs and table rows"""
        import io
        import zipfile

        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>3. Available budget</w:t></w:r></w:p>'
            '<w:p><w:r><w:t xml:space="preserve">The budget is </w:t></w:r><w:r><w:t>EUR 4 000 000.</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Topic</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>Budget</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '</w:body></w:document>'
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", xml)
        docx_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

        markdown = convert(buffer.getvalue(), docx_type, "https://x.eu/call.docx")

        assert "# 3. Available budget" in markdown
        assert "The budget is EUR 4 000 000." in markdown
        assert "Topic | Budget" in markdown

    def test_a_zip_that_is_not_docx_is_unsupported(self):
        """An archive without word/document.xml is still unsupported"""
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.txt", "x")
        with pytest.raises(UnsupportedContentType):
            convert(buffer.getvalue(), "application/octet-stream", "https://x.eu/a.docx")

    @pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext not installed")
    def test_pdf_to_text_reads_a_real_pdf(self):
        """pdftotext extracts text from a minimal PDF"""
        pdf = (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R"
            b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
            b"4 0 obj<</Length 42>>stream\nBT /F1 12 Tf 20 100 Td (Hello CISUC) Tj ET\nendstream endobj\n"
            b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
            b"trailer<</Root 1 0 R/Size 6>>\n%%EOF\n"
        )

        assert "Hello CISUC" in pdf_to_text(pdf)


class TestDocumentStore:
    """Test DocumentStore"""

    def test_save_and_load_roundtrip(self, tmp_path):
        """A saved document can be loaded by URL and by id"""
        store = DocumentStore(tmp_path)

        doc_id = store.save("https://x.eu/a.pdf", "# Hello\n", {"role": "call_specific"})

        by_url = store.load("https://x.eu/a.pdf")
        assert by_url.doc_id == doc_id
        assert by_url.markdown == "# Hello\n"
        assert by_url.meta["role"] == "call_specific"
        assert by_url.meta["chars"] == 8
        assert store.load_by_id(doc_id).url == "https://x.eu/a.pdf"

    def test_unknown_document_is_none(self, tmp_path):
        """Loading an unstored URL returns None"""
        assert DocumentStore(tmp_path).load("https://x.eu/none") is None


class StubFetcher:
    """Fetcher double keyed by URL; counts requests."""

    def __init__(self, results):
        self.results = results
        self.requests: list[str] = []

    def get(self, url):
        self.requests.append(url)
        return self.results[url]


def html(text):
    """A fresh HTML FetchResult."""
    return FetchResult("fresh", f"<p>{text}</p>".encode(), "text/html")


class TestMalformedLinks:
    """Test discover_links() against junk URLs seen in scraped data"""

    @pytest.mark.parametrize(
        "bad", [" https:", ": https://: h", "ttp:", "https://nodot", "https://a.eu/x y", "https://", "mailto:a@b.eu"]
    )
    def test_malformed_urls_are_skipped(self, bad):
        """Only well-formed http(s) URLs with a dotted host and no whitespace survive"""
        record = rec(conditions_links=[{"text": "x", "url": bad}, {"text": "ok", "url": "https://a.eu/x.pdf"}])

        links = discover_links(record, POLICY)

        assert [link.url for link in links] == ["https://a.eu/x.pdf"]


class TestProfileClassification:
    """Test classify_url() with link text, the generic text rule and profile rules"""

    def _profile(self, tmp_path, rules):
        import yaml

        from external_sources.documents.profiles import load_profiles

        (tmp_path / "prog.yaml").write_text(yaml.safe_dump({"programmes": {"p": {"id": "1", "label": "P"}}}))
        (tmp_path / "prof.yaml").write_text(
            yaml.safe_dump({"profiles": {"p": {"classify": rules}}})
        )
        return load_profiles(tmp_path / "prof.yaml", tmp_path / "prog.yaml").for_programme("P")

    def test_generic_call_text_rule(self):
        """Link text 'Call document' marks call_specific even on a boilerplate-looking URL"""
        url = "https://eur-lex.europa.eu/x"
        url = "https://a.eu/x/y.pdf"
        assert classify_url(url, "A-1", "A", POLICY, text="  Call Document") == "call_specific"
        assert classify_url(url, "A-1", "A", POLICY, text="The call document") == "other"

    def test_boilerplate_url_beats_generic_call_text(self):
        """A 'Call document' link under /common/ or op.europa.eu stays boilerplate"""
        for url in (f"{BASE}/common/ftp/tc_en.pdf", "https://op.europa.eu/x"):
            assert classify_url(url, "A-1", "A", POLICY, text="Call document") == "boilerplate"

    def test_call_text_beats_programme_level_url(self):
        """Generic call text still outranks programme-level URL patterns"""
        url = "https://a.eu/work-programme/x.pdf"
        assert classify_url(url, "A-1", "A", POLICY, text="Call document") == "call_specific"

    def test_profile_rule_beats_boilerplate_url(self, tmp_path):
        """Profile rules run before boilerplate patterns"""
        profile = self._profile(tmp_path, [{"role": "call_specific", "url": "op.europa.eu"}])
        assert classify_url("https://op.europa.eu/x", "A-1", "A", POLICY, profile=profile) == "call_specific"

    def test_ignore_beats_profile_and_profile_beats_generic(self, tmp_path):
        """Order: policy ignore, profile rules (first wins), then the generic logic"""
        profile = self._profile(
            tmp_path,
            [{"role": "other", "url": "work-programme"}, {"role": "boilerplate", "url": "work-programme"}],
        )
        wp = "https://a.eu/work-programme/x.pdf"
        assert classify_url(wp, "A-1", "A", POLICY, profile=profile) == "other"
        assert classify_url(wp, "A-1", "A", POLICY) == "programme_level"
        ignored = "https://a.eu/submission/manage/work-programme"
        assert classify_url(ignored, "A-1", "A", POLICY, profile=profile) == "ignore"

    def test_rule_needs_all_conditions(self, tmp_path):
        """host + text must both match"""
        profile = self._profile(
            tmp_path, [{"role": "call_specific", "host": r"efsa\.europa\.eu$", "text": "annex"}]
        )
        url = "https://www.efsa.europa.eu/x.pdf"
        assert classify_url(url, "A-1", "A", POLICY, text="Annex 1", profile=profile) == "call_specific"
        assert classify_url(url, "A-1", "A", POLICY, text="Other", profile=profile) == "other"
        assert classify_url("https://b.eu/x.pdf", "A-1", "A", POLICY, text="Annex", profile=profile) == "other"


class TestPolicyTextPatterns:
    """Test call_specific_text_patterns loading"""

    def test_default_when_constructing_directly(self):
        """The field defaults to empty so existing constructions keep working"""
        assert dataclasses.replace(POLICY, call_specific_text_patterns=()).call_specific_text_patterns == ()

    def test_real_policy_has_pattern_and_bad_values_rejected(self, tmp_path):
        """The shipped policy defines the pattern; a non-list or bad regex raises"""
        import yaml

        assert POLICY.call_specific_text_patterns
        base = yaml.safe_load(open("external_sources/config/document_policy.yaml"))
        for bad in ("nope", ["("]):
            base["call_specific_text_patterns"] = bad
            path = tmp_path / "p.yaml"
            path.write_text(yaml.safe_dump(base))
            with pytest.raises(ValueError):
                load_policy(path)


class TestDocumentPipeline:
    """Test DocumentPipeline.process()"""

    def _record(self):
        return rec(
            conditions_links=[
                {"text": "Call document", "url": f"{BASE}/smp/wp-call/2026/call-fiche_x.html"},
                {"text": "T&C", "url": f"{BASE}/common/ftp/tc_en.html"},
                {"text": "Random page", "url": "https://cordis.europa.eu/project/id/1"},
            ],
            links=[{"url": "https://submit.eu/x"}],
        )

    def test_roles_statuses_and_store(self, tmp_path):
        """Fetches call-specific + boilerplate, skips role `other`, ignores submission links"""
        fetcher = StubFetcher(
            {
                f"{BASE}/smp/wp-call/2026/call-fiche_x.html": html("fiche text"),
                f"{BASE}/common/ftp/tc_en.html": html("terms text"),
            }
        )
        pipeline = DocumentPipeline(fetcher, DocumentStore(tmp_path), POLICY)

        docs = pipeline.process(self._record())

        by_role = {d.role: d for d in docs}
        assert by_role["call_specific"].status == "fresh" and by_role["call_specific"].doc_id
        assert by_role["boilerplate"].status == "fresh"
        assert by_role["other"].status == "skipped_role" and by_role["other"].doc_id is None
        assert len(docs) == 3
        assert len(fetcher.requests) == 2
        stored = DocumentStore(tmp_path).load(f"{BASE}/smp/wp-call/2026/call-fiche_x.html")
        assert "fiche text" in stored.markdown

    def test_landing_page_download_link_is_followed_once(self, tmp_path):
        """A europa.eu document landing page ('Download (996 KB - PDF)') is replaced by the file it offers"""
        page = "https://cybersecurity-centre.europa.eu/document/8e1d_en"
        target = "https://cybersecurity-centre.europa.eu/document/download/8e1d_en?filename=Call.pdf"
        landing = FetchResult(
            "fresh", b'<h1>Call</h1><a href="/document/download/8e1d_en?filename=Call.pdf">Download (996.35 KB - PDF)</a>',
            "text/html")
        fetcher = StubFetcher({page: landing, target: html("## 3. Available budget The budget is EUR 5 000 000.")})
        pipeline = DocumentPipeline(fetcher, DocumentStore(tmp_path), POLICY)

        docs = pipeline.process(rec(conditions_links=[{"text": "Call document", "url": page}]))

        assert fetcher.requests == [page, target]
        stored = DocumentStore(tmp_path).load(page)
        assert "EUR 5 000 000" in stored.markdown and stored.meta["resolved_url"] == target
        assert docs[0].status == "fresh"

    def test_download_link_to_another_host_is_not_followed(self, tmp_path):
        page = "https://cybersecurity-centre.europa.eu/document/8e1d_en"
        landing = FetchResult("fresh", b'<a href="https://evil.example/x.pdf">Download (1 KB - PDF)</a>', "text/html")
        fetcher = StubFetcher({page: landing})
        pipeline = DocumentPipeline(fetcher, DocumentStore(tmp_path), POLICY)

        pipeline.process(rec(conditions_links=[{"text": "Call document", "url": page}]))

        assert fetcher.requests == [page]

    def test_shared_document_is_fetched_once_per_run(self, tmp_path):
        """Two calls linking the same URL trigger one fetch"""
        url = f"{BASE}/common/ftp/tc_en.html"
        fetcher = StubFetcher({url: html("terms")})
        pipeline = DocumentPipeline(fetcher, DocumentStore(tmp_path), POLICY)
        a = rec("A-1", conditions_links=[{"text": "T", "url": url}])
        b = rec("B-1", conditions_links=[{"text": "T", "url": url}])

        docs_a, docs_b = pipeline.process(a), pipeline.process(b)

        assert fetcher.requests == [url]
        assert docs_a[0].doc_id == docs_b[0].doc_id

    def test_not_found_is_reported_without_losing_the_call(self, tmp_path):
        """A 404 document becomes a not_found entry"""
        url = f"{BASE}/smp/wp-call/2026/call-fiche_x.html"
        fetcher = StubFetcher({url: FetchResult("not_found", error="HTTP 404")})
        pipeline = DocumentPipeline(fetcher, DocumentStore(tmp_path), POLICY)

        docs = pipeline.process(rec(conditions_links=[{"text": "d", "url": url}]))

        assert docs[0].status == "not_found" and docs[0].doc_id is None

    def test_stale_refresh_reuses_stored_copy(self, tmp_path):
        """A failed refresh with a stored copy is reported stale but keeps the doc_id"""
        url = f"{BASE}/smp/wp-call/2026/call-fiche_x.html"
        store = DocumentStore(tmp_path)
        doc_id = store.save(url, "old markdown", {"role": "call_specific"})
        stale = FetchResult("error", b"<p>old</p>", "text/html", from_cache=True, error="down")
        pipeline = DocumentPipeline(StubFetcher({url: stale}), store, POLICY)

        docs = pipeline.process(rec(conditions_links=[{"text": "d", "url": url}]))

        assert docs[0].status == "stale" and docs[0].doc_id == doc_id
        assert "down" in docs[0].error

    def test_not_modified_reuses_stored_copy_without_converting(self, tmp_path):
        """not_modified reuses the stored Markdown"""
        url = f"{BASE}/smp/wp-call/2026/call-fiche_x.html"
        store = DocumentStore(tmp_path)
        doc_id = store.save(url, "stored markdown", {"role": "call_specific"})
        cached = FetchResult("not_modified", b"<p>ignored</p>", "text/html", from_cache=True)
        pipeline = DocumentPipeline(StubFetcher({url: cached}), store, POLICY)

        docs = pipeline.process(rec(conditions_links=[{"text": "d", "url": url}]))

        assert docs[0].status == "not_modified" and docs[0].doc_id == doc_id
        assert store.load(url).markdown == "stored markdown"

    def test_unsupported_type_is_reported(self, tmp_path):
        """A zip is unsupported_type, not a crash"""
        url = f"{BASE}/smp/wp-call/2026/call-fiche_x.zip"
        fetcher = StubFetcher({url: FetchResult("fresh", b"PK", "application/zip")})
        pipeline = DocumentPipeline(fetcher, DocumentStore(tmp_path), POLICY)

        docs = pipeline.process(rec(conditions_links=[{"text": "d", "url": url}]))

        assert docs[0].status == "unsupported_type"

    def test_too_large_is_skipped(self, tmp_path):
        """Bodies above policy.max_bytes are not converted"""
        url = f"{BASE}/smp/wp-call/2026/call-fiche_x.html"
        tiny_limit = dataclasses.replace(POLICY, max_bytes=10)
        big = FetchResult("fresh", b"x" * 11, "text/html")
        pipeline = DocumentPipeline(StubFetcher({url: big}), DocumentStore(tmp_path), tiny_limit)

        docs = pipeline.process(rec(conditions_links=[{"text": "d", "url": url}]))

        assert docs[0].status == "too_large"


class TestPipelineProfiles:
    """Test DocumentPipeline with a programme profile"""

    def _profiles(self, tmp_path):
        import yaml

        from external_sources.documents.profiles import load_profiles

        (tmp_path / "prog.yaml").write_text(yaml.safe_dump({"programmes": {"p": {"id": "1", "label": "Prog"}}}))
        (tmp_path / "prof.yaml").write_text(yaml.safe_dump(
            {"profiles": {"p": {"skip": [r"legacy\.eu/"]}}}))
        return load_profiles(tmp_path / "prof.yaml", tmp_path / "prog.yaml")

    def test_skip_and_blocked_robots(self, tmp_path):
        """Skip-pattern URLs are never fetched; a robots block maps to blocked_robots"""
        blocked = f"{BASE}/smp/wp-call/2026/call-fiche_x.html"
        fetcher = StubFetcher({blocked: FetchResult("blocked_robots", error="disallowed by robots.txt")})
        pipeline = DocumentPipeline(fetcher, DocumentStore(tmp_path / "s"), POLICY, self._profiles(tmp_path))
        record = rec("A-1", conditions_links=[
            {"text": "Call document", "url": "https://legacy.eu/call-document.pdf"},
            {"text": "Call document", "url": blocked},
        ])
        record = record.model_copy(update={"programme": "Prog"})

        docs = pipeline.process(record)

        assert [d.status for d in docs] == ["skipped_policy", "blocked_robots"]
        assert all(d.doc_id is None for d in docs)
        assert fetcher.requests == [blocked]


class TestGlobalSkipPolicy:
    """Test DocumentPolicy.skip_url_patterns"""

    PORTAL = "https://ec.europa.eu/research/participants/data/ref/h2020/x.pdf"

    def test_real_policy_skips_participant_portal(self):
        """The shipped policy lists the legacy participant portal"""
        assert POLICY.skip_url_patterns

    def test_default_is_empty(self):
        """Constructing directly without the field gives no skip patterns"""
        assert dataclasses.replace(POLICY, skip_url_patterns=()).skip_url_patterns == ()

    def test_pipeline_skips_for_every_programme(self, tmp_path):
        """No profile needed: the URL is skipped_policy and never fetched"""
        fetcher = StubFetcher({})
        pipeline = DocumentPipeline(fetcher, DocumentStore(tmp_path), POLICY)

        docs = pipeline.process(rec(conditions_links=[{"text": "Call document", "url": self.PORTAL}]))

        assert [d.status for d in docs] == ["skipped_policy"]
        assert fetcher.requests == []

    def test_invalid_regex_is_rejected_at_load(self, tmp_path):
        """Bad regexes or non-lists fail loudly"""
        import yaml

        base = yaml.safe_load(open(DEFAULT_POLICY_PATH, encoding="utf-8"))
        for bad in (["("], "x", [1]):
            base["skip_url_patterns"] = bad
            path = tmp_path / "p.yaml"
            path.write_text(yaml.safe_dump(base))
            with pytest.raises(ValueError):
                load_policy(path)


class RaisingFetcher:
    """Fetcher whose get always raises."""

    def get(self, url):
        raise RuntimeError("kaboom")


class TestPipelineRobustness:
    """Test that one bad document never aborts process()"""

    URL = f"{BASE}/smp/wp-call/2026/call-fiche_x.html"

    def test_exception_becomes_error_status(self, tmp_path):
        """An unexpected exception is reported on that document only"""
        pipeline = DocumentPipeline(RaisingFetcher(), DocumentStore(tmp_path), POLICY)

        docs = pipeline.process(rec(conditions_links=[{"text": "d", "url": self.URL}]))

        assert docs[0].status == "error" and "kaboom" in docs[0].error

    def test_corrupt_store_metadata_means_not_stored(self, tmp_path):
        """Garbage store JSON is treated as no stored copy; the document is re-saved"""
        store = DocumentStore(tmp_path)
        doc_id = store.save(self.URL, "old", {"role": "call_specific"})
        (tmp_path / f"{doc_id}.json").write_text("{broken")
        assert store.load(self.URL) is None
        pipeline = DocumentPipeline(StubFetcher({self.URL: html("new")}), store, POLICY)

        docs = pipeline.process(rec(conditions_links=[{"text": "d", "url": self.URL}]))

        assert docs[0].status == "fresh"
        assert "new" in store.load(self.URL).markdown

    def test_too_large_fetch_error_maps_to_too_large(self, tmp_path):
        """The fetcher's size-cap error is reported as too_large"""
        big = FetchResult("error", error="too large (> 5 bytes)")
        pipeline = DocumentPipeline(StubFetcher({self.URL: big}), DocumentStore(tmp_path), POLICY)

        docs = pipeline.process(rec(conditions_links=[{"text": "d", "url": self.URL}]))

        assert docs[0].status == "too_large"
