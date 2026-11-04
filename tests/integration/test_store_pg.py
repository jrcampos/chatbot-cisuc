"""
Integration tests of the store against a real Postgres + pgvector (see conftest.py).

Covers: migrations, publish/idempotency, read-only role, provenance and ranges, grace expiry,
partial runs, deadline expiry, validation rejection (last good data kept), failed sources (stale),
dead letters, the read API, retention, DB outage + spool, connection loss mid-load, and
kill -9 / SIGTERM mid-run followed by a resume.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from external_sources.documents.store import DocumentStore
from external_sources.store import queries
from external_sources.store.batch import LoadBatch
from external_sources.store.checkpoint import list_runs, read_jsonl
from external_sources.store.loader import Loader
from external_sources.store.migrate import MigrationError, migrate
from external_sources.store.settings import load_store_policy
from tests.integration.store_fakes import WP, FakePlugin, SlowEnricher, make_record

pytestmark = pytest.mark.integration
POLICY = load_store_policy()
REPO = Path(__file__).resolve().parents[2]


def batch(records, source="ft_portal", run="00000000-0000-0000-0000-00000000000{}", n=1, partial=None,
          failures=None, documents=None, documents_dir=None, fetched_at="2026-10-28T0{}:00:00+00:00"):
    return LoadBatch(source, run.format(n), fetched_at.format(n), fetched_at.format(n),
                     [r.model_dump() for r in records], documents or {}, failures or [], partial or [], {},
                     documents_dir)


def scalar(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def live_keys(conn, source="ft_portal"):
    with conn.cursor() as cur:
        cur.execute("SELECT natural_key FROM current.records WHERE source = %s ORDER BY 1", (source,))
        return [r[0] for r in cur.fetchall()]


def test_migrations_idempotent_and_guarded(db, tmp_path):
    assert migrate(db) == []
    assert scalar(db, "SELECT count(*) FROM extsrc.schema_migrations") >= 3
    assert scalar(db, "SELECT count(*) FROM pg_extension WHERE extname = 'vector'") == 1
    changed = tmp_path / "m"
    changed.mkdir()
    (changed / "0001_core.sql").write_text("SELECT 1")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate(db, changed)


def test_publish_idempotent_and_read_only_role(db, reader):
    result = Loader(db, POLICY).load(batch([make_record("HORIZON-A-01"), make_record("HORIZON-A-02")]))
    assert result.status == "published" and result.counts["records"] == 2
    assert Loader(db, POLICY).load(batch([make_record("HORIZON-A-01"), make_record("HORIZON-A-02")])).status \
        == "already_loaded"
    assert live_keys(reader) == ["HORIZON-A-01", "HORIZON-A-02"]
    import psycopg
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        reader.execute("SELECT count(*) FROM extsrc.snapshot")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        reader.execute("CREATE TABLE current.x (a int)")


def test_conflicting_values_are_all_kept_with_ranges(db, reader):
    Loader(db, POLICY).load(batch([make_record("HORIZON-C-01", deadline="2099-04-15T00:00:00+00:00",
                                               doc_deadline="2099-04-13", doc_budget=4_700_000.0)]))
    with reader.cursor() as cur:
        cur.execute("""SELECT layer, role, value_ts::date::text, source_url, page FROM current.field_values
                       WHERE natural_key = 'HORIZON-C-01' AND field = 'deadline_date' ORDER BY layer""")
        rows = cur.fetchall()
    assert ("api", "value", "2099-04-15", rows[0][3], None) == rows[0]
    assert rows[1][:3] == ("document", "alternative", "2099-04-13") and rows[1][3] == WP and rows[1][4] == 37
    with reader.cursor() as cur:
        cur.execute("""SELECT status, value_min_ts::date::text, value_max_ts::date::text FROM current.resolved_values
                       WHERE natural_key = 'HORIZON-C-01' AND field = 'deadline_date'""")
        assert cur.fetchone() == ("conflicting", "2099-04-13", "2099-04-15")
        cur.execute("""SELECT value_min_num, value_max_num FROM current.resolved_values
                       WHERE natural_key = 'HORIZON-C-01' AND field = 'funding_amount'""")
        assert tuple(float(v) for v in cur.fetchone()) == (4_700_000.0, 5_000_000.0)


def test_grace_carry_then_expire(db, reader):
    a, b = make_record("HORIZON-G-01"), make_record("HORIZON-G-02")
    Loader(db, POLICY).load(batch([a, b], n=1))
    second = Loader(db, POLICY).load(batch([a], n=2))
    assert second.changes["missing"] == 1 and second.counts["carried"] == 1
    assert live_keys(reader) == ["HORIZON-G-01", "HORIZON-G-02"]
    assert scalar(reader, "SELECT carried_over FROM current.records WHERE natural_key = 'HORIZON-G-02'") is True
    third = Loader(db, POLICY).load(batch([a], n=3))
    assert third.changes["expired"] == 1
    assert live_keys(reader) == ["HORIZON-G-01"]
    with db.cursor() as cur:
        cur.execute("SELECT state FROM extsrc.record_change WHERE natural_key = 'HORIZON-G-02' ORDER BY id")
        assert [r[0] for r in cur.fetchall()] == ["new", "missing", "expired"]
    assert scalar(db, "SELECT expired_at IS NOT NULL FROM extsrc.record_tracking WHERE natural_key = 'HORIZON-G-02'")
    # it comes back: new again, live again
    assert Loader(db, POLICY).load(batch([a, b], n=4)).changes["new"] == 1
    assert live_keys(reader) == ["HORIZON-G-01", "HORIZON-G-02"]


def test_partial_runs_never_count_as_absence(db, reader):
    a, b = make_record("HORIZON-P-01"), make_record("HORIZON-P-02")
    Loader(db, POLICY).load(batch([a, b], n=1))
    for n in (2, 3, 4):
        result = Loader(db, POLICY).load(batch([a], n=n, partial=["horizon: page 2 failed (Timeout)"]))
        assert result.status == "partial"
    assert live_keys(reader) == ["HORIZON-P-01", "HORIZON-P-02"]
    assert scalar(db, "SELECT absent_runs FROM extsrc.record_tracking WHERE natural_key = 'HORIZON-P-02'") == 0
    freshness = queries.source_freshness(reader)[0]
    assert freshness["partial"] is True


def test_records_whose_deadlines_passed_are_not_carried(db, reader):
    past = make_record("HORIZON-D-01", deadline="2020-01-01T00:00:00+00:00")
    Loader(db, POLICY).load(batch([past, make_record("HORIZON-D-02")], n=1))
    assert scalar(reader, "SELECT status FROM current.records WHERE natural_key = 'HORIZON-D-01'") == "closed"
    result = Loader(db, POLICY).load(batch([make_record("HORIZON-D-02")], n=2))
    assert result.changes["expired"] == 1 and result.counts["carried"] == 0
    assert scalar(db, "SELECT reason FROM extsrc.record_change WHERE natural_key = 'HORIZON-D-01' "
                      "AND state = 'expired'") == "every deadline has passed"


def test_rejected_batch_keeps_the_last_good_data(db, reader):
    many = [make_record(f"HORIZON-R-{i:02d}") for i in range(20)]
    Loader(db, POLICY).load(batch(many, n=1))
    result = Loader(db, POLICY).load(batch(many[:5], n=2))
    assert result.status == "rejected"
    assert any(c["check"] == "record_count_ratio" and not c["ok"] for c in result.validation["checks"])
    assert result.alerts[0]["code"] == "snapshot_rejected"
    assert len(live_keys(reader)) == 20  # last good snapshot still served (15 would be carried anyway)
    freshness = queries.source_freshness(reader)[0]
    assert freshness["stale"] is True and "rejected" in freshness["stale_reason"]
    forced = Loader(db, POLICY).load(batch(many[:5], n=2), force_publish=True)
    assert forced.status == "published" and forced.validation.get("forced")
    assert queries.source_freshness(reader)[0]["stale"] is False


def test_failed_source_is_flagged_stale_and_still_served(db, reader):
    Loader(db, POLICY).load(batch([make_record("HORIZON-F-01")], n=1))
    failed = batch([], n=2, partial=["fetch failed: ConnectionError"],
                   failures=[{"stage": "fetch", "key": "ft_portal", "error": "ConnectionError: down"}])
    assert Loader(db, POLICY).mark_failed(failed, "fetch failed: ConnectionError").status == "failed"
    assert live_keys(reader) == ["HORIZON-F-01"]
    calls = queries.search_calls(reader)
    assert calls[0]["freshness"]["stale"] is True
    assert queries.render_context(calls[0], "find_calls").text.startswith("[Data note]")
    assert scalar(db, "SELECT count(*) FROM extsrc.dead_letter WHERE resolved_at IS NULL") == 1


def test_dead_letters_upsert_and_resolve(db):
    failure = {"stage": "normalize", "key": "HORIZON-X-01", "error": "KeyError: title",
               "payload": {"metadata": {"identifier": ["HORIZON-X-01"]}}}
    Loader(db, POLICY).load(batch([make_record("HORIZON-L-01")], n=1, failures=[failure]))
    Loader(db, POLICY).load(batch([make_record("HORIZON-L-01"), make_record("HORIZON-L-02")], n=2,
                                  failures=[failure]))
    assert scalar(db, "SELECT attempts FROM extsrc.dead_letter WHERE item_key = 'HORIZON-X-01'") == 2
    assert scalar(db, "SELECT payload->'metadata'->'identifier'->>0 FROM extsrc.dead_letter") == "HORIZON-X-01"
    Loader(db, POLICY).load(batch([make_record("HORIZON-L-01"), make_record("HORIZON-X-01")], n=3))
    assert scalar(db, "SELECT resolution FROM extsrc.dead_letter WHERE item_key = 'HORIZON-X-01'").startswith("loaded")


def test_read_api_filters_ranges_text_and_citations(db, reader, tmp_path):
    store = DocumentStore(tmp_path / "documents")
    doc_id = store.save(WP, "<!-- page 37 -->\n# HORIZON-Q-01: Quantum\nThe deadline is 13 April 2099.\n\n"
                        "## Eligibility\nConsortia of three legal entities. Lump sum funding.", {"role": "programme_level"})
    docs = {"HORIZON-Q-01": [{"url": WP, "role": "programme_level", "title": "WP", "status": "fresh",
                              "doc_id": doc_id, "chars": 100, "error": None}]}
    records = [make_record("HORIZON-Q-01", deadline="2099-04-15T00:00:00+00:00", doc_deadline="2099-04-13",
                           budget=5_000_000.0, doc_budget=4_700_000.0),
               make_record("HORIZON-CL5-01", deadline="2099-06-01T00:00:00+00:00", budget=20_000_000.0),
               make_record("HORIZON-CL6-01", deadline="2099-09-01T00:00:00+00:00", budget=None)]
    Loader(db, POLICY).load(batch(records, documents=docs, documents_dir=str(tmp_path / "documents")))
    # the document's date (13 April) lies only inside the range -> still found
    hit = queries.search_calls(reader, deadline_from=date(2099, 4, 13), deadline_to=date(2099, 4, 13))
    assert [h["natural_key"] for h in hit] == ["HORIZON-Q-01"]
    assert hit[0]["ranges"]["deadline_date"]["status"] == "conflicting"
    # budget overlap: 4.8M is inside 4.7..5.0M; calls without a budget never match
    assert [h["natural_key"] for h in queries.search_calls(reader, budget_min=4_800_000, budget_max=4_900_000)] \
        == ["HORIZON-Q-01"]
    assert [h["natural_key"] for h in queries.search_calls(reader, budget_min=10_000_000)] == ["HORIZON-CL5-01"]
    text = queries.search_calls(reader, text="quantum computing", order="relevance")
    assert text[0]["natural_key"] == "HORIZON-Q-01" and text[0]["rank"] > 0
    assert text[0]["sources"][1]["url"] == WP and text[0]["attribution"]
    assert queries.get_call(reader, "horizon-q-01")["budget"]["min"] == 4_700_000.0
    assert [h["natural_key"] for h in queries.search_calls(reader, programme="horizon")][:1] == ["HORIZON-Q-01"]
    chunks = queries.search_documents(reader, "lump sum", natural_key="HORIZON-Q-01")
    assert chunks and chunks[0]["url"] == WP and chunks[0]["page_start"] == 37
    pack = queries.render_context(text[0], "deadlines")
    assert WP in pack.citations
    assert queries.list_programmes(reader)[0]["live_calls"] == 3


def test_retention_keeps_published_and_previous(db):
    for n in range(1, 5):
        Loader(db, POLICY).load(batch([make_record("HORIZON-T-01", title=f"v{n}")], n=n))
    counts = Loader(db, {**POLICY, "retention": {**POLICY["retention"], "keep_published_snapshots": 1}}).prune()
    assert counts["snapshots_superseded"] == 2
    assert scalar(db, "SELECT count(*) FROM extsrc.snapshot") == 2


def _runner(work, db_factory, records):
    from external_sources.store.runner import Runner, RunOptions
    return Runner(work, RunOptions(["ft_portal"]), POLICY, db_factory,
                  plugin_factory=lambda s, r: FakePlugin(records), enricher_factory=lambda s, r: SlowEnricher())


def test_database_outage_spools_and_next_run_loads(db, pg_server, tmp_path):
    from external_sources.store.db import connect
    from external_sources.store.runner import EXIT_DB_DOWN, EXIT_OK
    from external_sources.store.settings import DBSettings
    bad = {**pg_server["env"], "EXTSRC_DB_PORT": "1"}
    down = _runner(tmp_path, lambda: connect(DBSettings.from_env("loader", bad), attempts=2, sleep=lambda s: None),
                   [make_record("HORIZON-O-01")])
    assert down.run() == EXIT_DB_DOWN
    def good():
        return connect(DBSettings.from_env("loader", pg_server["env"]))
    assert _runner(tmp_path, good, [make_record("HORIZON-O-02")]).run() == EXIT_OK
    # the spooled batch (O-01) loaded first; the new run then replaced it (O-01 carried in grace)
    with db.cursor() as cur:
        cur.execute("SELECT natural_key, carried_over FROM current.records ORDER BY 1")
        assert cur.fetchall() == [("HORIZON-O-01", True), ("HORIZON-O-02", False)]


def test_connection_loss_mid_load_is_spooled(db, pg_server, tmp_path):
    from external_sources.store.db import connect
    from external_sources.store.runner import EXIT_DB_DOWN
    from external_sources.store.settings import DBSettings

    def dying_connection():
        conn = connect(DBSettings.from_env("loader", pg_server["env"]))
        db.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))
        time.sleep(0.2)
        return conn

    assert _runner(tmp_path, dying_connection, [make_record("HORIZON-K-01")]).run() == EXIT_DB_DOWN
    [run] = list_runs(tmp_path)
    assert run.source_state("ft_portal")["stage"] == "pending_load"


def _spawn(work, env, delay):
    return subprocess.Popen([sys.executable, str(REPO / "tests/integration/run_fake_store.py"), str(work), str(delay)],
                            env={**os.environ, **env}, cwd=str(REPO), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, shell=False)


def _wait_lines(path, n, proc, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and sum(1 for _ in read_jsonl(path)) >= n:
            return
        if proc.poll() is not None:
            raise AssertionError(f"runner exited early: {proc.stdout.read().decode()[-2000:]}")
        time.sleep(0.05)
    raise AssertionError("timeout waiting for checkpoints")


@pytest.mark.parametrize("sig, expected", [(signal.SIGKILL, -signal.SIGKILL), (signal.SIGTERM, 75)])
def test_kill_mid_run_then_resume(db, pg_server, tmp_path, sig, expected):
    first = _spawn(tmp_path, pg_server["env"], 0.15)
    runs_dir = tmp_path / "runs"
    while not runs_dir.exists() or not any(runs_dir.iterdir()):
        if first.poll() is not None:
            raise AssertionError(f"runner exited early: {first.stdout.read().decode()[-2000:]}")
        time.sleep(0.05)
    run_dir = next(runs_dir.iterdir())
    _wait_lines(run_dir / "ft_portal" / "enriched.jsonl", 5, first)
    first.send_signal(sig)
    assert first.wait(timeout=30) == expected
    done_before = sum(1 for _ in read_jsonl(run_dir / "ft_portal" / "enriched.jsonl"))
    assert 5 <= done_before < 30
    assert scalar(db, "SELECT count(*) FROM current.records") == 0  # nothing half-loaded
    second = _spawn(tmp_path, pg_server["env"], 0.0)
    output = second.communicate(timeout=120)[0].decode()
    assert second.returncode == 0, output[-2000:]
    assert f"resuming run {run_dir.name}" in output
    assert "already enriched" in output
    keys = [row["key"] for row in read_jsonl(run_dir / "ft_portal" / "enriched.jsonl")]
    assert len(keys) == len(set(keys)) == 30  # every record enriched exactly once
    assert scalar(db, "SELECT count(*) FROM current.records") == 30
