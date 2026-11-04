"""
Transforms specific to the EU Funding & Tenders Portal payload.

Registered into the shared transform registry on import. The field spec
(config/ft_portal_fields.yaml) lists this module under `transform_modules`, so
loading the spec imports it before its steps are validated.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from external_sources.normalization.transforms import (
    html_to_text,
    iso_datetime,
    strip,
    transform,
)

_TOPIC_API_URL = re.compile(
    r"^https?://[^/]+/info/funding-tenders/opportunities/data/topicDetails/"
    r"(?P<id>[^/?#]+?)(?:\.json)?/?$",
    re.IGNORECASE,
)
_TOPIC_PAGE_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
    "opportunities/topic-details/{id}"
)

_MALFORMED = (AttributeError, TypeError, ValueError, OverflowError)


def _to_float(value: Any) -> float | None:
    """Finite float from a number/numeric string; None for anything else."""
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _own_actions(budget: dict[str, Any], natural_key: str) -> list[dict[str, Any]]:
    """
    Actions of `budgetTopicActionMap` that belong to THIS record.

    The map lists sibling topics of the same call too; summing all of it would
    inflate the budget, so only actions whose "<IDENTIFIER> - <type>" head
    equals the record's own identifier count. Malformed shapes are skipped.
    """
    topic_map = budget.get("budgetTopicActionMap")
    if not isinstance(topic_map, dict):
        return []
    actions: list[dict[str, Any]] = []
    for entries in topic_map.values():
        if not isinstance(entries, list):
            continue
        for action in entries:
            if not isinstance(action, dict):
                continue
            head = str(action.get("action", "")).split(" - ", 1)[0].strip()
            if head == natural_key:
                actions.append(action)
    return actions


def _year_amounts(action: dict[str, Any]) -> dict[str, float]:
    """Positive per-year amounts of one action (zero/invalid = unstated)."""
    year_map = action.get("budgetYearMap")
    if not isinstance(year_map, dict):
        return {}
    amounts: dict[str, float] = {}
    for year, raw_amount in year_map.items():
        number = _to_float(raw_amount)
        if number is not None and number > 0:
            amounts[str(year)] = number
    return amounts


def _per_year(actions: list[dict[str, Any]]) -> dict[str, float]:
    """
    Per-year totals over the record's own actions. Actions often repeat the
    same topic-level budget, so identical year maps are counted once; genuinely
    different maps are summed.
    """
    totals: dict[str, float] = {}
    seen: set[tuple[tuple[str, float], ...]] = set()
    for action in actions:
        amounts = _year_amounts(action)
        signature = tuple(sorted(amounts.items()))
        if not signature or signature in seen:
            continue
        seen.add(signature)
        for year, number in amounts.items():
            totals[year] = totals.get(year, 0.0) + number
    return totals


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _parse_dates(value: Any, ctx: dict[str, Any]) -> list[tuple[datetime, str]]:
    """(aware datetime, raw string) for every parseable date in `value`."""
    parsed: list[tuple[datetime, str]] = []
    for item in _as_list(value):
        iso = iso_datetime(item, ctx)
        if iso:
            parsed.append((datetime.fromisoformat(iso), item))
    return parsed


@transform("ft_budget_total")
def ft_budget_total(value: Any, ctx: dict[str, Any]) -> float | None:
    """Total EUR budget of the record's own action(s); None when unstated/zero."""
    try:
        if not isinstance(value, dict):
            return None
        total = sum(_per_year(_own_actions(value, ctx.get("natural_key", ""))).values())
        return total if math.isfinite(total) and total > 0 else None
    except _MALFORMED:
        return None


