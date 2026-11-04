"""Unit tests for external_sources.sources (citable sources of a record)."""

from external_sources.documents.pipeline import CallDocument
from external_sources.documents.store import DocumentStore
from external_sources.plugins.base import NormalizedRecord
from external_sources.sources import build_sources, render_sources, topic_page_url

TOPIC = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/"
FICHE = "https://ec.europa.eu/info/funding-tenders/opportunities/docs/2021-2027/smp/wp-call/2026/call-fiche_x.pdf"
WP = "https://ec.europa.eu/info/funding-tenders/opportunities/docs/2021-2027/horizon/wp-call/2026-2027/wp-7_en.pdf"
GUIDE = "https://ec.europa.eu/info/funding-tenders/opportunities/docs/2021-2027/horizon/guidance/programme-guide_en.pdf"


def record(**kwargs):
    base = dict(natural_key="SMP-FOOD-2026-FW-STAKEHOLDERS-PJ", record_type="call", domain="projects",
                programme="Single Market Programme", title="Food waste", source_url=TOPIC + "SMP-FOOD-2026-FW-STAKEHOLDERS-PJ",
                raw={}, field_sources={"title": "api", "description": "api", "deadline_date": "api"})
    base.update(kwargs)
    return NormalizedRecord(**base)


def stored(tmp_path, url, role):
    store = DocumentStore(tmp_path)
    doc_id = store.save(url, "<!-- page 1 -->\ntext\n", {"role": role, "last_modified": "Tue, 29 Sep 2026 10:00:00 GMT"})
    return store, CallDocument(url, role, "Call document" if role == "call_specific" else "Work programme",
                               "fresh", doc_id, 20)


class TestTopicPageUrl:
    def test_portal_page_is_kept(self):
        assert topic_page_url(record()) == TOPIC + "SMP-FOOD-2026-FW-STAKEHOLDERS-PJ"

    def test_api_path_is_rewritten_to_the_portal_page(self):
        api = "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/SMP-X.json"
        assert topic_page_url(record(natural_key="SMP-X", source_url=api)) == TOPIC + "SMP-X"

    def test_non_portal_record_has_none(self):
        assert topic_page_url(record(source_url="https://keep.eu/projects/1")) is None


class TestBuildSources:
    def test_topic_page_comes_first_with_the_api_fields(self, tmp_path):
        store, _ = stored(tmp_path, FICHE, "call_specific")

        sources = build_sources(record(), [], store)

        assert sources[0]["kind"] == "topic_page" and sources[0]["id"] == "S1"
        assert sources[0]["url"] == TOPIC + "SMP-FOOD-2026-FW-STAKEHOLDERS-PJ"
        assert sources[0]["fields"] == ["deadline_date", "description", "title"]

    def test_document_that_supplied_values_is_cited_with_pages_and_hash(self, tmp_path):
        store, doc = stored(tmp_path, FICHE, "call_specific")
        rec = record(funding_amount=4e6, field_sources={"funding_amount": f"document:{doc.doc_id}"}, details={
            "evidence": {"funding_amount": {"doc_id": doc.doc_id, "url": FICHE, "page": 8},
                         "details.budget_statement": {"doc_id": doc.doc_id, "url": FICHE, "page": 9}}})

        source = build_sources(rec, [doc], store)[1]

        assert source["kind"] == "call_document" and source["doc_id"] == doc.doc_id
        assert source["fields"] == ["details.budget_statement", "funding_amount"]
        assert source["pages"] == [8, 9]
        assert len(source["content_sha256"]) == 64 and source["retrieved_at"]
        assert source["last_modified"] == "2026-09-29T10:00:00+00:00"
        assert source["title"] == "Call document"

    def test_landing_page_document_cites_the_downloaded_file(self, tmp_path):
        store = DocumentStore(tmp_path)
        page = "https://cybersecurity-centre.europa.eu/document/8e1d_en"
        doc_id = store.save(page, "text", {"role": "call_specific", "resolved_url": page + "?filename=Call.pdf"})
        doc = CallDocument(page, "call_specific", "call document", "fresh", doc_id, 4)

        source = build_sources(record(), [doc], store)[1]

        assert source["url"] == page and source["download_url"] == page + "?filename=Call.pdf"

    def test_document_whose_value_disagrees_is_still_cited(self, tmp_path):
        """A range must be able to cite both sides: the disagreeing document is a source too"""
        store, doc = stored(tmp_path, FICHE, "programme_level")
        rec = record(funding_amount=4e6, data_gaps=[{"field": "funding_amount", "kind": "discrepancy",
                                                     "existing": 4e6, "document": 5e6, "url": FICHE}])

        source = build_sources(rec, [doc], store)[1]

        assert source["url"] == FICHE and source["fields"] == ["funding_amount"]
        assert source["disagrees_on"] == ["funding_amount"]

    def test_corroborating_document_is_cited(self, tmp_path):
        store, doc = stored(tmp_path, WP, "programme_level")
        rec = record(funding_amount=4e6, details={"corroboration": {"funding_amount": [
            {"doc_id": doc.doc_id, "url": WP, "page": 112, "value": 4e6}]}})

        source = build_sources(rec, [], store, extra_documents=[doc])[1]

        assert source["kind"] == "work_programme" and source["pages"] == [112]
        assert source["fields"] == ["funding_amount"]

    def test_unused_programme_document_is_not_cited_but_a_call_document_is(self, tmp_path):
        store, guide = stored(tmp_path, GUIDE, "programme_level")
        _, fiche = stored(tmp_path, FICHE, "call_specific")

        kinds = [s["kind"] for s in build_sources(record(), [guide, fiche], store)]

        assert kinds == ["topic_page", "call_document"]

    def test_unreadable_document_is_not_cited(self, tmp_path):
        store = DocumentStore(tmp_path)
        broken = CallDocument(FICHE, "call_specific", "Call document", "not_found")

        assert [s["kind"] for s in build_sources(record(), [broken], store)] == ["topic_page"]


class TestRender:
    def test_render_numbers_sources_and_names_pages(self, tmp_path):
        store, doc = stored(tmp_path, FICHE, "call_specific")
        rec = record(field_sources={"funding_amount": f"document:{doc.doc_id}", "title": "api"},
                     funding_amount=4e6, details={"evidence": {"funding_amount": {"doc_id": doc.doc_id, "page": 8}}})
        rec.details["sources"] = build_sources(rec, [doc], store)

        text = render_sources(rec)

        assert text.startswith("[1] EU Funding & Tenders Portal topic page")
        assert "[2] Call document" in text and "p. 8" in text and FICHE in text

    def test_render_without_sources_falls_back_to_the_topic_page(self):
        assert TOPIC in render_sources(record())
