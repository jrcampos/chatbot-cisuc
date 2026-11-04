"""
Dead-letter inspection and retry (`python -m external_sources.store dead-letters list|retry`).

Retry re-attempts what failed, with the current code and configuration, and marks the letter
resolved when it now works (the next scheduled run then picks the item up normally):

- document: re-fetch the URL (warms the HTTP cache and document store)
- detail:   re-fetch the per-topic detail payload
- normalize: re-normalize the stored raw payload with the current field spec
- fetch:    re-request the failed search page with the stored query
Other stages are retried by the next run itself and are only reported.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from external_sources.store.registry import SOURCES

logger = logging.getLogger(__name__)


def list_open(conn: Any, source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, source, stage, item_key, error, attempts, first_failed_at, last_failed_at, retryable
               FROM extsrc.dead_letter WHERE resolved_at IS NULL AND (%s::text IS NULL OR source = %s)
               ORDER BY last_failed_at DESC LIMIT %s""", (source, source, limit))
        names = [d.name for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def _plugin(source: str) -> Any:
    return SOURCES[source].build()


def retry_document(letter: dict[str, Any], fetcher: Any) -> tuple[bool, str]:
    result = fetcher.get(letter["item_key"])
    return result.usable and result.status != "error", f"fetch status {result.status}"


def retry_detail(letter: dict[str, Any], fetcher: Any) -> tuple[bool, str]:
    from external_sources.enrichment.detail import DetailEnricher
    plugin = _plugin(letter["source"])
    if not getattr(plugin, "detail_config", None):
        return False, "source has no detail endpoint"
    result = DetailEnricher(fetcher, **plugin.detail_config).fetch(letter["item_key"])
    return result.payload is not None, f"detail status {result.status}"


def retry_normalize(letter: dict[str, Any], fetcher: Any) -> tuple[bool, str]:
    payload = letter.get("payload")
    if not isinstance(payload, dict):
        return False, "no stored payload"
    plugin = _plugin(letter["source"])
    plugin.failures, plugin.partial_reasons = [], []
    record = plugin._safe_record(payload, "retry")
    if record is None:
        return False, plugin.failures[-1].error if plugin.failures else "still fails"
    return True, f"normalizes now as {record.natural_key}; included by the next run"


def retry_fetch(letter: dict[str, Any], fetcher: Any) -> tuple[bool, str]:
    payload = letter.get("payload")
    if not isinstance(payload, dict) or "query" not in payload:
        return False, "no stored query"
    plugin = _plugin(letter["source"])
    data = plugin._post_search(payload["query"], int(payload.get("page") or 1))
    results = data.get("results")
    return isinstance(results, list), f"page answers with {len(results or [])} results"


HANDLERS: dict[str, Callable[[dict[str, Any], Any], tuple[bool, str]]] = {
    "document": retry_document, "detail": retry_detail, "normalize": retry_normalize, "fetch": retry_fetch,
}


def retry(conn: Any, fetcher: Any, source: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Retry open dead letters; returns one outcome per letter."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, source, stage, item_key, payload FROM extsrc.dead_letter
               WHERE resolved_at IS NULL AND retryable AND (%s::text IS NULL OR source = %s)
               ORDER BY first_failed_at LIMIT %s""", (source, source, limit))
        letters = [dict(zip(("id", "source", "stage", "item_key", "payload"), row)) for row in cur.fetchall()]
    outcomes = []
    for letter in letters:
        handler = HANDLERS.get(letter["stage"])
        if handler is None:
            outcomes.append({**letter, "ok": None, "detail": "retried by the next scheduled run"})
            continue
        try:
            ok, detail = handler(letter, fetcher)
        except Exception as exc:  # noqa: BLE001 — a retry failure is reported, never fatal
            ok, detail = False, f"{type(exc).__name__}: {str(exc)[:300]}"
        with conn.cursor() as cur:
            if ok:
                cur.execute("UPDATE extsrc.dead_letter SET resolved_at = now(), resolution = %s WHERE id = %s",
                            (f"manual retry: {detail}"[:500], letter["id"]))
            else:
                cur.execute(
                    """UPDATE extsrc.dead_letter SET attempts = attempts + 1, last_failed_at = now(),
                           error = %s WHERE id = %s""", (f"retry: {detail}"[:4000], letter["id"]))
        outcomes.append({**{k: letter[k] for k in ("id", "source", "stage", "item_key")}, "ok": ok, "detail": detail})
    return outcomes
