"""Unit tests for store settings, migrations discovery, query building, embedding deferral, status."""

from __future__ import annotations

from datetime import timedelta

import pytest

from external_sources.store import embed, migrate
from external_sources.store.queries import (
    CallQuery,
    build_call_query,
    freshness_note,
    render_context,
    to_record,
)
from external_sources.store.settings import ConfigError, DBSettings, load_store_policy, work_dir
from external_sources.store.status import health_code, render

ENV = {"EXTSRC_DB_HOST": "127.0.0.1", "EXTSRC_DB_PORT": "5433", "EXTSRC_DB_NAME": "db",
       "EXTSRC_DB_USER": "loader", "EXTSRC_DB_PASSWORD": "s3cret-loader",
       "EXTSRC_DB_READER_USER": "reader", "EXTSRC_DB_READER_PASSWORD": "s3cret-reader",
       "EXTSRC_DB_LOAD_TIMEOUT_MS": "600000", "EXTSRC_DB_READER_STATEMENT_TIMEOUT_MS": "5000"}


class TestSettings:
    def test_loader_and_reader(self):
        loader, reader = DBSettings.from_env("loader", ENV), DBSettings.from_env("reader", ENV)
        assert (loader.user, loader.statement_timeout_ms) == ("loader", 600_000)
        assert (reader.user, reader.statement_timeout_ms) == ("reader", 5_000)

    def test_timeouts_and_work_dir_are_required(self):
        """No hidden defaults: a missing setting fails loudly (config/chatbot.env)."""
        for role, name in (("loader", "EXTSRC_DB_LOAD_TIMEOUT_MS"), ("reader", "EXTSRC_DB_READER_STATEMENT_TIMEOUT_MS")):
            with pytest.raises(ConfigError, match=name):
                DBSettings.from_env(role, {k: v for k, v in ENV.items() if k != name})
        with pytest.raises(ConfigError, match="EXTSRC_WORK_DIR"):
            work_dir({})
        assert str(work_dir({"EXTSRC_WORK_DIR": "/data"})) == "/data"

    def test_password_never_in_repr_or_describe(self):
        settings = DBSettings.from_env("loader", ENV)
        assert "s3cret" not in repr(settings) and "s3cret" not in settings.describe()

    def test_missing_names_listed_without_values(self):
        env = {k: v for k, v in ENV.items() if k != "EXTSRC_DB_PASSWORD"}
        with pytest.raises(ConfigError) as info:
            DBSettings.from_env("loader", env)
        assert "EXTSRC_DB_PASSWORD" in str(info.value) and "s3cret" not in str(info.value)

    def test_store_policy_sections(self):
        policy = load_store_policy()
        assert policy["tracking"]["grace_runs"] == 2
        assert set(policy["runner"]["sources"]) == {"ft_portal", "ft_cascade", "ft_europeaid"}


class TestMigrations:
    def test_bundled_migrations_are_ordered_and_unique(self):
        versions = [m.version for m in migrate.discover()]
        assert versions == sorted(versions) and versions[0] == "0001"

    def test_bad_names_and_duplicates_are_errors(self, tmp_path):
        (tmp_path / "1_x.sql").write_text("select 1")
        with pytest.raises(migrate.MigrationError):
            migrate.discover(tmp_path)
        (tmp_path / "1_x.sql").unlink()
        (tmp_path / "0001_a.sql").write_text("select 1")
        (tmp_path / "0001_b.sql").write_text("select 2")
        with pytest.raises(migrate.MigrationError):
            migrate.discover(tmp_path)

    def test_checksum_tracks_content(self, tmp_path):
        (tmp_path / "0001_a.sql").write_text("select 1")
        first = migrate.discover(tmp_path)[0].checksum
        (tmp_path / "0001_a.sql").write_text("select 2")
        assert migrate.discover(tmp_path)[0].checksum != first


class TestQueryBuilding:
    def test_values_are_parameters_never_sql(self):
        evil = "x'); DROP TABLE extsrc.snapshot; --"
        sql, params = build_call_query(CallQuery(text=evil, identifier=evil, natural_keys=[evil]))
        assert evil not in sql and "DROP" not in sql
        assert params["text"] == evil and params["ident"] == evil and params["keys"] == [evil]

    def test_any_term_text_search(self):
        sql, params = build_call_query(CallQuery(text="quantum computing", text_any=True, order="relevance"))
        assert "replace(plainto_tsquery('english', %(text)s)::text, '&', '|')" in sql
        assert "ORDER BY rank DESC" in sql and params["text"] == "quantum computing"

    def test_limit_is_capped_and_order_is_validated(self):
        _, params = build_call_query(CallQuery(limit=10_000))
        assert params["limit"] == 100
        with pytest.raises(ValueError):
            build_call_query(CallQuery(order="title; DROP"))


