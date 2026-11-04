"""
Enrichment orchestrator: documents -> extraction -> provenance -> gaps -> quality.

Reads the records the extraction step wrote (`output/ft_portal.json`), reads every
document each call links to and applies the extracted values with these rules: an EMPTY
field is filled; an existing API value is replaced only when it is a repeated call total
(`api_is_call_total`) or the document is newer (`newer_document`), and the old value is
kept in `details.value_history`; any other disagreement stays as a `discrepancy` gap and
the API value is kept. Provenance and gaps are recorded on each record, records are classified
against the previous run, the enriched output and a quality report are written, and
alerts are sent. Nothing here drops a record.

Used by the store runner (`external_sources/store/runner.py`), which collects, enriches and loads
every source in one scheduled run.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from external_sources.documents.pipeline import CallDocument
from external_sources.extraction.fields import (
    Extraction,
    Family,
    extract_fields,
    match_topic_rows,
    merge_extractions,
)
from external_sources.plugins.base import NormalizedRecord
from external_sources.quality import (
    compute_gaps,
)
from external_sources.sources import build_sources
from external_sources.verification import Verifier, value_ranges

DEFAULT_OUTPUT_DIR = Path(__file__).parent / "output"
TRIED_SOURCES = ["api", "detail", "documents"]


def _iso_utc(http_date: str | None) -> str | None:
    """An HTTP date (Last-Modified) as ISO-8601 UTC, or None when absent/unparsable."""
    if not http_date:
        return None
    try:
        return parsedate_to_datetime(http_date).astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        return None


class Enricher:
    """Applies the document pipeline and extraction families to records."""

    def __init__(
        self,
        pipeline: Any,
        families: dict[str, Family],
        required_fields: list[str],
        required_any: dict[str, list[str]] | None = None,
        profiles: Any = None,
        verifier: Verifier | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.verifier = verifier
        self.families = families
        self.required_fields = required_fields
        self.required_any = required_any or {}
        self.profiles = profiles
        if profiles is not None:
            for profile in profiles:
                for names in profile.families.values():
                    unknown = [n for n in names if n not in families]
                    if unknown:
                        raise ValueError(
                            f"profile {profile.key!r} names unknown famil(ies) {unknown}; "
                            f"known: {sorted(families)}"
                        )

    def _families_for(self, record: NormalizedRecord, document: CallDocument) -> list[Family]:
        if self.profiles is None:
            return [f for f in self.families.values() if document.role in f.roles]
        profile = self.profiles.for_programme(record.programme)
        if profile is None:
            return [f for f in self.families.values() if document.role in f.roles]
        return [self.families[n] for n in profile.families_for(document.role)]

    def _topic_extractions(
        self,
        record: NormalizedRecord,
        siblings: list[NormalizedRecord],
        document: CallDocument,
        found: list[Extraction],
        published_at: str | None,
    ) -> list[Extraction]:
        """
        This topic's own row of the call document's budget table, as three extractions:
        `details.topic_budget_eur`, `details.topic_budget_match` (how it was matched) and a
        candidate for `funding_amount`. Call totals known for the call travel with the
        candidate so the merge can recognise an API value that is a call total repeated.
        """
        table = next((e for e in found if e.field == "details.topic_budget_table"), None)
        if table is None:
            return []
        rows = table.value
        matches = match_topic_rows(siblings, rows)
        match = matches.get(record.natural_key)
        if match is None:
            return []
        row = match["row"]
        call_budget = next((e.value for e in found if e.field == "details.call_budget_eur"), None)
        rows_total = sum(r["budget_eur"] for r in rows)
        totals = [call_budget, rows_total]
        api_values = [s.funding_amount for s in siblings if s.funding_amount]
        if (
            len(siblings) > 1 and len(api_values) == len(siblings)
            and len(matches) == len(siblings) == len(rows)
            and all(abs(v - api_values[0]) <= 0.01 * api_values[0] for v in api_values)
        ):
            shared = api_values[0]
            # Only a shared value near the table's own total (or the stated call budget) is
            # a call total; otherwise it is a plain discrepancy.
            if any(t and abs(shared - t) <= 0.05 * t for t in (rows_total, call_budget)):
                totals.append(shared)
        common = {
            "evidence": table.evidence,
            "doc_id": document.doc_id, "url": document.url, "section": table.section,
            "published_at": published_at,
        }
        return [
            Extraction("details.topic_budget_eur", row["budget_eur"], **common),
            Extraction(
                "details.topic_budget_match",
                {"how": match["how"], "label": row["label"], "topic_no": row["topic_no"]}, **common,
            ),
            Extraction(
                "funding_amount", row["budget_eur"], call_totals=tuple(t for t in totals if t), **common
            ),
        ]

    @staticmethod
    def _add_row_labels(record: NormalizedRecord, extractions: list[Extraction]) -> NormalizedRecord:
        """Name the matched table row in the evidence of the fields taken from it."""
        match = next((e.value for e in extractions if e.field == "details.topic_budget_match"), None)
        evidence = record.details.get("evidence")
        if not match or not evidence:
            return record
        updated = dict(evidence)
        for name in ("details.topic_budget_eur", "details.topic_budget_match", "funding_amount"):
            entry = updated.get(name)
            if isinstance(entry, dict) and record.field_sources.get(name, "").startswith("document:"):
                if any(e.field == name and e.doc_id == entry.get("doc_id") for e in extractions):
                    updated[name] = {**entry, "row": match["label"]}
        return record.model_copy(update={"details": {**record.details, "evidence": updated}})

    def _pool_families(self, record: NormalizedRecord, document: CallDocument) -> list[Family]:
        """Families allowed on a pool document: only those that anchor on the call's identifier."""
        return [f for f in self._families_for(record, document) if f.anchored or f.anchor or f.call_anchored]

    def enrich_record(
        self,
        record: NormalizedRecord,
        siblings: list[NormalizedRecord] | None = None,
        documents: list[CallDocument] | None = None,
        pool_documents: list[CallDocument] | None = None,
        tried: set[tuple[str, str]] | None = None,
    ) -> tuple[NormalizedRecord, list[CallDocument]]:
        """
        Read the call's documents, fill empty fields, and record what is still missing.
        `siblings` are all topics of the same call (used to match budget-table rows).
        `documents` are the record's own documents when already processed (default: process now).
        `pool_documents` are programme-level documents of the same programme that this call
        does not link; only anchoring families read them, and only for targets no own document
        yielded. `tried` caches (doc_id, identifier) pairs already attempted.
        The returned list is always the record's own documents.
        """
        siblings = siblings or [record]
        if documents is None:
            documents = self.pipeline.process(record)
        tried = tried if tried is not None else set()
        extractions: list[Extraction] = []
        anchor_missing = False
        documents_read = 0
        own_ids = {d.doc_id for d in documents if d.doc_id}

        def read(document: CallDocument, families: list[Family], count: bool) -> list[Extraction]:
            nonlocal anchor_missing, documents_read
            stored = self.pipeline.store.load_by_id(document.doc_id)
            if stored is None:
                return []
            if count:
                documents_read += 1
            published_at = _iso_utc(stored.meta.get("last_modified"))
            out: list[Extraction] = []
            for family in families:
                found, notes = extract_fields(
                    family, stored.markdown, record.natural_key, document.doc_id, document.url,
                    published_at, call_identifier=str(record.details.get("call_identifier") or "") or None,
                )
                out.extend(found)
                out.extend(self._topic_extractions(record, siblings, document, found, published_at))
                if count:
                    anchor_missing = anchor_missing or "anchor_not_found" in notes
            return out

        for document in documents:
            if document.doc_id is None:
                continue
            extractions.extend(read(document, self._families_for(record, document), True))

        yielded = {e.field for e in extractions}
        for document in pool_documents or []:
            if document.doc_id is None or document.doc_id in own_ids:
                continue
            key = (document.doc_id, record.natural_key)
            if key in tried:
                continue
            tried.add(key)
            families = self._pool_families(record, document)
            if not families:
                continue
            for extraction in read(document, families, False):
                if extraction.field not in yielded:
                    extractions.append(extraction)
            yielded |= {e.field for e in extractions}

        merged = merge_extractions(record, extractions)
        merged = self._add_row_labels(merged, extractions)
        if documents_read == 0:
            reason = "no_document_available"
        elif anchor_missing and not extractions:
            reason = "document_anchor_not_found"
        else:
            reason = "absent_in_documents"
        kept = [g for g in merged.data_gaps if g.get("kind") != "missing_required"]
        missing = compute_gaps(merged, self.required_fields, TRIED_SOURCES, reason, self.required_any)
        merged = merged.model_copy(update={"data_gaps": kept + missing})
        return self._annotate(merged, documents, pool_documents or []), documents

    def _annotate(
        self, record: NormalizedRecord, documents: list[CallDocument], pool_documents: list[CallDocument]
    ) -> NormalizedRecord:
        """
        Add `details.sources` (citable URLs), `details.verification` (per-field verdicts) and, when
        sources disagree on a number or date, `details.value_range[field]` = {min, max, primary,
        settled, status, values: [{value, layer, source, page?}]}.
        """
        if self.verifier is None:
            self.verifier = Verifier()
        details = record.details
        supplying = {
            str(e.get("doc_id")) for e in (details.get("evidence") or {}).values() if isinstance(e, dict)
        } | {
            str(c.get("doc_id")) for entries in (details.get("corroboration") or {}).values() for c in entries or []
        }
        own_ids = {d.doc_id for d in documents if d.doc_id}
        extra = [d for d in pool_documents if d.doc_id in supplying and d.doc_id not in own_ids]
        texts: dict[str, tuple[str, str]] = {}
        for document in [*documents, *extra]:
            if not document.doc_id or document.doc_id in texts:
                continue
            if document.role != "call_specific" and document.doc_id not in supplying \
                    and record.natural_key.lower() not in (document.url or "").lower():
                stored = self.pipeline.store.load_by_id(document.doc_id)
                if stored is None or record.natural_key not in stored.markdown:
                    continue
            else:
                stored = self.pipeline.store.load_by_id(document.doc_id)
            if stored is not None:
                texts[document.doc_id] = (document.url, stored.markdown)
        sources = build_sources(record, documents, self.pipeline.store, extra_documents=extra)
        annotated = record.model_copy(update={"details": {**details, "sources": sources}})
        verification = self.verifier.verify(annotated, texts)
        details = {**annotated.details, "verification": verification}
        details.pop("value_range", None)
        ranges = value_ranges(verification)
        if ranges:  # sources disagree: keep min..max and every value with its source, never one picked value
            details["value_range"] = ranges
        return annotated.model_copy(update={"details": details})

    def iter_enrich(
        self, records: list[NormalizedRecord], pool: list[NormalizedRecord] | None = None,
        skip: set[str] | None = None,
    ) -> Iterator[tuple[NormalizedRecord, NormalizedRecord | None, list[CallDocument], str | None]]:
        """
        Two-phase enrichment as a stream: yields `(record, enriched, own_documents, error)` in
        input order. `enriched` is None and `error` the message when the record failed.

        `pool` is every known record (defaults to `records`); topics of one call are grouped
        by `details.call_identifier` from the pool, so a sample run still sees all siblings of
        a call. Phase 1 processes every record's documents; phase 2 enriches each record with
        its own documents plus the programme-level documents of OTHER calls of its programme
        (identifiers are unique across work programmes, so anchoring on the topic id inside
        them is safe). Pool documents never appear in `own_documents`.

        `skip`: natural keys already enriched (a resumed run): their documents still feed the
        programme pool in phase 1, but phase 2 does not yield them again.
        """
        pool = records if pool is None else pool
        by_call: dict[str, list[NormalizedRecord]] = {}
        for candidate in pool:
            call_id = str(candidate.details.get("call_identifier") or "")
            if call_id:
                by_call.setdefault(call_id, []).append(candidate)
        processed: list[list[CallDocument] | None] = []
        errors: dict[int, str] = {}
        for position, record in enumerate(records):
            try:
                processed.append(self.pipeline.process(record))
            except Exception as exc:  # noqa: BLE001 — one bad record must not abort the run
                processed.append(None)
                errors[position] = f"{type(exc).__name__}: {exc}"
        programme_pool: dict[str, dict[str, CallDocument]] = {}
        for record, documents in zip(records, processed):
            bucket = programme_pool.setdefault(record.programme or "", {})
            for document in documents or []:
                if document.role == "programme_level" and document.doc_id:
                    bucket.setdefault(document.doc_id, document)
        tried: set[tuple[str, str]] = set()
        for position, (record, documents) in enumerate(zip(records, processed)):
            if skip and record.natural_key in skip:
                continue
            if documents is None:
                yield record, None, [], errors[position]
                continue
            siblings = by_call.get(str(record.details.get("call_identifier") or ""))
            try:
                new_record, own = self.enrich_record(
                    record, siblings, documents=documents,
                    pool_documents=list(programme_pool.get(record.programme or "", {}).values()),
                    tried=tried,
                )
            except Exception as exc:  # noqa: BLE001 — report, keep going
                yield record, None, [], f"{type(exc).__name__}: {exc}"
                continue
            yield record, new_record, own, None

    def enrich(
        self, records: list[NormalizedRecord], pool: list[NormalizedRecord] | None = None
    ) -> tuple[list[NormalizedRecord], dict[str, list[CallDocument]]]:
        """
        Enrich every record (see `iter_enrich`); returns the records and a natural_key ->
        own-documents index. A failing record is kept unchanged with an `enrichment_error` gap.
        """
        enriched: list[NormalizedRecord] = []
        index: dict[str, list[CallDocument]] = {}
        for record, new_record, documents, error in self.iter_enrich(records, pool):
            if new_record is None:
                gap = {"field": "enrichment", "kind": "enrichment_error", "error": error}
                new_record = record.model_copy(update={"data_gaps": [*record.data_gaps, gap]})
            enriched.append(new_record)
            index[record.natural_key] = documents
        return enriched, index