@transform("ft_budget_summary")
def ft_budget_summary(value: Any, ctx: dict[str, Any]) -> dict[str, Any] | None:
    """
    Prepared budget details of the record's own action(s).

    Amounts (`per_year`, `total_eur`, `currency`) are omitted when the source
    states none (zero/empty year maps); None when nothing at all remains.
    """
    try:
        if not isinstance(value, dict):
            return None
        actions = _own_actions(value, ctx.get("natural_key", ""))
        if not actions:
            return None
        per_year = _per_year(actions)
        total = sum(per_year.values())
        has_amounts = bool(per_year) and math.isfinite(total)
        grants = sum(int(_to_float(a.get("expectedGrants")) or 0) for a in actions)
        minimums = [n for a in actions if (n := _to_float(a.get("minContribution"))) and n > 0]
        maximums = [n for a in actions if (n := _to_float(a.get("maxContribution"))) and n > 0]
        opening = next(
            (p for a in actions if (p := iso_datetime(a.get("plannedOpeningDate"), ctx))),
            None,
        )
        summary: dict[str, Any] = {
            "currency": "EUR" if has_amounts else None,
            "per_year": per_year if has_amounts else None,
            "total_eur": total if has_amounts else None,
            "expected_grants": grants or None,
            "min_contribution": min(minimums) if minimums else None,
            "max_contribution": max(maximums) if maximums else None,
            "planned_opening_date": opening,
        }
        summary = {key: val for key, val in summary.items() if val}
        return summary or None
    except _MALFORMED:
        return None


@transform("ft_latest_infos")
def ft_latest_infos(value: Any, ctx: dict[str, Any]) -> list[dict[str, str]] | None:
    """Decoded 'latest infos' as [{"date", "text"}] (HTML content -> text)."""
    if not isinstance(value, list):
        return None
    updates: list[dict[str, str]] = []
    for item in value:
        try:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            text = html_to_text(content, ctx) if isinstance(content, str) and content else None
            date = iso_datetime(item.get("lastChangeDate") or item.get("approvalDate"), ctx)
        except _MALFORMED:
            continue
        entry = {k: v for k, v in (("date", date), ("text", text)) if v}
        if entry:
            updates.append(entry)
    return updates or None


@transform("ft_pick_deadline")
def ft_pick_deadline(value: Any, ctx: dict[str, Any]) -> str | None:
    """
    Pick the deadline that matters from a (possibly multi-cut-off) list: the
    RAW string of the earliest date that is not in the past, else the latest
    date. None when no date parses.
    """
    try:
        dates = _parse_dates(value, ctx)
        if not dates:
            return None
        now = ctx.get("now") or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        upcoming = [d for d in dates if d[0] >= now]
        chosen = min(upcoming) if upcoming else max(dates)
        return chosen[1]
    except _MALFORMED:
        return None


@transform("ft_all_dates", produces_date=True)
def ft_all_dates(value: Any, ctx: dict[str, Any]) -> list[str] | None:
    """Sorted, de-duplicated ISO datetimes of every parseable date; None if none."""
    try:
        return sorted({iso for item in _as_list(value) if (iso := iso_datetime(item, ctx))}) or None
    except _MALFORMED:
        return None


@transform("ft_topic_url")
def ft_topic_url(value: Any, ctx: dict[str, Any]) -> str | None:
    """
    Canonical, browsable topic page for a portal URL.

    The API path `.../opportunities/data/topicDetails/<ID>[.json]` becomes
    `.../opportunities/portal/screen/opportunities/topic-details/<ID>`; any
    other URL passes through with a trailing `.json` removed.
    """
    if not isinstance(value, str):
        return None
    match = _TOPIC_API_URL.match(value.strip())
    if match:
        return _TOPIC_PAGE_URL.format(id=match.group("id"))
    text = value.strip()
    return text.removesuffix(".json")


def _text(value: Any) -> str | None:
    """Whitespace-collapsed non-empty string, or None for anything else."""
    return strip(value, {}) if isinstance(value, str) else None


def _label(value: Any) -> str | None:
    """A status/procedure label: a dict's abbreviation (else description), or a string."""
    if isinstance(value, dict):
        return _text(value.get("abbreviation")) or _text(value.get("description"))
    return _text(value)


