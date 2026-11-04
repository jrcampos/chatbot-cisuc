"""
Health/status report: per-source freshness, staleness, partial flags, failure counts, open dead
letters, recent runs and local batches waiting for the database. Works (with less detail) when
the database is down.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from external_sources.store.checkpoint import list_runs, pending_batches


def collect_status(conn: Any | None, work: Path) -> dict[str, Any]:
    status: dict[str, Any] = {"database": "unreachable" if conn is None else "ok", "sources": [],
                              "dead_letters": {}, "runs": [], "pending_batches": []}
    status["pending_batches"] = [{"run_id": run.run_id, "source": source, "path": str(path)}
                                 for run, source, path in pending_batches(work)]
    status["local_runs"] = [{k: run.state().get(k) for k in ("run_id", "status", "started_at", "finished_at",
                                                              "exit_code", "checkpoint_disabled")}
                            for run in list_runs(work)[-5:]]
    if conn is None:
        return status
    with conn.cursor() as cur:
        cur.execute(
            """SELECT source, snapshot_id, data_as_of, data_age, stale, stale_since, stale_reason, partial,
                      partial_reasons, record_count, last_attempt_at, last_success_at, last_status,
                      consecutive_failures
               FROM current.source_freshness ORDER BY source""")
        names = [d.name for d in cur.description]
        status["sources"] = [dict(zip(names, row)) for row in cur.fetchall()]
        cur.execute(
            """SELECT source, stage, count(*) FROM extsrc.dead_letter WHERE resolved_at IS NULL
               GROUP BY source, stage ORDER BY source, stage""")
        for source, stage, count in cur.fetchall():
            status["dead_letters"].setdefault(source, {})[stage] = count
        cur.execute(
            """SELECT r.run_id, r.started_at, r.finished_at, r.status,
                      coalesce(json_agg(json_build_object('source', s.source, 'status', s.status,
                          'fetched', s.records_fetched, 'loaded', s.records_loaded, 'failed', s.records_failed))
                          FILTER (WHERE s.source IS NOT NULL), '[]')
               FROM extsrc.run r LEFT JOIN extsrc.source_run s ON s.run_id = r.run_id
               GROUP BY r.run_id ORDER BY r.started_at DESC LIMIT 5""")
        status["runs"] = [dict(zip(("run_id", "started_at", "finished_at", "status", "sources"), row))
                          for row in cur.fetchall()]
        cur.execute("SELECT count(*) FROM current.calls")
        status["live_calls"] = cur.fetchone()[0]
        cur.execute("SELECT source, count(*) FROM current.calls GROUP BY source ORDER BY source")
        status["live_calls_by_source"] = dict(cur.fetchall())
    return status


def _age(value: Any) -> str:
    if not isinstance(value, timedelta):
        return "-"
    hours = value.total_seconds() / 3600
    return f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"


def render(status: dict[str, Any]) -> str:
    lines = [f"database: {status['database']}"]
    if status.get("live_calls") is not None:
        lines.append(f"live calls (open/forthcoming): {status['live_calls']} {status.get('live_calls_by_source')}")
    if status["sources"]:
        lines.append("")
        lines.append(f"{'source':14} {'records':>7} {'age':>7} {'stale':>5} {'partial':>7} {'last':>9} {'fails':>5}  open dead letters")
        for s in status["sources"]:
            letters = status["dead_letters"].get(s["source"], {})
            lines.append(
                f"{s['source']:14} {s['record_count'] or 0:>7} {_age(s['data_age']):>7} "
                f"{'YES' if s['stale'] else 'no':>5} {'YES' if s['partial'] else 'no':>7} "
                f"{s['last_status'] or '-':>9} {s['consecutive_failures'] or 0:>5}  "
                f"{', '.join(f'{k}={v}' for k, v in letters.items()) or '-'}")
            if s["stale"]:
                lines.append(f"{'':14} stale since {s['stale_since']}: {s['stale_reason']}")
            if s["partial"]:
                lines.append(f"{'':14} partial: {'; '.join(s['partial_reasons'] or [])[:300]}")
    if status["pending_batches"]:
        lines.append("")
        lines.append(f"batches waiting for the database: {len(status['pending_batches'])}")
        for item in status["pending_batches"]:
            lines.append(f"  {item['source']} from run {item['run_id']}")
    if status.get("runs"):
        lines.append("")
        lines.append("recent runs:")
        for run in status["runs"]:
            parts = ", ".join(f"{s['source']}={s['status']}({s.get('loaded')})" for s in run["sources"])
            lines.append(f"  {run['started_at']} {run['status']:<11} {parts}")
    elif status.get("local_runs"):
        lines.append("")
        lines.append("recent local runs:")
        for run in status["local_runs"]:
            lines.append(f"  {run['started_at']} {run['status']} exit={run.get('exit_code')}")
    return "\n".join(lines)


def health_code(status: dict[str, Any]) -> int:
    """0 healthy, 1 degraded (stale/partial/failing/pending), 3 database unreachable."""
    if status["database"] != "ok":
        return 3
    degraded = any(s["stale"] or s["partial"] or (s["consecutive_failures"] or 0) > 0 or s["snapshot_id"] is None
                   for s in status["sources"])
    return 1 if degraded or status["pending_batches"] else 0


COVERAGE_SQL = """
SELECT r.source,
       count(*)                                                                         AS calls,
       count(*) FILTER (WHERE coalesce(r.description, '') <> '')                         AS description,
       count(*) FILTER (WHERE r.funding_amount IS NOT NULL OR r.details ? 'topic_budget_eur'
                              OR r.details ? 'call_budget_eur')                           AS budget_any,
       count(*) FILTER (WHERE r.funding_amount IS NOT NULL)                              AS topic_budget,
       count(*) FILTER (WHERE r.next_deadline IS NOT NULL OR r.deadline_date IS NOT NULL) AS deadline,
       count(*) FILTER (WHERE coalesce(r.details->>'conditions', '') <> ''
                              OR ev.status IN ('confirmed', 'single_source', 'resolved'))  AS eligibility,
       count(*) FILTER (WHERE ev.status = 'confirmed')                                   AS eligibility_confirmed,
       count(*) FILTER (WHERE dl.status = 'confirmed')                                   AS deadline_confirmed,
       count(*) FILTER (WHERE dl.status = 'conflicting')                                 AS deadline_conflicting,
       count(*) FILTER (WHERE bu.status = 'conflicting')                                 AS budget_conflicting,
       count(*) FILTER (WHERE jsonb_array_length(coalesce(r.details->'sources', '[]')) > 1) AS cites_document,
       count(*) FILTER (WHERE r.carried_over)                                            AS carried_over,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(r.data_gaps) g
                                      WHERE g->>'kind' = 'schema_drift'))                AS schema_drift
