"""
EU Funding Calls Hybrid Retrieval Engine

Mirrors RAG_CISUC/retrieval.py, over the external-sources store (Postgres + pgvector) instead of
ChromaDB:
- Lexical search: Postgres full-text search over the live calls (any-term match, like BM25).
- Semantic search: pgvector cosine search over the call embeddings (MODEL_EMBEDDINGS on Ollama).

Both candidate lists are fused with weighted Reciprocal Rank Fusion, the pool is widened past the
fusion and rescored by the same cross-encoder reranker as RAG_CISUC. A call or topic identifier
written in the query is looked up exactly and goes first, before the reranked calls. Complete,
non-empty results are kept in an LRU cache. Each result is one call rendered as a bounded, cited
text block (external_sources/context.py): the first CALLS_DETAILED calls in full, the rest compact.
"""
from __future__ import annotations

import copy
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from sentence_transformers import CrossEncoder

from external_sources.store.embed import OllamaEmbedder, embedder_from_env
from external_sources.store.queries import (
    CallQuery, connect_reader, render_context, search_calls, vector_search_calls,
)

# ===== Configuration from Environment (same knobs as RAG_CISUC, CALLS_ prefix) =====
LLM_PROVIDER: str = os.environ["LLM_PROVIDER"].lower()
RETRIEVER_K_LEXICAL: int = int(os.environ["CALLS_RETRIEVER_K_LEXICAL"])
RETRIEVER_K_VECTOR: int = int(os.environ["CALLS_RETRIEVER_K_VECTOR"])
RRF_WEIGHT_LEXICAL: float = float(os.environ["CALLS_RRF_WEIGHT_LEXICAL"])
RRF_WEIGHT_VECTOR: float = float(os.environ["CALLS_RRF_WEIGHT_VECTOR"])
POOL_MULTIPLIER: int = int(os.environ["CALLS_POOL_MULTIPLIER"])
POOL_MIN_SIZE: int = int(os.environ["CALLS_POOL_MIN_SIZE"])
RERANKER_MODEL: str = os.environ["CALLS_RERANKER_MODEL"]
QUERY_CACHE_SIZE: int = int(os.environ["CALLS_QUERY_CACHE_SIZE"])  # 0 disables the cache
DETAILED: int = int(os.environ["CALLS_DETAILED"])
DETAILED_MAX_CHARS: int = int(os.environ["CALLS_DETAILED_MAX_CHARS"])
RERANK_PASSAGE_CHARS = 2000  # the cross-encoder reads at most 512 tokens anyway

retriever_weights: dict[str, float] = {"lexical": RRF_WEIGHT_LEXICAL, "vector": RRF_WEIGHT_VECTOR}

# The call embeddings are made with MODEL_EMBEDDINGS on Ollama (store `embed` step); with another
# provider the semantic side is off and retrieval is lexical only.
embedder: OllamaEmbedder | None = embedder_from_env() if LLM_PROVIDER == "ollama" else None
print(f"[INFO] Motor de concursos: lexical + {'vetorial (' + embedder.model + ')' if embedder else 'sem vetores'}")

# Cross-encoder loaded once at import, CPU only; max_length caps the per-pair cost (as in RAG_CISUC).
print(f"[INFO] A carregar o reranker: {RERANKER_MODEL}")
reranker = CrossEncoder(RERANKER_MODEL, max_length=512)
_reranker_lock = threading.Lock()


