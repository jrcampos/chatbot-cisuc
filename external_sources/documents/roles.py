"""Link discovery and role classification for call documents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from external_sources.documents.policy import DocumentPolicy
from external_sources.plugins.base import NormalizedRecord

# Identifiers shorter than this never count as "the URL names this call": a name
# like "A" or "X" is a substring of almost every URL.
MIN_IDENTIFIER_CHARS = 6


# Scraped link fields contain junk such as " https:", ": https://: h" or "ttp:".
_WELL_FORMED_URL = re.compile(r"^https?://[^\s/]+\.[^\s/]+", re.IGNORECASE)


@dataclass(frozen=True)
class DocLink:
    """A document link found on a record."""

    url: str
    text: str
    source_field: str


def discover_links(record: NormalizedRecord, policy: DocumentPolicy) -> list[DocLink]:
    """Collect de-duplicated http(s) links from the policy's `details.*` link fields."""
    seen: set[str] = set()
    links: list[DocLink] = []
    for field_name in policy.link_fields:
        entries = record.details.get(field_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            if not _WELL_FORMED_URL.match(url) or re.search(r"\s", url) or url in seen:
                continue
            seen.add(url)
            links.append(DocLink(url, str(entry.get("text") or "").strip(), field_name))
    return links


def _names_call(low_url: str, name: str) -> bool:
    """True when `name` appears in the URL as a whole token (not inside another word)."""
    if len(name) < MIN_IDENTIFIER_CHARS:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])", low_url) is not None


def classify_url(
    url: str,
    identifier: str,
    call_identifier: str,
    policy: DocumentPolicy,
    text: str = "",
    profile: Any = None,
) -> str:
    """
    Role of a linked document: "ignore", "call_specific", "boilerplate",
    "programme_level" or "other". Order matters: policy ignore patterns; the
    programme profile's classify rules (first match wins); a URL naming the call
    or a call-fiche marker; boilerplate URL patterns (so a "Call document" link
    under /common/ or op.europa.eu stays boilerplate); the generic call-specific
    link-text patterns; programme-level URL patterns; otherwise "other".
    """
    low = url.lower()
    if any(pattern in low for pattern in policy.ignore_url_patterns):
        return "ignore"
    for rule in getattr(profile, "classify", ()):
        if rule.matches(url, text):
            return rule.role
    if any(_names_call(low, name) for name in (identifier, call_identifier) if name) or any(
        marker in low for marker in policy.call_specific_markers
    ):
        return "call_specific"
    if any(pattern in low for pattern in policy.boilerplate_url_patterns):
        return "boilerplate"
    if any(re.search(p, text or "", re.IGNORECASE) for p in policy.call_specific_text_patterns):
        return "call_specific"
    if any(pattern in low for pattern in policy.programme_level_url_patterns):
        return "programme_level"
    return "other"
