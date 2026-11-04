"""
Parse the submission deadlines written in call documents ("16 February 2027 – 17:00:00
CET (Brussels)"). Times are Brussels local time and are returned as ISO-8601 UTC; a
date without a time is returned as a plain `YYYY-MM-DD`.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

_MONTHS = {
    name: i for i, names in enumerate(
        ("jan january", "feb february", "mar march", "apr april", "may", "jun june",
         "jul july", "aug august", "sep sept september", "oct october", "nov november",
         "dec december"), start=1,
    ) for name in names.split()
}
_DEADLINE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3,9})\.?\s+(?P<year>\d{4})"
    r"(?:\s*(?:[\u2013\u2014,-]|at)?\s*(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?"
    r"(?:\s*(?P<ap>[ap])\.?m\b\.?)?)?",
    re.IGNORECASE,
)
_BRUSSELS = ZoneInfo("Europe/Brussels")
_UTC = ZoneInfo("UTC")


def parse_deadline(text: str) -> str | None:
    """ISO UTC datetime (with time) or `YYYY-MM-DD` (without) from `text`; None if absent/invalid."""
    if not isinstance(text, str):
        return None
    for match in _DEADLINE.finditer(text):
        month = _MONTHS.get(match.group("month").lower())
        if month is None:
            continue
        try:
            day, year = int(match.group("day")), int(match.group("year"))
            if match.group("h") is None:
                return datetime(year, month, day).date().isoformat()
            hour = int(match.group("h"))
            meridiem = (match.group("ap") or "").lower()
            if meridiem and not 1 <= hour <= 12:
                continue
            if meridiem == "p" and hour < 12:
                hour += 12
            elif meridiem == "a" and hour == 12:
                hour = 0
            local = datetime(
                year, month, day, hour, int(match.group("m")),
                int(match.group("s") or 0), tzinfo=_BRUSSELS,
            )
            return local.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        except ValueError:
            continue
    return None


def calendar_date(value: str | None) -> date | None:
    """
    The Europe/Brussels calendar date of an ISO value: an instant (`...T16:00:00+00:00`) is
    converted to Brussels time first; a plain `YYYY-MM-DD` is taken as is. None if unparseable.

    Used to compare deadlines across layers: the portal API often stores the date at midnight
    UTC while call documents state "17:00:00 CET (Brussels)" — same day, different instant.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "T" not in text and " " not in text:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(_BRUSSELS).date()
