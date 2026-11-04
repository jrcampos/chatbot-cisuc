"""
Slicing of converted documents.

`split_sections` cuts by Markdown headings. `find_topic_slice` finds the part of a
SHARED document (e.g. a Horizon work programme) that belongs to one call by the
call's own identifier: every occurrence of a heading `ID: title` is a candidate
(table-of-contents entry, summary-table row, real section) and each candidate runs
to the next topic heading — the real section is by far the longest, so the longest
candidate wins. This needs no per-programme code, only that topics are introduced
by their identifier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_NEXT_TOPIC = re.compile(r"^(?:#{1,6}\s*)?[A-Z][A-Z0-9]*(?:-[A-Za-z0-9]+){2,}\s*:\s+\S", re.MULTILINE)
_PAGE_MARKER = re.compile(r"^<!-- page \d+ -->\s*$", re.MULTILINE)
_FOOTNOTE_LINE = re.compile(r"^\[\^\d+\]:[^\n]*$", re.MULTILINE)


@dataclass
class Section:
    """A heading and the text under it (up to the next heading)."""

    heading: str
    level: int
    start: int
    end: int
    text: str


def split_sections(markdown: str) -> list[Section]:
    """Split Markdown into sections by heading."""
    matches = list(_HEADING.finditer(markdown))
    sections: list[Section] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[match.end():end].strip("\n")
        sections.append(
            Section(match.group(2), len(match.group(1)), match.start(), end, body.strip())
        )
    return sections


def find_topic_slice(
    markdown: str, identifier: str, max_chars: int = 60_000
) -> tuple[int, int] | None:
    """(start, end) of the longest section introduced by `identifier: title`, or None."""
    start_re = re.compile(
        rf"^(?:#{{1,6}}\s*)?{re.escape(identifier)}\s*:\s+\S", re.MULTILINE | re.IGNORECASE
    )
    best: tuple[int, int] | None = None
    for match in start_re.finditer(markdown):
        following = _NEXT_TOPIC.search(markdown, match.end())
        end = min(following.start() if following else len(markdown), match.start() + max_chars)
        if best is None or end - match.start() > best[1] - best[0]:
            best = (match.start(), end)
    return best


def clean_text(markdown: str) -> str:
    """Strip page markers, footnote lines (`[^n]: ...`) and heading hashes; collapse blank lines."""
    text = _FOOTNOTE_LINE.sub("", _PAGE_MARKER.sub("", markdown))
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
