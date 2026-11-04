"""
Optional, deferred and resumable embedding step (`python -m external_sources.store embed`).

Embeds document chunks and record texts that have no embedding yet, with MODEL_EMBEDDINGS on the
Ollama server in OLLAMA_URL (both read from the environment at run time; the URL is never printed
or stored). When the server is unreachable the step is DEFERRED: it logs that, changes nothing and
exits 0, so the daily run never depends on the VPN. Because only rows without an embedding are
selected, every invocation resumes where the last one stopped. Full-text search never needs it.
No billed API is ever called.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import requests

logger = logging.getLogger(__name__)

DIMENSIONS = 768
BATCH = 32
EXIT_OK, EXIT_DEFERRED_OK = 0, 0


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, session: Any = None, timeout: float = 120.0) -> None:
        self._base = base_url.rstrip("/")
        self.model = model
        self.session = session or requests.Session()
        self.timeout = timeout

    def reachable(self) -> bool:
        try:
            return self.session.get(f"{self._base}/api/version", timeout=5).status_code == 200
        except requests.RequestException:
            return False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = self.session.post(f"{self._base}/api/embed", json={"model": self.model, "input": list(texts)},
                                     timeout=self.timeout)
        response.raise_for_status()
        vectors = response.json().get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("embedding response has the wrong shape")
        for vector in vectors:
            if len(vector) != DIMENSIONS:
                raise ValueError(f"model returned {len(vector)} dimensions, the schema expects {DIMENSIONS}")
        return vectors


def embedder_from_env(env: Mapping[str, str] | None = None) -> OllamaEmbedder | None:
    env = os.environ if env is None else env
    url, model = env.get("OLLAMA_URL"), env.get("MODEL_EMBEDDINGS")
    if not url or not model:
        return None
    return OllamaEmbedder(url, model)


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"


def _has_vector(conn: Any) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('extsrc.record_embedding') IS NOT NULL")
        return bool(cur.fetchone()[0])


def run_embedding(conn: Any, embedder: OllamaEmbedder | None = None, limit: int | None = None,
                  batch: int = BATCH, log: Callable[[str], None] = print, records_only: bool = False) -> int:
    """Embed what is missing; returns 0 (also when deferred: deferral is not an error).

    `records_only` skips the document chunks (tens of thousands of texts) and embeds only the call
    records (a few hundred), which is all the chatbot's semantic call search needs."""
    if not _has_vector(conn):
        log("embedding deferred: pgvector columns are not installed (full-text search only)")
        return EXIT_DEFERRED_OK
    embedder = embedder or embedder_from_env()
    if embedder is None:
        log("embedding deferred: OLLAMA_URL / MODEL_EMBEDDINGS not set in the environment")
        return EXIT_DEFERRED_OK
    if not embedder.reachable():
        log("embedding deferred: the embedding server is not reachable (VPN?); nothing changed")
        return EXIT_DEFERRED_OK
    done = 0
    chunks: list[Any] = []
    with conn.cursor() as cur:
        if not records_only:
            cur.execute(
                """SELECT c.doc_id, c.content_sha256, c.chunk_no, c.text FROM extsrc.document_chunk c
                   JOIN extsrc.document d ON d.doc_id = c.doc_id AND d.latest_sha256 = c.content_sha256
                   WHERE c.embedding IS NULL AND d.role IS DISTINCT FROM 'boilerplate'
                   ORDER BY c.doc_id, c.chunk_no LIMIT %s""", (limit,))
            chunks = cur.fetchall()
    for start in range(0, len(chunks), batch):
        part = chunks[start:start + batch]
        vectors = embedder.embed([row[3] for row in part])
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                """UPDATE extsrc.document_chunk SET embedding = %s::vector, embedding_model = %s
                   WHERE doc_id = %s AND content_sha256 = %s AND chunk_no = %s""",
                [(_vector(v), embedder.model, r[0], r[1], r[2]) for r, v in zip(part, vectors)])
        done += len(part)
    from external_sources.plugins.base import NormalizedRecord
    from external_sources.store.rows import embed_text
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (r.embed_text_sha256) r.embed_text_sha256, r.natural_key, r.title, r.description,
                      r.programme, r.details->>'call_title'
               FROM current.records r
               WHERE r.embed_text_sha256 IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM extsrc.record_embedding e WHERE e.text_sha256 = r.embed_text_sha256 AND e.model = %s)
               LIMIT %s""", (embedder.model, limit))
        records = cur.fetchall()
    for start in range(0, len(records), batch):
        part = records[start:start + batch]
        texts = [embed_text(NormalizedRecord(natural_key=r[1], record_type="call", domain="projects", title=r[2],
                                             description=r[3], programme=r[4], source_url="-", raw={},
                                             details={"call_title": r[5]} if r[5] else {})) for r in part]
        vectors = embedder.embed(texts)
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO extsrc.record_embedding (text_sha256, model, embedding) VALUES (%s, %s, %s::vector)
                   ON CONFLICT DO NOTHING""", [(r[0], embedder.model, _vector(v)) for r, v in zip(part, vectors)])
        done += len(part)
    log(f"embedded {done} item(s) with {embedder.model}")
    return EXIT_OK
