"""
Chaos tests for the document layer through the REAL CachedFetcher + DocumentPipeline, with a fake
transport: huge, binary-garbage, corrupt DOCX, empty and HTML-error bodies, 5xx storms and
network errors. Every document gets a status, the other documents of the call still load, and a
previously stored copy is served (stale) when a refresh fails.
"""

from __future__ import annotations

import io
import zipfile

import requests

from external_sources.documents.pipeline import DocumentPipeline
from external_sources.documents.policy import load_policy
from external_sources.documents.store import DocumentStore
from external_sources.http_cache import CachedFetcher
from external_sources.plugins.base import NormalizedRecord
from external_sources.resilience import ResilientSession, RetryPolicy

BASE = "https://ec.europa.eu/info/funding-tenders/opportunities/docs/2021-2027/x/"
GOOD = "https://www.other-host.eu/good-call-document.html"  # another host: its circuit stays closed


class Resp:
    def __init__(self, status=200, body=b"", content_type="application/pdf", headers=None):
        self.status_code = status
        self.content = body
        self.headers = {"Content-Type": content_type, **(headers or {})}

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        pass


def transport(routes):
    def send(method, url, **kwargs):
        item = routes.get(url, Resp(404))
        if isinstance(item, list):
            item = item.pop(0) if len(item) > 1 else item[0]
        if isinstance(item, BaseException):
            raise item
        return item
    return send


def call_with(urls):
    links = [{"text": "Call document", "url": u} for u in urls]
    return NormalizedRecord(natural_key="SMP-TEST-01", record_type="call", domain="projects", title="t",
                            source_url="https://example.org/t", raw={},
                            details={"call_identifier": "SMP-TEST", "description_links": links})


def pipeline(tmp_path, routes, max_bytes=60_000_000):
    session = ResilientSession(RetryPolicy(max_attempts=3, base_delay=0), sleep=lambda s: None,
                               transport=transport(routes))
    fetcher = CachedFetcher(tmp_path / "cache", session=session, min_interval=0, max_bytes=max_bytes)
    return DocumentPipeline(fetcher, DocumentStore(tmp_path / "docs"), load_policy())


def corrupt_docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document><w:body><w:p>unclosed")
    return buffer.getvalue()


def test_every_bad_document_gets_a_status_and_the_rest_still_load(tmp_path):
    good_html = b"<html><body><h1>Call document</h1><p>The budget is EUR 4 000 000.</p></body></html>"
    routes = {
        BASE + "huge-call-document.pdf": Resp(200, b"%PDF-1.4 " + b"0" * 5000),
        BASE + "garbage-call-document.pdf": Resp(200, b"\x00\xff\xfe binary garbage \x89PNG"),
        BASE + "corrupt-call-document.docx": Resp(
            200, corrupt_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        BASE + "empty-call-document.pdf": Resp(200, b""),
        BASE + "storm-call-document.pdf": Resp(503),
        BASE + "offline-call-document.pdf": requests.ConnectionError("connection reset"),
        GOOD: Resp(200, good_html, "text/html"),
    }
    docs = pipeline(tmp_path, routes, max_bytes=4000).process(call_with(list(routes)))
    status = {d.url.rsplit("/", 1)[1]: d.status for d in docs}
    assert status["huge-call-document.pdf"] == "too_large"
    assert status["garbage-call-document.pdf"] in ("convert_error", "unsupported_type")
    assert status["corrupt-call-document.docx"] in ("convert_error", "unsupported_type")
    assert status["empty-call-document.pdf"] in ("convert_error", "unsupported_type", "fresh")
    assert status["storm-call-document.pdf"] == "error"
    assert status["offline-call-document.pdf"] == "error"
    assert status["good-call-document.html"] == "fresh"
    good = next(d for d in docs if d.status == "fresh" and d.url.endswith(".html"))
    assert good.doc_id and good.chars > 0
    assert all(d.error for d in docs if d.status in ("too_large", "error", "convert_error"))


def test_failed_refresh_serves_the_stored_copy(tmp_path):
    url = BASE + "wp-call-document.html"
    first = pipeline(tmp_path, {url: Resp(200, b"<p>EUR 5 000 000</p>", "text/html")}).process(call_with([url]))
    assert first[0].status == "fresh"
    later = pipeline(tmp_path, {url: Resp(500)}).process(call_with([url]))
    assert later[0].status == "stale" and later[0].doc_id == first[0].doc_id


def test_open_circuit_serves_the_stored_copy_without_a_request(tmp_path):
    url = BASE + "fiche-call-document.html"
    assert pipeline(tmp_path, {url: Resp(200, b"<p>EUR 1</p>", "text/html")}).process(call_with([url]))[0].status \
        == "fresh"
    calls = []

    def down(method, u, **kwargs):
        calls.append(u)
        raise requests.ConnectionError("down")

    session = ResilientSession(RetryPolicy(max_attempts=1), sleep=lambda s: None, transport=down)
    for _ in range(5):  # trip the breaker for the host
        session.http.breakers.get("ec.europa.eu").record_failure()
    fetcher = CachedFetcher(tmp_path / "cache", session=session, min_interval=0)
    docs = DocumentPipeline(fetcher, DocumentStore(tmp_path / "docs"), load_policy()).process(call_with([url]))
    assert docs[0].status == "stale" and "circuit open" in docs[0].error and calls == []
