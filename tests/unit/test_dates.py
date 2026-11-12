"""Unit tests for external_sources.extraction.dates.parse_deadline."""

import pytest

from external_sources.extraction.dates import parse_deadline


@pytest.mark.parametrize("text,expected", [
    ("16 February 2027 – 17:00:00 CET (Brussels)", "2027-02-16T16:00:00+00:00"),
    ("15 July 2026 – 17:00:00 CEST", "2026-07-15T15:00:00+00:00"),
    ("5 Feb 2026 - 17:00 CET", "2026-02-05T16:00:00+00:00"),
    ("15 December 2026, 17:00:00 Brussels time", "2026-12-15T16:00:00+00:00"),
    ("1 SEPTEMBER 2026 at 12:30", "2026-09-01T10:30:00+00:00"),
    ("28 October 2026", "2026-10-28"),
    ("3 Mar 2027", "2027-03-03"),
])
def test_parse_deadline_variants(text, expected):
    """Brussels local times become UTC; no time keeps the plain date"""
    assert parse_deadline(text) == expected


@pytest.mark.parametrize("text", ["", "soon", "TBD", "31 February 2026", "17:00:00 CET", "12 Foo 2026"])
def test_parse_deadline_garbage_is_none(text):
    """Unparseable or invalid input yields None without raising"""
    assert parse_deadline(text) is None


def test_parse_deadline_non_string_is_none():
    """Never raises on odd input"""
    assert parse_deadline(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("text,expected", [
    ("16 February 2027 17:00", "2027-02-16T16:00:00+00:00"),
    ("16 February 2027 5:00 pm", "2027-02-16T16:00:00+00:00"),
    ("16 February 2027 - 12:00 am", "2027-02-15T23:00:00+00:00"),
    ("16 February 2027 at 12:00 PM", "2027-02-16T11:00:00+00:00"),
    ("31 February 2026 then 28 October 2026", "2026-10-28"),
])
def test_parse_deadline_review_fixes(text, expected):
    """am/pm honoured, time glued to the year accepted, invalid date falls through to the next"""
    assert parse_deadline(text) == expected


def test_calendar_date_is_the_brussels_date():
    """Instants map to their Europe/Brussels calendar date; date-only strings stay as they are"""
    from datetime import date

    from external_sources.extraction.dates import calendar_date

    assert calendar_date("2026-11-05T00:00:00+00:00") == date(2026, 11, 5)
    assert calendar_date("2026-11-03T16:00:00+00:00") == date(2026, 11, 3)
    assert calendar_date("2025-05-26T22:00:00+00:00") == date(2025, 5, 27)
    assert calendar_date("2026-11-03") == date(2026, 11, 3)
    assert calendar_date("2026-11-03T16:00:00Z") == date(2026, 11, 3)
    assert calendar_date("nonsense") is None and calendar_date(None) is None
