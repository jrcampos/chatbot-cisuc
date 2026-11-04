"""
Read API for the chatbot (orchestrator). Reads only the `current` schema, through the read-only
login (`connect_reader`): read-only transactions and a statement timeout on top of the role's
own restrictions. Every statement is parameterised; the WHERE clause is assembled only from the
fixed fragments below.

Every result carries the resolved values WITH their ranges and verdicts, the numbered citable
sources, the source's attribution, and freshness (stale / data age / partial / carried over), so
the answer can say how current and how certain each fact is.

Range-aware filters:
- deadline window [from, to]: a call matches when ANY of its cut-offs falls inside the window,
  or when the min..max range of its resolved deadline (API vs documents) overlaps it;
- budget [min, max]: a call matches when the min..max range of its resolved budget overlaps it
  (a call whose sources disagree, e.g. 4.7M vs 5.0M, matches a "at least 4.8M" query); calls
  with no stated budget never match a budget filter.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from external_sources.plugins.base import NormalizedRecord

DEFAULT_STATUSES = ("open", "forthcoming")
# Any-term match: the AND query of plainto_tsquery with every '&' turned into '|' (OR). Only the
# server-side normalised lexemes are re-parsed, never the user's text.
_ANY_TERMS = "to_tsquery('english', replace(plainto_tsquery('english', %(text)s)::text, '&', '|'))"
MAX_LIMIT = 100
_ORDER = {
    "deadline": "r.next_deadline ASC NULLS LAST, r.natural_key",
    "relevance": "rank DESC, r.next_deadline ASC NULLS LAST, r.natural_key",
    "budget": "coalesce(bu.value_max_num, r.funding_amount) DESC NULLS LAST, r.natural_key",
}


@dataclass
class CallQuery:
    statuses: Sequence[str] = DEFAULT_STATUSES
    programme: str | None = None            # label substring (case-insensitive) or exact programme key
    deadline_from: date | datetime | None = None
    deadline_to: date | datetime | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    text: str | None = None                 # full-text (websearch syntax: "quoted phrase", -not, or)
    text_any: bool = False                  # match ANY term of `text` (keyword strings, like BM25)
    identifier: str | None = None           # topic id / natural key or call identifier, case-insensitive
    natural_keys: Sequence[str] | None = None  # exact natural keys (e.g. the hits of a vector search)
    sources: Sequence[str] | None = None
    include_stale: bool = True
    order: str = "deadline"                 # deadline | relevance | budget
    limit: int = 20
    extra: dict[str, Any] = field(default_factory=dict)


def connect_reader(env: Any = None) -> Any:
    """Read-only connection with the reader statement timeout (EXTSRC_DB_READER_*)."""
    from external_sources.store.db import connect
    from external_sources.store.settings import DBSettings
    return connect(DBSettings.from_env("reader", env), read_only=True, attempts=2)


def _bound(value: date | datetime | None, end: bool) -> Any:
    if value is None:
        return "infinity" if end else "-infinity"
    if isinstance(value, datetime):
        return value.isoformat()
    # a date bound covers the whole day
    return (datetime.combine(value + timedelta(days=1), datetime.min.time()) - timedelta(microseconds=1)
            ).isoformat() + "+00:00" if end else value.isoformat() + "T00:00:00+00:00"


def build_call_query(q: CallQuery) -> tuple[str, dict[str, Any]]:
    """SQL text + parameters for `search_calls` (exposed for tests)."""
    if q.order not in _ORDER:
        raise ValueError(f"order must be one of {sorted(_ORDER)}")
    params: dict[str, Any] = {"statuses": list(q.statuses), "limit": max(1, min(int(q.limit), MAX_LIMIT))}
    where = ["r.status = ANY(%(statuses)s)"]
    rank = "0::real"
    if q.programme:
        where.append("(r.programme ILIKE %(programme_like)s OR r.programme_key = %(programme)s)")
        params["programme_like"] = "%" + q.programme.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params["programme"] = q.programme
    if q.deadline_from is not None or q.deadline_to is not None:
        where.append(
            "(EXISTS (SELECT 1 FROM unnest(coalesce(r.deadline_dates, ARRAY[r.deadline_date])) AS d "
            "         WHERE d BETWEEN %(d_from)s::timestamptz AND %(d_to)s::timestamptz)"
            " OR (coalesce(dl.value_min_ts, r.deadline_date) <= %(d_to)s::timestamptz"
            "     AND coalesce(dl.value_max_ts, r.deadline_date) >= %(d_from)s::timestamptz))")
        params["d_from"] = _bound(q.deadline_from, end=False)
        params["d_to"] = _bound(q.deadline_to, end=True)
    if q.budget_min is not None:
        where.append("coalesce(bu.value_max_num, r.funding_amount) >= %(b_min)s")
        params["b_min"] = float(q.budget_min)
    if q.budget_max is not None:
        where.append("coalesce(bu.value_min_num, r.funding_amount) <= %(b_max)s")
        params["b_max"] = float(q.budget_max)
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
    if q.sources:
        where.append("r.source = ANY(%(sources)s)")
        params["sources"] = list(q.sources)
    if not q.include_stale:
        where.append("NOT r.stale")
    order = _ORDER["relevance"] if q.text and q.order == "deadline" and q.extra.get("prefer_relevance") else _ORDER[q.order]
    sql = f"""
        SELECT r.snapshot_id, r.source, r.natural_key, r.record_type, r.record_kind, r.domain, r.title,
               r.description, r.programme, r.programme_key, r.call_identifier, r.call_title, r.country_scope,
               r.status, r.source_derived_status, r.funding_amount, r.currency, r.start_date, r.deadline_date,
               r.deadline_dates, r.next_deadline, r.source_url, r.source_updated_at, r.details, r.field_sources,
               r.data_gaps, r.stale, r.stale_since, r.data_age, r.source_partial, r.data_as_of, r.carried_over,
               r.absent_runs, r.fetched_at, f.attribution, f.licence,
               dl.value_min_ts AS deadline_min, dl.value_max_ts AS deadline_max, dl.status AS deadline_status,
               bu.value_min_num AS budget_min, bu.value_max_num AS budget_max, bu.status AS budget_status,
               {rank} AS rank
        FROM current.records r
        JOIN current.source_freshness f ON f.source = r.source
        LEFT JOIN current.resolved_values dl
               ON dl.snapshot_id = r.snapshot_id AND dl.natural_key = r.natural_key AND dl.field = 'deadline_date'
        LEFT JOIN current.resolved_values bu
               ON bu.snapshot_id = r.snapshot_id AND bu.natural_key = r.natural_key AND bu.field = 'funding_amount'
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT %(limit)s"""
    return sql, params


def _resolved(conn: Any, rows: list[dict[str, Any]]) -> dict[tuple[int, str], dict[str, Any]]:
    if not rows:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT snapshot_id, natural_key, field, value, status, value_min_num, value_max_num, value_min_ts,
                      value_max_ts, settled, agree_count, basis, contributing, sources, checked_at
               FROM current.resolved_values WHERE snapshot_id = ANY(%s) AND natural_key = ANY(%s)""",
            (sorted({r["snapshot_id"] for r in rows}), [r["natural_key"] for r in rows]))
        out: dict[tuple[int, str], dict[str, Any]] = {}
        for row in cur.fetchall():
            (snapshot_id, key, name, value, status, min_num, max_num, min_ts, max_ts, settled, agree, basis,
             contributing, sources, checked) = row
            low = min_num if min_num is not None else min_ts
            high = max_num if max_num is not None else max_ts
            out.setdefault((snapshot_id, key), {})[name] = {
                "value": value, "status": status, "min": _plain(low), "max": _plain(high), "settled": settled,
                "agree_count": agree, "basis": basis, "contributing": contributing or [], "sources": sources or [],
                "checked_at": _plain(checked),
            }
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is not None and not isinstance(value, (str, int, float, bool, list, dict)):
        try:
            return float(value)  # Decimal
        except (TypeError, ValueError):
            return str(value)
    return value


