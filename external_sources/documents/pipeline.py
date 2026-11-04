"""
Fetch every document a call links to, convert it, store it once.

CallDocument.status values: fresh | not_modified | stale (fetch failed, previous
copy used) | not_found | error | too_large | unsupported_type | convert_error
| skipped_role (role not in policy.fetch_roles) | skipped_policy (URL matches the
policy's `skip_url_patterns` or the programme profile's `skip` patterns; never fetched) | blocked_robots (the host
answered 403 "Disallowed by robots.txt"; doc_id stays None). The last two are
policy outcomes, not failures.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from external_sources.documents.convert import UnsupportedContentType, convert
from external_sources.documents.policy import DocumentPolicy
from external_sources.documents.roles import classify_url, discover_links
from external_sources.documents.store import DocumentStore
from external_sources.plugins.base import NormalizedRecord


@dataclass
class CallDocument:
    """One document as seen from one call."""

    url: str
    role: str
    title: str
    status: str
    doc_id: str | None = None
    chars: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict for JSON output."""
        return asdict(self)


class DocumentPipeline:
    """
    Discover → classify → fetch → convert → store, memoised per URL within a run
    so a shared document (e.g. the Terms & Conditions linked by ~every call) is
    downloaded and converted once.
    """

    def __init__(
        self, fetcher: Any, store: DocumentStore, policy: DocumentPolicy, profiles: Any = None
    ) -> None:
        self.fetcher = fetcher
        self.profiles = profiles
        self.store = store
        self.policy = policy
        self._memo: dict[str, dict[str, Any]] = {}
        self._skip = tuple(re.compile(p) for p in policy.skip_url_patterns)
        self._download = tuple(re.compile(p, re.IGNORECASE) for p in policy.follow_download_text_patterns)

    def process(self, record: NormalizedRecord) -> list[CallDocument]:
        """Materialise every fetchable document linked from `record`."""
        call_identifier = str(record.details.get("call_identifier") or "")
        documents: list[CallDocument] = []
        profile = self.profiles.for_programme(record.programme) if self.profiles else None
        for link in discover_links(record, self.policy):
            role = classify_url(
                link.url, record.natural_key, call_identifier, self.policy,
                text=link.text, profile=profile,
            )
            if role == "ignore":
                continue
            if any(p.search(link.url) for p in self._skip) or (
                profile is not None and profile.is_skipped(link.url)
            ):
                documents.append(CallDocument(link.url, role, link.text, "skipped_policy"))
                continue
            if role not in self.policy.fetch_roles:
                documents.append(CallDocument(link.url, role, link.text, "skipped_role"))
                continue
            try:
                state = self._materialise(link.url, role)
            except Exception as exc:  # noqa: BLE001 — one bad document must not stop the run
                state = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            documents.append(CallDocument(link.url, role, link.text, **state))
        return documents

    def _materialise(self, url: str, role: str) -> dict[str, Any]:
        if url in self._memo:
            return self._memo[url]
        result = self.fetcher.get(url)
        existing = self.store.load(url)
        if result.status == "blocked_robots":
            state: dict[str, Any] = {"status": "blocked_robots", "error": result.error}
        elif result.body is None and (result.error or "").startswith("too large"):
            state = {"status": "too_large", "error": result.error}
        elif result.body is None:
            state = {
                "status": "not_found" if result.status == "not_found" else "error",
                "error": result.error,
            }
        elif len(result.body) > self.policy.max_bytes:
            state = {"status": "too_large", "error": f"{len(result.body)} bytes"}
        elif existing and result.status in ("not_modified", "error"):
            state = {
                "status": "not_modified" if result.status == "not_modified" else "stale",
                "doc_id": existing.doc_id, "chars": len(existing.markdown),
                "error": result.error,
            }
        else:
            resolved = None
            if role in ("call_specific", "programme_level"):
                target = self._download_link(url, result)
                if target is not None:
                    followed = self.fetcher.get(target)
                    if followed.body is not None and len(followed.body) <= self.policy.max_bytes:
                        result, resolved = followed, target
            try:
                markdown = convert(result.body, result.content_type, resolved or url)
            except UnsupportedContentType as exc:
                state = {"status": "unsupported_type", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 — one bad document must not stop the run
                state = {"status": "convert_error", "error": f"{type(exc).__name__}: {exc}"}
            else:
                meta = {"role": role, "content_type": result.content_type,
                        "etag": result.etag, "last_modified": result.last_modified}
                if resolved:
                    meta["resolved_url"] = resolved
                doc_id = self.store.save(url, markdown, meta)
                state = {
                    "status": "stale" if result.status == "error" else "fresh",
                    "doc_id": doc_id, "chars": len(markdown), "error": result.error,
                }
        self._memo[url] = state
        return state

    def _download_link(self, url: str, result: Any) -> str | None:
        """Same-host target of a landing page's 'Download (... PDF)' link, else None."""
        if not self._download or "html" not in (result.content_type or "").lower():
            return None
        soup = BeautifulSoup(result.body, "html.parser")
        for anchor in soup.find_all("a", href=True):
            text = " ".join(anchor.get_text(" ", strip=True).split())
            if any(p.search(text) for p in self._download):
                target = urljoin(url, anchor["href"])
                if urlparse(target).netloc == urlparse(url).netloc and target != url:
                    return target
                return None
        return None
