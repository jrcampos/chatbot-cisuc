"""De-duplication of extracted records by natural key."""

from __future__ import annotations

from typing import Any


def dedupe_by_natural_key(records: list[Any]) -> tuple[list[Any], int]:
    """
    Drop records that repeat a `natural_key`, returning (kept, dropped_count).

    Among duplicates the record with the most `raw` keys wins; ties keep the
    first one seen. Survivors keep first-occurrence order. Records with an
    empty natural key are never merged (the plugins dead-letter them instead).
    """
    kept: list[Any] = []
    index_by_key: dict[str, int] = {}
    dropped = 0
    for record in records:
        key = record.natural_key
        if not key:
            kept.append(record)
            continue
        if key not in index_by_key:
            index_by_key[key] = len(kept)
            kept.append(record)
            continue
        dropped += 1
        position = index_by_key[key]
        if len(record.raw) > len(kept[position].raw):
            kept[position] = record
    return kept, dropped
