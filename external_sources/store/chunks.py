"""
Section-aware chunking of stored documents (Markdown from documents/convert.py).

Chunks follow the heading tree first (sections, not fixed-size windows); a section longer than
`max_chars` is split on paragraph
boundaries, and a paragraph longer than that on sentence boundaries. Each chunk keeps its heading
and the PDF page range read from the `<!-- page N -->` markers, so every passage stays citable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from external_sources.store.rows import sha256_text

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_PAGE = re.compile(r"^<!--\s*page\s+(\d+)\s*-->\s*$")
_FOOTNOTE = re.compile(r"^\[\^\d+\]:")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

MAX_CHARS = 2000
MIN_CHARS = 200
MAX_CHUNKS = 5000  # per document version; a 1M-character work programme gives ~600


@dataclass
class Chunk:
    chunk_no: int
    heading: str | None
    page_start: int | None
    page_end: int | None
    text: str

    @property
    def text_sha256(self) -> str:
        return sha256_text(self.text)


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(text):
        while len(sentence) > limit:  # no sentence boundary at all: hard cut
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def chunk_markdown(markdown: str, max_chars: int = MAX_CHARS, min_chars: int = MIN_CHARS) -> list[Chunk]:
    """Split a stored document into citable chunks (heading + page range + text)."""
    blocks: list[tuple[str | None, int | None, str]] = []  # (heading, page, paragraph)
    heading: str | None = None
    page: int | None = None
    paragraph: list[str] = []

    def flush() -> None:
        text = re.sub(r"[ \t]+", " ", " ".join(paragraph)).strip()
        if text:
            blocks.append((heading, page, text))
        paragraph.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        page_match = _PAGE.match(stripped)
        if page_match:
            flush()
            page = int(page_match.group(1))
            continue
        heading_match = _HEADING.match(stripped)
        if heading_match:
            flush()
            heading = heading_match.group(2)[:300]
            continue
        if not stripped:
            flush()
            continue
        if _FOOTNOTE.match(stripped):
            continue
        paragraph.append(stripped)
    flush()

    chunks: list[Chunk] = []
    buffer: list[str] = []
    buffer_heading: str | None = None
    first_page: int | None = None
    last_page: int | None = None

    def emit() -> None:
        nonlocal buffer, first_page, last_page
        text = "\n\n".join(buffer).strip()
        if text and len(chunks) < MAX_CHUNKS:
            chunks.append(Chunk(len(chunks), buffer_heading, first_page, last_page, text))
        buffer = []
        first_page = last_page = None

    for block_heading, block_page, text in blocks:
        new_section = block_heading != buffer_heading
        size = sum(len(b) + 2 for b in buffer)
        if buffer and (new_section and size >= min_chars or size + len(text) > max_chars):
            emit()
        if not buffer:
            buffer_heading = block_heading
        for piece in _split_long(text, max_chars):
            if buffer and sum(len(b) + 2 for b in buffer) + len(piece) > max_chars:
                emit()
                buffer_heading = block_heading
            buffer.append(piece)
            first_page = first_page if first_page is not None else block_page
            last_page = block_page if block_page is not None else last_page
    emit()
    return chunks
