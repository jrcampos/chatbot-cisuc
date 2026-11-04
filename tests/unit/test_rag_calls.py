"""
Unit tests for the RAG_CALLS retrieval engine (application/RAG_CALLS/retrieval.py) and the
Orchestrator's source routing (application/Orchestrator/routing.py). No database, no model:
the store reads are replaced by fakes.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR_DIR = REPO_ROOT / "application" / "Orchestrator"
if str(ORCHESTRATOR_DIR) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_DIR))

os.environ.setdefault("LLM_PROVIDER", "ollama")
os.environ.setdefault("CALLS_CANDIDATES", "20")
os.environ.setdefault("CALLS_DETAILED", "1")
os.environ.setdefault("CALLS_DETAILED_MAX_CHARS", "4000")

# RAG_CISUC also has a `retrieval` module: load this one under its own name.
_spec = importlib.util.spec_from_file_location("rag_calls_retrieval", REPO_ROOT / "application" / "RAG_CALLS" / "retrieval.py")
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

from routing import CALLS, CISUC, build_context, calls_key, parse_targets, retrieve  # noqa: E402


# ---------------------------------------------------------------- RAG_CALLS retrieval
class TestFuse:
    def test_weighted_rrf_like_rag_cisuc(self):
        # B is 2nd lexical (0.6/2) + 1st vector (0.4/1) = 0.7 > A 1st lexical only (0.6)
        assert rc.fuse([(["A", "B"], 0.6), (["B"], 0.4)]) == ["B", "A"]

    def test_empty_lists(self):
        assert rc.fuse([([], 0.6), ([], 0.4)]) == []


class TestIdentifiers:
    @pytest.mark.parametrize("text,found", [
        ("when does HORIZON-CL4-2027-04-DIGITAL-EMERGING-05 close", ["HORIZON-CL4-2027-04-DIGITAL-EMERGING-05"]),
        ("Quando fecha o HORIZON-CL4-2027-04-DIGITAL-EMERGING-05?", ["HORIZON-CL4-2027-04-DIGITAL-EMERGING-05"]),
        ("EuropeAid/187686/ID/ACT/MK", ["EuropeAid/187686/ID/ACT/MK"]),
        ("(HORIZON-MSCA-2027-COFUND-02) and HORIZON-MSCA-2027-COFUND-02", ["HORIZON-MSCA-2027-COFUND-02"]),
        ("quantum computing", []),
        ("AI-driven robotics", []),            # two parts: not an identifier
        ("joão-maria-silva", []),              # non-ASCII parts: not an identifier
        ("a--b-c", []),                        # empty part
        ("state-of-the art", ["state-of-the"]),  # harmless: an exact lookup that finds nothing
    ])
    def test_finds_identifiers(self, text, found):
        assert rc.identifiers(text) == found

    def test_bounded_number_of_lookups(self):
        query = " ".join(f"A-B-{i}" for i in range(20))
        assert len(rc.identifiers(query)) == rc.MAX_IDENTIFIERS

    def test_linear_on_adversarial_input(self):
        """CodeQL py/polynomial-redos: the old regex took ~33 s on 40k zeros."""
        import time
        for attack in ("0" * 400_000, "0-" * 200_000, "a-" * 100_000 + "!" * 100_000):
            started = time.perf_counter()
            rc.identifiers(attack)
            assert time.perf_counter() - started < 0.5


def hit(key, title="T"):
    return {"natural_key": key, "source": "ft_portal", "title": title, "programme": "Horizon Europe",
            "status": "open", "next_deadline": "2027-01-14T17:00:00+00:00", "url": f"https://x.eu/{key}",
            "freshness": {"data_as_of": "2026-11-04T00:00:00+00:00"}}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestGetRelevantCalls:
    @pytest.fixture
    def store(self, monkeypatch):
        calls = {"EXACT-ID-01": hit("EXACT-ID-01"), "LEX-1": hit("LEX-1"), "LEX-2": hit("LEX-2"), "VEC-1": hit("VEC-1")}

        def search_calls(conn, q):
            if q.identifier:
                return [calls["EXACT-ID-01"]] if q.identifier == "EXACT-ID-01" else []
            if q.natural_keys is not None:
                return [calls[k] for k in q.natural_keys if k in calls]
            return [calls["LEX-1"], calls["LEX-2"]]

        rendered = []

        class Pack:
            def __init__(self, text):
                self.text = text

        def render_context(h, intent, max_chars=None):
            rendered.append((h["natural_key"], intent))
            return Pack(f"<{h['natural_key']}:{intent}>")

        monkeypatch.setattr(rc, "search_calls", search_calls)
        monkeypatch.setattr(rc, "render_context", render_context)
        monkeypatch.setattr(rc, "_vector", lambda conn, query: ["VEC-1", "LEX-2"])
        return rendered

    def test_result_shape_and_detail_levels(self, store):
        results = rc.get_relevant_calls("ai health", 3, connect=FakeConn)
        # LEX-1 0.6 (lexical 1st) > LEX-2 0.3 + 0.2 (lexical 2nd, vector 2nd) > VEC-1 0.4 (vector 1st)
        assert [r["metadata"]["natural_key"] for r in results] == ["LEX-1", "LEX-2", "VEC-1"]
        assert store == [("LEX-1", "explain_call"), ("LEX-2", "list_calls"), ("VEC-1", "list_calls")]
        meta = results[0]["metadata"]
        assert meta["type"] == "funding_call" and meta["url"] == "https://x.eu/LEX-1"
        assert set(results[0]) == {"text", "metadata"}

    def test_exact_identifier_goes_first(self, store):
        results = rc.get_relevant_calls("deadline of EXACT-ID-01 please", 2, connect=FakeConn)
        assert [r["metadata"]["natural_key"] for r in results] == ["EXACT-ID-01", "LEX-1"]

    def test_empty_query_returns_nothing_without_db(self):
        def boom():
            raise AssertionError("must not connect")
        assert rc.get_relevant_calls("  ''  ", 5, connect=boom) == []


# ---------------------------------------------------------------- Orchestrator routing
class TestParseTargets:
    @pytest.mark.parametrize("raw,sources,targets", [
        ("[CALLS] quantum computing", (CALLS,), ["quantum computing"]),
        ("[cisuc] Fernando Boavida | Jorge Sá Silva", (CISUC,), ["Fernando Boavida", "Jorge Sá Silva"]),
        ("[AMBOS] information systems", (CISUC, CALLS), ["information systems"]),
        ("[BOTH] iot", (CISUC, CALLS), ["iot"]),
        ("Fernando Boavida", (CISUC,), ["Fernando Boavida"]),          # no tag: CISUC, as before
        ("[OTHER] x", (CISUC,), ["[OTHER] x"]),                        # unknown tag: CISUC
        ("[CALLS]", (CALLS,), []),
    ])
    def test_tags(self, raw, sources, targets):
        assert parse_targets(raw, calls_enabled=True) == (sources, targets)

    def test_calls_disabled_always_cisuc(self):
        assert parse_targets("[CALLS] quantum", calls_enabled=False) == ((CISUC,), ["quantum"])


class TestRetrieve:
    def test_dedup_cap_and_failure_isolation(self):
        def post(target):
            if target == "down":
                raise ConnectionError("unreachable")
            return [{"text": "a", "metadata": {"natural_key": "K1"}}, {"text": "b", "metadata": {"natural_key": target}}]

        docs = retrieve(post, ["X", "down", "Y"], top_k=3, key=calls_key)
        assert [d["metadata"]["natural_key"] for d in docs] == ["K1", "X", "Y"]


class TestBuildContext:
    def test_sections_per_source(self):
        text = build_context([{"text": "cisuc chunk"}], [{"text": "call block"}], (CISUC, CALLS))
        assert text.index("### CISUC knowledge base") < text.index("cisuc chunk") < text.index("### EU funding calls")
        assert "call block" in text

    def test_empty_calls_says_so(self):
        assert "no matching open or forthcoming funding calls" in build_context([], [], (CALLS,))
        assert "EU funding calls" not in build_context([{"text": "x"}], [], (CISUC,))
