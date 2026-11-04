"""
Normalise an F&T Portal per-topic DETAIL payload into the SEARCH payload shape.

The detail endpoint carries the same content as the search index under
different names and plain (non-array) values. `fill_from_detail` injects only
what the search metadata lacks, as single-element lists, so the one field spec
(config/ft_portal_fields.yaml) keeps working unchanged.
"""

from __future__ import annotations

import json
from typing import Any

# search key -> detail key
DETAIL_TO_SEARCH: dict[str, str] = {
    "descriptionByte": "description",
    "topicConditions": "conditions",
    "supportInfo": "supportInfo",
    "sepTemplate": "sepTemplate",
    "budgetOverview": "budgetOverviewJSONItem",
    "latestInfos": "latestInfos",
    "links": "links",
    "actions": "actions",
    "keywords": "keywords",
    "tags": "tags",
    "callTitle": "callTitle",
    "callIdentifier": "callIdentifier",
    "allowPartnerSearch": "allowPartnerSearch",
    "title": "title",
    "identifier": "identifier",
}

# Detail-only keys stored (untransformed) under metadata["detail"].
EXTRA_DETAIL_KEYS = ("publicationDateLong", "sme", "topicMGAs")

_JSON_KEYS = {"budgetOverview", "latestInfos", "links", "actions"}
_LIST_KEYS = {"keywords", "tags"}


def _is_empty(value: Any) -> bool:
    """True for None, "", [], {}, "[]" and lists whose items are all empty."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in ("", "[]")
    if isinstance(value, (list, tuple)):
        return all(_is_empty(item) for item in value)
    if isinstance(value, dict):
        return not value
    return False


def _to_search_shape(search_key: str, value: Any) -> list[Any] | None:
    """Wrap a detail value as a search-shaped list; None when nothing usable."""
    if _is_empty(value):
        return None
    if search_key in _LIST_KEYS and isinstance(value, list):
        items = [str(v) for v in value if not _is_empty(v)]
        return items or None
    if search_key in _JSON_KEYS and isinstance(value, (dict, list)):
        return [json.dumps(value, ensure_ascii=False)]
    if isinstance(value, bool):
        return [str(value).lower()]
    if isinstance(value, str):
        return [value]
    if isinstance(value, (dict, list)):
        return [json.dumps(value, ensure_ascii=False)]
    return [str(value)]


def fill_from_detail(
    metadata: dict[str, Any], payload: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Return (new metadata, injected search keys); inputs are never mutated."""
    filled = dict(metadata)
    injected: list[str] = []
    for search_key, detail_key in DETAIL_TO_SEARCH.items():
        if not _is_empty(metadata.get(search_key)):
            continue
        wrapped = _to_search_shape(search_key, payload.get(detail_key))
        if wrapped is None:
            continue
        filled[search_key] = wrapped
        injected.append(search_key)
    return filled, injected
