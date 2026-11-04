"""
Reusable value transforms for declarative field specs.

A transform is a pure function `fn(value, ctx, **kwargs)`; `ctx` is a plain dict
(`record_type`, `natural_key`, `now`, ...). Field specs chain transforms by
name (see spec.py). The chain stops with None as soon as a value becomes empty,
so transforms never need to handle None/""/[]/{} themselves.

Plugins register source-specific transforms with the same `@transform`
decorator (e.g. plugins/ft_portal_transforms.py).
"""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

Transform = Callable[..., Any]
_REGISTRY: dict[str, Transform] = {}
_DATE_PRODUCERS: set[str] = set()

_BLOCK_TAGS = [
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "table", "td", "th", "section", "blockquote",
]
_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def transform(name: str, produces_date: bool = False) -> Callable[[Transform], Transform]:
    """
    Register `fn` under `name`. Raises ValueError if the name is taken.

    `produces_date=True` flags a transform whose output is ISO-8601 UTC date
    string(s): `load_spec` requires such a step for every date target.
    """

    def decorator(fn: Transform) -> Transform:
        if name in _REGISTRY:
            raise ValueError(f"Transform {name!r} is already registered")
        _REGISTRY[name] = fn
        if produces_date:
            _DATE_PRODUCERS.add(name)
        return fn

    return decorator


def get_transform(name: str) -> Transform:
    """Return the transform registered under `name` (KeyError if unknown)."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"Unknown transform: {name!r}") from None


def registered_transforms() -> set[str]:
    """Names of every registered transform."""
    return set(_REGISTRY)


def is_date_producer(name: str) -> bool:
    """True when the transform `name` was registered with `produces_date=True`."""
    return name in _DATE_PRODUCERS


@transform("first")
def first(value: Any, ctx: dict[str, Any]) -> Any:
    """Unwrap a list to its first non-None element; pass scalars through."""
    if isinstance(value, (list, tuple)):
        for item in value:
            if item is not None:
                return item
        return None
    return value


@transform("strip")
def strip(value: Any, ctx: dict[str, Any]) -> str | None:
    """Collapse whitespace runs to single spaces and trim; blank -> None."""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


@transform("strip_suffix")
def strip_suffix(value: Any, ctx: dict[str, Any], suffix: str) -> Any:
    """Remove `suffix` from the end of a string when present."""
    if suffix and isinstance(value, str) and value.endswith(suffix):
        return value[: -len(suffix)]
    return value


@transform("html_to_text")
def html_to_text(value: Any, ctx: dict[str, Any]) -> str | None:
    """
    Convert an HTML fragment to readable plain text.

    Uses BeautifulSoup (like preprocessing/'s ContentExtractor) rather than a
    regex tag-stripper. Block elements become lines, list items get a "- "
    bullet, <br> becomes a newline, entities decode, NBSP/BOM/zero-width
    characters are removed. Links are NOT lost here — use `html_links` in a
    second spec rule to keep them as structured data.
    """
    if not isinstance(value, str):
        return value
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(value, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        # Only add "- " if li has non-whitespace text
        if li.get_text(strip=True):
            li.insert(0, "- ")
    for element in soup.find_all(_BLOCK_TAGS):
        # Don't add pre-newline if this block is a child of <li>
        # (the "- " from <li> already separates it)
        if element.parent and element.parent.name == "li":
            element.append("\n")
        else:
            element.insert_before("\n")
            element.append("\n")
    text = soup.get_text()
    text = text.replace("\xa0", " ").replace("\ufeff", "").replace("\u200b", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    cleaned = re.sub(r"\n\n+", "\n", "\n".join(lines)).strip()
    return cleaned or None


@transform("html_links")
def html_links(value: Any, ctx: dict[str, Any]) -> list[dict[str, str]] | None:
    """Extract de-duplicated http(s) links as [{"text", "url"}]."""
    if not isinstance(value, str):
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(value, "html.parser")
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        url = str(anchor["href"]).strip()
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        links.append({"text": re.sub(r"\s+", " ", anchor.get_text()).strip(), "url": url})
    return links or None


@transform("json_in_string")
def json_in_string(value: Any, ctx: dict[str, Any]) -> Any:
    """Decode JSON stored inside a string; already-parsed values pass through."""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def _iter_strings(value: Any):
    """Yield cleaned strings from nested lists, unpacking JSON-encoded lists."""
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)
        return
    text = str(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, list):
            yield from _iter_strings(parsed)
            return
    if text:
        yield re.sub(r"\s+", " ", text)


@transform("string_list")
def string_list(value: Any, ctx: dict[str, Any]) -> list[str] | None:
    """Normalise to a de-duplicated list of clean strings (or None)."""
    seen: set[str] = set()
    out: list[str] = []
    try:
        for text in _iter_strings(value):
            if text not in seen:
                seen.add(text)
                out.append(text)
    except RecursionError:
        return None
    return out or None


def _deep_clean(value: Any) -> Any:
    """Recursively clean strings; drop empty strings/None/empty containers."""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip() or None
    if isinstance(value, dict):
        cleaned = {k: c for k, v in value.items() if (c := _deep_clean(v)) is not None}
        return cleaned or None
    if isinstance(value, (list, tuple)):
        items = [c for v in value if (c := _deep_clean(v)) is not None]
        return items or None
    return value


@transform("deep_clean")
def deep_clean(value: Any, ctx: dict[str, Any]) -> Any:
    """Collapse whitespace in every nested string, dropping what becomes empty."""
    try:
        return _deep_clean(value)
    except RecursionError:
        return None


@transform("boolean")
def boolean(value: Any, ctx: dict[str, Any]) -> bool | None:
    """'true'/'false' (any case) -> bool; anything else -> None."""
    return {"true": True, "false": False}.get(str(value).strip().lower())


@transform("iso_datetime", produces_date=True)
def iso_datetime(value: Any, ctx: dict[str, Any]) -> str | None:
    """
    Normalise a date/datetime string to ISO-8601 UTC
    (`YYYY-MM-DDTHH:MM:SS+00:00`). Naive values and bare dates are treated as
    UTC. Unparseable input yields None, recorded as a gap on the record.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in _DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        try:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).replace(microsecond=0).isoformat()
        except (ValueError, OverflowError):
            return None
    return None


@transform("map")
def map_value(value: Any, ctx: dict[str, Any], mapping: dict[str, Any]) -> Any:
    """Map a source code to a label; unknown codes yield None (visible gap)."""
    return mapping.get(str(value).strip())


@transform("get")
def get_path(value: Any, ctx: dict[str, Any], path: str) -> Any:
    """Look up a dotted `path` in nested dicts; None when any step is missing."""
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


@transform("epoch_ms", produces_date=True)
def epoch_ms(value: Any, ctx: dict[str, Any]) -> str | None:
    """Epoch milliseconds (int or numeric string) -> ISO-8601 UTC, whole seconds."""
    if isinstance(value, bool):
        return None
    try:
        millis = int(str(value).strip())
        moment = datetime.fromtimestamp(millis / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return moment.replace(microsecond=0).isoformat()
