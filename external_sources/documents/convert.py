"""
Convert fetched documents to Markdown.

PDFs go through the system `pdftotext -layout` (called with a list, never a
shell). Layout mode keeps table rows readable but interleaves table columns, so
treat table-derived text as evidence to verify, not as clean prose. Numbered
headings, topic headings (`ID: title`) and page breaks are turned into Markdown
so later stages can slice by heading and cite a page. Page furniture is removed per
page: running headers/footers (a line repeated at the top or bottom of at least 40% of
the pages, and of 3 or more), bare page numbers, and footnotes at the page tail, which
are kept as `[^n]: text` lines at the end of their page (`clean_text` drops them from
extracted prose; the stored document keeps them). Word documents (.docx) are read with
the standard library (zip + XML): paragraphs, heading styles and table rows. A better
converter can be swapped in by replacing `pdf_to_text`/`text_to_markdown`; callers only
use `convert()`.
"""

from __future__ import annotations

import io
import math
import re
import subprocess
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup
from lxml import etree

from external_sources.normalization.transforms import html_to_text

_HEADING = re.compile(r"^\s{0,4}(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+([A-Z][^\n]{2,90}?)\s*$")
_TOPIC = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Za-z0-9]+){2,}\s*:\s+\S")
_DOT_LEADER = re.compile(r"\.{4,}\s*\d*\s*$")
_PAGE_NUMBER = re.compile(r"^\s*(?:page\s+)?\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?\s*$", re.IGNORECASE)
_FOOTNOTE_MARK = re.compile(r"^\s*(\d{1,3})\s*$")
_HEADER_ZONE = 3          # non-empty lines at the top/bottom of a page checked for running headers
_RUNNING_SHARE = 0.4      # ... repeated on at least this share of pages (and on >= 3 pages)
_FOOTNOTE_ZONE = 25       # footnotes are looked for in the last N lines of a page
_FOOTNOTE_MAX_LINES = 4   # text lines one footnote may span
_DOCX_MAX_BYTES = 50_000_000
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class UnsupportedContentType(ValueError):
    """Raised for content types the pipeline does not convert."""


def pdf_to_text(body: bytes) -> str:
    """PDF bytes -> layout-preserving text using `pdftotext` (poppler-utils)."""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "in.pdf"
        pdf.write_bytes(body)
        completed = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            capture_output=True, check=True, timeout=300,
        )
    return completed.stdout.decode("utf-8", errors="replace")


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def _running_lines(pages: list[list[str]]) -> set[str]:
    """Lines repeated at the top or bottom of many pages (running headers and footers)."""
    counts: Counter[str] = Counter()
    filled = 0
    for lines in pages:
        nonempty = [_norm(line) for line in lines if line.strip()]
        if not nonempty:
            continue
        filled += 1
        zone = set(nonempty[:_HEADER_ZONE] + nonempty[-_HEADER_ZONE:])
        counts.update(z for z in zone if not _PAGE_NUMBER.match(z))
    threshold = max(3, math.ceil(_RUNNING_SHARE * filled))
    return {line for line, count in counts.items() if count >= threshold}


def _strip_furniture(lines: list[str], running: set[str]) -> list[str]:
    """Drop running headers/footers (in the page's top/bottom zone) and bare page numbers."""
    nonempty = [i for i, line in enumerate(lines) if line.strip()]
    zone = set(nonempty[:_HEADER_ZONE] + nonempty[-_HEADER_ZONE:])
    kept = [line for i, line in enumerate(lines) if not (i in zone and _norm(line) in running)]
    for edge in (-1, 0):
        filled = [i for i, line in enumerate(kept) if line.strip()]
        if filled and _PAGE_NUMBER.match(kept[filled[edge]]):
            del kept[filled[edge]]
    return kept


def _footnote_zone(lines: list[str]) -> list[tuple[int, str]] | None:
    """Parse `lines` as footnotes (`n` alone, then 1..4 text lines; n ascending), else None."""
    notes: list[tuple[int, list[str]]] = []
    for line in lines:
        if not line.strip():
            continue
        mark = _FOOTNOTE_MARK.match(line)
        if mark:
            number = int(mark.group(1))
            if notes and (number <= notes[-1][0] or not notes[-1][1]):
                return None
            notes.append((number, []))
        elif notes and len(notes[-1][1]) < _FOOTNOTE_MAX_LINES:
            notes[-1][1].append(_norm(line))
        else:
            return None
    if not notes or not all(text for _, text in notes):
        return None
    return [(number, " ".join(text)) for number, text in notes]


