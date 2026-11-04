"""Document policy: which links are documents, their roles, and what to fetch."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_POLICY_PATH = Path(__file__).parent.parent / "config" / "document_policy.yaml"
_LIST_KEYS = (
    "link_fields", "ignore_url_patterns", "call_specific_markers",
    "boilerplate_url_patterns", "programme_level_url_patterns", "fetch_roles",
)


@dataclass(frozen=True)
class DocumentPolicy:
    """Loaded document policy (see config/document_policy.yaml)."""

    link_fields: tuple[str, ...]
    ignore_url_patterns: tuple[str, ...]
    call_specific_markers: tuple[str, ...]
    boilerplate_url_patterns: tuple[str, ...]
    programme_level_url_patterns: tuple[str, ...]
    fetch_roles: tuple[str, ...]
    max_bytes: int
    call_specific_text_patterns: tuple[str, ...] = ()
    skip_url_patterns: tuple[str, ...] = ()
    follow_download_text_patterns: tuple[str, ...] = ()


def load_policy(path: str | Path = DEFAULT_POLICY_PATH) -> DocumentPolicy:
    """Load and validate the policy YAML; raises ValueError when malformed."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    values: dict[str, tuple[str, ...]] = {}
    for key in _LIST_KEYS:
        raw = data.get(key)
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError(f"{path}: {key!r} must be a list of strings")
        values[key] = tuple(raw)
    optional: dict[str, tuple[str, ...]] = {}
    for key in ("call_specific_text_patterns", "skip_url_patterns", "follow_download_text_patterns"):
        patterns = data.get(key, [])
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ValueError(f"{path}: {key!r} must be a list of strings")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"{path}: invalid regex {pattern!r}: {exc}") from exc
        optional[key] = tuple(patterns)
    max_bytes = data.get("max_bytes")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError(f"{path}: 'max_bytes' must be a positive integer")
    return DocumentPolicy(max_bytes=max_bytes, **optional, **values)