FROM current.calls r
LEFT JOIN current.resolved_values ev ON ev.snapshot_id = r.snapshot_id AND ev.natural_key = r.natural_key
                                     AND ev.field = 'eligibility'
LEFT JOIN current.resolved_values dl ON dl.snapshot_id = r.snapshot_id AND dl.natural_key = r.natural_key
                                     AND dl.field = 'deadline_date'
LEFT JOIN current.resolved_values bu ON bu.snapshot_id = r.snapshot_id AND bu.natural_key = r.natural_key
                                     AND bu.field = 'funding_amount'
GROUP BY ROLLUP (r.source) ORDER BY r.source NULLS LAST
"""


def coverage(conn: Any) -> list[dict[str, Any]]:
    """Per-source (and total) coverage of the live calls' key fields."""
    with conn.cursor() as cur:
        cur.execute(COVERAGE_SQL)
        names = [d.name for d in cur.description]
        rows = [dict(zip(names, row)) for row in cur.fetchall()]
    for row in rows:
        row["source"] = row["source"] or "TOTAL"
    return rows


def render_coverage(rows: list[dict[str, Any]]) -> str:
    columns = ["calls", "description", "budget_any", "topic_budget", "deadline", "eligibility",
               "eligibility_confirmed", "deadline_confirmed", "deadline_conflicting", "budget_conflicting",
               "cites_document", "carried_over", "schema_drift"]
    lines = ["| source | " + " | ".join(columns) + " |", "|---" * (len(columns) + 1) + "|"]
    for row in rows:
        cells = [str(row["calls"])]
        for name in columns[1:]:
            pct = 100.0 * row[name] / row["calls"] if row["calls"] else 0.0
            cells.append(f"{row[name]} ({pct:.1f}%)")
        lines.append(f"| {row['source']} | " + " | ".join(cells) + " |")
    return "\n".join(lines)
