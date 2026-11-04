"""
`LoadBatch`: everything one source run produced, in a form that can be spooled to disk (JSON)
and loaded later. It is the unit of idempotency: its sha256 identifies the snapshot, so loading
the same batch twice is a no-op.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from external_sources.plugins.base import NormalizedRecord, record_hash
from external_sources.store.rows import canonical_json, sha256_text


@dataclass
class LoadBatch:
    source: str
    run_id: str
    run_started_at: str
    fetched_at: str
    records: list[dict[str, Any]]                     # NormalizedRecord.model_dump()
    documents: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # key -> CallDocument dicts
    failures: list[dict[str, Any]] = field(default_factory=list)              # Failure dicts
    partial_reasons: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    documents_dir: str | None = None                  # DocumentStore root holding the texts

    @property
    def complete(self) -> bool:
        return not self.partial_reasons

    def normalized_records(self) -> list[NormalizedRecord]:
        return [NormalizedRecord.model_validate(r) for r in self.records]

    @property
    def batch_sha256(self) -> str:
        """
        Identity of this load: run, source, records, document outcomes and completeness. Loading
        the same batch again (a resumed or retried run) is a no-op; the same content fetched by a
        NEW run is a new snapshot, so data age and absence counting always advance.
        """
        records = sorted((r.natural_key, record_hash(r), sha256_text(canonical_json(r.raw)))
                         for r in self.normalized_records())
        docs = sorted((key, d.get("url"), d.get("status"), d.get("doc_id"))
                      for key, items in self.documents.items() for d in items)
        return sha256_text(canonical_json({"source": self.source, "run_id": self.run_id, "records": records,
                                           "docs": docs,
                                           "partial": sorted(self.partial_reasons)}))

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, text: str) -> LoadBatch:
        data = json.loads(text)
        return cls(**data)

    def write(self, path: Path) -> Path:
        """Atomic write (temp file + rename) so a crash never leaves a truncated spool file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(self.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        return path

    @classmethod
    def read(cls, path: Path) -> LoadBatch:
        return cls.from_json(path.read_text(encoding="utf-8"))
