"""
Unit tests for the Orchestrator's RAG client (sequential/parallel queries, strict failures).
requests.post is faked: no RAG service is needed.
"""

import os
import sys
import threading
import time
from pathlib import Path

import pytest
import requests
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_DIR = REPO_ROOT / "application" / "Orchestrator"
if str(ORCHESTRATOR_DIR) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_DIR))

os.environ.setdefault("RAG_API_URL", "http://rag.test/query")
os.environ.setdefault("RAG_TOP_K", "15")
os.environ.setdefault("RAG_REQUEST_TIMEOUT", "30")
os.environ.setdefault("ORCH_RAG_MAX_PARALLEL", "1")

import rag_client


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} error")

    def json(self):
        return self.payload


@pytest.fixture
def fake_post(monkeypatch):
    """Records every call; each query returns [{"text": <query>}], optionally delayed."""
    calls = []
    delays = {}
    failing = set()

    def post(url, json, timeout):
        calls.append({"query": json["query"], "timeout": timeout})
        time.sleep(delays.get(json["query"], 0))
        if json["query"] in failing:
            raise requests.ConnectionError("rag down")
        return FakeResponse({"results": [{"text": json["query"]}]})

    monkeypatch.setattr(rag_client.requests, "post", post)
    return type("Fake", (), {"calls": calls, "delays": delays, "failing": failing})


class TestPesquisar:
    def test_returns_results_list(self, fake_post):
        assert rag_client.pesquisar("q", 5) == [{"text": "q"}]

    def test_connection_error_raises_502_not_empty_list(self, fake_post):
        fake_post.failing.add("q")

        with pytest.raises(HTTPException) as exc:
            rag_client.pesquisar("q", 5)

        assert exc.value.status_code == 502

    def test_http_error_status_raises_502(self, monkeypatch):
        monkeypatch.setattr(
            rag_client.requests, "post", lambda *a, **k: FakeResponse({}, status=500)
        )

        with pytest.raises(HTTPException) as exc:
            rag_client.pesquisar("q", 5)

        assert exc.value.status_code == 502

    def test_response_without_results_key_raises_502(self, monkeypatch):
        monkeypatch.setattr(
            rag_client.requests, "post", lambda *a, **k: FakeResponse({"error": "x"})
        )

        with pytest.raises(HTTPException) as exc:
            rag_client.pesquisar("q", 5)

        assert exc.value.status_code == 502


class TestPesquisarVarias:
    def test_sequential_mode_calls_in_order_with_base_timeout(
        self, fake_post, monkeypatch
    ):
        monkeypatch.setattr(rag_client, "MAX_PARALLEL", 1)
        monkeypatch.setattr(rag_client, "REQUEST_TIMEOUT", 30.0)

        out = rag_client.pesquisar_varias(["a", "b", "c"])

        assert out == [[{"text": "a"}], [{"text": "b"}], [{"text": "c"}]]
        assert [c["query"] for c in fake_post.calls] == ["a", "b", "c"]
        assert {c["timeout"] for c in fake_post.calls} == {30.0}

    def test_parallel_mode_keeps_input_order_even_if_first_is_slowest(
        self, fake_post, monkeypatch
    ):
        monkeypatch.setattr(rag_client, "MAX_PARALLEL", 4)
        fake_post.delays.update({"a": 0.2, "b": 0.0, "c": 0.05})

        out = rag_client.pesquisar_varias(["a", "b", "c"])

        assert out == [[{"text": "a"}], [{"text": "b"}], [{"text": "c"}]]

    def test_parallel_and_sequential_return_identical_results(
        self, fake_post, monkeypatch
    ):
        queries = ["full question", "k1", "k2", "k3"]
        monkeypatch.setattr(rag_client, "MAX_PARALLEL", 1)
        sequential = rag_client.pesquisar_varias(queries)
        monkeypatch.setattr(rag_client, "MAX_PARALLEL", 4)
        parallel = rag_client.pesquisar_varias(queries)

        assert parallel == sequential

    def test_parallel_timeout_is_scaled_by_worker_count(self, fake_post, monkeypatch):
        monkeypatch.setattr(rag_client, "REQUEST_TIMEOUT", 30.0)
        monkeypatch.setattr(rag_client, "MAX_PARALLEL", 4)

        rag_client.pesquisar_varias(["a", "b", "c"])  # 3 queries -> 3 workers

        assert {c["timeout"] for c in fake_post.calls} == {90.0}

    def test_parallel_timeout_capped_by_max_parallel(self, fake_post, monkeypatch):
        monkeypatch.setattr(rag_client, "REQUEST_TIMEOUT", 30.0)
        monkeypatch.setattr(rag_client, "MAX_PARALLEL", 2)

        rag_client.pesquisar_varias(["a", "b", "c", "d", "e"])

        assert {c["timeout"] for c in fake_post.calls} == {60.0}

    def test_parallel_mode_actually_overlaps_calls(self, monkeypatch):
        state = {"active": 0, "max": 0}
        guard = threading.Lock()

        def post(url, json, timeout):
            with guard:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.05)
            with guard:
                state["active"] -= 1
            return FakeResponse({"results": []})

        monkeypatch.setattr(rag_client.requests, "post", post)
        monkeypatch.setattr(rag_client, "MAX_PARALLEL", 3)

        rag_client.pesquisar_varias(["a", "b", "c"])

        assert state["max"] > 1

    def test_failure_in_any_query_raises_502_in_both_modes(
        self, fake_post, monkeypatch
    ):
        fake_post.failing.add("b")
        for workers in (1, 4):
            monkeypatch.setattr(rag_client, "MAX_PARALLEL", workers)

            with pytest.raises(HTTPException) as exc:
                rag_client.pesquisar_varias(["a", "b", "c"])

            assert exc.value.status_code == 502