def _citations(row: dict[str, Any]) -> list[dict[str, Any]]:
    sources = (row.get("details") or {}).get("sources")
    if isinstance(sources, list) and sources:
        return [{k: s.get(k) for k in ("n", "kind", "title", "url", "fields", "pages", "retrieved_at",
                                       "last_modified", "disagrees_on") if s.get(k) is not None}
                for s in sources if isinstance(s, dict) and s.get("url")]
    return [{"n": 1, "kind": "source_page", "url": row["source_url"]}] if row.get("source_url") else []


def _result(row: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
    age = row.get("data_age")
    ranges = {name: {"min": v["min"], "max": v["max"], "status": v["status"]}
              for name, v in resolved.items() if v["min"] is not None and v["min"] != v["max"]}
    return {
        "natural_key": row["natural_key"], "source": row["source"], "kind": row["record_kind"],
        "title": row["title"], "programme": row["programme"], "call_identifier": row["call_identifier"],
        "status": row["status"], "next_deadline": _plain(row["next_deadline"]),
        "deadline_dates": [_plain(d) for d in row["deadline_dates"] or []],
        "deadline": {"value": _plain(row["deadline_date"]), "min": _plain(row["deadline_min"]),
                     "max": _plain(row["deadline_max"]), "status": row["deadline_status"]},
        "budget": {"value": _plain(row["funding_amount"]), "currency": row["currency"], "min": _plain(row["budget_min"]),
                   "max": _plain(row["budget_max"]), "status": row["budget_status"]},
        "url": row["source_url"],
        "resolved": resolved,
        "verdicts": {name: v["status"] for name, v in resolved.items()},
        "ranges": ranges,
        "sources": _citations(row),
        "attribution": row["attribution"], "licence": row["licence"],
        "freshness": {"stale": row["stale"], "stale_since": _plain(row["stale_since"]),
                      "data_as_of": _plain(row["data_as_of"]), "fetched_at": _plain(row["fetched_at"]),
                      "data_age_hours": round(age.total_seconds() / 3600, 1) if isinstance(age, timedelta) else None,
                      "source_partial": row["source_partial"], "carried_over": row["carried_over"],
                      "absent_runs": row["absent_runs"]},
        "rank": float(row["rank"] or 0),
        "_record": {k: row[k] for k in ("natural_key", "record_type", "domain", "programme", "country_scope", "title",
                                        "description", "status", "funding_amount", "start_date", "deadline_date",
                                        "details", "source_url", "source_updated_at", "field_sources", "data_gaps")},
    }


def search_calls(conn: Any, query: CallQuery | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    """Calls matching the filters, with resolved values, ranges, verdicts, citations and freshness."""
    q = query or CallQuery(**kwargs)
    sql, params = build_call_query(q)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        names = [d.name for d in cur.description]
        rows = [dict(zip(names, row)) for row in cur.fetchall()]
    resolved = _resolved(conn, rows)
    return [_result(row, resolved.get((row["snapshot_id"], row["natural_key"]), {})) for row in rows]


def vector_search_calls(conn: Any, vector: Sequence[float], model: str,
                        statuses: Sequence[str] = DEFAULT_STATUSES, limit: int = 20) -> list[tuple[str, float]]:
    """(natural_key, cosine similarity) of the calls whose record embedding is closest to `vector`.

    Needs pgvector and record embeddings made with the same `model` (`store embed --records-only`);
    returns [] when the semantic layer is not installed. The vector is sent as a parameter."""
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


def get_call(conn: Any, identifier: str) -> dict[str, Any] | None:
    """One call by topic id / natural key (any status, including closed ones still in the snapshot)."""
    results = search_calls(conn, CallQuery(identifier=identifier, statuses=("open", "forthcoming", "closed"), limit=1))
    return results[0] if results else None


def search_documents(conn: Any, text: str, natural_key: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """Full-text search over document chunks (optionally only documents linked from one call)."""
    params: dict[str, Any] = {"text": text, "limit": max(1, min(int(limit), MAX_LIMIT))}
    scope = ""
    if natural_key:
        scope = ("AND c.doc_id IN (SELECT rd.doc_id FROM current.record_documents rd "
                 "WHERE lower(rd.natural_key) = lower(%(key)s))")
        params["key"] = natural_key
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT c.doc_id, c.chunk_no, c.heading, c.page_start, c.page_end, c.text, c.url, c.resolved_url,
                       c.document_title, c.role,
                       ts_rank_cd(c.tsv, websearch_to_tsquery('english', %(text)s)) AS rank
                FROM current.document_chunks c
                WHERE c.tsv @@ websearch_to_tsquery('english', %(text)s) AND c.role IS DISTINCT FROM 'boilerplate'
                {scope}
                ORDER BY rank DESC LIMIT %(limit)s""", params)
        names = [d.name for d in cur.description]
        return [{**dict(zip(names, row)), "rank": float(row[-1])} for row in cur.fetchall()]


def list_programmes(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT programme_key, label, sources, live_calls FROM current.programmes ORDER BY live_calls DESC, label")
        return [dict(zip(("programme_key", "label", "sources", "live_calls"), row)) for row in cur.fetchall()]


def source_freshness(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""SELECT source, stale, stale_since, stale_reason, data_as_of, data_age, partial, record_count,
                              attribution FROM current.source_freshness ORDER BY source""")
        names = [d.name for d in cur.description]
        return [{k: _plain(v) if k != "data_age" else v for k, v in zip(names, row)} for row in cur.fetchall()]


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
    """The same capped, cited text blocks as context.py, plus a data-freshness note when needed."""
    from external_sources.context import build_context
    pack = build_context(to_record(result), intent, max_chars=max_chars)
    note = freshness_note(result)
    if note:
        pack.text = note + "\n\n" + pack.text
    if result.get("attribution") and result["attribution"] not in pack.text:
        pack.text += f"\n\n[Attribution] {result['attribution']}"
    return pack
