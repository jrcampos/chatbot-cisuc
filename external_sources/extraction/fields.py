"""
Config-driven extraction of fields from converted documents, plus the rules that
decide what a document value may do to a record.

Families come from config/document_fields.yaml. Extraction only ever *proposes*
values with verbatim evidence; `merge_extractions` decides: fill empty fields (with
provenance); replace an existing numeric value ONLY when the document is demonstrably
better (the API value is a call total repeated on every topic, or the document was
published after the API last indexed the record); otherwise record the disagreement
for a human. Nothing is ever silently lost: a replaced value is kept in
`details.value_history`.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from external_sources.documents.sections import (
    clean_text,
    find_topic_slice,
    split_sections,
)
from external_sources.extraction.amounts import EUR_AMOUNT, parse_eur
from external_sources.extraction.dates import parse_deadline
from external_sources.plugins.base import NormalizedRecord

DEFAULT_FIELDS_PATH = Path(__file__).parent.parent / "config" / "document_fields.yaml"
_PARSERS = ("eur", "text", "topic_table", "date", "eur_breakdown")
_PAGE = re.compile(r"^<!-- page (\d+) -->", re.MULTILINE)
_ROW = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?P<no>\d{1,2})\s*[.\u2014\u2013-]\s*(?P<label>[^\n]*?)\s+" + EUR_AMOUNT, re.MULTILINE | re.IGNORECASE
)
_TABLE_HEADER = re.compile(r"(?i)\btopic\b.*\bbudget\b")
_BUDGET_LABEL = re.compile(r"(?i)\b(?:available|estimated|total)\s+budget\b")
_ROW_START = re.compile(r"^\s*(?:#{1,6}\s*)?\d{1,2}\s*[.\u2014\u2013-]")
_STOP_WORDS = ("We ", "The ", "Topic", "##")


@dataclass(frozen=True)
class FieldRule:
    """One field a family can extract."""

    name: str
    target: str
    parse: str
    pattern: str | None
    sections: tuple[str, ...]
    min_chars: int
    until: str | None = None
    max_chars: int | None = None
    slice_from: str | None = None
    slice_to: str | None = None
    patterns: tuple[str, ...] = ()
    items: tuple[tuple[str, str], ...] = ()
    level: str = "call_type"
    unit: float = 1.0


@dataclass(frozen=True)
class Family:
    """A document template and what to extract from it."""

    name: str
    roles: tuple[str, ...]
    anchored: bool
    sections: dict[str, tuple[str, ...]]
    fields: tuple[FieldRule, ...]
    anchor: tuple[tuple[str, str], ...] = ()
    call_anchored: bool = False


@dataclass
class Extraction:
    """
    A value proposed from a document, with the verbatim text that supports it.

    `published_at` is the document's own publication/last-modified time (ISO UTC) when
    known; `call_totals` lists amounts known to be CALL-level totals for the same call
    (used to recognise an API value that is a call total repeated on every topic).
    """

    field: str
    value: Any
    evidence: str
    doc_id: str
    url: str
    section: str | None
    published_at: str | None = None
    call_totals: tuple[float, ...] = ()
    page: int | None = None


def load_families(path: str | Path = DEFAULT_FIELDS_PATH) -> dict[str, Family]:
    """Load and validate the families YAML; raises ValueError when malformed."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or not isinstance(data.get("families"), dict):
        raise ValueError(f"{path}: expected a top-level 'families' mapping")
    families: dict[str, Family] = {}
    for name, body in data["families"].items():
        sections: dict[str, tuple[str, ...]] = {}
        for section_name, raw in (body.get("sections") or {}).items():
            patterns = (raw,) if isinstance(raw, str) else tuple(raw or ())
            if not patterns or not all(isinstance(p, str) for p in patterns):
                raise ValueError(f"{path}: {name}.sections.{section_name}: expected a regex or a list of regexes")
            for pattern in patterns:
                _compile(pattern, f"{path}: {name}.sections.{section_name}")
            sections[section_name] = patterns
        anchor: list[tuple[str, str]] = []
        by_heading = (body.get("anchor") or {}).get("by_heading") or []
        if by_heading and body.get("anchored"):
            raise ValueError(f"{path}: {name}: 'anchored' and 'anchor' are mutually exclusive")
        for rule in by_heading:
            if not isinstance(rule, dict) or not rule.get("identifier") or not rule.get("heading"):
                raise ValueError(f"{path}: {name}.anchor.by_heading needs identifier and heading")
            _compile(rule["identifier"], f"{path}: {name}.anchor identifier")
            _compile(rule["heading"], f"{path}: {name}.anchor heading")
            anchor.append((rule["identifier"], rule["heading"]))
        rules: list[FieldRule] = []
        for field_name, spec in (body.get("fields") or {}).items():
            parse = spec.get("parse")
            if parse not in _PARSERS:
                raise ValueError(f"{path}: {name}.{field_name}: parse must be one of {_PARSERS}")
            names = tuple(spec.get("sections") or ([spec["section"]] if spec.get("section") else []))
            for section_name in names:
                if section_name not in sections:
                    raise ValueError(f"{path}: {name}.{field_name}: unknown section {section_name!r}")
            until = spec.get("until")
            if until is not None and until not in sections:
                raise ValueError(f"{path}: {name}.{field_name}: unknown until-section {until!r}")
            if parse == "date":
                if not spec.get("pattern"):
                    raise ValueError(f"{path}: {name}.{field_name}: date needs a pattern")
                if _compile(spec["pattern"], f"{path}: {name}.{field_name}").groups != 1:
                    raise ValueError(f"{path}: {name}.{field_name}: date pattern needs exactly one capture group")
            slice_from, slice_to = spec.get("slice_from"), spec.get("slice_to")
            if slice_from is not None or slice_to is not None:
                if parse != "text" or names or not slice_from:
                    raise ValueError(
                        f"{path}: {name}.{field_name}: slice_from/slice_to need parse: text, "
                        "slice_from and no sections"
                    )
                _compile(slice_from, f"{path}: {name}.{field_name} slice_from")
                if slice_to is not None:
                    _compile(slice_to, f"{path}: {name}.{field_name} slice_to")
            raw_pattern = spec.get("pattern")
            patterns = tuple(raw_pattern) if isinstance(raw_pattern, list) else ((raw_pattern,) if raw_pattern else ())
            if not all(isinstance(p, str) and p for p in patterns):
                raise ValueError(f"{path}: {name}.{field_name}: pattern must be a regex or a list of regexes")
            if parse == "date" and len(patterns) > 1:
                raise ValueError(f"{path}: {name}.{field_name}: date takes a single pattern")
            if body.get("call_anchored") and not (patterns and all("{CALL}" in p for p in patterns)):
                raise ValueError(f"{path}: {name}.{field_name}: call_anchored families need {{CALL}} in every pattern")
            if parse == "eur":
                for pattern in patterns or ("{EUR}",):
                    _check_eur_pattern(pattern.replace("{CALL}", "X"), f"{path}: {name}.{field_name}")
            items: list[tuple[str, str]] = []
            if parse == "eur_breakdown":
                raw_items = spec.get("items")
                if not isinstance(raw_items, list) or not raw_items:
                    raise ValueError(f"{path}: {name}.{field_name}: eur_breakdown needs a non-empty 'items' list")
                for item in raw_items:
                    if (not isinstance(item, dict) or not item.get("label") or not isinstance(item.get("pattern"), str)
                            or item["pattern"].count("{EUR}") != 1):
                        raise ValueError(
                            f"{path}: {name}.{field_name}: eur_breakdown items need a label and a pattern "
                            "with exactly one {EUR}"
                        )
                    _check_eur_pattern(item["pattern"], f"{path}: {name}.{field_name}")
                    items.append((str(item["label"]), item["pattern"]))
            if until is not None and parse not in ("topic_table", "text"):
                raise ValueError(f"{path}: {name}.{field_name}: until needs parse topic_table or text")
            if until is not None and not names:
                raise ValueError(f"{path}: {name}.{field_name}: until needs a section")
            if parse == "topic_table" and not names:
                raise ValueError(f"{path}: {name}.{field_name}: topic_table needs a section")
            rules.append(
                FieldRule(
                    name=field_name, target=spec.get("target", field_name), parse=parse,
                    pattern=patterns[0] if patterns else None, sections=names,
                    min_chars=int(spec.get("min_chars", 0)), until=until,
                    max_chars=int(spec["max_chars"]) if spec.get("max_chars") is not None else None,
                    slice_from=slice_from, slice_to=slice_to, patterns=patterns, items=tuple(items),
                    level=str(spec.get("level", "call_type")),
                    unit={"million": 1e6, None: 1.0}.get(spec.get("unit"), None) or _bad_unit(spec, path, name, field_name),
                )
            )
        families[name] = Family(
            name=name, roles=tuple(body.get("roles") or ()), anchored=bool(body.get("anchored")),
            sections=sections, fields=tuple(rules), anchor=tuple(anchor),
            call_anchored=bool(body.get("call_anchored")),
        )
    return families


