"""
Chaos tests for the store runner, without a database: database down (spool + reload), a source
that raises, a record that fails enrichment, failing documents, SIGTERM (interrupt + resume),
resume from the fetch checkpoint, disk full, a concurrent run, and connection loss mid-load.
"""

from __future__ import annotations

import errno
import fcntl
import json
from pathlib import Path
from typing import Any

import pytest

from external_sources.plugins.base import Failure, NormalizedRecord, SourcePlugin
from external_sources.store import checkpoint
from external_sources.store.batch import LoadBatch
from external_sources.store.checkpoint import RunDir, list_runs, read_jsonl
from external_sources.store.db import DatabaseUnavailable
from external_sources.store.loader import LoadResult
from external_sources.store.runner import (
    EXIT_DB_DOWN,
    EXIT_DEGRADED,
    EXIT_INTERRUPTED,
    EXIT_LOCKED,
    EXIT_OK,
    Runner,
    RunOptions,
    StopFlag,
)
from external_sources.store.settings import load_store_policy

POLICY = load_store_policy()


def record(key: str) -> NormalizedRecord:
    return NormalizedRecord(natural_key=key, record_type="call", domain="projects", title=f"Title {key}",
                            status="open", deadline_date="2099-01-01T00:00:00+00:00",
                            source_url=f"https://example.org/{key}", raw={"identifier": [key]})


class FakePlugin(SourcePlugin):
    name = "fake"

    def __init__(self, keys, raise_exc=None, failures=None, partial=None):
        self.keys, self.raise_exc, self.extra_failures, self.partial = keys, raise_exc, failures or [], partial or []
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        self.failures.extend(self.extra_failures)
        self.partial_reasons.extend(self.partial)
        return [record(k) for k in self.keys]


class FakePipeline:
    def __init__(self):
        self.store = None
        self.processed: list[str] = []

    def process(self, rec):
        self.processed.append(rec.natural_key)
        return []


class FakeEnricher:
    """Mimics Enricher.iter_enrich: phase 1 (pipeline.process for all), phase 2 yields."""

    def __init__(self, fail_keys=(), bad_docs=None, on_yield=None):
        self.pipeline = FakePipeline()
        self.fail_keys, self.bad_docs, self.on_yield = set(fail_keys), bad_docs or {}, on_yield
        self.skips: list[set[str]] = []

    def iter_enrich(self, records, pool=None, skip=None):
        self.skips.append(set(skip or ()))
        for rec in records:
            self.pipeline.process(rec)
        for rec in records:
            if skip and rec.natural_key in skip:
                continue
            if self.on_yield:
                self.on_yield(rec)
            if rec.natural_key in self.fail_keys:
                yield rec, None, [], "ValueError: broken table"
                continue
            enriched = rec.model_copy(update={"details": {"enriched": True}})
            from external_sources.documents.pipeline import CallDocument
            docs = [CallDocument(**d) for d in self.bad_docs.get(rec.natural_key, [])]
            yield rec, enriched, docs, None


class FakeLoader:
    loaded: list[LoadBatch] = []
    failed: list[LoadBatch] = []
    raise_on_load: BaseException | None = None

    def __init__(self, conn, policy, notifier=None):
        self.conn = conn

    def load(self, batch, force_publish=False):
        if FakeLoader.raise_on_load is not None:
            raise FakeLoader.raise_on_load
        FakeLoader.loaded.append(batch)
        return LoadResult(batch.source, "partial" if batch.partial_reasons else "published", 1,
                          {"ok": True}, counts={"records": len(batch.records)})

    def mark_failed(self, batch, reason):
        FakeLoader.failed.append(batch)
        return LoadResult(batch.source, "failed")

    def finish_run(self, *args, **kwargs):
        pass

    def prune(self):
        return {}


@pytest.fixture(autouse=True)
def reset_loader():
    FakeLoader.loaded, FakeLoader.failed, FakeLoader.raise_on_load = [], [], None
    yield


class FakeConn:
    def close(self):
        pass


