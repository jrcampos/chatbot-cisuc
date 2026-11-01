"""
Unit tests for the Orchestrator's RAG client (strict failures, ordered results).
requests.post is faked: no RAG service is needed.
"""

import os
import sys
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
    def test_calls_in_order_with_base_timeout(self, fake_post, monkeypatch):
        monkeypatch.setattr(rag_client, "REQUEST_TIMEOUT", 30.0)

        out = rag_client.pesquisar_varias(["a", "b", "c"])

        assert out == [[{"text": "a"}], [{"text": "b"}], [{"text": "c"}]]
        assert [c["query"] for c in fake_post.calls] == ["a", "b", "c"]
        assert {c["timeout"] for c in fake_post.calls} == {30.0}

    def test_failure_in_any_query_raises_502(self, fake_post):
        fake_post.failing.add("b")

        with pytest.raises(HTTPException) as exc:
            rag_client.pesquisar_varias(["a", "b", "c"])

        assert exc.value.status_code == 502
