"""
Context packing: turn a call record (and optional document sections) into a bounded,
intent-shaped text block for the model.

Nothing here talks to a database or a model; retrieval decides WHICH calls, this
module decides HOW MUCH of each goes into the prompt. Rules: the card is always
included; other blocks follow the intent's priority order, each capped, inside a
total character budget; truncation cuts at a paragraph/sentence boundary and says
so, pointing at the source URL; empty blocks are skipped; boilerplate document
sections are excluded unless the intent opts in. Every answering intent carries a
`sources` block right after the card: the numbered, citable sources of the record
(`details.sources`, see sources.py), so the model can cite the page a fact comes from.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from external_sources.plugins.base import NormalizedRecord
from external_sources.sources import render_sources

DEFAULT_CONTEXT_PATH = Path(__file__).parent / "config" / "context_blocks.yaml"
MIN_USEFUL_CHARS = 200
_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class DocSection:
    """A named slice of a document (e.g. the 'eligibility' section of a call fiche)."""

    name: str
    text: str
    url: str
    role: str = "call_specific"


@dataclass
class ContextPack:
    """The assembled context and what happened while assembling it."""

    text: str
    blocks_used: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    omitted: list[dict[str, str]] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        """Length of the assembled text."""
        return len(self.text)


def load_context_policy(path: str | Path = DEFAULT_CONTEXT_PATH) -> dict[str, Any]:
    """Load the context policy YAML."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def truncate_text(text: str, limit: int, marker: str) -> str:
    """Cut `text` to at most `limit` chars (marker included), preferring paragraph/sentence ends."""
    if len(text) <= limit:
        return text
    room = max(limit - len(marker), 0)
    cut = text[:room]
    for separator in ("\n\n", "\n", ". ", " "):
        index = cut.rfind(separator)
        if index >= room * 0.5:
            cut = cut[: index + (1 if separator == ". " else 0)]
            break
    return cut.rstrip() + marker


def _money(value: float) -> str:
    return f"EUR {value:,.0f}"


def _is_call_total(record: NormalizedRecord) -> bool:
    """funding_amount verified as the CALL total repeated on the topic (see verification.py)."""
    verdict = (record.details.get("verification") or {}).get("funding_amount") or {}
    return verdict.get("level") == "call_total"


def build_card(record: NormalizedRecord) -> str:
    """~250-char summary: title, id, programme, status, deadline, budget, URL."""
    facts = [
        f"Programme: {record.programme}" if record.programme else None,
        f"Status: {record.status}" if record.status else None,
        f"Deadline: {record.deadline_date[:10]}" if record.deadline_date else None,
    ]
    lines = [f"{record.title} ({record.natural_key})", " | ".join(f for f in facts if f)]
    call_budget = record.details.get("call_budget_eur")
    if record.funding_amount and _is_call_total(record):
        lines.append(f"Call budget (shared by all topics of the call): {_money(record.funding_amount)}")
    elif record.funding_amount:
        lines.append(f"Budget: {_money(record.funding_amount)}")
    elif call_budget:
        lines.append(f"Call budget (covers all topics of the call): {_money(call_budget)}")
    lines.append(f"URL: {record.source_url}")
    return "\n".join(line for line in lines if line)


def _description(record: NormalizedRecord) -> str | None:
    return record.description


def _budget(record: NormalizedRecord) -> str | None:
    budget = record.details.get("budget") or {}
    lines: list[str] = []
    if record.funding_amount and not _is_call_total(record):
        lines.append(f"Topic budget: {_money(record.funding_amount)}")
    per_year = budget.get("per_year")
    replaced = any(
        isinstance(e, dict) and e.get("field") == "funding_amount"
        for e in (record.details.get("value_history") or [])
    )
    if isinstance(per_year, dict) and per_year:
        if replaced:
            lines.append(
                "API budget table lists the call total; topic budget taken from the call document"
            )
        else:
            lines.append("Per year: " + "; ".join(f"{y}: {_money(v)}" for y, v in per_year.items()))
    if budget.get("expected_grants"):
        lines.append(f"Expected grants: {budget['expected_grants']}")
    low, high = budget.get("min_contribution"), budget.get("max_contribution")
    if low or high:
        lines.append(f"Grant size: {_money(low) if low else '?'} - {_money(high) if high else '?'}")
    topic_row = record.details.get("topic_budget_eur")
    if topic_row and topic_row != record.funding_amount:
        lines.append(f"Topic budget per call document: {_money(topic_row)}")
    call_total = record.details.get("call_budget_eur")
    if call_total:
        lines.append(f"Call budget (all topics of the call): {_money(call_total)}")
    lines += _range_lines(record, ("funding_amount", "details.topic_budget_eur", "details.call_budget_eur"))
    return "\n".join(lines) or None


_RANGE_LABELS = {
    "funding_amount": "Budget", "details.topic_budget_eur": "Topic budget",
    "details.call_budget_eur": "Call budget", "deadline_date": "Deadline", "start_date": "Opening date",
}


