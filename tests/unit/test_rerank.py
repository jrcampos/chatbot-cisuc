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