def fake_result(**freshness):
    record = {"natural_key": "T-1", "record_type": "call", "domain": "projects", "programme": "Horizon Europe",
              "country_scope": "EU", "title": "Topic", "description": "Expected outcome text.", "status": "open",
              "funding_amount": 1_000_000.0, "start_date": None, "deadline_date": "2027-01-01T00:00:00+00:00",
              "details": {"sources": [{"n": 1, "url": "https://example.org/T-1", "kind": "topic_page"}]},
              "source_url": "https://example.org/T-1", "source_updated_at": None, "field_sources": {},
              "data_gaps": []}
    fresh = {"stale": False, "stale_since": None, "data_as_of": "2026-10-27", "source_partial": False,
             "carried_over": False, "absent_runs": 0}
    fresh.update(freshness)
    return {"_record": record, "freshness": fresh, "attribution": "Source: EU Funding & Tenders Portal"}


class TestRenderContext:
    def test_renders_the_context_pack_with_attribution(self):
        pack = render_context(fake_result(), "find_calls")
        assert "Topic" in pack.text and "[Attribution] Source: EU Funding & Tenders Portal" in pack.text
        assert pack.citations[0] == "https://example.org/T-1"
        assert to_record(fake_result()).raw == {}

    def test_stale_and_carried_over_are_said(self):
        result = fake_result(stale=True, stale_since="2026-10-26", carried_over=True, absent_runs=1)
        assert freshness_note(result).startswith("[Data note]")
        assert render_context(result, "find_calls").text.startswith("[Data note]")
        assert freshness_note(fake_result()) is None


class FakeEmbedder:
    def __init__(self, up):
        self.up, self.model = up, "m"

    def reachable(self):
        return self.up


class FakeConn:
    def __init__(self, has_vector=True):
        self.has_vector = has_vector

    def cursor(self):
        outer = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a):
                pass

            def fetchone(self):
                return (outer.has_vector,)

        return Cur()


class TestEmbedDeferral:
    def test_unreachable_server_defers_without_error(self):
        lines: list[str] = []
        assert embed.run_embedding(FakeConn(), FakeEmbedder(False), log=lines.append) == 0
        assert "deferred" in lines[0]

    def test_not_configured_defers(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        lines: list[str] = []
        assert embed.run_embedding(FakeConn(), None, log=lines.append) == 0 and "not set" in lines[0]

    def test_records_only_skips_document_chunks(self):
        executed: list[str] = []

        class Conn(FakeConn):
            def cursor(self):
                class Cur:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def execute(self, sql, *a):
                        executed.append(sql)

                    def fetchone(self):
                        return (True,)

                    def fetchall(self):
                        return []

                return Cur()

        lines: list[str] = []
        assert embed.run_embedding(Conn(), FakeEmbedder(True), log=lines.append, records_only=True) == 0
        assert not any("document_chunk" in sql for sql in executed)
        assert any("current.records" in sql for sql in executed) and "embedded 0" in lines[-1]

    def test_no_pgvector_defers(self):
        lines: list[str] = []
        assert embed.run_embedding(FakeConn(False), FakeEmbedder(True), log=lines.append) == 0
        assert "pgvector" in lines[0]

    def test_dimension_mismatch_is_rejected(self):
        class Session:
            def post(self, *a, **k):
                class R:
                    def raise_for_status(self):
                        pass

                    def json(self):
                        return {"embeddings": [[0.1, 0.2]]}
                return R()

        with pytest.raises(ValueError, match="dimensions"):
            embed.OllamaEmbedder("http://server", "m", session=Session()).embed(["text"])


class TestStatus:
    def status(self, **source):
        base = {"source": "ft_portal", "snapshot_id": 3, "record_count": 330, "data_age": timedelta(hours=5),
                "stale": False, "stale_since": None, "stale_reason": None, "partial": False, "partial_reasons": [],
                "last_status": "published", "consecutive_failures": 0}
        base.update(source)
        return {"database": "ok", "sources": [base], "dead_letters": {"ft_portal": {"document": 3}},
                "runs": [], "pending_batches": [], "live_calls": 330, "live_calls_by_source": {"ft_portal": 330}}

    def test_healthy(self):
        status = self.status()
        assert health_code(status) == 0
        text = render(status)
        assert "ft_portal" in text and "5.0h" in text and "document=3" in text

    def test_stale_is_degraded_and_explained(self):
        status = self.status(stale=True, stale_since="2026-10-27", stale_reason="API down", consecutive_failures=2)
        assert health_code(status) == 1 and "API down" in render(status)

    def test_database_down(self):
        status = {"database": "unreachable", "sources": [], "dead_letters": {}, "runs": [],
                  "pending_batches": [{"run_id": "r", "source": "ft_portal", "path": "p"}], "local_runs": []}
        assert health_code(status) == 3 and "waiting for the database: 1" in render(status)


def test_coverage_table_renders_counts_and_percentages():
    from external_sources.store.status import render_coverage
    row = {"source": "TOTAL", "calls": 4, "description": 4, "budget_any": 3, "topic_budget": 3, "deadline": 4,
           "eligibility": 2, "eligibility_confirmed": 1, "deadline_confirmed": 2, "deadline_conflicting": 1,
           "budget_conflicting": 0, "cites_document": 2, "carried_over": 0, "schema_drift": 0}
    table = render_coverage([row])
    assert table.splitlines()[0].startswith("| source | calls | description")
    assert "| TOTAL | 4 | 4 (100.0%) | 3 (75.0%)" in table
