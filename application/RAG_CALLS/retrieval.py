"""
EU Funding Calls Hybrid Retrieval Engine

Same shape as RAG_CISUC/retrieval.py, over the external-sources store (Postgres + pgvector)
instead of ChromaDB:
- Lexical search: Postgres full-text search over the live calls (any-term match, like BM25).
- Semantic search: pgvector cosine search over the call embeddings (MODEL_EMBEDDINGS on Ollama).

Both candidate lists are fused with the same weighted Reciprocal Rank Fusion as RAG_CISUC; a call
or topic identifier written in the query is looked up exactly and goes first. Each
result is one call rendered as a bounded, cited text block (external_sources/context.py): the
first CALLS_DETAILED calls in full, the rest as compact list entries. Only open and forthcoming
calls are searched; the read-only login and statement timeout come from the store settings.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

from external_sources.store.embed import OllamaEmbedder, embedder_from_env
from external_sources.store.queries import (
    CallQuery, connect_reader, render_context, search_calls, vector_search_calls,
)

# ===== Configuration from Environment =====
LLM_PROVIDER: str = os.environ["LLM_PROVIDER"].lower()
CANDIDATES: int = int(os.environ["CALLS_CANDIDATES"])
DETAILED: int = int(os.environ["CALLS_DETAILED"])
DETAILED_MAX_CHARS: int = int(os.environ["CALLS_DETAILED_MAX_CHARS"])

# Same fusion weights as RAG_CISUC.
retriever_weights: dict[str, float] = {
    "lexical": 0.6,
    "vector": 0.4,
}

# The call embeddings are made with MODEL_EMBEDDINGS on Ollama (store `embed` step); with another
# provider the semantic side is off and retrieval is lexical only.
embedder: OllamaEmbedder | None = embedder_from_env() if LLM_PROVIDER == "ollama" else None
print(f"[INFO] Motor de concursos: lexical + {'vetorial (' + embedder.model + ')' if embedder else 'sem vetores'}")


def fuse(ranked_lists: list[tuple[list[str], float]]) -> list[str]:
    """Weighted RRF, as in RAG_CISUC: score = weight * 1 / (rank + 1), summed over lists."""
    scores: dict[str, float] = {}
    for keys, weight in ranked_lists:
        for rank, key in enumerate(keys):
            scores[key] = scores.get(key, 0.0) + weight * (1.0 / (rank + 1))
    return [key for key, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


# Call / topic identifiers (HORIZON-CL4-2027-04-DIGITAL-EMERGING-05, EuropeAid/187686/ID/ACT/MK):
# full-text search splits them into words, so an exact lookup puts the named call first.
_IDENTIFIER = re.compile(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+){2,}")


def _exact(conn: Any, query: str) -> list[str]:
    keys: list[str] = []
    for token in dict.fromkeys(_IDENTIFIER.findall(query)):
        for hit in search_calls(conn, CallQuery(identifier=token, limit=CANDIDATES)):
            if hit["natural_key"] not in keys:
                keys.append(hit["natural_key"])
    return keys


def _lexical(conn: Any, query: str) -> list[str]:
    hits = search_calls(conn, CallQuery(text=query, text_any=True, order="relevance", limit=CANDIDATES))
    return [hit["natural_key"] for hit in hits]


def _vector(conn: Any, query: str) -> list[str]:
    if embedder is None:
        return []
    try:
        vector = embedder.embed([query])[0]
    except Exception as e:  # noqa: BLE001 - embedding server down: lexical results still answer
        print(f"[ERRO VETORES] {e}")
        return []
    return [key for key, _ in vector_search_calls(conn, vector, embedder.model, limit=CANDIDATES)]


def _metadata(hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "funding_call",
        "source_file": hit["source"],
        "natural_key": hit["natural_key"],
        "title": hit["title"],
        "programme": hit["programme"],
        "status": hit["status"],
        "next_deadline": hit["next_deadline"],
        "url": hit["url"],
        "data_as_of": hit["freshness"]["data_as_of"],
    }


def get_relevant_calls(query: str, top_k: int, connect: Callable[[], Any] = connect_reader) -> list[dict[str, Any]]:
    """
    Hybrid search over the live calls; returns up to `top_k` dicts with 'text' and 'metadata'
    (the RAG_CISUC result shape).
    """
    query = query.replace("'", "").replace('"', "").strip()
    print(f"\n[RAG CALLS DEBUG] A procurar a query: '{query}'")
    if not query or top_k < 1:
        return []
    with connect() as conn:
        exact = _exact(conn, query)
        lexical = _lexical(conn, query)
        vector = _vector(conn, query)
        print(f"[RAG CALLS DEBUG] Exatos: {len(exact)} | Lexical: {len(lexical)} | Vetores: {len(vector)}")
        fused = fuse([(lexical, retriever_weights["lexical"]), (vector, retriever_weights["vector"])])
        keys = (exact + [key for key in fused if key not in exact])[:top_k]
        if not keys:
            return []
        hits = {hit["natural_key"]: hit for hit in search_calls(conn, CallQuery(natural_keys=keys, limit=len(keys)))}

    results: list[dict[str, Any]] = []
    for key in keys:
        hit = hits.get(key)
        if hit is None:  # closed or unpublished between the two queries
            continue
        if len(results) < DETAILED:
            pack = render_context(hit, "explain_call", max_chars=DETAILED_MAX_CHARS)
        else:
            pack = render_context(hit, "list_calls")
        results.append({"text": pack.text, "metadata": _metadata(hit)})
    return results
