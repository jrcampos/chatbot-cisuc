"""Transforms for the F&T cascade-funding spec (config/ft_cascade_fields.yaml)."""

from __future__ import annotations

from typing import Any

from external_sources.normalization.transforms import transform


@transform("ft_cascade_key")
def ft_cascade_key(value: Any, ctx: dict[str, Any]) -> str | None:
    """The portal's cascade-call id (ccm2Id) as a natural key: '46786367' -> 'FT-CASCADE-46786367'."""
    text = str(value).strip() if value is not None else ""
    return f"FT-CASCADE-{text}" if text else None


@transform("number")
def number(value: Any, ctx: dict[str, Any]) -> float | None:
    """A numeric string ('2000000', '91800.50') as a float; None when empty or not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return None
