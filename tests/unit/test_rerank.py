"""
Unit tests for the post-RRF lexical rescoring in the RAG_CISUC retrieval module.
Tests _rerank_pool/_rrf_key in isolation from the ChromaDB/embeddings connection.
"""

import os
import sys
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

from langchain_core.documents import Document
from retrieval import _rerank_pool, _rrf_key


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
    def test_empty_pool_returns_empty(self):
        assert _rerank_pool([], "query", {}, top_k=5) == []

    def test_lexically_closer_candidate_outranks_worse_match_with_similar_rrf(self):
        # candidate A matches the query lexically; candidate B shares no terms.
        # RRF scores favor B slightly, but not so much that it should dominate
        # a pool of otherwise-unrelated filler documents.
        candidate_a = doc("Raul Barbosa is the leader of the SSE research group")
        candidate_b = doc("Unrelated content about databases and networks")
        filler = [doc(f"Filler document number {i} about unrelated topics") for i in range(3)]

        pool = [candidate_a, candidate_b] + filler
        rrf_scores = {_rrf_key(d): 0.5 for d in pool}
        rrf_scores[_rrf_key(candidate_a)] = 0.4
        rrf_scores[_rrf_key(candidate_b)] = 0.6

        ranked = _rerank_pool(
            pool,
            query="Raul Barbosa SSE group leader",
            rrf_scores=rrf_scores,
            top_k=len(pool),
        )

        assert ranked.index(candidate_a) < ranked.index(candidate_b)

    def test_truncates_to_top_k(self):
        candidates = [doc(f"content about topic {i}") for i in range(10)]
        rrf_scores = {_rrf_key(c): 1.0 for c in candidates}

        ranked = _rerank_pool(candidates, "topic", rrf_scores, top_k=3)

        assert len(ranked) == 3


class TestTokenizer:
    def test_case_and_punctuation_do_not_split_matches(self):
        from retrieval import _tokenizar

        # "Bycatch," in the corpus must match a lowercase query "bycatch"
        assert _tokenizar("Projeto Bycatch, REDUCE!") == ["projeto", "bycatch", "reduce"]