def _grant_types(value: Any) -> list[dict[str, str]]:
    """[{"code", "description"}] from a `typeOfMGA` list (opaque ids dropped)."""
    grants: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        entry = {
            key: text
            for key, text in (
                ("code", _text(item.get("abbreviation"))),
                ("description", _text(item.get("description"))),
            )
            if text
        }
        if entry:
            grants.append(entry)
    return grants


def _action_types(value: Any) -> list[dict[str, Any]]:
    """[{"action", "grant_type"}] from an action's `types` list."""
    types: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        grants = _grant_types(item.get("typeOfMGA"))
        entry = {
            key: val
            for key, val in (("action", _text(item.get("typeOfAction"))), ("grant_type", grants))
            if val
        }
        if entry:
            types.append(entry)
    return types


@transform("ft_actions")
def ft_actions(value: Any, ctx: dict[str, Any]) -> list[dict[str, Any]] | None:
    """
    Structured form of the decoded `actions` list: action types with their
    grant types, status, submission procedure, planned opening date and every
    deadline (ISO). Opaque ids are dropped; empty keys are omitted; malformed
    shapes never raise.
    """
    if not isinstance(value, list):
        return None
    actions: list[dict[str, Any]] = []
    for item in value:
        try:
            if not isinstance(item, dict):
                continue
            deadlines = item.get("deadlineDates")
            entry = {
                "types": _action_types(item.get("types")),
                "status": _label(item.get("status")),
                "submission_procedure": _label(item.get("submissionProcedure")),
                "planned_opening_date": iso_datetime(item.get("plannedOpeningDate"), ctx),
                "deadline_dates": ft_all_dates(deadlines, ctx) if deadlines else None,
            }
        except _MALFORMED:
            continue
        entry = {key: val for key, val in entry.items() if val}
        if entry:
            actions.append(entry)
    return actions or None


def _link_date(value: Any, ctx: dict[str, Any]) -> str | None:
    """ISO UTC for epoch milliseconds (int or digit string) or a parseable date string."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            moment = datetime.fromtimestamp(int(value) / 1000, tz=UTC)
            return moment.replace(microsecond=0).isoformat()
        return iso_datetime(value, ctx)
    except (OverflowError, OSError, ValueError):
        return None


# (output key, source key, is_date) in output order
_LINK_FIELDS = (
    ("call_identifier", "callIdentifier", False),
    ("criterion_code", "criterionCode", False),
    ("criterion_description", "criterionDescription", False),
    ("url", "url", False),
    ("mga_code", "mgaCode", False),
    ("mga_description", "mgaDescription", False),
    ("start_date", "startDate", True),
    ("end_date", "endDate", True),
)


@transform("ft_links")
def ft_links(value: Any, ctx: dict[str, Any]) -> list[dict[str, str]] | None:
    """
    Structured submission links from the decoded `links` list: call and
    criterion identifiers, MGA code/description, the http(s) `url` and
    start/end dates (epoch milliseconds -> ISO UTC). Entries without an
    http(s) url are dropped; unparsable dates are omitted; never raises.
    """
    if not isinstance(value, list):
        return None
    links: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _text(item.get("url"))
        if not url or not url.lower().startswith(("http://", "https://")):
            continue  # a link without a usable url carries nothing
        entry: dict[str, str] = {}
        for target, source, is_date in _LINK_FIELDS:
            result = _link_date(item.get(source), ctx) if is_date else _text(item.get(source))
            if result:
                entry[target] = result
        links.append(entry)
    return links or None


@transform("ft_grant_types")
def ft_grant_types(value: Any, ctx: dict[str, Any]) -> list[dict[str, str]] | None:
    """`topicMGAs` -> [{"code", "description"}]; opaque ids are dropped."""
    if not isinstance(value, list):
        return None
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entry = {
            key: str(item[src]).strip()
            for key, src in (("code", "abbreviation"), ("description", "description"))
            if item.get(src) and str(item[src]).strip()
        }
        if entry:
            out.append(entry)
    return out or None
