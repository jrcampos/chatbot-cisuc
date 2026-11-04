"""File-based document store: `<doc_id>.md` plus `<doc_id>.json` metadata."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class StoredDocument:
    """A stored document with its Markdown and metadata."""

    doc_id: str
    url: str
    markdown: str
    meta: dict[str, Any]


class DocumentStore:
    """One directory of Markdown documents keyed by a hash of their URL."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def doc_id_for(url: str) -> str:
        """Stable document id derived from the URL."""
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]

    def save(self, url: str, markdown: str, meta: dict[str, Any]) -> str:
        """Write the Markdown and its metadata; returns the doc_id."""
        doc_id = self.doc_id_for(url)
        record = {
            **meta,
            "url": url,
            "doc_id": doc_id,
            "chars": len(markdown),
            "content_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "stored_at": datetime.now(UTC).isoformat(),
        }
        (self.root / f"{doc_id}.md").write_text(markdown, encoding="utf-8")
        (self.root / f"{doc_id}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return doc_id

    def load_by_id(self, doc_id: str) -> StoredDocument | None:
        """Load a stored document by id, or None."""
        md_path, meta_path = self.root / f"{doc_id}.md", self.root / f"{doc_id}.json"
        if not md_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return StoredDocument(doc_id, meta["url"], md_path.read_text(encoding="utf-8"), meta)
        except (ValueError, OSError, KeyError, TypeError):
            return None  # unreadable store entry == not stored

    def load(self, url: str) -> StoredDocument | None:
        """Load a stored document by URL, or None."""
        return self.load_by_id(self.doc_id_for(url))