def fuse(ranked_lists: list[tuple[list[str], float]]) -> list[str]:
    """Weighted RRF, as in RAG_CISUC: score = weight * 1 / (rank + 1), summed over lists."""
    scores: dict[str, float] = {}
    for keys, weight in ranked_lists:
        for rank, key in enumerate(keys):
            scores[key] = scores.get(key, 0.0) + weight * (1.0 / (rank + 1))
    return [key for key, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


# Call / topic identifiers (HORIZON-CL4-2027-04-DIGITAL-EMERGING-05, EuropeAid/187686/ID/ACT/MK):
# full-text search splits them into words, so an exact lookup puts the named call first.
# Parsed with plain string operations, linear in the query length: a regex over the raw query
# backtracks quadratically on long runs of letters/digits (CodeQL py/polynomial-redos).
_EDGE_PUNCTUATION = ".,;:!?()[]{}<>\"'«»“”‘’"
MAX_IDENTIFIER_CHARS = 120
MAX_IDENTIFIERS = 5


def identifiers(query: str) -> list[str]:
    """Whitespace-separated tokens made of >= 3 ASCII alphanumeric parts joined by '-' or '/'."""
    found: list[str] = []
    for raw in query.split():
        token = raw.strip(_EDGE_PUNCTUATION)
        if not token or len(token) > MAX_IDENTIFIER_CHARS or token in found:
            continue
        parts = token.replace("/", "-").split("-")
        if len(parts) >= 3 and all(part.isascii() and part.isalnum() for part in parts):
            found.append(token)
            if len(found) == MAX_IDENTIFIERS:
                break
    return found


def _exact(conn: Any, query: str) -> list[str]:
    keys: list[str] = []
    for token in identifiers(query):
        for hit in search_calls(conn, CallQuery(identifier=token, limit=RETRIEVER_K_LEXICAL)):
            if hit["natural_key"] not in keys:
                keys.append(hit["natural_key"])
    return keys


def _lexical(conn: Any, query: str) -> list[str]:
    hits = search_calls(conn, CallQuery(text=query, text_any=True, order="relevance", limit=RETRIEVER_K_LEXICAL))
    return [hit["natural_key"] for hit in hits]


def _vector(conn: Any, query: str) -> list[str]:
    if embedder is None:
        return []
    vector = embedder.embed([query])[0]
    return [key for key, _ in vector_search_calls(conn, vector, embedder.model, limit=RETRIEVER_K_VECTOR)]


def _passage(hit: dict[str, Any]) -> str:
    record = hit["_record"]
    parts = [hit["title"], str(record["details"].get("call_title") or ""), hit["programme"] or "",
             (record["description"] or "")[:RERANK_PASSAGE_CHARS]]
    return "\n".join(p for p in parts if p)


def _rerank(query: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score each (query, call) pair with the cross-encoder; best first."""
    if len(hits) < 2:
        return hits
    # One predict at a time: concurrent predicts only fight over the same CPU threads.
    with _reranker_lock:
        scores = reranker.predict([(query, _passage(hit)) for hit in hits])
    return [hit for hit, _ in sorted(zip(hits, scores), key=lambda pair: float(pair[1]), reverse=True)]


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


# LRU cache of final results per (query, top_k), as in RAG_CISUC: only complete results (every
# retriever answered) are cached, callers get copies, and it is invalidated only by a restart.
# The calls change once per load; the cache entries are small next to a cross-encoder pass.
_query_cache: OrderedDict[tuple[str, int], list[dict[str, Any]]] = OrderedDict()
_query_cache_lock = threading.Lock()


def _cache_get(key: tuple[str, int]) -> list[dict[str, Any]] | None:
    if QUERY_CACHE_SIZE <= 0:
        return None
    with _query_cache_lock:
        hit = _query_cache.get(key)
        if hit is not None:
            _query_cache.move_to_end(key)
    return copy.deepcopy(hit) if hit is not None else None


def _cache_put(key: tuple[str, int], results: list[dict[str, Any]]) -> None:
    if QUERY_CACHE_SIZE <= 0:
        return
    entry = copy.deepcopy(results)
    with _query_cache_lock:
        _query_cache[key] = entry
        _query_cache.move_to_end(key)
        while len(_query_cache) > QUERY_CACHE_SIZE:
            _query_cache.popitem(last=False)


def get_relevant_calls(query: str, top_k: int, connect: Callable[[], Any] = connect_reader) -> list[dict[str, Any]]:
    """
    Hybrid search over the live calls; returns up to `top_k` dicts with 'text' and 'metadata'
    (the RAG_CISUC result shape). A database failure raises (the API answers 500); a failed
    embedding call leaves a lexical-only result that is returned but never cached.
    """
    query = query.replace("'", "").replace('"', "").strip()
    print(f"\n[RAG CALLS DEBUG] A procurar a query: '{query}'")
    if not query or top_k < 1:
        return []
    cache_key = (query, top_k)
    cached = _cache_get(cache_key)
    if cached is not None:
        print(f"[RAG CALLS DEBUG] cache HIT ({len(cached)} concursos).")
        return cached

    complete = True
    with connect() as conn:
        exact = _exact(conn, query)
        lexical = _lexical(conn, query)
        vector: list[str] = []
        try:
            vector = _vector(conn, query)
        except Exception as e:  # noqa: BLE001 - embedding server down: lexical results still answer
            complete = False
            print(f"[ERRO VETORES] {e}")
        fused = [key for key in fuse([(lexical, retriever_weights["lexical"]), (vector, retriever_weights["vector"])])
                 if key not in exact]
        pool_keys = fused[:max(top_k * POOL_MULTIPLIER, POOL_MIN_SIZE)]
        wanted = exact + pool_keys
        hits: dict[str, dict[str, Any]] = {}
        if wanted:
            for hit in search_calls(conn, CallQuery(natural_keys=wanted, limit=len(wanted))):
                hits[hit["natural_key"]] = hit
    print(f"[RAG CALLS DEBUG] Exatos: {len(exact)} | Lexical: {len(lexical)} | Vetores: {len(vector)} | pool: {len(pool_keys)}")

    started = time.time()
    ranked = [hits[k] for k in exact if k in hits] + _rerank(query, [hits[k] for k in pool_keys if k in hits])
    print(f"[RAG CALLS DEBUG] rerank: {time.time() - started:.2f}s")
    results: list[dict[str, Any]] = []
    for hit in ranked[:top_k]:
        if len(results) < DETAILED:
            pack = render_context(hit, "explain_call", max_chars=DETAILED_MAX_CHARS)
        else:
            pack = render_context(hit, "list_calls")
        results.append({"text": pack.text, "metadata": _metadata(hit)})

    if complete and results:
        _cache_put(cache_key, results)
    return results
