"""
Failure rules shared by RAG_CISUC and RAG_CALLS: the Orchestrator's RAG client
turns any RAG failure into HTTP 502, the calls API answers 500 on failure (never an empty list) and
/health is 503 until warm.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
for _name, _value in {"LLM_PROVIDER": "ollama", "CALLS_RETRIEVER_K_LEXICAL": "20", "CALLS_RETRIEVER_K_VECTOR": "20",
                      "CALLS_RRF_WEIGHT_LEXICAL": "0.6", "CALLS_RRF_WEIGHT_VECTOR": "0.4",
                      "CALLS_POOL_MULTIPLIER": "2", "CALLS_POOL_MIN_SIZE": "20", "CALLS_RERANKER_MODEL": "fake/model",
                      "CALLS_QUERY_CACHE_SIZE": "0", "CALLS_DETAILED": "1", "CALLS_DETAILED_MAX_CHARS": "4000",
                      "RAG_REQUEST_TIMEOUT": "5", "ORCH_RAG_MAX_PARALLEL": "1"}.items():
    os.environ.setdefault(_name, _value)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeCrossEncoder:
    """Stands in for sentence_transformers.CrossEncoder (no model download in unit tests); keeps the
    given order: earlier pairs score higher."""

    def __init__(self, *args, **kwargs):
        pass

    def predict(self, pairs):
        return [float(-i) for i in range(len(pairs))]


def _load_without_model(name, path):
    """Load a module with sentence_transformers replaced by the fake cross-encoder during its import."""
    import types
    fake = types.ModuleType("sentence_transformers")
    fake.CrossEncoder = _FakeCrossEncoder
    saved = sys.modules.get("sentence_transformers")
    sys.modules["sentence_transformers"] = fake
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop("sentence_transformers", None)
        else:
            sys.modules["sentence_transformers"] = saved
    return module


rag_client = load("orchestrator_rag_client", REPO_ROOT / "application" / "Orchestrator" / "rag_client.py")


class Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class TestRagClient:
    def test_any_failure_is_502(self, monkeypatch):
        for outcome in (requests.ConnectionError("down"), Resp({}, 500), Resp({"no_results": []})):
            def post(url, json, timeout, outcome=outcome):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            monkeypatch.setattr(rag_client.requests, "post", post)
            with pytest.raises(HTTPException) as err:
                rag_client.pesquisar("http://rag_calls:8003/query", "quantum", 8, 5)
            assert err.value.status_code == 502

    def test_results_keep_query_order_in_parallel(self, monkeypatch):
        monkeypatch.setattr(rag_client, "MAX_PARALLEL", 3)
        monkeypatch.setattr(rag_client.requests, "post",
                            lambda url, json, timeout: Resp({"results": [{"text": json["query"]}]}))
        lists = rag_client.pesquisar_varias("http://x/query", ["full question", "kw1", "kw2"], 8)
        assert [lst[0]["text"] for lst in lists] == ["full question", "kw1", "kw2"]


@pytest.fixture
def calls_api(monkeypatch):
    """application/RAG_CALLS/script.py with its own `retrieval` module (RAG_CISUC has one too)."""
    retrieval = _load_without_model("rag_calls_retrieval_api", REPO_ROOT / "application" / "RAG_CALLS" / "retrieval.py")
    monkeypatch.setitem(sys.modules, "retrieval", retrieval)
    script = load("rag_calls_script", REPO_ROOT / "application" / "RAG_CALLS" / "script.py")
    return script


class TestCallsApi:
    def test_query_failure_is_500_not_an_empty_list(self, calls_api, monkeypatch):
        def broken(query, top_k):
            raise ConnectionError("database unreachable")
        monkeypatch.setattr(calls_api, "get_relevant_calls", broken)
        with TestClient(calls_api.app, raise_server_exceptions=False) as client:
            assert client.post("/query", json={"query": "quantum", "top_k": 3}).status_code == 500

    def test_health_is_503_until_warm(self, calls_api, monkeypatch):
        def broken(query, top_k):
            raise ConnectionError("database unreachable")
        monkeypatch.setattr(calls_api, "get_relevant_calls", broken)  # warm-up fails
        with TestClient(calls_api.app) as client:
            assert client.get("/health").status_code == 503

    def test_health_ok_after_warm_up(self, calls_api, monkeypatch):
        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        monkeypatch.setattr(calls_api, "get_relevant_calls", lambda query, top_k: [])
        monkeypatch.setattr(calls_api, "connect_reader", Conn)
        monkeypatch.setattr(calls_api, "source_freshness", lambda conn: [{"source": "ft_portal"}])
        with TestClient(calls_api.app) as client:
            response = client.get("/health")
        assert response.status_code == 200 and response.json()["status"] == "ok"
