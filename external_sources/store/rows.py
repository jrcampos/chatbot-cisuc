"""
Pure transformation of enriched records into the rows of one snapshot (no database here).

- `record_row`: the NormalizedRecord as an `external_record` row; the untouched payload is
  stored once by content hash (`payload`).
- `provenance_rows`: one `field_value` row per field per layer — the value the record holds
  (`role=value`), every other value a layer stated (`alternative`, from the verification layers
  and `value_range`), agreeing document values (`corroboration`), values that were replaced
  (`superseded`, from `value_history`) and presence checks (`check`). Inconsistent values are
  all kept; nothing is overwritten.
- `resolved_rows`: per field the chosen value, its status (confirmed | single_source |
  conflicting | resolved | absent), the min..max range over every contributing value (numbers
  and dates), the contributing values and the citable sources.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from external_sources.plugins.base import NormalizedRecord, record_hash

ANNOTATION_KEYS = frozenset({"evidence", "verification", "value_range", "sources", "corroboration",
                             "value_history", "_set"})
TOP_FIELDS = ("title", "description", "status", "funding_amount", "start_date", "deadline_date",
              "programme", "source_url", "source_updated_at", "country_scope")
RESOLVED_ALWAYS = ("title", "description", "status", "funding_amount", "start_date", "deadline_date",
                   "programme", "source_url")
RESOLVED_DETAILS = ("details.call_budget_eur", "details.topic_budget_eur", "details.call_identifier",
                    "details.deadline_model", "details.currency")
RECORD_KINDS = {"ft_portal": "topic", "ft_cascade": "cascade_call", "ft_europeaid": "external_action_call"}
LAYER_CONFIDENCE = {"api": 0.9, "detail": 0.9, "document": 0.8, "other": 0.5}
LONG_VALUE_CHARS = 2000
STATUSES = ("confirmed", "single_source", "conflicting", "resolved", "absent")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
_SEARCH_EXTRA_KEYS = ("keywords", "tags", "types_of_action", "call_identifier", "destination_description",
                      "cross_cutting_priorities", "parent_topic", "parent_topic_title",
                      "organiser_project_acronym", "geographical_zones", "contract_type")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def as_timestamp(value: Any) -> str | None:
    """ISO date/datetime string (Postgres parses it; date-only means 00:00 UTC), else None."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and _DATE.match(value.strip()):
        return value.strip()
    return None


def is_date_field(field: str) -> bool:
    name = field.split(".")[-1]
    return name.endswith("_date") or name.endswith("_at") or name in ("deadline_date", "start_date")


def norm(value: Any) -> str:
    """Comparison key: numbers rounded, dates as their day, everything else canonical JSON."""
    number = as_number(value) if not isinstance(value, str) else None
    if number is not None:
        return f"n:{round(number, 2)}"
    stamp = as_timestamp(value)
    if stamp:
        return f"d:{stamp[:10]}"
    if isinstance(value, str):
        number = as_number(value)
        if number is not None:
            return f"n:{round(number, 2)}"
        return "s:" + re.sub(r"\s+", " ", value).strip().lower()
    return "j:" + canonical_json(value)


def parse_layer(label: Any) -> tuple[str, str | None, str | None]:
    """field_sources label -> (layer, doc_id, note)."""
    if not label:
        return "other", None, "no provenance label"
    text = str(label)
    if text.startswith("document:"):
        return "document", text.split(":", 1)[1] or None, None
    if text in ("api", "detail", "document"):
        return text, None, None
    return "other", None, text


def field_value(record: NormalizedRecord, field: str) -> Any:
    if field.startswith("details."):
        return record.details.get(field[len("details."):])
    return getattr(record, field, None)


def is_filled(value: Any) -> bool:
    return not (value is None or value == "" or value == [] or value == {})


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def programme_key(record: NormalizedRecord) -> str | None:
    raw_id = _first(record.raw.get("frameworkProgramme")) if isinstance(record.raw, dict) else None
    if raw_id:
        return str(raw_id)
    if record.programme:
        return "label:" + re.sub(r"[^a-z0-9]+", "-", record.programme.lower()).strip("-")
    return None