def _fmt(value: Any) -> str:
    return _money(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def _range_lines(record: NormalizedRecord, fields: tuple[str, ...]) -> list[str]:
    """Ranges across disagreeing sources: min..max, how it stands, and every value with its source."""
    lines: list[str] = []
    for name in fields:
        rng = (record.details.get("value_range") or {}).get(name)
        if not isinstance(rng, dict):
            continue
        label = _RANGE_LABELS.get(name, name)
        if rng.get("settled"):
            state = f"settled by the official source: {_fmt(rng.get('primary'))}"
        else:
            state = f"sources disagree; portal value {_fmt(rng.get('primary'))}"
        lines.append(f"{label} range across sources: {_fmt(rng.get('min'))} – {_fmt(rng.get('max'))} ({state})")
        for entry in rng.get("values") or []:
            page = f", p. {entry['page']}" if entry.get("page") else ""
            lines.append(f"- {_fmt(entry.get('value'))} ({entry.get('layer')}, {entry.get('source')}{page})")
    return lines


def _range_sources(record: NormalizedRecord) -> list[str]:
    return [
        str(entry["source"])
        for rng in (record.details.get("value_range") or {}).values() if isinstance(rng, dict)
        for entry in rng.get("values") or [] if entry.get("source")
    ]


def _deadlines(record: NormalizedRecord) -> str | None:
    lines: list[str] = []
    if record.deadline_date:
        lines.append(f"Deadline: {record.deadline_date[:10]}")
    dates = record.details.get("deadline_dates")
    if isinstance(dates, list) and len(dates) > 1:
        lines.append("All cut-off dates: " + ", ".join(str(d)[:10] for d in dates))
    verdict = (record.details.get("verification") or {}).get("deadline_date") or {}
    if verdict.get("status") == "conflicting":
        layers = verdict.get("layers") or {}
        lines.append(
            f"Note: sources disagree on the deadline — portal: {', '.join(layers.get('api') or [])}; "
            f"call document / work programme: {layers.get('document')}. Check the topic page."
        )
    opening = (record.details.get("budget") or {}).get("planned_opening_date")
    if opening:
        lines.append(f"Planned opening: {str(opening)[:10]}")
    lines += _range_lines(record, ("deadline_date", "start_date"))
    return "\n".join(lines) or None


def _latest_updates(record: NormalizedRecord) -> str | None:
    updates = record.details.get("latest_updates")
    if not isinstance(updates, list):
        return None
    lines = [f"{str(u.get('date', ''))[:10]}: {u.get('text', '')}".strip(": ") for u in updates if isinstance(u, dict)]
    return "\n".join(line for line in lines if line) or None


def _links(record: NormalizedRecord) -> str | None:
    links = record.details.get("description_links")
    if not isinstance(links, list):
        return None
    lines = [f"- {link.get('text', '')}: {link['url']}" for link in links[:5] if isinstance(link, dict) and link.get("url")]
    return "\n".join(lines) or None


def _detail_text(name: str) -> Callable[[NormalizedRecord], str | None]:
    return lambda record: (record.details.get(name) or None)


def _destination(record: NormalizedRecord) -> str | None:
    parts = [record.details.get("destination_description"), record.details.get("destination_details")]
    return "\n".join(str(p) for p in parts if p) or None


BLOCKS: dict[str, Callable[[NormalizedRecord], str | None]] = {
    "description": _description,
    "budget": _budget,
    "deadlines": _deadlines,
    "conditions": _detail_text("conditions"),
    "support": _detail_text("support"),
    "submission_notice": _detail_text("submission_notice"),
    "destination": _destination,
    "latest_updates": _latest_updates,
    "links": _links,
    "sources": render_sources,
}


def build_context(
    record: NormalizedRecord,
    intent: str,
    policy: dict[str, Any] | None = None,
    doc_sections: list[DocSection] | None = None,
    max_chars: int | None = None,
) -> ContextPack:
    """
    Assemble the context for `record` under `intent`.

    Raises ValueError for an unknown intent. The card is always included; every
    other block is capped by its own limit and by what remains of the budget.
    """
    policy = policy or load_context_policy()
    intents = policy["intents"]
    if intent not in intents:
        raise ValueError(f"unknown intent {intent!r}; valid intents: {sorted(intents)}")
    spec = intents[intent]
    budget = int(max_chars or spec.get("max_chars") or policy["default_max_chars"])
    marker = policy["truncation_marker"].format(url=record.source_url)
    allow_boilerplate = bool(spec.get("include_boilerplate"))
    documents = {
        d.name: d for d in (doc_sections or []) if allow_boilerplate or d.role != "boilerplate"
    }

    parts: list[str] = []
    pack = ContextPack(text="", citations=[record.source_url])
    remaining = budget
    for name in spec["blocks"]:
        if name == "card":
            text: str | None = build_card(record)
            limit = len(text)
        else:
            if name.startswith("doc:"):
                document = documents.get(name[4:])
                text = document.text if document else None
                cap_key = "doc"
            else:
                text = BLOCKS[name](record)
                cap_key = name
            cap = int(policy["blocks"].get(cap_key, {}).get("max_chars", budget))
            header = f"[{name.split(':')[-1].replace('_', ' ').title()}]\n"
            limit = min(cap, remaining - len(header) - len(_SEPARATOR))
            if text:
                text = str(text)
        if not text:
            pack.omitted.append({"block": name, "reason": "empty"})
            continue
        if name != "card" and limit < MIN_USEFUL_CHARS:
            pack.omitted.append({"block": name, "reason": "no_budget"})
            continue
        if len(text) > limit:
            text = truncate_text(text, limit, marker)
            pack.truncated.append(name)
        piece = text if name == "card" else header + text
        parts.append(piece)
        pack.blocks_used.append(name)
        remaining -= len(piece) + len(_SEPARATOR)
        if name.startswith("doc:"):
            pack.citations.append(documents[name[4:]].url)
        if name in ("budget", "deadlines"):
            for url in _range_sources(record):
                if url not in pack.citations:
                    pack.citations.append(url)
        if name == "sources":
            for source in record.details.get("sources") or []:
                if source.get("url") and source["url"] not in pack.citations:
                    pack.citations.append(source["url"])
    pack.text = _SEPARATOR.join(parts)
    return pack
