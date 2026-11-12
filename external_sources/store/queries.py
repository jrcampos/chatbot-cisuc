"""
Read API for RAG_CALLS. Reads only the `current` schema through the read-only login
(`connect_reader`: read-only transactions and a statement timeout on top of the role's own
restrictions). Every value is a bound parameter; the WHERE clause is assembled only from the
fixed fragments below.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from external_sources.plugins.base import NormalizedRecord

DEFAULT_STATUSES = ("open", "forthcoming")
MAX_LIMIT = 100
# Any-term match: the AND query of plainto_tsquery with every '&' turned into '|' (OR). Only the
# server-side normalised lexemes are re-parsed, never the user's text.
_ANY_TERMS = "to_tsquery('english', replace(plainto_tsquery('english', %(text)s)::text, '&', '|'))"
_ORDER = {
    "deadline": "r.next_deadline ASC NULLS LAST, r.natural_key",
    "relevance": "rank DESC, r.next_deadline ASC NULLS LAST, r.natural_key",
}
_SELECT = """
    SELECT r.source, r.natural_key, r.record_type, r.domain, r.title, r.description, r.programme,
           r.country_scope, r.status, r.funding_amount, r.start_date, r.deadline_date, r.next_deadline,
           r.source_url, r.source_updated_at, r.details, r.field_sources, r.data_gaps, r.stale,
           r.stale_since, r.source_partial, r.data_as_of, r.carried_over, r.absent_runs, f.attribution,
           {rank} AS rank
    FROM current.records r
    JOIN current.source_freshness f ON f.source = r.source"""
_RECORD_FIELDS = ("natural_key", "record_type", "domain", "programme", "country_scope", "title", "description",
                  "status", "funding_amount", "start_date", "deadline_date", "details", "source_url",
                  "source_updated_at", "field_sources", "data_gaps")


@dataclass
class CallQuery:
    statuses: Sequence[str] = DEFAULT_STATUSES
    text: str | None = None                    # full-text (websearch syntax), or any term with text_any
    text_any: bool = False
    identifier: str | None = None              # topic id / natural key or call identifier, case-insensitive
    natural_keys: Sequence[str] | None = None  # exact natural keys (e.g. the hits of a vector search)
    order: str = "deadline"                    # deadline | relevance
    limit: int = 20


def connect_reader(env: Any = None) -> Any:
    """Read-only connection with the reader statement timeout (EXTSRC_DB_READER_*)."""
    from external_sources.store.db import connect
    from external_sources.store.settings import DBSettings
    return connect(DBSettings.from_env("reader", env), read_only=True, attempts=2)


def build_call_query(q: CallQuery) -> tuple[str, dict[str, Any]]:
    """SQL text + parameters for `search_calls` (exposed for tests)."""
    if q.order not in _ORDER:
        raise ValueError(f"order must be one of {sorted(_ORDER)}")
    params: dict[str, Any] = {"statuses": list(q.statuses), "limit": max(1, min(int(q.limit), MAX_LIMIT))}
    where = ["r.status = ANY(%(statuses)s)"]
    rank = "0::real"
    if q.text:
        tsq = _ANY_TERMS if q.text_any else "websearch_to_tsquery('english', %(text)s)"
        where.append(f"r.search_tsv @@ {tsq}")
        rank = f"ts_rank_cd(r.search_tsv, {tsq})"
        params["text"] = q.text
    if q.identifier:
        where.append("(lower(r.natural_key) = lower(%(ident)s) OR lower(r.call_identifier) = lower(%(ident)s))")
        params["ident"] = q.identifier.strip()
    if q.natural_keys is not None:
        where.append("r.natural_key = ANY(%(keys)s)")
        params["keys"] = list(q.natural_keys)
    # Only the fixed fragments above are interpolated; every value is a bound parameter.
    sql = f"{_SELECT.replace('{rank}', rank)} WHERE {' AND '.join(where)} ORDER BY {_ORDER[q.order]} LIMIT %(limit)s"  # nosec B608
    return sql, params


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is not None and not isinstance(value, (str, int, float, bool, list, dict)):
        try:
            return float(value)  # Decimal
        except (TypeError, ValueError):
            return str(value)
    return value


def _result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "natural_key": row["natural_key"], "source": row["source"], "title": row["title"],
        "programme": row["programme"], "status": row["status"], "next_deadline": _plain(row["next_deadline"]),
        "url": row["source_url"], "attribution": row["attribution"], "rank": float(row["rank"] or 0),
        "freshness": {k: _plain(row[k]) for k in ("stale", "stale_since", "data_as_of", "source_partial",
                                                   "carried_over", "absent_runs")},
        "_record": {k: row[k] for k in _RECORD_FIELDS},
    }


def search_calls(conn: Any, query: CallQuery | None = None) -> list[dict[str, Any]]:
    """Calls matching the query (default: every open/forthcoming call), with attribution and freshness."""
    sql, params = build_call_query(query or CallQuery())
    with conn.cursor() as cur:
        cur.execute(sql, params)
        names = [d.name for d in cur.description]
        return [_result(dict(zip(names, row))) for row in cur.fetchall()]


def vector_search_calls(conn: Any, vector: Sequence[float], model: str,
                        statuses: Sequence[str] = DEFAULT_STATUSES, limit: int = 20) -> list[tuple[str, float]]:
    """(natural_key, cosine similarity) of the calls whose record embedding is closest to `vector`;
    [] when pgvector is not installed. The vector is sent as a parameter."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('current.record_embeddings') IS NOT NULL")
        if not cur.fetchone()[0]:
            return []
        cur.execute(
            """SELECT e.natural_key, 1 - (e.embedding <=> %(v)s::vector) AS similarity
               FROM current.record_embeddings e
               JOIN current.records r ON r.snapshot_id = e.snapshot_id AND r.natural_key = e.natural_key
               WHERE e.model = %(model)s AND r.status = ANY(%(statuses)s)
               ORDER BY e.embedding <=> %(v)s::vector
               LIMIT %(limit)s""",
            {"v": "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]", "model": model,
             "statuses": list(statuses), "limit": max(1, min(int(limit), MAX_LIMIT))})
        return [(row[0], float(row[1])) for row in cur.fetchall()]