def _search_extra(details: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in _SEARCH_EXTRA_KEYS:
        value = details.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value if isinstance(v, (str, int, float)))
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            parts.append(str(value))
    return " ".join(parts)[:20000]


def embed_text(record: NormalizedRecord) -> str:
    """What a record embedding represents: title, identifiers, programme and description."""
    parts = [record.natural_key, record.title, str(record.details.get("call_title") or ""),
             record.programme or "", (record.description or "")[:6000]]
    return "\n".join(p for p in parts if p)


def record_row(record: NormalizedRecord, source: str, fetched_at: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """(external_record row without snapshot_id, raw sha256, raw payload)."""
    raw = record.raw if isinstance(record.raw, dict) else {"_raw": record.raw}
    raw_sha = sha256_text(canonical_json(raw))
    details = record.details or {}
    cutoffs = [as_timestamp(v) for v in details.get("deadline_dates") or [] if as_timestamp(v)]
    if not cutoffs and as_timestamp(record.deadline_date):
        cutoffs = [as_timestamp(record.deadline_date)]
    row = {
        "source": source,
        "natural_key": record.natural_key,
        "record_type": record.record_type,
        "record_kind": RECORD_KINDS.get(source, source),
        "domain": record.domain,
        "programme": record.programme,
        "programme_key": programme_key(record),
        "call_identifier": str(details.get("call_identifier") or "") or None,
        "call_title": str(details.get("call_title") or "") or None,
        "country_scope": record.country_scope,
        "title": record.title or record.natural_key,
        "description": record.description,
        "status": record.status,
        "funding_amount": as_number(record.funding_amount),
        "currency": str(details.get("currency") or "EUR"),
        "start_date": as_timestamp(record.start_date),
        "deadline_date": as_timestamp(record.deadline_date),
        "deadline_dates": cutoffs or None,
        "source_url": record.source_url or "",
        "source_updated_at": as_timestamp(record.source_updated_at),
        "details": details,
        "field_sources": record.field_sources,
        "data_gaps": record.data_gaps,
        "raw_sha256": raw_sha,
        "content_hash": record_hash(record),
        "fetched_at": fetched_at,
        "search_extra": _search_extra(details),
        "embed_text_sha256": sha256_text(embed_text(record)),
    }
    return row, raw_sha, raw


def _encode(row: dict[str, Any], field: str, value: Any) -> dict[str, Any]:
    """Typed columns for a value; long texts are kept by hash (the full text is in the record)."""
    serialized = canonical_json(value)
    row["content_hash"] = sha256_text(serialized)
    if len(serialized) > LONG_VALUE_CHARS:
        row["value"] = None
        text = value if isinstance(value, str) else serialized
        row["value_text"] = text[:300]
        row["note"] = (row.get("note") + "; " if row.get("note") else "") + \
            f"long value ({len(serialized)} chars): full value in external_record"
    else:
        row["value"] = value
        row["value_text"] = value if isinstance(value, str) else None
    row["value_num"] = as_number(value) if not isinstance(value, str) or not is_date_field(field) else None
    row["value_ts"] = as_timestamp(value) if is_date_field(field) else None
    return row


def _base(record: NormalizedRecord, field: str, layer: str, role: str, **extra: Any) -> dict[str, Any]:
    row = {"natural_key": record.natural_key, "field": field, "layer": layer, "role": role,
           "source_url": None, "doc_id": None, "page": None, "section": None, "evidence_quote": None,
           "retrieved_at": None, "published_at": None, "confidence": LAYER_CONFIDENCE.get(layer, 0.5),
           "note": None}
    row.update({k: v for k, v in extra.items() if v is not None})
    return row


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _layer_name(name: str) -> tuple[str, str | None]:
    if name in ("api", "detail", "document"):
        return name, None
    return "other", name


def provenance_rows(record: NormalizedRecord, fetched_at: str,
                    doc_retrieved: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every value of every field from every layer (see module docstring)."""
    doc_retrieved = doc_retrieved or {}
    details = record.details or {}
    evidence = details.get("evidence") if isinstance(details.get("evidence"), dict) else {}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    held: dict[tuple[str, str], str] = {}  # (field, layer) -> norm of the value the record holds

    def add(row: dict[str, Any], value: Any) -> None:
        key = (row["field"], row["layer"], row["role"], norm(value), row.get("source_url"), row.get("page"),
               row.get("doc_id"))
        if key in seen:
            return
        seen.add(key)
        rows.append(_encode(row, row["field"], value))

    targets = [f for f in TOP_FIELDS if is_filled(field_value(record, f))]
    targets += [f"details.{k}" for k in details if k not in ANNOTATION_KEYS and is_filled(details[k])]
    for target in targets:
        value = field_value(record, target)
        layer, doc_id, note = parse_layer(record.field_sources.get(target))
        ev = evidence.get(target) if isinstance(evidence.get(target), dict) else {}
        doc_id = doc_id or ev.get("doc_id")
        url = ev.get("url") if layer == "document" else record.source_url
        if layer == "detail":
            note = "per-topic detail endpoint"
        row = _base(record, target, layer, "value", source_url=url, doc_id=doc_id, page=_int(ev.get("page")),
                    section=ev.get("section"), evidence_quote=ev.get("quote"),
                    published_at=as_timestamp(ev.get("published_at")),
                    retrieved_at=doc_retrieved.get(doc_id or "") if layer == "document" else fetched_at,
                    note=note)
        held[(target, layer)] = norm(value)
        add(row, value)

    verification = details.get("verification") if isinstance(details.get("verification"), dict) else {}
    for field, verdict in verification.items():
        if not isinstance(verdict, dict):
            continue
        sources = {s.get("layer"): s for s in verdict.get("sources") or [] if isinstance(s, dict)}
        for layer_name, stated in (verdict.get("layers") or {}).items():
            layer, note = _layer_name(str(layer_name))
            source = sources.get(layer_name) or {}
            for value in stated if isinstance(stated, list) else [stated]:
                if value is None:
                    continue
                if isinstance(value, bool):
                    role = "check"
                elif held.get((field, layer)) == norm(value):
                    continue
                else:
                    role = "alternative"
                row = _base(record, field, layer, role, source_url=source.get("url") or record.source_url,
                            page=_int(source.get("page")), retrieved_at=verdict.get("checked_at"),
                            note=note or (f"verification basis: {verdict.get('basis')}" if role == "check" else None))
                add(row, value)
        for entry in (verdict.get("range") or {}).get("values") or []:
            if not isinstance(entry, dict) or entry.get("value") is None:
                continue
            layer, note = _layer_name(str(entry.get("layer") or "other"))
            if held.get((field, layer)) == norm(entry["value"]) and entry.get("source") in (None, record.source_url):
                continue
            row = _base(record, field, layer, "alternative", source_url=entry.get("source"),
                        page=_int(entry.get("page")), note=note)
            add(row, entry["value"])

    corroboration = details.get("corroboration") if isinstance(details.get("corroboration"), dict) else {}
    for field, entries in corroboration.items():
        for entry in entries or []:
            if not isinstance(entry, dict) or entry.get("value") is None:
                continue
            row = _base(record, field, "document", "corroboration", source_url=entry.get("url"),
                        doc_id=entry.get("doc_id"), page=_int(entry.get("page")), section=entry.get("section"),
                        evidence_quote=entry.get("quote"), retrieved_at=doc_retrieved.get(entry.get("doc_id") or ""))
            add(row, entry["value"])

    for entry in details.get("value_history") or []:
        if not isinstance(entry, dict) or not entry.get("field"):
            continue
        layer, doc_id, _ = parse_layer(entry.get("source"))
        note = f"replaced by {entry.get('replaced_by')} ({entry.get('reason')})"
        row = _base(record, str(entry["field"]), layer, "superseded", source_url=record.source_url,
                    doc_id=doc_id, retrieved_at=fetched_at, note=note)
        add(row, entry.get("value"))
    return rows


def _typed_range(field: str, values: Iterable[Any]) -> dict[str, Any]:
    vals = [v for v in values if v is not None and not isinstance(v, bool)]
    if is_date_field(field):
        stamps = sorted(s for s in (as_timestamp(v) for v in vals) if s)
        return {"value_min_ts": stamps[0], "value_max_ts": stamps[-1]} if stamps else {}
    numbers = [n for n in (as_number(v) for v in vals) if n is not None]
    return {"value_min_num": min(numbers), "value_max_num": max(numbers)} if numbers else {}


def _typed_value(field: str, value: Any) -> dict[str, Any]:
    if is_date_field(field):
        return {"value_ts": as_timestamp(value), "value_num": None}
    return {"value_ts": None, "value_num": as_number(value) if not isinstance(value, str) else None}


def resolved_rows(record: NormalizedRecord) -> list[dict[str, Any]]:
    """One row per field: chosen value, status, min..max over all contributing values, sources."""
    details = record.details or {}
    verification = details.get("verification") if isinstance(details.get("verification"), dict) else {}
    value_range = details.get("value_range") if isinstance(details.get("value_range"), dict) else {}
    fields = list(dict.fromkeys([*RESOLVED_ALWAYS, *verification.keys(),
                                 *(f for f in RESOLVED_DETAILS if is_filled(field_value(record, f)))]))
    rows: list[dict[str, Any]] = []
    for field in fields:
        verdict = verification.get(field) if isinstance(verification.get(field), dict) else None
        held = field_value(record, field)
        row: dict[str, Any] = {"natural_key": record.natural_key, "field": field, "value_min_num": None,
                               "value_max_num": None, "value_min_ts": None, "value_max_ts": None,
                               "settled": None, "agree_count": None, "basis": None, "checked_at": None}
        if verdict is not None:
            status = verdict.get("status") if verdict.get("status") in STATUSES else "single_source"
            value = verdict.get("value") if verdict.get("value") is not None else held
            rng = verdict.get("range") or value_range.get(field) or {}
            contributing = [v for v in rng.get("values") or [] if isinstance(v, dict)]
            if not contributing:
                for layer_name, stated in (verdict.get("layers") or {}).items():
                    for v in stated if isinstance(stated, list) else [stated]:
                        if v is not None:
                            contributing.append({"value": v, "layer": layer_name})
            bounds = [rng.get("min"), rng.get("max")] if rng else [c.get("value") for c in contributing]
            if not rng and is_filled(value):
                bounds.append(value)
            row.update({"status": status, "value": value, "contributing": contributing,
                        "sources": verdict.get("sources") or [], "agree_count": verdict.get("agree_count"),
                        "basis": verdict.get("basis"), "checked_at": verdict.get("checked_at"),
                        "settled": rng.get("settled") if rng else (status in ("confirmed", "resolved") or None)})
            if verdict.get("basis") and verdict.get("note"):
                row["basis"] = f"{verdict['basis']}: {verdict['note']}"
        elif is_filled(held):
            layer, doc_id, _ = parse_layer(record.field_sources.get(field))
            contributing = [{"value": held, "layer": layer, "source": record.source_url}]
            bounds = [held]
            row.update({"status": "single_source", "value": held, "contributing": contributing,
                        "sources": [{"layer": layer, "url": record.source_url}], "agree_count": 1,
                        "basis": "single layer"})
        else:
            bounds = []
            row.update({"status": "absent", "value": None, "contributing": [], "sources": [],
                        "basis": "no layer states a value"})
        row.update(_typed_value(field, row["value"]))
        row.update(_typed_range(field, bounds))
        rows.append(row)
    return rows
