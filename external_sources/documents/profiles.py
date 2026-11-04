"""
Programme profiles: how each EU programme documents its calls.

Loaded from config/programme_profiles.yaml and validated against
config/eu_programmes.yaml (every programme needs a profile, and vice versa).
A profile carries the document-role classify rules, URL skip patterns, and which
extraction families read each document role.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

CONFIG_DIR = Path(__file__).parent.parent / "config"
DEFAULT_PROFILES_PATH = CONFIG_DIR / "programme_profiles.yaml"
DEFAULT_EU_PROGRAMMES = CONFIG_DIR / "eu_programmes.yaml"

FAMILY_ROLES = ("call_specific", "programme_level")
RULE_ROLES = ("call_specific", "programme_level", "boilerplate", "other", "ignore")
_PROFILE_KEYS = {"extractor", "families", "classify", "skip", "expect"}
_RULE_KEYS = {"role", "url", "text", "host"}


@dataclass(frozen=True)
class ClassifyRule:
    """One classify rule: matches when ALL given conditions match."""

    role: str
    url: re.Pattern[str] | None = None
    text: re.Pattern[str] | None = None
    host: re.Pattern[str] | None = None

    def matches(self, url: str, text: str = "") -> bool:
        """True when every given condition matches the link."""
        if self.url is not None and not self.url.search(url):
            return False
        if self.text is not None and not self.text.search(text or ""):
            return False
        if self.host is not None and not self.host.search(urlparse(url).netloc):
            return False
        return True


@dataclass(frozen=True)
class Profile:
    """Document handling for one programme."""

    key: str
    label: str
    extractor: str
    families: dict[str, tuple[str, ...]]
    classify: tuple[ClassifyRule, ...] = ()
    skip: tuple[re.Pattern[str], ...] = ()
    expect: dict[str, Any] = field(default_factory=dict)

    def families_for(self, role: str) -> tuple[str, ...]:
        """Family names that read documents of `role` (empty when none)."""
        return self.families.get(role, ())

    def is_skipped(self, url: str) -> bool:
        """True when `url` matches a skip pattern (never fetched)."""
        return any(p.search(url) for p in self.skip)


class ProfileRegistry:
    """Profiles addressable by programme label or key."""

    def __init__(self, profiles: list[Profile]) -> None:
        self.profiles = list(profiles)
        self._index: dict[str, Profile] = {}
        for p in self.profiles:
            self._index[p.key.lower()] = p
            self._index[p.label.lower()] = p

    def for_programme(self, name: str | None) -> Profile | None:
        """Profile for a programme label (eu_programmes.yaml) or key, else None."""
        return self._index.get((name or "").strip().lower())

    def __iter__(self):
        return iter(self.profiles)


def _regex(pattern: Any, where: str) -> re.Pattern[str]:
    if not isinstance(pattern, str):
        raise ValueError(f"{where}: regex must be a string, got {pattern!r}")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{where}: invalid regex {pattern!r}: {exc}") from exc


def _families(raw: Any, where: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: 'families' must be a mapping")
    out: dict[str, tuple[str, ...]] = {}
    for role, names in raw.items():
        if role not in FAMILY_ROLES:
            raise ValueError(f"{where}: family role {role!r} must be one of {FAMILY_ROLES}")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError(f"{where}: families.{role} must be a list of strings")
        out[role] = tuple(names)
    return out


def _rule(raw: Any, where: str) -> ClassifyRule:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: classify rule must be a mapping")
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown classify rule keys {sorted(unknown)}")
    if raw.get("role") not in RULE_ROLES:
        raise ValueError(f"{where}: rule role {raw.get('role')!r} must be one of {RULE_ROLES}")
    if not any(k in raw for k in ("url", "text", "host")):
        raise ValueError(f"{where}: rule needs at least one of url/text/host")
    return ClassifyRule(
        role=raw["role"],
        url=_regex(raw["url"], where) if "url" in raw else None,
        text=re.compile(_regex(raw["text"], where).pattern, re.IGNORECASE) if "text" in raw else None,
        host=_regex(raw["host"], where) if "host" in raw else None,
    )


def load_profiles(
    profiles_path: str | Path = DEFAULT_PROFILES_PATH,
    programmes_path: str | Path = DEFAULT_EU_PROGRAMMES,
) -> ProfileRegistry:
    """Load and validate the profiles; raises ValueError when malformed or incomplete."""
    with open(profiles_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    with open(programmes_path, encoding="utf-8") as f:
        programmes = (yaml.safe_load(f) or {}).get("programmes") or {}
    if not isinstance(data, dict):
        raise ValueError(f"{profiles_path}: top level must be a mapping")
    unknown_top = set(data) - {"defaults", "profiles"}
    if unknown_top:
        raise ValueError(f"{profiles_path}: unknown top-level keys {sorted(unknown_top)}")
    defaults = data.get("defaults") or {}
    if set(defaults) - {"families"}:
        raise ValueError(f"{profiles_path}: unknown defaults keys {sorted(set(defaults) - {'families'})}")
    default_families = _families(defaults.get("families") or {}, f"{profiles_path}: defaults")
    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise ValueError(f"{profiles_path}: 'profiles' must be a mapping")

    extra = sorted(set(raw_profiles) - set(programmes))
    if extra:
        raise ValueError(f"{profiles_path}: profiles for unknown programmes: {extra}")
    missing = sorted(set(programmes) - set(raw_profiles))
    if missing:
        raise ValueError(f"{profiles_path}: programmes without a profile: {missing}")

    profiles: list[Profile] = []
    for key, body in raw_profiles.items():
        where = f"{profiles_path}: profile {key!r}"
        if not isinstance(body, dict):
            raise ValueError(f"{where} must be a mapping")
        unknown = set(body) - _PROFILE_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
        if not isinstance(body.get("extractor"), str):
            raise ValueError(f"{where}: 'extractor' must be a string")
        families = {**default_families, **_families(body.get("families") or {}, where)}
        classify = tuple(_rule(r, where) for r in body.get("classify") or [])
        skip_raw = body.get("skip") or []
        if not isinstance(skip_raw, list):
            raise ValueError(f"{where}: 'skip' must be a list of regexes")
        expect = body.get("expect") or {}
        if not isinstance(expect, dict):
            raise ValueError(f"{where}: 'expect' must be a mapping")
        profiles.append(
            Profile(
                key=key, label=str(programmes[key].get("label", key)), extractor=body["extractor"],
                families=families, classify=classify,
                skip=tuple(_regex(p, where) for p in skip_raw), expect=dict(expect),
            )
        )
    return ProfileRegistry(profiles)