def _bad_unit(spec: dict[str, Any], path: Any, name: str, field_name: str) -> float:
    raise ValueError(f"{path}: {name}.{field_name}: unit must be 'million' (or omitted), got {spec.get('unit')!r}")


def _check_eur_pattern(pattern: str, where: str) -> None:
    if "{EUR}" in pattern:
        if pattern.count("{EUR}") != 1:
            raise ValueError(f"{where}: eur pattern may use {{EUR}} only once")
        _compile(pattern.replace("{EUR}", EUR_AMOUNT), where)
    elif _compile(pattern, where).groups != 1:
        raise ValueError(f"{where}: eur pattern needs {{EUR}} or exactly one capture group")


def _scaled(numbers: str, unit: float) -> float | None:
    """
    '645.99' with unit 1e6 -> 645 990 000.0; several numbers (one column per budget year,
    '23.00 37.00') are summed. Sanity-bounded like parse_eur.
    """
    try:
        value = sum(float(n.replace(",", ".")) for n in numbers.split()) * unit
    except ValueError:
        return None
    return value if 1_000.0 <= value <= 1e11 else None


def page_at(markdown: str, position: int) -> int | None:
    """Page number of `position` in a converted PDF (the last `<!-- page N -->` before it), else None."""
    page = None
    for match in _PAGE.finditer(markdown, 0, max(position, 0) + 1):
        page = int(match.group(1))
    return page


