"""
EU Funding & Tenders Portal — cascade-funding calls (financial support to third parties).

Same public search API and terms as `FTPortalPlugin` (EC content, reuse with attribution under
Commission Decision 2011/833/EU), but search type 8: open calls launched by EU-funded projects.
One query covers every programme (the programme label is looked up from the payload's
frameworkProgramme id: config label first, then the portal's facet label); expired calls the
source still flags open are dropped like in the topic plugin. No detail endpoint is used: the
search payload is the whole record. Failures are isolated per page and per record exactly like
the topic plugin (`failures`, `partial_reasons`).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from external_sources.normalization import dedupe_by_natural_key
from external_sources.normalization.transforms import first
from external_sources.plugins.base import NormalizedRecord
from external_sources.plugins.ft_portal import FTPortalPlugin

_SPEC = Path(__file__).parent.parent / "config" / "ft_cascade_fields.yaml"
CASCADE_TYPE = "8"


class FTCascadePlugin(FTPortalPlugin):
    """Open / forthcoming cascade-funding calls from the F&T search API."""

    name = "ft_cascade"
    SEARCH_TYPE = CASCADE_TYPE

    def __init__(self, spec_path: str | Path = _SPEC, **kwargs: Any) -> None:
        super().__init__(spec_path=spec_path, **kwargs)
        self._labels = {str(p["id"]): p["label"] for p in self.programmes.values()}

    def _build_cascade_query(self, status_codes: list[str]) -> dict[str, Any]:
        return {"bool": {"must": [{"terms": {"type": [self.SEARCH_TYPE]}}, {"terms": {"status": status_codes}}]}}

    def _fetch_cascade_page(self, status_codes: list[str], page_number: int) -> dict[str, Any]:
        return self._post_search(self._build_cascade_query(status_codes), page_number)

    def _programme_label(self, result: Any) -> str:
        metadata = result.get("metadata") if isinstance(result, dict) else None
        programme_id = str(first((metadata or {}).get("frameworkProgramme"), {}) or "")
        return self._label_for(programme_id)

    def fetch(self) -> list[NormalizedRecord]:
        self.failures = []
        self.partial_reasons = []
        status_codes = [self._status_codes[s] for s in self.statuses]
        if self.discover_all:
            self.programme_labels = self.fetch_programme_labels()
        records: list[NormalizedRecord] = []
        query = self._build_cascade_query(status_codes)
        pages = self._paged_results(lambda n: self._fetch_cascade_page(status_codes, n), self.name, query=query)
        for result in pages:
            record = self._safe_record(result, self._programme_label(result))
            if record is not None:
                records.append(record)
        records, _ = dedupe_by_natural_key(records)
        kept, dropped = self._filter_in_scope(records)
        if dropped:
            print(f"[INFO] {self.name}: dropped {sum(dropped.values())} out-of-scope record(s) {dropped}",
                  file=sys.stderr)
        return [self._postprocess(r) for r in kept]

    def _postprocess(self, record: NormalizedRecord) -> NormalizedRecord:
        return self._description_fallback(record)

    @staticmethod
    def _description_fallback(record: NormalizedRecord) -> NormalizedRecord:
        """Portal 'description' empty: use the call's 'further information' text, and say so."""
        further = record.details.get("further_information")
        if record.description or not further:
            return record
        return record.model_copy(update={
            "description": further,
            "details": {**record.details, "description_from": "furtherInformation"},
            "field_sources": {**record.field_sources, "description": "api"},
        })
