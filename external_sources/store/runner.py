"""
The scheduled run: collect -> enrich -> spool -> load, per source, resilient end to end.

    python -m external_sources.store run --all

- Per-source isolation: a source that raises is recorded as failed (its last snapshot stays
  published, flagged stale) and the next source runs.
- Per-record isolation: a record that fails enrichment is loaded without enrichment, with an
  `enrichment_error` gap and a dead letter; a document that fails is a dead letter too.
- Checkpoints (see checkpoint.py): a crashed or killed run resumes where it stopped.
- SIGTERM/SIGINT: the current record finishes, checkpoints are flushed, the run is marked
  `interrupted` and exits 75; the next invocation resumes it.
- Database down: the batch stays spooled (`pending_load`); exit code 3; the next run loads it
  before collecting anything new.
- One run at a time: an exclusive file lock (released by the OS if the process dies).

Exit codes: 0 every source published complete; 1 degraded (a source partial, rejected or
failed: data still served, flagged); 2 usage/config error; 3 database unreachable (batches
spooled); 4 another run holds the lock; 75 interrupted (resumable).
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import socket
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from external_sources.http_cache import USER_AGENT, CachedFetcher
from external_sources.plugins.base import (
    Failure,
    FetchOutcome,
    NormalizedRecord,
    SourcePlugin,
)
from external_sources.resilience import (
    BreakerRegistry,
    ResilientSession,
    RetryingHTTP,
    RetryPolicy,
)
from external_sources.store import logs
from external_sources.store.batch import LoadBatch
from external_sources.store.checkpoint import (
    CheckpointWriter,
    RunDir,
    list_runs,
    pending_batches,
    prune_run_dirs,
    read_json,
    read_jsonl,
    resumable_run,
)
from external_sources.store.db import DatabaseUnavailable
from external_sources.store.loader import FAILED_DOC_STATUSES, Loader, LoadResult
from external_sources.store.registry import SOURCES

logger = logging.getLogger(__name__)

EXIT_OK, EXIT_DEGRADED, EXIT_USAGE, EXIT_DB_DOWN, EXIT_LOCKED, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 75


class StopRequested(BaseException):
    """Raised at a safe point after SIGTERM/SIGINT. BaseException so per-record
    `except Exception` isolation does not swallow it."""


class StopFlag:
    def __init__(self) -> None:
        self.requested = False
        self.signal: int | None = None

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle)

    def _handle(self, signum: int, frame: Any) -> None:
        self.requested = True
        self.signal = signum
        logger.warning("signal %s received: finishing the current item, then stopping", signum)

    def is_set(self) -> bool:
        return self.requested

    def check(self) -> None:
        if self.requested:
            raise StopRequested()


@dataclass
class RunOptions:
    sources: list[str]
    fresh: bool = False
    force_publish: bool = False
    documents: bool = True
    force_refresh: bool = False
    limit: int | None = None          # first N records per source (smoke tests only)
    trigger: str = "manual"


@dataclass
class SourceReport:
    source: str
    status: str
    records: int = 0
    failures: int = 0
    partial_reasons: list[str] = field(default_factory=list)
    load: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class _NullPipeline:
    """Document layer switched off: no document is fetched, records are still annotated."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def process(self, record: NormalizedRecord) -> list[Any]:
        return []


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Runner:
    def __init__(
        self,
        work: Path,
        options: RunOptions,
        policy: dict[str, Any],
        db_factory: Callable[[], Any],
        plugin_factory: Callable[[str, Runner], SourcePlugin] | None = None,
        enricher_factory: Callable[[str, Runner], Any] | None = None,
        notifier: Any = None,
        stop: StopFlag | None = None,
        loader_cls: Any = Loader,
    ) -> None:
        self.work = Path(work)
        self.loader_cls = loader_cls
        self.options = options
        self.policy = policy
        self.db_factory = db_factory
        self.plugin_factory = plugin_factory or default_plugin_factory
        self.enricher_factory = enricher_factory or default_enricher_factory
        self.notifier = notifier
        self.stop = stop or StopFlag()
        self.writer = CheckpointWriter()
        self.breakers = BreakerRegistry(failure_threshold=8, reset_timeout=300)
        self._fetcher: CachedFetcher | None = None
        self.reports: list[SourceReport] = []
        self.db_down = False

    # ---- shared resources --------------------------------------------------------------------
    @property
    def fetcher(self) -> CachedFetcher:
        if self._fetcher is None:
            runner = self.policy["runner"]
            session = ResilientSession(RetryPolicy(max_attempts=4, base_delay=2.0, max_delay=60.0),
                                       self.breakers, timeout=(10.0, 90.0), user_agent=USER_AGENT)
            self._fetcher = CachedFetcher(
                self.work / "cache" / "http", session=session, timeout=90,
                min_interval=float(runner.get("min_request_interval_s", 0.25)),
                force_refresh=self.options.force_refresh, respect_robots=True,
                fresh_for=0 if self.options.force_refresh else float(runner.get("document_revalidate_hours", 12)) * 3600,
            )
        return self._fetcher

    def http(self) -> RetryingHTTP:
        import requests
        return RetryingHTTP(lambda method, url, **kw: requests.request(method, url, **kw),
                            RetryPolicy(max_attempts=4, base_delay=2.0, max_delay=60.0), self.breakers)

    # ---- the run -------------------------------------------------------------------------------
    def run(self) -> int:
        self.work.mkdir(parents=True, exist_ok=True)
        lock_path = self.work / "run.lock"
        lock_file = open(lock_path, "a+")  # noqa: SIM115 — held for the whole run
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.error("another run holds %s", lock_path)
            lock_file.close()
            return EXIT_LOCKED
        try:
            return self._run_locked()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()

    def _run_locked(self) -> int:
        resumed = None if self.options.fresh else resumable_run(
            self.work, float(self.policy["runner"].get("resume_max_age_hours", 36)))
        run = resumed or RunDir(self.work, str(uuid.uuid4()))
        logs.RUN_ID.set(run.run_id)
        logs.configure(self.work / "logs", run.run_id)
        if resumed:
            logger.info("resuming run %s (started %s)", run.run_id, run.state().get("started_at"))
            run.set_state(self.writer, status="running", pid=os.getpid())
        else:
            run.set_state(self.writer, run_id=run.run_id, started_at=_now(), status="running", pid=os.getpid(),
                          host=socket.gethostname(), sources=self.options.sources, trigger=self.options.trigger)
        started_at = run.state().get("started_at") or _now()
        self._load_pending(exclude=run.run_id)
        interrupted = False
        for source in self.options.sources:
            logs.SOURCE.set(source)
            if run.source_state(source).get("stage") == "done":
                logger.info("already done in this run")
                continue
            try:
                self.reports.append(self._process_source(run, source, started_at))
            except StopRequested:
                run.set_source_state(self.writer, source, run.source_state(source).get("stage") or "collecting",
                                     interrupted_at=_now())
                interrupted = True
                break
            except Exception as exc:
                logger.exception("source failed")
                run.set_source_state(self.writer, source, "failed", error=f"{type(exc).__name__}: {exc}"[:1000])
                self.reports.append(SourceReport(source, "failed", error=f"{type(exc).__name__}: {exc}"[:500]))
        logs.SOURCE.set(None)
        code = self._exit_code(interrupted)
        status = "interrupted" if interrupted else "succeeded" if code == EXIT_OK else "partial"
        run.set_state(self.writer, status=status, finished_at=_now(), exit_code=code,
                      reports=[asdict(r) for r in self.reports],
                      checkpoint_disabled=self.writer.disabled_reason)
        if not interrupted:
            self._finish(run, status, code)
        logger.info("run finished: %s (exit %s)", status, code, extra={"exit_code": code})
        return code

    def _exit_code(self, interrupted: bool) -> int:
        if interrupted:
            return EXIT_INTERRUPTED
        if self.db_down:
            return EXIT_DB_DOWN
        if all(r.status == "published" for r in self.reports) and not self.writer.disabled_reason:
            return EXIT_OK
        return EXIT_DEGRADED

    def _finish(self, run: RunDir, status: str, code: int) -> None:
        summary = {"reports": [asdict(r) for r in self.reports], "exit_code": code,
                   "checkpoint_disabled": self.writer.disabled_reason}
        try:
            conn = self.db_factory()
        except DatabaseUnavailable:
            return
        try:
            loader = self.loader_cls(conn, self.policy)
            loader.finish_run(run.run_id, "failed" if code == EXIT_DB_DOWN else status, summary)
            loader.prune()
        except Exception:
            logger.exception("run bookkeeping/retention failed")
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            prune_run_dirs(self.work, float(self.policy["retention"].get("run_dir_days", 14)))
        except OSError as exc:
            logger.warning("could not prune run directories: %s", exc)

    # ---- spool ---------------------------------------------------------------------------------
    def _load_pending(self, exclude: str) -> None:
        for run, source, path in pending_batches(self.work, exclude_run=exclude):
            logs.SOURCE.set(source)
            try:
                batch = LoadBatch.read(path)
            except (OSError, ValueError) as exc:
                logger.error("spooled batch %s unreadable: %s", path, exc)
                run.set_source_state(self.writer, source, "failed", error=f"unreadable batch: {exc}")
                continue
            logger.info("loading spooled batch from run %s", run.run_id)
            result = self._load(batch)
            if result is not None:
                run.set_source_state(self.writer, source, "done", load=asdict(result))
                if run.state().get("status") in ("partial", "abandoned"):
                    run.set_state(self.writer, pending_loaded_at=_now())
        logs.SOURCE.set(None)

    def _load(self, batch: LoadBatch) -> LoadResult | None:
        """Load one batch; None when the database is unreachable (batch stays spooled)."""
        try:
            conn = self.db_factory()
        except DatabaseUnavailable as exc:
            logger.error("database unavailable, batch kept in the spool: %s", exc)
            self.db_down = True
            return None
        try:
            loader = self.loader_cls(conn, self.policy, self.notifier)
            if not batch.records and not batch.complete:
                return loader.mark_failed(batch, "; ".join(batch.partial_reasons) or "no records")
            return loader.load(batch, force_publish=self.options.force_publish)
        except Exception as exc:
            from external_sources.store.db import is_connection_error
            if is_connection_error(exc):
                logger.error("database connection lost during load, batch kept in the spool: %s",
                             str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
                self.db_down = True
                return None
            raise
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- one source ------------------------------------------------------------------------------
    def _process_source(self, run: RunDir, source: str, run_started_at: str) -> SourceReport:
        directory = run.source_dir(source)
        state = run.source_state(source)
        source_started = state.get("started_at") or _now()
        if state.get("stage") == "pending_load" and (directory / "batch.json").exists():
            batch = LoadBatch.read(directory / "batch.json")
        else:
            outcome, fetched_at = self._collect(run, source, source_started)
            records, documents, failures = self._enrich(run, source, outcome)
            batch = self._build_batch(run, source, run_started_at, source_started, fetched_at, outcome,
                                      records, documents, failures)
        result = self._load(batch)
        if result is None:
            return SourceReport(source, "spooled", len(batch.records), len(batch.failures), batch.partial_reasons)
        run.set_source_state(self.writer, source, "done", load=asdict(result))
        status = result.status if result.status in ("published", "partial", "rejected", "failed") else "published"
        if result.status in ("already_loaded",):
            status = "published" if batch.complete else "partial"
        return SourceReport(source, status, len(batch.records), len(batch.failures), batch.partial_reasons,
                            load={"status": result.status, "snapshot_id": result.snapshot_id,
                                  "counts": result.counts, "changes": result.changes,
                                  "validation_ok": result.validation.get("ok") if result.validation else None})

    def _collect(self, run: RunDir, source: str, started: str) -> tuple[FetchOutcome, str]:
        path = run.source_dir(source) / "fetched.json"
        saved = read_json(path)
        if isinstance(saved, dict) and "records" in saved:
            logger.info("resume: using the fetched checkpoint (%d records)", len(saved["records"]))
            outcome = FetchOutcome([NormalizedRecord.model_validate(r) for r in saved["records"]],
                                   [Failure(**f) for f in saved.get("failures", [])], saved.get("partial_reasons", []))
            return outcome, saved["fetched_at"]
        run.set_source_state(self.writer, source, "collecting", started_at=started)
        plugin = self.plugin_factory(source, self)
        plugin.stop_requested = self.stop.is_set
        fetched_at = _now()
        outcome = plugin.collect()
        if self.stop.is_set():
            raise StopRequested()
        if self.options.limit:
            outcome.records = outcome.records[: self.options.limit]
        logger.info("collected %d records, %d failures, partial=%s", len(outcome.records), len(outcome.failures),
                    bool(outcome.partial_reasons), extra={"event": "collected", "count": len(outcome.records)})
        self.writer.write_json(path, {"fetched_at": fetched_at, "records": [r.model_dump() for r in outcome.records],
                                      "failures": [asdict(f) for f in outcome.failures],
                                      "partial_reasons": outcome.partial_reasons})
        run.set_source_state(self.writer, source, "fetched", records=len(outcome.records))
        return outcome, fetched_at

    def _enrich(self, run: RunDir, source: str, outcome: FetchOutcome
                ) -> tuple[list[NormalizedRecord], dict[str, list[dict[str, Any]]], list[Failure]]:
        path = run.source_dir(source) / "enriched.jsonl"
        done: dict[str, dict[str, Any]] = {row["key"]: row for row in read_jsonl(path) if "key" in row}
        if done:
            logger.info("resume: %d of %d records already enriched", len(done), len(outcome.records))
        run.set_source_state(self.writer, source, "enriching")
        enricher = self.enricher_factory(source, self)
        pipeline = enricher.pipeline
        original = pipeline.process

        def process(record: NormalizedRecord) -> list[Any]:
            self.stop.check()
            return original(record)

        pipeline.process = process
        memory: dict[str, dict[str, Any]] = dict(done)
        for record, enriched, documents, error in enricher.iter_enrich(outcome.records, skip=set(done)):
            if enriched is None:
                gap = {"field": "enrichment", "kind": "enrichment_error", "error": error}
                enriched = record.model_copy(update={"data_gaps": [*record.data_gaps, gap]})
            row = {"key": record.natural_key, "record": enriched.model_dump(),
                   "docs": [d.to_dict() for d in documents], "error": error}
            memory[record.natural_key] = row
            self.writer.append_line(path, row)
            self.stop.check()
        pipeline.process = original
        records: list[NormalizedRecord] = []
        documents_index: dict[str, list[dict[str, Any]]] = {}
        failures = list(outcome.failures)
        for record in outcome.records:
            row = memory.get(record.natural_key)
            if row is None:  # never reached (should not happen): keep the fetched record
                records.append(record)
                continue
            records.append(NormalizedRecord.model_validate(row["record"]))
            documents_index[record.natural_key] = row.get("docs") or []
            if row.get("error"):
                failures.append(Failure("enrich", record.natural_key, str(row["error"])))
        seen_urls: set[str] = set()
        for docs in documents_index.values():
            for doc in docs:
                if doc.get("status") in FAILED_DOC_STATUSES and doc.get("url") not in seen_urls:
                    seen_urls.add(doc["url"])
                    failures.append(Failure("document", doc["url"], f"{doc['status']}: {doc.get('error') or ''}"[:1000],
                                            {"role": doc.get("role"), "title": doc.get("title")}))
        return records, documents_index, failures

    def _build_batch(self, run: RunDir, source: str, run_started_at: str, source_started: str, fetched_at: str,
                     outcome: FetchOutcome, records: list[NormalizedRecord],
                     documents: dict[str, list[dict[str, Any]]], failures: list[Failure]) -> LoadBatch:
        docs = [d for items in documents.values() for d in items]
        partial = list(outcome.partial_reasons)
        if self.writer.disabled_reason:
            logger.warning("checkpointing disabled: %s", self.writer.disabled_reason)
        stats = {"host": socket.gethostname(), "pid": os.getpid(), "source_started_at": source_started,
                 "documents_total": len({d.get("url") for d in docs}),
                 "documents_failed": len({d.get("url") for d in docs if d.get("status") in FAILED_DOC_STATUSES}),
                 "failures_by_stage": _count(f.stage for f in failures),
                 "checkpoint_disabled": self.writer.disabled_reason,
                 "breakers": self.breakers.snapshot()}
        batch = LoadBatch(source, run.run_id, run_started_at, fetched_at, [r.model_dump() for r in records],
                          documents, [asdict(f) for f in failures], partial, stats,
                          str(self.work / "documents"))
        if not self.writer.write_json(run.source_dir(source) / "batch.json", json_ready(batch)):
            logger.warning("batch could not be spooled to disk; loading from memory")
        run.set_source_state(self.writer, source, "pending_load", records=len(records), failures=len(failures))
        return batch


def json_ready(batch: LoadBatch) -> dict[str, Any]:
    return asdict(batch)


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def default_plugin_factory(source: str, runner: Runner) -> SourcePlugin:
    if source not in SOURCES:
        raise KeyError(f"unknown source {source!r}; known: {sorted(SOURCES)}")
    info = SOURCES[source]
    plugin = info.build(discover_all=True, http=runner.http())
    if info.with_detail and getattr(plugin, "detail_config", None):
        from external_sources.enrichment.detail import DetailEnricher
        plugin.detail_enricher = DetailEnricher(runner.fetcher, **plugin.detail_config)
    return plugin


def default_enricher_factory(source: str, runner: Runner) -> Any:
    from external_sources.documents.pipeline import DocumentPipeline
    from external_sources.documents.policy import load_policy
    from external_sources.documents.profiles import load_profiles
    from external_sources.documents.store import DocumentStore
    from external_sources.enrich import Enricher
    from external_sources.extraction.fields import load_families
    from external_sources.quality import load_quality_policy

    store = DocumentStore(runner.work / "documents")
    profiles = load_profiles()
    pipeline: Any = DocumentPipeline(runner.fetcher, store, load_policy(), profiles) \
        if runner.options.documents else _NullPipeline(store)
    quality = load_quality_policy(source)
    return Enricher(pipeline, load_families(), list(quality["required_fields"]), quality.get("required_any"),
                    profiles)


def latest_runs(work: Path, limit: int = 5) -> list[dict[str, Any]]:
    return [run.state() for run in list_runs(work)[-limit:]]
