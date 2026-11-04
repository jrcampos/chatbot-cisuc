"""Parsing of euro amounts as written in EU documents."""

from __future__ import annotations

import re

# A number is either grouped (`4 000 000`, `1.000.000`, `2,500,000`, `1. 000. 000` — PDF text
# sometimes puts a space after the separator — optionally with a decimal part) or plain (`20.00`, `1,5`, `634`), and must not run into following
# digits (a page number or a year on the next token).
NUM = (
    r"(?:\d{1,3}(?:[ \u00a0.,][ ]?\d{3})+(?:[.,]\d+)?(?!\s?%)|\d+(?:[.,]\d+)?)(?!\d)"
)
# ONE multiplier vocabulary (word-bounded, case-insensitive wherever the pattern is compiled with re.I).
_MULT = r"million|mio|mln|billion|bn|mld|m"
# Regex SOURCE with one NAMED capturing group `eur`: "EUR <amount> [multiplier]".
EUR_AMOUNT = rf"(?P<eur>(?:EUR|€)\s*{NUM}(?:\s*(?:{_MULT})\b)?)"

# Sanity bounds for any budget amount: below EUR 1,000 is a stray number (a page, a rate, a
# column), above EUR 100 billion is a neighbouring column glued on by the PDF conversion.
MIN_EUR = 1_000.0
MAX_EUR = 1e11

_EUR_RE = re.compile(rf"(?:EUR|€)\s*(?P<num>{NUM})(?:\s*(?P<mult>{_MULT})\b)?", re.IGNORECASE)
_MULTIPLIER = {
    "million": 1e6, "mio": 1e6, "mln": 1e6, "m": 1e6,
    "billion": 1e9, "bn": 1e9, "mld": 1e9,
}


def _to_number(raw: str, has_multiplier: bool) -> float | None:
    text = raw.replace("\u00a0", " ").strip(" .,\t\n")
    if has_multiplier:  # "20.00 million", "1,5 million": . or , is a decimal mark
        text = re.sub(r"\s+", "", text).replace(",", ".")
        if text.count(".") > 1:
            head, _, tail = text.rpartition(".")
            text = head.replace(".", "") + "." + tail
        try:
            return float(text)
        except ValueError:
            return None
    text = re.sub(r"\s+", "", text)
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d{1,2}", text):  # 500,000.00
        return float(text.replace(",", ""))
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", text):  # 1.500.000,50
        return float(text.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", text):  # 1.000.000 / 2,500,000
        return float(re.sub(r"[.,]", "", text))
    if re.fullmatch(r"\d+[.,]\d{1,2}", text):  # 6.50 / 6,5
        return float(text.replace(",", "."))
    return float(text) if text.isdigit() else None


def parse_eur(text: str) -> float | None:
    """First euro amount in `text` as a float number of euros, or None (also when outside the sanity bounds)."""
    match = _EUR_RE.search(text or "")
    if not match:
        return None
    multiplier = (match.group("mult") or "").lower()
    value = _to_number(match.group("num"), bool(multiplier))
    if value is None:
        return None
    value *= _MULTIPLIER.get(multiplier, 1.0)
    return value if MIN_EUR <= value <= MAX_EUR else None
