"""
Load a `LoadBatch` into Postgres with full-replace semantics, done safely:

1. **Stage** (transaction 1): insert a `snapshot` in status `staging` with every record,
   provenance row, resolved value and document link; records absent from this run but still in
   their grace period are carried over from the published snapshot (`carried_over = true`).
   Readers never see staging rows (`current.*` follows `source_state.published_snapshot_id`).
2. **Validate**: record count vs the previous snapshot, required-field coverage, referential
   integrity, empty batch. Hard failures reject the snapshot; soft ones only alert.
3. **Publish** (transaction 2): move the source's pointer to the new snapshot, update change
   tracking/history, programmes and calls, resolve dead letters that succeeded now.
   On rejection the previous snapshot stays published and the source is flagged stale.

Idempotent (a batch already loaded is a no-op, keyed by its sha256), resumable (a staged but
unpublished snapshot is re-validated, not re-inserted) and serialised per source with an
advisory lock. Records are never hard-deleted on one absence: they expire after `grace_runs`
complete runs (a partial run never counts as an absence) or when every deadline has passed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from external_sources.documents.store import DocumentStore
from external_sources.plugins.base import record_hash
from external_sources.quality import load_quality_policy
from external_sources.store.batch import LoadBatch
from external_sources.store.chunks import chunk_markdown
from external_sources.store.registry import SOURCES
from external_sources.store.rows import provenance_rows, record_row, resolved_rows

logger = logging.getLogger(__name__)

USABLE_DOC_STATUSES = ("fresh", "not_modified", "stale")
FAILED_DOC_STATUSES = ("not_found", "error", "convert_error", "unsupported_type", "too_large")

RECORD_COLUMNS = (
    "snapshot_id", "source", "natural_key", "record_type", "record_kind", "domain", "programme",
    "programme_key", "call_identifier", "call_title", "country_scope", "title", "description", "status",
    "funding_amount", "currency", "start_date", "deadline_date", "deadline_dates", "source_url",
    "source_updated_at", "details", "field_sources", "data_gaps", "raw_sha256", "content_hash",
    "fetched_at", "change_state", "carried_over", "absent_runs", "search_extra", "embed_text_sha256",
)
FIELD_VALUE_COLUMNS = (
    "snapshot_id", "natural_key", "field", "layer", "role", "value", "value_text", "value_num", "value_ts",
    "source_url", "doc_id", "page", "section", "evidence_quote", "retrieved_at", "published_at",
    "content_hash", "confidence", "note",
)
RESOLVED_COLUMNS = (
    "snapshot_id", "natural_key", "field", "value", "value_num", "value_ts", "status", "value_min_num",
    "value_max_num", "value_min_ts", "value_max_ts", "settled", "agree_count", "basis", "contributing",
    "sources", "checked_at",
)
RECORD_DOCUMENT_COLUMNS = (
    "snapshot_id", "natural_key", "url", "doc_id", "role", "title", "status", "error", "content_sha256", "chars",
)
JSON_COLUMNS = {"details", "field_sources", "data_gaps", "value", "contributing", "sources", "payload",
                "partial_reasons", "validation", "errors", "stats", "options", "summary", "body"}
CASTS = {"deadline_dates": "::timestamptz[]", "start_date": "::timestamptz", "deadline_date": "::timestamptz",
         "source_updated_at": "::timestamptz", "fetched_at": "::timestamptz", "value_ts": "::timestamptz",
         "retrieved_at": "::timestamptz", "published_at": "::timestamptz", "value_min_ts": "::timestamptz",
         "value_max_ts": "::timestamptz", "checked_at": "::timestamptz"}
# Top-level columns whose coverage can be validated directly (whitelist: never user input).
COVERAGE_COLUMNS = {"title", "description", "status", "funding_amount", "start_date", "deadline_date",
                    "source_url", "programme", "call_identifier"}


@dataclass
class LoadResult:
    source: str
    status: str                      # published | partial | rejected | already_loaded | obsolete
    snapshot_id: int | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    changes: dict[str, int] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _jsonb(value: Any) -> Any:
    from psycopg.types.json import Jsonb
    return Jsonb(value)


def strip_nul(value: Any) -> Any:
    """Postgres text/jsonb cannot hold NUL characters: drop them anywhere in a value."""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_nul(v) for v in value]
    return value


def _row_values(row: dict[str, Any], columns: Iterable[str]) -> tuple[Any, ...]:
    values = []
    for column in columns:
        value = strip_nul(row.get(column))
        values.append(_jsonb(value) if column in JSON_COLUMNS and value is not None else value)
    return tuple(values)


def insert_many(cur: Any, table: str, columns: tuple[str, ...], rows: list[dict[str, Any]],
                conflict: str = "") -> None:
    """Parameterised bulk insert; `table`/`columns` are module constants, never user input."""
    if not rows:
        return
    from psycopg import sql
    schema, name = table.split(".")
    statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) {}").format(
        sql.Identifier(schema), sql.Identifier(name),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.SQL(", ").join(sql.SQL("%s" + CASTS.get(c, "")) for c in columns),
        sql.SQL(conflict),
    )
    cur.executemany(statement, [_row_values(r, columns) for r in rows])


def _alert(level: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"level": level, "code": code, "message": message, **extra}


class Loader:
    """Loads batches into the store. `conn` is an autocommit psycopg connection."""

    def __init__(self, conn: Any, store_policy: dict[str, Any], notifier: Any = None) -> None:
        self.conn = conn
        self.policy = store_policy
        self.notifier = notifier

    # ---- helpers ---------------------------------------------------------------------------
    def _one(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()

    def _all(self, query: str, params: tuple[Any, ...] = ()) -> list[Any]:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def _quality(self, source: str) -> dict[str, Any]:
        try:
            return load_quality_policy(source)
        except (KeyError, ValueError):
            return {"required_fields": ["title", "source_url"]}

    def ensure_source(self, source: str) -> None:
        info = SOURCES.get(source)
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO extsrc.source (name, description, base_url, licence, attribution)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description,
                       base_url = EXCLUDED.base_url, licence = EXCLUDED.licence,
                       attribution = EXCLUDED.attribution, updated_at = now()""",
                (source, info.description if info else None, info.base_url if info else None,
                 info.licence if info else None, info.attribution if info else None))
            cur.execute("INSERT INTO extsrc.source_state (source) VALUES (%s) ON CONFLICT DO NOTHING", (source,))

    def record_run(self, batch: LoadBatch) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO extsrc.run (run_id, started_at, status, host, pid)
                   VALUES (%s, %s::timestamptz, 'running', %s, %s) ON CONFLICT (run_id) DO NOTHING""",
                (batch.run_id, batch.run_started_at, batch.stats.get("host"), batch.stats.get("pid")))
            cur.execute(
                """INSERT INTO extsrc.source_run (run_id, source, started_at, records_fetched, records_failed,
                       documents_total, documents_failed, partial_reasons, errors, stats)
                   VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (run_id, source) DO UPDATE SET records_fetched = EXCLUDED.records_fetched,
                       records_failed = EXCLUDED.records_failed, documents_total = EXCLUDED.documents_total,
                       documents_failed = EXCLUDED.documents_failed, partial_reasons = EXCLUDED.partial_reasons,
                       errors = EXCLUDED.errors, stats = EXCLUDED.stats""",
                (batch.run_id, batch.source, batch.stats.get("source_started_at") or batch.run_started_at,
                 len(batch.records), len(batch.failures), batch.stats.get("documents_total"),
                 batch.stats.get("documents_failed"), _jsonb(batch.partial_reasons),
                 _jsonb([{"stage": f.get("stage"), "key": f.get("key"), "error": (f.get("error") or "")[:500]}
                         for f in batch.failures[:200]]), _jsonb(batch.stats)))

    def _finish_source_run(self, batch: LoadBatch, status: str, result: LoadResult) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE extsrc.source_run SET finished_at = now(), status = %s, snapshot_id = %s,
                       records_loaded = %s, records_carried = %s, validation = %s
                   WHERE run_id = %s AND source = %s""",
                (status, result.snapshot_id, result.counts.get("records"), result.counts.get("carried"),
                 _jsonb(result.validation) if result.validation else None, batch.run_id, batch.source))

    # ---- dead letters ----------------------------------------------------------------------
    def record_failures(self, batch: LoadBatch) -> int:
        """Upsert every failure of the batch into the dead-letter table (attempts counted)."""
        rows = []
        for failure in batch.failures:
            payload = failure.get("payload")
            rows.append((batch.source, failure.get("stage") or "unknown",
                         strip_nul(str(failure.get("key") or "?"))[:1000], strip_nul(failure.get("error") or "")[:4000],
                         _jsonb(strip_nul(payload)) if payload is not None else None,
                         bool(failure.get("retryable", True)), batch.run_id, batch.run_id))
        if not rows:
            return 0
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO extsrc.dead_letter (source, stage, item_key, error, payload, retryable,
                       first_run_id, last_run_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (source, stage, item_key) WHERE resolved_at IS NULL DO UPDATE SET
                       error = EXCLUDED.error, payload = coalesce(EXCLUDED.payload, extsrc.dead_letter.payload),
                       attempts = extsrc.dead_letter.attempts
                                  + CASE WHEN extsrc.dead_letter.last_run_id = EXCLUDED.last_run_id THEN 0 ELSE 1 END,
                       last_run_id = EXCLUDED.last_run_id, last_failed_at = now()""", rows)
        return len(rows)

    def _resolve_dead_letters(self, cur: Any, batch: LoadBatch, loaded_keys: list[str], doc_urls: list[str]) -> None:
        failed = {(f.get("stage"), str(f.get("key"))) for f in batch.failures}
        open_rows = self._all(
            "SELECT id, stage, item_key FROM extsrc.dead_letter WHERE source = %s AND resolved_at IS NULL",
            (batch.source,))
        loaded, urls = set(loaded_keys), set(doc_urls)
        resolve = []
        for letter_id, stage, key in open_rows:
            if (stage, key) in failed:
                continue
            if stage in ("normalize", "detail", "enrich") and key in loaded:
                resolve.append((f"loaded in run {batch.run_id}", letter_id))
            elif stage == "document" and key in urls:
                resolve.append((f"document fetched in run {batch.run_id}", letter_id))
            elif stage in ("fetch", "load") and batch.complete:
                resolve.append((f"complete run {batch.run_id}", letter_id))
        if resolve:
            cur.executemany("UPDATE extsrc.dead_letter SET resolved_at = now(), resolution = %s WHERE id = %s",
                            resolve)

    # ---- documents -------------------------------------------------------------------------
    def _load_documents(self, cur: Any, batch: LoadBatch) -> tuple[list[dict[str, Any]], dict[str, str], int]:
        """Insert documents/versions/chunks; returns record_document rows, doc_id->retrieved, chunks."""
        store = DocumentStore(batch.documents_dir) if batch.documents_dir else None
        seen_docs: dict[str, dict[str, Any]] = {}
        retrieved: dict[str, str] = {}
        links: list[dict[str, Any]] = []
        chunk_count = 0
        known_versions = {(d, s) for d, s in self._all(
            "SELECT doc_id, content_sha256 FROM extsrc.document_version WHERE doc_id = ANY(%s)",
            ([d.get("doc_id") for docs in batch.documents.values() for d in docs if d.get("doc_id")],))}
        for key, docs in batch.documents.items():
            for doc in docs:
                url = str(doc.get("url") or "")
                if not url:
                    continue
                doc_id = doc.get("doc_id")
                sha = None
                if doc_id and doc_id not in seen_docs:
                    stored = store.load_by_id(doc_id) if store else None
                    meta = stored.meta if stored else {}
                    sha = meta.get("content_sha256")
                    seen_docs[doc_id] = {"sha": sha}
                    retrieved[doc_id] = meta.get("stored_at") or meta.get("fetched_at") or batch.fetched_at
                    cur.execute(
                        """INSERT INTO extsrc.document (doc_id, url, resolved_url, title, role, mime, latest_sha256,
                               last_status, last_error)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (doc_id) DO UPDATE SET url = EXCLUDED.url,
                               resolved_url = coalesce(EXCLUDED.resolved_url, extsrc.document.resolved_url),
                               title = coalesce(EXCLUDED.title, extsrc.document.title), role = EXCLUDED.role,
                               mime = coalesce(EXCLUDED.mime, extsrc.document.mime),
                               latest_sha256 = coalesce(EXCLUDED.latest_sha256, extsrc.document.latest_sha256),
                               last_status = EXCLUDED.last_status, last_error = EXCLUDED.last_error,
                               last_seen = now()""",
                        (doc_id, url, meta.get("resolved_url"), doc.get("title"), doc.get("role"),
                         meta.get("content_type"), sha, doc.get("status") if stored else "missing_in_store",
                         doc.get("error")))
                    if stored is not None and sha and (doc_id, sha) not in known_versions:
                        cur.execute(
                            """INSERT INTO extsrc.document_version (doc_id, content_sha256, text, chars, mime, etag,
                                   last_modified, retrieved_at)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz) ON CONFLICT DO NOTHING""",
                            (doc_id, sha, stored.markdown.replace("\x00", ""), len(stored.markdown),
                             meta.get("content_type"), meta.get("etag"), meta.get("last_modified"),
                             retrieved[doc_id]))
                        chunks = chunk_markdown(stored.markdown.replace("\x00", ""))
                        cur.executemany(
                            """INSERT INTO extsrc.document_chunk (doc_id, content_sha256, chunk_no, heading,
                                   page_start, page_end, text, text_sha256)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                            [(doc_id, sha, c.chunk_no, c.heading, c.page_start, c.page_end, c.text, c.text_sha256)
                             for c in chunks])
                        chunk_count += len(chunks)
                        known_versions.add((doc_id, sha))
                elif doc_id:
                    sha = seen_docs[doc_id]["sha"]
                links.append({"natural_key": key, "url": url, "doc_id": doc_id, "role": doc.get("role"),
                              "title": doc.get("title"), "status": doc.get("status") or "unknown",
                              "error": doc.get("error"), "content_sha256": sha, "chars": doc.get("chars")})
        return links, retrieved, chunk_count

    # ---- tracking --------------------------------------------------------------------------
    def _classify(self, batch: LoadBatch, records: list[Any]) -> dict[str, Any]:
        grace = int(self.policy["tracking"].get("grace_runs", 2))
        entries = {row[0]: {"hash": row[1], "absent_runs": row[2], "expired_at": row[3]} for row in self._all(
            "SELECT natural_key, last_hash, absent_runs, expired_at FROM extsrc.record_tracking WHERE source = %s",
            (batch.source,))}
        current = {r.natural_key: record_hash(r) for r in records}
        out: dict[str, Any] = {"new": [], "changed": [], "unchanged": [], "missing": {}, "expired": {},
                               "hashes": current}
        for key, digest in current.items():
            entry = entries.get(key)
            if entry is None or entry["expired_at"] is not None:
                out["new"].append(key)
            elif entry["hash"] != digest:
                out["changed"].append(key)
            else:
                out["unchanged"].append(key)
        for key, entry in entries.items():
            if key in current or entry["expired_at"] is not None:
                continue
            absent = entry["absent_runs"] + (1 if batch.complete else 0)
            if batch.complete and absent >= grace:
                out["expired"][key] = (absent, f"absent from {absent} consecutive complete runs")
            else:
                out["missing"][key] = absent
        return out

    # ---- validation ------------------------------------------------------------------------
    def _coverage(self, snapshot_id: int, fields: list[str]) -> dict[str, float]:
        from psycopg import sql
        total = self._one("SELECT count(*) FROM extsrc.external_record WHERE snapshot_id = %s AND NOT carried_over",
                          (snapshot_id,))[0]
        out: dict[str, float] = {}
        if not total:
            return {f: 0.0 for f in fields}
        for name in fields:
            if name.startswith("details."):
                query = sql.SQL(
                    "SELECT count(*) FROM extsrc.external_record WHERE snapshot_id = %s AND NOT carried_over "
                    "AND details ? %s AND details -> %s NOT IN ('null'::jsonb, '\"\"'::jsonb, '[]'::jsonb, '{}'::jsonb)")
                params: tuple[Any, ...] = (snapshot_id, name[8:], name[8:])
            elif name in COVERAGE_COLUMNS:
                column = sql.Identifier(name)
                query = sql.SQL("SELECT count(*) FROM extsrc.external_record WHERE snapshot_id = %s "
                                "AND NOT carried_over AND {} IS NOT NULL AND {}::text <> ''").format(column, column)
                params = (snapshot_id,)
            else:
                continue
            with self.conn.cursor() as cur:
                cur.execute(query, params)
                out[name] = round(100.0 * cur.fetchone()[0] / total, 2)
        return out

    def validate(self, batch: LoadBatch, snapshot_id: int, previous_id: int | None,
                 fetched: int) -> dict[str, Any]:
        rules = self.policy["validation"]
        quality = self._quality(batch.source)
        checks: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        previous_fetched = None
        if previous_id is not None:
            previous_fetched = self._one("SELECT fetched_count FROM extsrc.snapshot WHERE snapshot_id = %s",
                                         (previous_id,))[0]
        if fetched == 0:
            # Empty is only plausible when the source was empty before too, or (first load) when
            # the run was complete; an empty PARTIAL run is a failure, never "no calls".
            if previous_fetched:
                ok = not rules.get("reject_empty_when_previous", True)
            else:
                ok = batch.complete
            checks.append({"check": "non_empty", "ok": ok,
                           "detail": f"0 records fetched (previous: {previous_fetched}, complete: {batch.complete})"})
        ratio_floor = float(quality.get("min_records_ratio") or rules["default_min_records_ratio"])
        if previous_fetched and previous_fetched >= int(rules.get("min_records_base", 10)) and fetched:
            ratio = fetched / previous_fetched
            checks.append({"check": "record_count_ratio", "ok": ratio >= ratio_floor,
                           "detail": f"{fetched}/{previous_fetched} = {ratio:.2f} (floor {ratio_floor})"})
        required = [f for f in quality.get("required_fields", []) if f in COVERAGE_COLUMNS or f.startswith("details.")]
        coverage = self._coverage(snapshot_id, required + [f for f in ("funding_amount",) if f not in required])
        previous_coverage = self._coverage(previous_id, required) if previous_id is not None else {}
        max_drop = float(rules.get("max_required_drop_pp", 25.0))
        for name in required:
            before, after = previous_coverage.get(name), coverage.get(name, 0.0)
            if before is not None and fetched:
                checks.append({"check": f"coverage:{name}", "ok": before - after <= max_drop,
                               "detail": f"{before}% -> {after}% (max drop {max_drop} pp)"})
        floors = quality.get("coverage_floor") or {}
        for name, floor in floors.items():
            if name in coverage and coverage[name] < 100.0 * float(floor):
                alerts.append(_alert("warning", "coverage_floor",
                                     f"{name} coverage {coverage[name]}% < floor {100 * float(floor):.0f}%"))
        orphans = self._one(
            """SELECT count(*) FROM extsrc.record_document rd LEFT JOIN extsrc.document d ON d.doc_id = rd.doc_id
               WHERE rd.snapshot_id = %s AND rd.doc_id IS NOT NULL AND d.doc_id IS NULL""", (snapshot_id,))[0]
        unresolved = self._one(
            """SELECT count(*) FROM extsrc.external_record r WHERE r.snapshot_id = %s AND NOT EXISTS (
                   SELECT 1 FROM extsrc.resolved_value v WHERE v.snapshot_id = r.snapshot_id
                   AND v.natural_key = r.natural_key AND v.field = 'title')""", (snapshot_id,))[0]
        checks.append({"check": "referential_integrity", "ok": orphans == 0 and unresolved == 0,
                       "detail": f"{orphans} orphan document links, {unresolved} records without resolved values"})
        no_url = self._one("SELECT count(*) FROM extsrc.external_record WHERE snapshot_id = %s AND source_url = ''",
                           (snapshot_id,))[0]
        if no_url:
            alerts.append(_alert("warning", "missing_source_url", f"{no_url} records have no citable URL"))
        if batch.partial_reasons:
            alerts.append(_alert("warning", "partial_run", "partial run: " + "; ".join(batch.partial_reasons)[:500]))
        if batch.failures:
            alerts.append(_alert("info", "dead_letters", f"{len(batch.failures)} item(s) failed and were dead-lettered"))
        ok = all(c["ok"] for c in checks)
        if not ok:
            failed = "; ".join(f"{c['check']}: {c['detail']}" for c in checks if not c["ok"])
            alerts.insert(0, _alert("critical", "snapshot_rejected",
                                    f"new snapshot rejected, last good data kept ({failed})"))
        return {"ok": ok, "checks": checks, "coverage": coverage, "previous_coverage": previous_coverage,
                "alerts": alerts}

    # ---- the load ----------------------------------------------------------------------------
    def load(self, batch: LoadBatch, force_publish: bool = False) -> LoadResult:
        self.ensure_source(batch.source)
        self.record_run(batch)
        self.record_failures(batch)
        lock = self._one("SELECT hashtext(%s)", ("extsrc_load:" + batch.source,))[0]
        self._one("SELECT pg_advisory_lock(%s)", (lock,))
        try:
            result = self._load_locked(batch, force_publish)
        finally:
            self._one("SELECT pg_advisory_unlock(%s)", (lock,))
        if self.notifier is not None and result.alerts:
            try:
                self.notifier.notify(result.alerts, batch.source)
            except Exception as exc:  # noqa: BLE001 — an alert channel failure never fails the load
                logger.warning("alert notification failed: %s", exc)
        return result

    def _load_locked(self, batch: LoadBatch, force_publish: bool) -> LoadResult:
        digest = batch.batch_sha256
        state = self._one("""SELECT st.published_snapshot_id, sn.fetched_at FROM extsrc.source_state st
                             LEFT JOIN extsrc.snapshot sn ON sn.snapshot_id = st.published_snapshot_id
                             WHERE st.source = %s""", (batch.source,))
        published_id, published_fetched = state if state else (None, None)
        existing = self._one("SELECT snapshot_id, status FROM extsrc.snapshot WHERE source = %s AND batch_sha256 = %s",
                             (batch.source, digest))
        if existing and existing[1] in ("published", "superseded"):
            result = LoadResult(batch.source, "already_loaded", existing[0])
            self._finish_source_run(batch, "already_loaded", result)
            return result
        batch_time = datetime.fromisoformat(batch.fetched_at)
        if published_fetched is not None and batch_time.tzinfo and published_fetched > batch_time:
            result = LoadResult(batch.source, "obsolete", None, alerts=[_alert(
                "warning", "obsolete_batch", f"batch fetched {batch.fetched_at} is older than the published data")])
            self._finish_source_run(batch, "obsolete", result)
            return result

        records, seen = [], set()
        for record in batch.normalized_records():
            if record.natural_key in seen:
                logger.warning("%s: duplicate natural key %s in batch; first kept", batch.source, record.natural_key)
                continue
            seen.add(record.natural_key)
            records.append(record)
        changes = self._classify(batch, records)
        if existing:  # staged (or rejected) earlier: re-validate, do not re-insert
            snapshot_id = existing[0]
            counts = dict(zip(("records", "carried"), self._one(
                "SELECT record_count, carried_count FROM extsrc.snapshot WHERE snapshot_id = %s", (snapshot_id,))))
            doc_urls = [d.get("url") for docs in batch.documents.values() for d in docs
                        if d.get("status") in USABLE_DOC_STATUSES]
            expired_deadline: dict[str, Any] = {}
        else:
            snapshot_id, counts, doc_urls, expired_deadline = self._stage(batch, records, changes, published_id)
        changes["expired"].update(expired_deadline)
        for key in expired_deadline:
            changes["missing"].pop(key, None)

        validation = self.validate(batch, snapshot_id, published_id, len(records))
        result = LoadResult(batch.source, "rejected", snapshot_id, validation,
                            {k: len(v) for k, v in changes.items() if k != "hashes"}, list(validation["alerts"]), counts)
        if not validation["ok"] and not force_publish:
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.execute("UPDATE extsrc.snapshot SET status = 'rejected', validation = %s WHERE snapshot_id = %s",
                            (_jsonb(validation), snapshot_id))
                cur.execute(
                    """UPDATE extsrc.source_state SET last_attempt_at = now(), last_status = 'rejected',
                           consecutive_failures = consecutive_failures + 1,
                           stale_since = coalesce(stale_since, now()), stale_reason = %s, updated_at = now()
                       WHERE source = %s""",
                    ("new data rejected by validation; serving the last good snapshot", batch.source))
            self._finish_source_run(batch, "rejected", result)
            return result
        if force_publish and not validation["ok"]:
            validation["forced"] = True
            result.alerts.append(_alert("warning", "forced_publish", "validation failed but publish was forced"))
        self._publish(batch, snapshot_id, published_id, changes, records, doc_urls, validation)
        result.status = "partial" if batch.partial_reasons else "published"
        self._finish_source_run(batch, result.status, result)
        return result

    def _stage(self, batch: LoadBatch, records: list[Any], changes: dict[str, Any],
               published_id: int | None) -> tuple[int, dict[str, int], list[str], dict[str, Any]]:
        new_keys = set(changes["new"])
        changed = set(changes["changed"])
        record_rows, payloads, provenance, resolved = [], {}, [], []
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO extsrc.snapshot (source, run_id, batch_sha256, fetched_at, status, partial,
                       partial_reasons, fetched_count)
                   VALUES (%s, %s, %s, %s::timestamptz, 'staging', %s, %s, %s) RETURNING snapshot_id""",
                (batch.source, batch.run_id, batch.batch_sha256, batch.fetched_at, bool(batch.partial_reasons),
                 _jsonb(batch.partial_reasons), len(records)))
            snapshot_id = cur.fetchone()[0]
            links, retrieved, chunk_count = self._load_documents(cur, batch)
            for record in records:
                row, raw_sha, raw = record_row(record, batch.source, batch.fetched_at)
                row["snapshot_id"] = snapshot_id
                row["change_state"] = "new" if record.natural_key in new_keys else \
                    "changed" if record.natural_key in changed else "unchanged"
                row["carried_over"], row["absent_runs"] = False, 0
                record_rows.append(row)
                payloads[raw_sha] = raw
                for prov in provenance_rows(record, batch.fetched_at, retrieved):
                    prov["snapshot_id"] = snapshot_id
                    provenance.append(prov)
                for res in resolved_rows(record):
                    res["snapshot_id"] = snapshot_id
                    resolved.append(res)
            cur.executemany("INSERT INTO extsrc.payload (sha256, body) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            [(sha, _jsonb(strip_nul(body))) for sha, body in payloads.items()])
            insert_many(cur, "extsrc.external_record", RECORD_COLUMNS, record_rows)
            insert_many(cur, "extsrc.field_value", FIELD_VALUE_COLUMNS, provenance)
            insert_many(cur, "extsrc.resolved_value", RESOLVED_COLUMNS, resolved)
            for link in links:
                link["snapshot_id"] = snapshot_id
            insert_many(cur, "extsrc.record_document", RECORD_DOCUMENT_COLUMNS, links,
                        "ON CONFLICT (snapshot_id, natural_key, url) DO NOTHING")
            carried, expired_deadline = self._carry_over(cur, snapshot_id, published_id, changes["missing"])
            cur.execute("UPDATE extsrc.snapshot SET record_count = %s, carried_count = %s WHERE snapshot_id = %s",
                        (len(record_rows) + carried, carried, snapshot_id))
        doc_urls = [link["url"] for link in links if link["status"] in USABLE_DOC_STATUSES]
        counts = {"records": len(record_rows) + carried, "carried": carried, "provenance": len(provenance),
                  "resolved": len(resolved), "document_links": len(links), "chunks": chunk_count}
        return snapshot_id, counts, doc_urls, expired_deadline

    def _carry_over(self, cur: Any, snapshot_id: int, published_id: int | None,
                    missing: dict[str, int]) -> tuple[int, dict[str, Any]]:
        """Copy records absent from this run (in grace) from the published snapshot. Records whose
        every deadline has passed are not carried: they expire now (reason deadline_passed)."""
        if published_id is None or not missing:
            return 0, {}
        keys = list(missing)
        cur.execute(
            """SELECT natural_key,
                      (SELECT max(d) FROM unnest(coalesce(deadline_dates, ARRAY[deadline_date])) AS d) < now()
               FROM extsrc.external_record WHERE snapshot_id = %s AND natural_key = ANY(%s)""",
            (published_id, keys))
        passed = {key: bool(flag) for key, flag in cur.fetchall()}
        carry = [k for k in keys if k in passed and not passed[k]]
        expired = {k: (missing[k], "every deadline has passed") for k in keys if passed.get(k)}
        if not carry:
            return 0, expired
        columns = [c for c in RECORD_COLUMNS if c not in ("snapshot_id", "change_state", "carried_over", "absent_runs")]
        from psycopg import sql
        cols = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
        cur.execute(sql.SQL(
            """INSERT INTO extsrc.external_record (snapshot_id, change_state, carried_over, absent_runs, {cols})
               SELECT %s, 'missing', true, (%s::jsonb ->> natural_key)::int, {cols}
               FROM extsrc.external_record WHERE snapshot_id = %s AND natural_key = ANY(%s)""").format(cols=cols),
            (snapshot_id, _jsonb({k: missing[k] for k in carry}), published_id, carry))
        for table, columns_ in (("field_value", FIELD_VALUE_COLUMNS), ("resolved_value", RESOLVED_COLUMNS),
                                ("record_document", RECORD_DOCUMENT_COLUMNS)):
            rest = [c for c in columns_ if c != "snapshot_id"]
            col_sql = sql.SQL(", ").join(sql.Identifier(c) for c in rest)
            cur.execute(sql.SQL(
                """INSERT INTO extsrc.{table} (snapshot_id, {cols})
                   SELECT %s, {cols} FROM extsrc.{table} WHERE snapshot_id = %s AND natural_key = ANY(%s)"""
            ).format(table=sql.Identifier(table), cols=col_sql), (snapshot_id, published_id, carry))
        return len(carry), expired

    def _publish(self, batch: LoadBatch, snapshot_id: int, published_id: int | None, changes: dict[str, Any],
                 records: list[Any], doc_urls: list[str], validation: dict[str, Any]) -> None:
        hashes = changes["hashes"]
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("SELECT published_snapshot_id FROM extsrc.source_state WHERE source = %s FOR UPDATE",
                        (batch.source,))
            cur.execute(
                """UPDATE extsrc.snapshot SET status = 'published', published_at = now(), validation = %s
                   WHERE snapshot_id = %s""", (_jsonb(validation), snapshot_id))
            if published_id is not None:
                cur.execute("UPDATE extsrc.snapshot SET status = 'superseded' WHERE snapshot_id = %s", (published_id,))
            stale_reason = None
            if batch.partial_reasons:
                stale_reason = "partial run: " + "; ".join(batch.partial_reasons)[:400]
            cur.execute(
                """UPDATE extsrc.source_state SET previous_snapshot_id = published_snapshot_id,
                       published_snapshot_id = %s, last_attempt_at = now(), last_success_at = now(),
                       last_status = %s, consecutive_failures = 0, stale_since = NULL, stale_reason = %s,
                       updated_at = now()
                   WHERE source = %s""",
                (snapshot_id, "partial" if batch.partial_reasons else "published", stale_reason, batch.source))
            # change tracking + history
            tracking = []
            history = []
            previous = {row[0]: row[1] for row in self._all(
                "SELECT natural_key, last_hash FROM extsrc.record_tracking WHERE source = %s", (batch.source,))}
            for state in ("new", "changed", "unchanged"):
                for key in changes[state]:
                    tracking.append((batch.source, key, hashes[key], snapshot_id, state in ("new", "changed")))
                    history.append((batch.run_id, batch.source, snapshot_id, key, state, previous.get(key),
                                    hashes[key], 0, None))
            cur.executemany(
                """INSERT INTO extsrc.record_tracking (source, natural_key, first_seen, last_seen, last_changed,
                       last_hash, absent_runs, last_snapshot_id)
                   VALUES (%s, %s, now(), now(), now(), %s, 0, %s)
                   ON CONFLICT (source, natural_key) DO UPDATE SET last_seen = now(),
                       last_changed = CASE WHEN %s OR extsrc.record_tracking.expired_at IS NOT NULL THEN now()
                                           ELSE extsrc.record_tracking.last_changed END,
                       first_seen = CASE WHEN extsrc.record_tracking.expired_at IS NOT NULL THEN now()
                                         ELSE extsrc.record_tracking.first_seen END,
                       last_hash = EXCLUDED.last_hash, absent_runs = 0, expired_at = NULL, expired_reason = NULL,
                       last_snapshot_id = EXCLUDED.last_snapshot_id""",
                [(s, k, h, sid, changed) for s, k, h, sid, changed in tracking])
            for key, absent in changes["missing"].items():
                history.append((batch.run_id, batch.source, snapshot_id, key, "missing", previous.get(key), None,
                                absent, None if batch.complete else "partial run: absence not counted"))
            cur.executemany("UPDATE extsrc.record_tracking SET absent_runs = %s WHERE source = %s AND natural_key = %s",
                            [(absent, batch.source, key) for key, absent in changes["missing"].items()])
            for key, (absent, reason) in changes["expired"].items():
                history.append((batch.run_id, batch.source, snapshot_id, key, "expired", previous.get(key), None,
                                absent, reason))
            cur.executemany(
                """UPDATE extsrc.record_tracking SET absent_runs = %s, expired_at = now(), expired_reason = %s
                   WHERE source = %s AND natural_key = %s""",
                [(absent, reason, batch.source, key) for key, (absent, reason) in changes["expired"].items()])
            cur.executemany(
                """INSERT INTO extsrc.record_change (run_id, source, snapshot_id, natural_key, state, previous_hash,
                       new_hash, absent_runs, reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""", history)
            # programmes and calls (reference data)
            programmes: dict[str, str] = {}
            calls: dict[str, tuple[str | None, str | None]] = {}
            for record in records:
                row, _, _ = record_row(record, batch.source, batch.fetched_at)
                if row["programme_key"]:
                    programmes[row["programme_key"]] = row["programme"] or row["programme_key"]
                if row["call_identifier"]:
                    calls[row["call_identifier"]] = (row["call_title"], row["programme_key"])
            cur.executemany(
                """INSERT INTO extsrc.programme (programme_key, label, sources) VALUES (%s, %s, ARRAY[%s::text])
                   ON CONFLICT (programme_key) DO UPDATE SET label = EXCLUDED.label, last_seen = now(),
                       sources = (SELECT array_agg(DISTINCT s) FROM unnest(extsrc.programme.sources || EXCLUDED.sources) s)""",
                [(key, label, batch.source) for key, label in programmes.items()])
            cur.executemany(
                """INSERT INTO extsrc.call (source, call_identifier, call_title, programme_key) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (source, call_identifier) DO UPDATE SET call_title = coalesce(EXCLUDED.call_title,
                       extsrc.call.call_title), programme_key = EXCLUDED.programme_key, last_seen = now()""",
                [(batch.source, call_id, title, key) for call_id, (title, key) in calls.items()])
            self._resolve_dead_letters(cur, batch, [r.natural_key for r in records], doc_urls)

    # ---- failed source (nothing to load) -------------------------------------------------------
    def mark_failed(self, batch: LoadBatch, reason: str) -> LoadResult:
        """A source that produced nothing usable: keep serving its last snapshot, flagged stale."""
        self.ensure_source(batch.source)
        self.record_run(batch)
        self.record_failures(batch)
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                """UPDATE extsrc.source_state SET last_attempt_at = now(), last_status = 'failed',
                       consecutive_failures = consecutive_failures + 1, stale_since = coalesce(stale_since, now()),
                       stale_reason = %s, updated_at = now() WHERE source = %s""", (reason[:500], batch.source))
        result = LoadResult(batch.source, "failed", alerts=[_alert(
            "critical", "source_failed", f"{reason[:300]}; serving the last good snapshot (stale)")])
        self._finish_source_run(batch, "failed", result)
        if self.notifier is not None:
            try:
                self.notifier.notify(result.alerts, batch.source)
            except Exception as exc:  # noqa: BLE001
                logger.warning("alert notification failed: %s", exc)
        return result

    def prune(self) -> dict[str, int]:
        from external_sources.store.retention import prune
        counts = prune(self.conn, self.policy["retention"])
        logger.info("retention: %s", counts)
        return counts

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any], error: str | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE extsrc.run SET finished_at = now(), status = %s, summary = %s, error = %s
                   WHERE run_id = %s""", (status, _jsonb(summary), error, run_id))


