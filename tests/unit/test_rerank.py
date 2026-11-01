"""
Unit tests for the cross-encoder reranking in the RAG_CISUC retrieval module.
Tests _rerank_pool/_rrf_key in isolation from the ChromaDB/embeddings connection.
The CrossEncoder is faked: the real model is never downloaded in unit tests.
"""

import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RAG_CISUC_DIR = REPO_ROOT / "application" / "RAG_CISUC"
if str(RAG_CISUC_DIR) not in sys.path:
    sys.path.insert(0, str(RAG_CISUC_DIR))

# retrieval.py connects to ChromaDB at import time; point it at an unreachable
# port so that fails fast (connection refused) instead of hanging on DNS/retries.
os.environ.setdefault("CHROMA_HOST", "localhost")
os.environ.setdefault("CHROMA_PORT", "1")
os.environ.setdefault("RAG_MAX_RETRYS", "1")
os.environ.setdefault("RAG_RETRY_DELAY", "0")
os.environ.setdefault("LLM_PROVIDER", "ollama")
os.environ.setdefault("MODEL_EMBEDDINGS", "test-embeddings")
os.environ.setdefault("OLLAMA_URL", "http://localhost:1")
os.environ.setdefault("RAG_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
os.environ.setdefault("RAG_RETRIEVER_K_BM25", "20")
os.environ.setdefault("RAG_RETRIEVER_K_VECTOR", "20")
os.environ.setdefault("RAG_RRF_WEIGHT_BM25", "0.6")
os.environ.setdefault("RAG_RRF_WEIGHT_VECTOR", "0.4")
os.environ.setdefault("RAG_POOL_MULTIPLIER", "2")
os.environ.setdefault("RAG_POOL_MIN_SIZE", "20")
os.environ.setdefault("RAG_QUERY_CACHE_SIZE", "512")

# sentence_transformers (and torch) are heavy and not in the unit-test env.
# Stub the module before retrieval is imported so the real model never loads.
_sentence_transformers_stub = types.ModuleType("sentence_transformers")


class _UnusedCrossEncoder:
    def __init__(self, *args, **kwargs):
        pass


_sentence_transformers_stub.CrossEncoder = _UnusedCrossEncoder
sys.modules["sentence_transformers"] = _sentence_transformers_stub

import retrieval
from langchain_core.documents import Document
from retrieval import _rerank_pool, _rrf_key


class FakeCrossEncoder:
    """Returns a fixed score per chunk text and records the pairs it was asked to score."""

    def __init__(self, scores_by_text):
        self.scores_by_text = scores_by_text
        self.seen_pairs = []

    def predict(self, pairs):
        self.seen_pairs.append(list(pairs))
        return [self.scores_by_text[text] for _, text in pairs]


def doc(text, source_file="a.md"):
    return Document(page_content=text, metadata={"source_file": source_file})


class TestRrfKey:
    def test_key_includes_source_file(self):
        d1 = doc("same text", source_file="a.md")
        d2 = doc("same text", source_file="b.md")

        assert _rrf_key(d1) != _rrf_key(d2)

    def test_key_stable_for_same_doc(self):
        d = doc("some text", source_file="a.md")

        assert _rrf_key(d) == _rrf_key(d)


class TestRerankPool:
    def test_empty_pool_returns_empty(self, monkeypatch):
        fake = FakeCrossEncoder({})
        monkeypatch.setattr(retrieval, "reranker", fake)

        assert _rerank_pool([], "query", top_k=5) == []
        assert fake.seen_pairs == []

    def test_orders_candidates_by_cross_encoder_score(self, monkeypatch):
        a = doc("chunk A")
        b = doc("chunk B")
        c = doc("chunk C")
        fake = FakeCrossEncoder({"chunk A": 0.2, "chunk B": 0.9, "chunk C": 0.5})
        monkeypatch.setattr(retrieval, "reranker", fake)

        ranked = _rerank_pool([a, b, c], "query", top_k=3)

        assert ranked == [b, c, a]

    def test_truncates_to_top_k(self, monkeypatch):
        candidates = [doc(f"content {i}") for i in range(10)]
        fake = FakeCrossEncoder({f"content {i}": float(i) for i in range(10)})
        monkeypatch.setattr(retrieval, "reranker", fake)

        ranked = _rerank_pool(candidates, "topic", top_k=3)

        assert len(ranked) == 3
        assert ranked[0].page_content == "content 9"

    def test_scores_query_chunk_pairs(self, monkeypatch):
        a = doc("chunk A")
        b = doc("chunk B")
        fake = FakeCrossEncoder({"chunk A": 0.1, "chunk B": 0.2})
        monkeypatch.setattr(retrieval, "reranker", fake)

        _rerank_pool([a, b], "my question", top_k=2)

        assert fake.seen_pairs == [
            [("my question", "chunk A"), ("my question", "chunk B")]
        ]


class TestRerankerLock:
    def test_concurrent_predicts_never_overlap(self, monkeypatch):
        state = {"active": 0, "max": 0}
        guard = threading.Lock()

        class SlowReranker:
            def predict(self, pairs):
                with guard:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                time.sleep(0.05)
                with guard:
                    state["active"] -= 1
                return [0.0] * len(pairs)

        monkeypatch.setattr(retrieval, "reranker", SlowReranker())
        threads = [
            threading.Thread(target=_rerank_pool, args=([doc("a")], "q", 1))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert state["max"] == 1


class FakeRetriever:
    """Stands in for the BM25/vector retrievers: counts calls, optionally raises."""

    def __init__(self, docs=None, error=None):
        self.docs = docs or []
        self.error = error
        self.calls = 0

    def invoke(self, query):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.docs)


@pytest.fixture
def engine(monkeypatch):
    docs = [doc(f"chunk {i}") for i in range(5)]
    bm25 = FakeRetriever(docs[:3])
    vector = FakeRetriever(docs[2:])
    reranker = FakeCrossEncoder({f"chunk {i}": float(i) for i in range(5)})
    monkeypatch.setattr(retrieval, "retriever_palavras_chave", bm25)
    monkeypatch.setattr(retrieval, "retriever_vetorial", vector)
    monkeypatch.setattr(retrieval, "reranker", reranker)
    monkeypatch.setattr(retrieval, "QUERY_CACHE_SIZE", 8)
    retrieval._query_cache.clear()
    yield types.SimpleNamespace(bm25=bm25, vector=vector, reranker=reranker)
    retrieval._query_cache.clear()


class TestQueryCache:
    def test_repeated_query_is_served_from_cache(self, engine):
        first = retrieval.get_relevant_chunks("topic", top_k=15)
        second = retrieval.get_relevant_chunks("topic", top_k=15)

        assert second == first
        assert engine.bm25.calls == 1
        assert engine.vector.calls == 1
        assert len(engine.reranker.seen_pairs) == 1

    def test_retriever_error_result_is_not_cached(self, engine):
        engine.vector.error = RuntimeError("embedding service down")
        partial = retrieval.get_relevant_chunks("topic", top_k=15)
        assert partial  # BM25 alone still answers

        engine.vector.error = None
        retrieval.get_relevant_chunks("topic", top_k=15)

        assert engine.bm25.calls == 2  # recomputed, the partial result was not reused

    def test_missing_retriever_result_is_not_cached(self, engine, monkeypatch):
        monkeypatch.setattr(retrieval, "retriever_vetorial", None)

        retrieval.get_relevant_chunks("topic", top_k=15)

        assert len(retrieval._query_cache) == 0

    def test_empty_result_is_not_cached(self, engine):
        engine.bm25.docs = []
        engine.vector.docs = []

        assert retrieval.get_relevant_chunks("topic", top_k=15) == []
        assert len(retrieval._query_cache) == 0

    def test_mutating_a_result_does_not_corrupt_the_cache(self, engine):
        first = retrieval.get_relevant_chunks("topic", top_k=15)
        original_text = first[0]["text"]
        first[0]["text"] = "tampered"
        first.pop()

        again = retrieval.get_relevant_chunks("topic", top_k=15)

        assert again[0]["text"] == original_text
        assert len(again) == 5

    def test_size_zero_disables_the_cache(self, engine, monkeypatch):
        monkeypatch.setattr(retrieval, "QUERY_CACHE_SIZE", 0)

        retrieval.get_relevant_chunks("topic", top_k=15)
        retrieval.get_relevant_chunks("topic", top_k=15)

        assert engine.bm25.calls == 2
        assert len(retrieval._query_cache) == 0

    def test_evicts_least_recently_used(self, engine, monkeypatch):
        monkeypatch.setattr(retrieval, "QUERY_CACHE_SIZE", 2)

        retrieval.get_relevant_chunks("q1", top_k=15)
        retrieval.get_relevant_chunks("q2", top_k=15)
        retrieval.get_relevant_chunks("q1", top_k=15)  # q1 is now the most recent
        retrieval.get_relevant_chunks("q3", top_k=15)  # evicts q2

        assert set(retrieval._query_cache) == {("q1", 15), ("q3", 15)}

    def test_key_distinguishes_top_k_and_case(self, engine):
        retrieval.get_relevant_chunks("topic", top_k=2)
        retrieval.get_relevant_chunks("topic", top_k=3)
        retrieval.get_relevant_chunks("Topic", top_k=3)

        assert engine.bm25.calls == 3