class DB:
    def __init__(self, up=True):
        self.up = up

    def __call__(self):
        if not self.up:
            raise DatabaseUnavailable("cannot connect to x@127.0.0.1:1/db: refused")
        return FakeConn()


def make_runner(tmp_path, plugins: dict[str, SourcePlugin], enricher=None, db=None, stop=None, **options):
    enricher = enricher or FakeEnricher()
    return Runner(tmp_path, RunOptions(list(plugins), **options), POLICY, db or DB(),
                  plugin_factory=lambda source, runner: plugins[source],
                  enricher_factory=lambda source, runner: enricher, stop=stop or StopFlag(),
                  loader_cls=FakeLoader)


def keys_of(batch):
    return [r["natural_key"] for r in batch.records]


def test_happy_path(tmp_path):
    runner = make_runner(tmp_path, {"a": FakePlugin(["A1", "A2"])})
    assert runner.run() == EXIT_OK
    assert keys_of(FakeLoader.loaded[0]) == ["A1", "A2"]
    assert FakeLoader.loaded[0].records[0]["details"] == {"enriched": True}
    [run] = list_runs(tmp_path)
    assert run.state()["status"] == "succeeded" and run.source_state("a")["stage"] == "done"


def test_database_down_spools_then_next_run_loads_it(tmp_path):
    db = DB(up=False)
    assert make_runner(tmp_path, {"a": FakePlugin(["A1"])}, db=db).run() == EXIT_DB_DOWN
    [run] = list_runs(tmp_path)
    assert run.source_state("a")["stage"] == "pending_load"
    assert (run.source_dir("a") / "batch.json").exists() and FakeLoader.loaded == []
    db.up = True
    assert make_runner(tmp_path, {"a": FakePlugin(["A9"])}, db=db, fresh=True).run() == EXIT_OK
    assert [keys_of(b) for b in FakeLoader.loaded] == [["A1"], ["A9"]]  # spooled batch first
    assert run.source_state("a")["stage"] == "done"


def test_failing_source_does_not_stop_the_others(tmp_path):
    plugins = {"bad": FakePlugin([], raise_exc=RuntimeError("API changed")), "good": FakePlugin(["G1"])}
    assert make_runner(tmp_path, plugins).run() == EXIT_DEGRADED
    assert [b.source for b in FakeLoader.failed] == ["bad"]
    assert FakeLoader.failed[0].failures[0]["error"] == "RuntimeError: API changed"
    assert keys_of(FakeLoader.loaded[0]) == ["G1"]


def test_record_that_fails_enrichment_is_still_loaded_with_a_gap_and_dead_letter(tmp_path):
    runner = make_runner(tmp_path, {"a": FakePlugin(["A1", "A2"])}, enricher=FakeEnricher(fail_keys={"A2"}))
    runner.run()
    batch = FakeLoader.loaded[0]
    a2 = next(r for r in batch.records if r["natural_key"] == "A2")
    assert a2["data_gaps"][-1]["kind"] == "enrichment_error"
    assert {"stage": "enrich", "key": "A2"}.items() <= batch.failures[0].items()


def test_failed_documents_become_dead_letters_once_per_url(tmp_path):
    bad = {"url": "https://example.org/broken.pdf", "role": "call_specific", "title": "Call document",
           "status": "convert_error", "error": "pdftotext failed: corrupt xref"}
    enricher = FakeEnricher(bad_docs={"A1": [bad], "A2": [bad]})
    make_runner(tmp_path, {"a": FakePlugin(["A1", "A2"])}, enricher=enricher).run()
    batch = FakeLoader.loaded[0]
    docs = [f for f in batch.failures if f["stage"] == "document"]
    assert len(docs) == 1 and docs[0]["key"] == bad["url"] and "corrupt" in docs[0]["error"]
    assert batch.stats["documents_failed"] == 1