def source_freshness(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""SELECT source, stale, stale_since, stale_reason, data_as_of, data_age, partial, record_count
                       FROM current.source_freshness ORDER BY source""")
        names = [d.name for d in cur.description]
        return [{k: str(v) if k == "data_age" and v is not None else _plain(v) for k, v in zip(names, row)}
                for row in cur.fetchall()]


def to_record(result: dict[str, Any]) -> NormalizedRecord:
    """The stored record as a NormalizedRecord (raw payload not included) for context.py."""
    data = {k: _plain(v) if k in ("start_date", "deadline_date", "source_updated_at", "funding_amount") else v
            for k, v in result["_record"].items()}
    return NormalizedRecord(**data, raw={})


def freshness_note(result: dict[str, Any]) -> str | None:
    fresh = result["freshness"]
    notes = []
    if fresh["stale"]:
        notes.append(f"the source could not be refreshed since {fresh['stale_since']}; data as of {fresh['data_as_of']}")
    if fresh["carried_over"]:
        notes.append(f"this call was missing from the last {fresh['absent_runs']} source run(s); kept from earlier data")
    if fresh["source_partial"]:
        notes.append("the last source run was partial")
    return ("[Data note] " + "; ".join(notes)) if notes else None


def render_context(result: dict[str, Any], intent: str = "explain_call", max_chars: int | None = None) -> Any:
    """The capped, cited text blocks of context.py, plus a data-freshness note when needed."""
    from external_sources.context import build_context
    pack = build_context(to_record(result), intent, max_chars=max_chars)
    note = freshness_note(result)
    if note:
        pack.text = note + "\n\n" + pack.text
    if result.get("attribution") and result["attribution"] not in pack.text:
        pack.text += f"\n\n[Attribution] {result['attribution']}"
    return pack