def _split_footnotes(lines: list[str]) -> tuple[list[str], list[str]]:
    """(body lines, `[^n]: text` lines) — footnotes start after a blank line near the page tail."""
    for i in range(max(0, len(lines) - _FOOTNOTE_ZONE), len(lines) - 1):
        if not _FOOTNOTE_MARK.match(lines[i]) or (i > 0 and lines[i - 1].strip()):
            continue
        if not lines[i + 1].strip() or _FOOTNOTE_MARK.match(lines[i + 1]):
            continue
        notes = _footnote_zone(lines[i:])
        if notes:
            return lines[:i], [f"[^{n}]: {text}" for n, text in notes]
    return lines, []


def text_to_markdown(text: str) -> str:
    """Add Markdown headings and page markers to layout text; strip page furniture."""
    pages = [page.splitlines() for page in text.split("\f")]
    running = _running_lines(pages)
    out: list[str] = []
    for number, raw_lines in enumerate(pages, start=1):
        if not any(line.strip() for line in raw_lines):
            continue
        body, footnotes = _split_footnotes(_strip_furniture(raw_lines, running))
        out.append(f"<!-- page {number} -->")
        for line in body:
            line = re.sub(r"[ \t]{2,}", " ", line).rstrip()
            stripped = line.strip()
            if _DOT_LEADER.search(line):
                out.append(stripped)
                continue
            match = _HEADING.match(line)
            if match and not stripped.endswith((".", ",", ";", ":")):
                out.append(f"## {match.group(1)}. {match.group(2)}")
            elif _TOPIC.match(stripped):
                out.append(f"### {stripped}")
            else:
                out.append(stripped)
        if footnotes:
            out += ["", *footnotes]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def _docx_xml(body: bytes) -> bytes | None:
    """`word/document.xml` of a .docx, or None when `body` is not a Word document."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > _DOCX_MAX_BYTES:
                raise UnsupportedContentType(f"docx document.xml too large: {info.file_size} bytes")
            return archive.read(info)
    except (zipfile.BadZipFile, KeyError):
        return None


def docx_to_markdown(document_xml: bytes) -> str:
    """WordprocessingML -> Markdown: heading styles become '#', table rows 'a | b'."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(document_xml, parser)
    body = root.find(f"{_W}body")
    out: list[str] = []

    def paragraph_text(p: etree._Element) -> str:
        return _norm("".join(t.text or "" for t in p.iter(f"{_W}t")))

    for child in body if body is not None else []:
        if child.tag == f"{_W}p":
            text = paragraph_text(child)
            if not text:
                continue
            style = child.find(f"{_W}pPr/{_W}pStyle")
            name = (style.get(f"{_W}val") or "") if style is not None else ""
            level = re.match(r"(?i)heading\s*(\d)", name)
            if level:
                out.append(f"{'#' * min(int(level.group(1)), 6)} {text}")
            elif name.lower() == "title":
                out.append(f"# {text}")
            else:
                out.append(text)
            out.append("")
        elif child.tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells = [" ".join(paragraph_text(p) for p in tc.iter(f"{_W}p")).strip()
                         for tc in row.iter(f"{_W}tc")]
                if any(cells):
                    out.append(" | ".join(cells))
            out.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def html_to_markdown(html: str) -> str:
    """HTML -> readable text; h1..h6 become Markdown headings so sections can be sliced."""
    soup = BeautifulSoup(html, "html.parser")
    for level in range(1, 7):
        for heading in soup.find_all(f"h{level}"):
            text = " ".join(heading.get_text(" ", strip=True).split())
            if text:
                heading.string = f"{'#' * level} {text}"
    return (html_to_text(str(soup), {}) or "") + "\n"


def convert(body: bytes, content_type: str | None, url: str) -> str:
    """Convert a fetched body to Markdown; raises UnsupportedContentType otherwise."""
    kind = (content_type or "").lower()
    if body.startswith(b"%PDF") or "pdf" in kind or url.lower().split("?")[0].endswith(".pdf"):
        return text_to_markdown(pdf_to_text(body))
    if body.startswith(b"PK"):
        document_xml = _docx_xml(body)
        if document_xml is not None:
            return docx_to_markdown(document_xml)
        raise UnsupportedContentType(f"unsupported content type: {content_type or 'zip archive'}")
    if "html" in kind or body.lstrip()[:1] == b"<":
        return html_to_markdown(body.decode("utf-8", errors="replace"))
    raise UnsupportedContentType(f"unsupported content type: {content_type or 'unknown'}")
