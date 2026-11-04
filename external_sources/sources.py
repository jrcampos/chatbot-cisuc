"""
Citable sources of a record, for the chatbot to show the user where each fact comes from.

`build_sources(record, documents, store)` returns `details.sources`: the official EU Funding &
Tenders topic page first (the human page for everything the search API / detail endpoint
supplied), then every stored document that supplied or corroborated a value (with the PDF pages
the values were read on, when and from where it was retrieved, and a content hash), then the
call-specific documents the call links that are readable even if no field was taken from them.
Programme-wide documents nothing was taken from (programme guides, annexes) are not cited.
`render_sources(record)` turns that list into the "Sources" section of the text the chatbot reads.
"""

from __future__ import annotations

import re
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlparse

TOPIC_PAGE = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{identifier}"
)
_PORTAL = re.compile(r"^https://ec\.europa\.eu/info/funding-tenders/opportunities/")
_WORK_PROGRAMME = re.compile(r"(?i)work[-_ ]?programme|/wp-call/|/wp[-_]\d|\bWP\b")
_READABLE = ("fresh", "not_modified", "stale")


def topic_page_url(record: Any) -> str | None:
    """The F&T portal topic page of a record (`.../topic-details/<IDENTIFIER>`), or None off-portal."""
    url = str(getattr(record, "source_url", "") or "")
    if "/portal/screen/opportunities/" in url:  # topic-details/<ID>, competitive-calls-cs/<id>, ...
        return url
    if _PORTAL.match(url) and getattr(record, "natural_key", None):
        return TOPIC_PAGE.format(identifier=record.natural_key)
    return None


def _iso(http_date: str | None) -> str | None:
    if not http_date:
        return None
    try:
        return parsedate_to_datetime(http_date).astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        return None


def _kind(role: str, url: str, title: str) -> str:
    if role == "call_specific":
        return "call_document"
    if _WORK_PROGRAMME.search(url) or _WORK_PROGRAMME.search(title or ""):
        return "work_programme"
    return "programme_document"


def _title_from_url(url: str) -> str:
    path = unquote(urlparse(url).path.rstrip("/"))
    name = path.rsplit("/", 1)[-1] or urlparse(url).netloc
    query = urlparse(url).query
    match = re.search(r"filename=([^&]+)", query)
    return unquote(match.group(1)) if match else name


def _doc_attr(document: Any, name: str) -> Any:
    return document.get(name) if isinstance(document, dict) else getattr(document, name, None)


def build_sources(
    record: Any, documents: list[Any], store: Any, extra_documents: list[Any] | None = None
) -> list[dict[str, Any]]:
    """
    `details.sources` for `record`. `documents` are its own CallDocuments (objects or dicts);
    `extra_documents` are documents it did not link but that supplied values (programme pool).
    """
    sources: list[dict[str, Any]] = []
    portal_fields = sorted(
        f for f, label in (record.field_sources or {}).items() if str(label).split(":", 1)[0] in ("api", "detail")
    )
    topic = topic_page_url(record)
    if topic:
        sources.append({
            "kind": "topic_page", "title": "EU Funding & Tenders Portal topic page", "url": topic,
            "fields": portal_fields,
        })

    used: dict[str, dict[str, Any]] = {}

    def note(doc_id: Any, field: str, page: Any) -> None:
        if not doc_id:
            return
        entry = used.setdefault(str(doc_id), {"fields": set(), "pages": set()})
        entry["fields"].add(field)
        if isinstance(page, int):
            entry["pages"].add(page)

    details = record.details or {}
    for field, evidence in (details.get("evidence") or {}).items():
        if isinstance(evidence, dict):
            note(evidence.get("doc_id"), field, evidence.get("page"))
    for field, entries in (details.get("corroboration") or {}).items():
        for entry in entries or []:
            note(entry.get("doc_id"), field, entry.get("page"))

    known = {}
    for document in list(documents) + list(extra_documents or []):
        doc_id = _doc_attr(document, "doc_id")
        if doc_id and _doc_attr(document, "status") in _READABLE:
            known.setdefault(str(doc_id), document)
    by_url = {str(_doc_attr(d, "url")): doc_id for doc_id, d in known.items()}
    disagrees: dict[str, set[str]] = {}
    for gap in record.data_gaps or []:
        doc_id = by_url.get(str(gap.get("url")))
        if gap.get("kind") == "discrepancy" and doc_id:
            note(doc_id, str(gap.get("field")), None)
            disagrees.setdefault(doc_id, set()).add(str(gap.get("field")))

    order = list(used) + [d for d, doc in known.items()
                          if d not in used and _doc_attr(doc, "role") == "call_specific"]
    for doc_id in order:
        stored = store.load_by_id(doc_id) if store is not None else None
        document = known.get(doc_id)
        url = (_doc_attr(document, "url") if document is not None else None) or (stored.url if stored else None)
        if not url:
            continue
        meta = stored.meta if stored else {}
        role = (_doc_attr(document, "role") if document is not None else None) or meta.get("role") or "other"
        title = (_doc_attr(document, "title") if document is not None else None) or _title_from_url(url)
        info = used.get(doc_id, {"fields": set(), "pages": set()})
        sources.append({
            "kind": _kind(role, url, title), "title": title, "url": url, "role": role, "doc_id": doc_id,
            "fields": sorted(info["fields"]), "pages": sorted(info["pages"]),
            "retrieved_at": meta.get("stored_at"), "last_modified": _iso(meta.get("last_modified")),
            "content_sha256": meta.get("content_sha256"),
        })
        if meta.get("resolved_url"):
            sources[-1]["download_url"] = meta["resolved_url"]
        if doc_id in disagrees:
            sources[-1]["disagrees_on"] = sorted(disagrees[doc_id])
    for index, source in enumerate(sources, 1):
        source["id"] = f"S{index}"
    return sources


def render_sources(record: Any) -> str:
    """'Sources' block body: one numbered, citable line per source (pages and retrieval date when known).
    The `[Sources]` header is added by context.py like every other block."""
    sources = (record.details or {}).get("sources") or []
    if not sources:
        topic = topic_page_url(record) or record.source_url
        return f"[1] EU Funding & Tenders Portal topic page — {topic}"
    lines = []
    for index, source in enumerate(sources, 1):
        extras = []
        if source.get("pages"):
            extras.append("p. " + ", ".join(str(p) for p in source["pages"]))
        if source.get("retrieved_at"):
            extras.append(f"retrieved {str(source['retrieved_at'])[:10]}")
        suffix = f" ({'; '.join(extras)})" if extras else ""
        download = f" (file: {source['download_url']})" if source.get("download_url") else ""
        lines.append(f"[{index}] {source.get('title') or source.get('kind')} — {source['url']}{download}{suffix}")
    return "\n".join(lines)