def _page_of(markdown: str, fragment: str, start: int) -> int | None:
    """Page of the first occurrence of `fragment` at/after `start` (or anywhere) in `markdown`."""
    fragment = fragment.strip()
    if not fragment or "<!-- page" not in markdown:
        return None
    position = markdown.find(fragment, max(start, 0))
    if position == -1:
        position = markdown.find(fragment)
    return page_at(markdown, position) if position != -1 else None


def _compile(pattern: str, where: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        raise ValueError(f"{where}: invalid regex {pattern!r}: {exc}") from exc


def truncate_text(text: str, max_chars: int) -> str:
    """Cut `text` to at most `max_chars`, preferring a paragraph, then sentence, then word boundary."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    floor = max_chars // 2
    paragraph = cut.rfind("\n\n")
    if paragraph >= floor:
        return cut[:paragraph].rstrip()
    sentence = max((m.end() for m in re.finditer(r"[.!?](?=\s|$)", cut)), default=0)
    if sentence >= floor:
        return cut[:sentence].rstrip()
    space = cut.rfind(" ")
    return (cut[:space] if space >= floor else cut).rstrip()


_ROMAN_HEADING = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?P<num>[IVXL]+(?:\.\d+)*)\.?[ \t]+(?P<title>[A-Z][^\n]*)$", re.MULTILINE
)
_LEADER_LINE = re.compile(r"\.{4,}\s*\d+\s*$")
_LONG_SLICE = 2_000


def _depth(num: str) -> int:
    return 1 + num.count(".")


def _heading_slice(markdown: str, heading_regex: str) -> str | None:
    """
    Plain-text slice for a Roman-numeral heading ("V.1 EIC Accelerator Open") whose title
    matches `heading_regex`, up to the next Roman heading of the same or a higher level.

    Table-of-contents lines (dot leaders) are ignored. The contents page lists every
    heading first, so the LAST candidate longer than 2,000 chars wins, else the longest.
    """
    matcher = re.compile(heading_regex, re.IGNORECASE)
    headings = list(_ROMAN_HEADING.finditer(markdown))
    candidates: list[str] = []
    for index, match in enumerate(headings):
        if _LEADER_LINE.search(match.group(0)) or not matcher.search(match.group("title")):
            continue
        depth = _depth(match.group("num"))
        end = next(
            (h.start() for h in headings[index + 1:] if _depth(h.group("num")) <= depth), len(markdown)
        )
        candidates.append(markdown[match.start():end])
    if not candidates:
        return None
    long_ones = [c for c in candidates if len(c) > _LONG_SLICE]
    return long_ones[-1] if long_ones else max(candidates, key=len)


def _anchor_slice(family: Family, markdown: str, identifier: str) -> str | None:
    """Slice of `markdown` for `identifier` via the family's heading rules; None if no rule applies."""
    for id_pattern, heading_pattern in family.anchor:
        if re.search(id_pattern, identifier, re.IGNORECASE):
            break
    else:
        return None
    matcher = re.compile(heading_pattern, re.IGNORECASE)
    sections = split_sections(markdown)
    for section in sections:
        if matcher.search(section.heading):
            end = next((o.start for o in sections if o.start > section.start and o.level <= section.level), len(markdown))
            return markdown[section.start:end]
    return _heading_slice(markdown, heading_pattern) or ""


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def table_rows(text: str, call_budget: float | None = None) -> list[dict[str, Any]]:
    """
    Rows of a per-topic budget table: `N — label EUR amount [more columns]`.

    The label may wrap onto following plain-text lines, and rows may have been
    rendered as Markdown headings (`## 1. ISF-... EUR 5 000 000`); the FIRST amount on
    a row is the topic budget (later columns, e.g. maximum EU contribution, are ignored).
    Rows are read only AFTER the table header line (`Topic ... budget`); without one there
    is no table. When `call_budget` is known, a row above it is a glued neighbouring
    column and is dropped.
    """
    lines = text.split("\n")
    header = next((i for i, line in enumerate(lines) if _TABLE_HEADER.search(line)), None)
    if header is None:
        return []
    rows: list[dict[str, Any]] = []
    for index in range(header + 1, len(lines)):
        match = _ROW.match(lines[index])
        if not match:
            continue
        value = parse_eur(match.group("eur"))
        if value is None or (call_budget is not None and value > call_budget):
            continue
        if _BUDGET_LABEL.search(match.group("label")):
            continue
        label = match.group("label").strip()
        for follow in lines[index + 1:]:
            stripped = follow.strip()
            if (not stripped or _ROW_START.match(follow) or "EUR" in follow
                    or stripped.startswith(_STOP_WORDS)):
                break
            label += " " + stripped
        rows.append({"topic_no": int(match.group("no")), "label": label, "budget_eur": value})
    return rows


def extract_fields(
    family: Family, markdown: str, identifier: str, doc_id: str, url: str,
    published_at: str | None = None, call_identifier: str | None = None,
) -> tuple[list[Extraction], list[str]]:
    """
    Apply `family` to a document. Returns (extractions, notes); notes explain why
    nothing could be tried (currently `"anchor_not_found"`). `{CALL}` in a pattern is the
    record's call identifier (escaped, followed by whitespace); a call-anchored family does
    nothing without one.
    """
    if family.call_anchored and not call_identifier:
        return [], []
    call_re = re.escape(call_identifier) + r"(?=\s)" if call_identifier else None
    text = markdown
    offset = 0
    if family.anchor:
        sliced = _anchor_slice(family, markdown, identifier)
        if sliced is None:
            return [], []
        if not sliced:
            return [], ["anchor_not_found"]
        text = sliced
        offset = max(markdown.find(sliced), 0)
    elif family.anchored:
        span = find_topic_slice(markdown, identifier)
        if span is None:
            return [], ["anchor_not_found"]
        text = markdown[span[0]:span[1]]
        offset = span[0]
    sections = split_sections(text)
    by_name: dict[str, Any] = {}
    for section_name, patterns in family.sections.items():
        matchers = [re.compile(p, re.IGNORECASE) for p in patterns]
        by_name[section_name] = next(
            (s for s in sections if any(m.search(s.heading) for m in matchers)), None
        )

    found: list[Extraction] = []
    for rule in family.fields:
        chosen = [by_name[n] for n in rule.sections if by_name[n] is not None]
        if rule.sections and (not chosen or (rule.parse != "text" and len(chosen) < len(rule.sections))):
            continue
        source = "\n\n".join(s.text for s in chosen) if chosen else text
        section_label = chosen[0].heading if len(chosen) == 1 else None
        hint = offset + (chosen[0].start if chosen else 0)  # page lookup starts at the section, not the contents
        if rule.slice_from:
            start_match = re.search(rule.slice_from, text, re.IGNORECASE | re.MULTILINE)
            if start_match is None:
                continue
            end_match = re.compile(rule.slice_to, re.IGNORECASE | re.MULTILINE).search(text, start_match.end()) if rule.slice_to else None
            source = text[start_match.start(): end_match.start() if end_match else len(text)]
        elif rule.until and rule.parse == "text":
            first = chosen[0]
            until_section = by_name.get(rule.until)
            stop = until_section.start if until_section is not None and until_section.start > first.start else first.end
            newline = text.find("\n", first.start, stop)
            source = text[newline + 1: stop] if newline != -1 else ""
            section_label = first.heading
        if rule.parse == "eur":
            match, value = None, None
            for raw in rule.patterns or ("{EUR}",):
                if "{CALL}" in raw:
                    if call_re is None:
                        continue
                    raw = raw.replace("{CALL}", call_re)
                match = re.search(raw.replace("{EUR}", EUR_AMOUNT), source, re.IGNORECASE | re.MULTILINE)
                if match is None:
                    continue
                if "eur" in match.groupdict():
                    value = parse_eur(match.group("eur"))
                elif rule.unit != 1.0:
                    value = _scaled(match.group(1), rule.unit)
                else:
                    value = parse_eur(match.group(1))
                if value is not None:
                    break
            if match is None or value is None:
                continue
            evidence = _squash(source[max(0, match.start() - 60): match.end() + 60])
            fragment = match.group(0)
        elif rule.parse == "eur_breakdown":
            value, quotes, fragment = [], [], ""
            for label, raw in rule.items:
                item = re.search(raw.replace("{EUR}", EUR_AMOUNT), source, re.IGNORECASE)
                amount = parse_eur(item.group("eur")) if item else None
                if item is None or amount is None:
                    continue
                value.append({"label": label, "budget_eur": amount, "level": rule.level})
                quotes.append(_squash(item.group(0)))
                fragment = fragment or item.group(0)
            if not value:
                continue
            evidence = " | ".join(quotes)
        elif rule.parse == "date":
            raw = rule.pattern or ""
            if "{CALL}" in raw:
                if call_re is None:
                    continue
                raw = raw.replace("{CALL}", call_re)
            match = re.search(raw, source, re.IGNORECASE | re.MULTILINE)
            value = parse_deadline(match.group(1)) if match else None
            if match is None or value is None:
                continue
            evidence = _squash(source[max(0, match.start() - 60): match.end() + 60])
            fragment = match.group(0)
        elif rule.parse == "topic_table":
            start_section = chosen[0]
            until_section = by_name.get(rule.until) if rule.until else None
            stop = (
                until_section.start
                if until_section is not None and until_section.start > start_section.start
                else start_section.end
            )
            table_text = text[start_section.start:stop]
            call_budget = next((e.value for e in found if e.field == "details.call_budget_eur"), None)
            value = table_rows(table_text, call_budget)
            if not value:
                continue
            evidence = _squash(table_text[:160])
            fragment = next((line for line in table_text.splitlines() if line.strip()), "")
        else:
            value = clean_text(source)
            if not value or len(value) < rule.min_chars:
                continue
            evidence = _squash(value[:160])
            fragment = next((line for line in source.splitlines()
                             if len(line.strip()) >= 20 and not line.startswith("<!--")), "")
            if rule.max_chars:
                value = truncate_text(value, rule.max_chars)
        found.append(Extraction(
            rule.target, value, evidence, doc_id, url, section_label, published_at,
            page=_page_of(markdown, fragment, hint),
        ))
    return found, []


def _tokens(text: str) -> list[str]:
    """Lower-case alphanumeric tokens with a trailing plural 's' removed (SNAPs -> snap)."""
    return [
        t[:-1] if (t.endswith("s") and len(t) > 3) else t
        for t in re.split(r"[^a-z0-9]+", text.lower())
        if t
    ]


def _identifier_score(record: NormalizedRecord, row: dict[str, Any], head_only: bool) -> float:
    """Share of the topic's DISTINCTIVE identifier tokens (beyond the call id) found in the row label."""
    call_tokens = set(_tokens(str(record.details.get("call_identifier") or "")))
    distinct = [t for t in _tokens(record.natural_key) if t not in call_tokens] or _tokens(record.natural_key)[-2:]
    label = re.split(r" - |: ", row["label"], maxsplit=1)[0] if head_only else row["label"]
    label_tokens = set(_tokens(label))
    return sum(1 for t in distinct if t in label_tokens) / len(distinct)


def _similarity(a: str, b: str) -> float:
    def norm(t):
        return re.sub(r"\W+", " ", t.lower()).strip()
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def match_topic_rows(
    records: list[NormalizedRecord], rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """
    Assign each topic record of ONE call to its row in the call document's budget table.

    Only confident matches are returned ({natural_key: {"row", "how"}}); `how` is one of
    identifier | label_words | title_similarity | elimination | single_row. Stages, per
    record: distinctive identifier tokens in the row's head, then anywhere in the label,
    then title similarity (clear winner only); rows claimed by two records are dropped;
    finally a lone leftover record is paired with a lone leftover row.
    """
    chosen: dict[str, tuple[int, str]] = {}
    for record in records:
        for head_only, how in ((True, "identifier"), (False, "label_words")):
            scores = [_identifier_score(record, row, head_only) for row in rows]
            winners = [i for i, score in enumerate(scores) if score >= 0.99]
            if len(winners) == 1:
                chosen[record.natural_key] = (winners[0], how)
                break
        if record.natural_key in chosen:
            continue
        ranked = sorted(
            ((_similarity(row["label"], record.title), i) for i, row in enumerate(rows)), reverse=True
        )
        if ranked and ranked[0][0] >= 0.6 and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.1):
            chosen[record.natural_key] = (ranked[0][1], "title_similarity")

    claims: dict[int, list[str]] = {}
    for key, (index, _) in chosen.items():
        claims.setdefault(index, []).append(key)
    for index, keys in claims.items():
        if len(keys) > 1:
            for key in keys:
                del chosen[key]

    loose = [r.natural_key for r in records if r.natural_key not in chosen]
    free = [i for i in range(len(rows)) if i not in {idx for idx, _ in chosen.values()}]
    contiguous = sorted(r["topic_no"] for r in rows) == list(range(1, len(rows) + 1))
    if len(loose) == 1 and len(free) == 1 and len(rows) == len(records) and contiguous:
        chosen[loose[0]] = (free[0], "single_row" if len(records) == 1 else "elimination")
    return {key: {"row": rows[index], "how": how} for key, (index, how) in chosen.items()}


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _should_replace(
    existing: float, extraction: Extraction, reference_updated_at: str | None, tolerance: float
) -> tuple[bool, str | None]:
    """
    May this document value replace the existing numeric one? Returns (replace, reason).

    `reference_updated_at` is when the existing value was last known current: the record's
    `source_updated_at` for an API value, the earlier document's `published_at` otherwise.
    """
    for total in extraction.call_totals:
        if (
            total
            and abs(existing - total) <= tolerance * total
            and abs(extraction.value - total) > tolerance * total
        ):
            return True, "api_is_call_total"
    if (
        extraction.published_at
        and reference_updated_at
        and extraction.published_at > reference_updated_at
    ):
        return True, "newer_document"
    return False, None


def merge_extractions(
    record: NormalizedRecord, extractions: list[Extraction], tolerance: float = 0.01
) -> NormalizedRecord:
    """
    Apply document extractions to a record and return a new record.

    - empty field: filled, with provenance (`document:<id>`) and evidence;
    - numeric field that differs by more than `tolerance`: replaced only if the existing
      value is a call total repeated on the topic (`api_is_call_total`) or the document is
      newer than the record's `source_updated_at` (`newer_document`); the old value goes to
      `details.value_history` and a `superseded` gap is recorded;
    - otherwise the existing value stays and a `discrepancy` gap records both values and
      both timestamps so a person can decide;
    - a numeric document value that AGREES with the existing value (from another source) is
      kept in `details.corroboration[field]` (value, doc_id, url, quote, page, section), so the
      verification layer can count independent layers that state the same value.
    """
    updates: dict[str, Any] = {}
    details = dict(record.details)
    sources = dict(record.field_sources)
    gaps = list(record.data_gaps)
    evidence_map = dict(details.get("evidence") or {})
    history = list(details.get("value_history") or [])
    corroboration: dict[str, list[dict[str, Any]]] = {
        k: list(v) for k, v in (details.get("corroboration") or {}).items()
    }
    document_published: dict[str, str | None] = {}  # target -> published_at of the document supplying it

    def current(target: str) -> Any:
        if target.startswith("details."):
            return details.get(target[len("details."):])
        return updates[target] if target in updates else getattr(record, target, None)

    def write(extraction: Extraction) -> None:
        target = extraction.field
        if target.startswith("details."):
            details[target[len("details."):]] = extraction.value
        else:
            updates[target] = extraction.value
        sources[target] = f"document:{extraction.doc_id}"
        document_published[target] = extraction.published_at
        evidence_map[target] = {
            "quote": extraction.evidence, "url": extraction.url,
            "doc_id": extraction.doc_id, "section": extraction.section,
            "published_at": extraction.published_at, "page": extraction.page,
        }

    for extraction in extractions:
        target = extraction.field
        existing = current(target)
        if _is_empty(existing):
            write(extraction)
            continue
        numeric = (
            isinstance(existing, (int, float)) and not isinstance(existing, bool)
            and isinstance(extraction.value, (int, float))
        )
        if not numeric:
            continue
        if abs(existing - extraction.value) <= tolerance * max(abs(existing), 1.0):
            if sources.get(target) != f"document:{extraction.doc_id}":
                corroboration.setdefault(target, []).append({
                    "value": extraction.value, "doc_id": extraction.doc_id, "url": extraction.url,
                    "quote": extraction.evidence, "page": extraction.page, "section": extraction.section,
                })
            continue
        existing_source = sources.get(target, "api")
        existing_updated_at = (
            document_published[target] if target in document_published else record.source_updated_at
        )
        replace, reason = _should_replace(existing, extraction, existing_updated_at, tolerance)
        if replace:
            confirmation = "pending" if reason == "newer_document" else "structural"
            history.append(
                {"field": target, "value": existing, "source": existing_source,
                 "replaced_by": f"document:{extraction.doc_id}", "reason": reason,
                 "confirmation": confirmation}
            )
            write(extraction)
            gaps.append(
                {"field": target, "kind": "superseded", "reason": reason, "previous": existing,
                 "new": extraction.value, "url": extraction.url, "confirmation": confirmation}
            )
        else:
            gap = {
                "field": target, "kind": "discrepancy", "existing": existing,
                "existing_source": existing_source, "document": extraction.value,
                "url": extraction.url, "evidence": extraction.evidence,
                "existing_updated_at": existing_updated_at, "api_updated_at": record.source_updated_at,
                "document_published_at": extraction.published_at,
            }
            if existing_source == "api":  # backward-compatible key
                gap["api"] = existing
            gaps.append(gap)
    if evidence_map:
        details["evidence"] = evidence_map
    if history:
        details["value_history"] = history
    if corroboration:
        details["corroboration"] = corroboration
    return record.model_copy(
        update={**updates, "details": details, "field_sources": sources, "data_gaps": gaps}
    )