def test_plugin_failures_and_partial_reasons_reach_the_batch(tmp_path):
    plugin = FakePlugin(["A1"], failures=[Failure("normalize", "X-1", "KeyError: title", {"metadata": {}})],
                        partial=["horizon: page 2 failed (Timeout)"])
    assert make_runner(tmp_path, {"a": plugin}).run() == EXIT_DEGRADED
    batch = FakeLoader.loaded[0]
    assert batch.partial_reasons == ["horizon: page 2 failed (Timeout)"]
    assert batch.failures[0]["payload"] == {"metadata": {}}


def test_sigterm_interrupts_cleanly_and_the_next_run_resumes(tmp_path):
    stop = StopFlag()
    seen: list[str] = []

    def stop_after_first(rec):
        seen.append(rec.natural_key)
        if len(seen) == 1:
            stop._handle(15, None)  # what the SIGTERM handler does

    enricher = FakeEnricher(on_yield=stop_after_first)
    plugin = FakePlugin(["A1", "A2", "A3"])
    assert make_runner(tmp_path, {"a": plugin}, enricher=enricher, stop=stop).run() == EXIT_INTERRUPTED
    [run] = list_runs(tmp_path)
    assert run.state()["status"] == "interrupted"
    assert [row["key"] for row in read_jsonl(run.source_dir("a") / "enriched.jsonl")] == ["A1"]
    assert FakeLoader.loaded == []

    plugin2, enricher2 = FakePlugin(["SHOULD-NOT-BE-FETCHED"]), FakeEnricher()
    assert make_runner(tmp_path, {"a": plugin2}, enricher=enricher2).run() == EXIT_OK
    assert plugin2.calls == 0                     # fetched.json checkpoint reused
    assert enricher2.skips == [{"A1"}]            # already-enriched record skipped
    assert keys_of(FakeLoader.loaded[0]) == ["A1", "A2", "A3"]
    assert list_runs(tmp_path)[0].run_id == run.run_id  # same run resumed


def test_disk_full_degrades_to_in_memory(tmp_path, monkeypatch):
    def full(path, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(checkpoint, "atomic_write_json", full)
    runner = make_runner(tmp_path, {"a": FakePlugin(["A1"])})
    assert runner.run() == EXIT_DEGRADED
    assert keys_of(FakeLoader.loaded[0]) == ["A1"]   # data still loaded
    assert "No space left" in runner.writer.disabled_reason


def test_concurrent_run_is_refused(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    with open(tmp_path / "run.lock", "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert make_runner(tmp_path, {"a": FakePlugin(["A1"])}).run() == EXIT_LOCKED


def test_connection_lost_mid_load_keeps_the_batch_spooled(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    FakeLoader.raise_on_load = psycopg.OperationalError("server closed the connection unexpectedly")
    assert make_runner(tmp_path, {"a": FakePlugin(["A1"])}).run() == EXIT_DB_DOWN
    [run] = list_runs(tmp_path)
    assert run.source_state("a")["stage"] == "pending_load"


def test_torn_checkpoint_line_is_skipped(tmp_path):
    path = tmp_path / "enriched.jsonl"
    path.write_text(json.dumps({"key": "A1"}) + "\n" + '{"key": "A2", "rec')
    assert [row["key"] for row in read_jsonl(path)] == ["A1"]


def test_old_unfinished_runs_are_abandoned_but_their_spool_still_loads(tmp_path):
    run = RunDir(tmp_path, "old")
    writer = checkpoint.CheckpointWriter()
    run.set_state(writer, run_id="old", started_at="2020-01-01T00:00:00+00:00", status="running")
    batch = LoadBatch("a", "old", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00",
                      [record("OLD").model_dump()])
    batch.write(run.source_dir("a") / "batch.json")
    run.set_source_state(writer, "a", "pending_load")
    assert checkpoint.resumable_run(tmp_path, 36) is None
    assert run.state()["status"] == "abandoned"
    assert [(r.run_id, s) for r, s, _ in checkpoint.pending_batches(tmp_path)] == [("old", "a")]


def _unused(*_: Any) -> Path:  # keeps the import list honest for type checkers
    return Path()
