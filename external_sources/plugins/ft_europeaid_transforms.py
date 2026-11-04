"""Transforms for the F&T external-action spec (config/ft_europeaid_fields.yaml)."""

from __future__ import annotations

import json
import re
from typing import Any

from external_sources.normalization.transforms import transform


@transform("ft_publication_documents")
def ft_publication_documents(value: Any, ctx: dict[str, Any]) -> list[dict[str, Any]] | None:
    """
    The JSON string of published documents as `[{text, url, type, language, published_at}]`
    (`text`/`url` so the document pipeline discovers them like any other link list).
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return None
    if not isinstance(value, list):
        return None
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not str(item.get("docUrl") or "").strip():
            continue
        published = str(item.get("finalPublicDocDate") or "").strip()
        match = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", published)
        out.append({
            "text": str(item.get("nameDoc") or "").strip(),
            "url": str(item["docUrl"]).strip(),
            "type": str(item.get("typeDoc") or "").strip().lower() or None,
            "language": str(item.get("languageDoc") or "").strip() or None,
            # The portal gives a Brussels local time without zone; kept as written.
            "published_at": f"{match.group(1)}T{match.group(2)}" if match else (published or None),
        })
    return out or None


@transform("ft_strip_label")
def ft_strip_label(value: Any, ctx: dict[str, Any]) -> str | None:
    """Text without a leading ':' separator ('  :  Ministry of ...' -> 'Ministry of ...')."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip().lstrip(":").strip()
    return text or None
