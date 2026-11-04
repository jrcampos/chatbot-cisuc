"""
EU Funding & Tenders Portal — EU external-action calls for proposals (search type 2).

Grants and twinning calls of EU external action (frameworkProgramme 111111, "EU External Action -
Prospect", formerly EuropeAid), published on the same portal and search API as the topics, under
the same terms (EC content, CC BY 4.0 / Commission Decision 2011/833/EU). One query, every status
in scope; expired calls the index still flags open are dropped by the derived status.

The payload has no description: the call texts are the published documents (guidelines for
applicants, twinning fiche), kept as `details.publication_documents` so the document pipeline can
read them. Coded facets (contract type, geographical zones, programme division) are decoded from
the portal's facet endpoint into `*_label(s)` details; the codes are kept as well.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from external_sources.plugins.base import NormalizedRecord
from external_sources.plugins.ft_cascade import FTCascadePlugin

_SPEC = Path(__file__).parent.parent / "config" / "ft_europeaid_fields.yaml"
EXTERNAL_ACTION_TYPE = "2"
_FACETS = ("frameworkProgramme", "contractType", "geographicalZones", "programmeDivisionProspect")


class FTEuropeAidPlugin(FTCascadePlugin):
    """Open / forthcoming EU external-action calls for proposals from the F&T search API."""

    name = "ft_europeaid"
    SEARCH_TYPE = EXTERNAL_ACTION_TYPE

    def __init__(self, spec_path: str | Path = _SPEC, **kwargs: Any) -> None:
        super().__init__(spec_path=spec_path, **kwargs)
        self.facet_labels: dict[str, dict[str, str]] = {}

    def fetch(self) -> list[NormalizedRecord]:
        # Facet labels are fetched once before the search; programme labels come from them too.
        self.facet_labels = self.fetch_facet_labels(_FACETS) if self.discover_all else {}
        self.programme_labels = self.facet_labels.get("frameworkProgramme", {})
        discover, self.discover_all = self.discover_all, False  # labels already fetched
        try:
            return super().fetch()
        finally:
            self.discover_all = discover

    def _postprocess(self, record: NormalizedRecord) -> NormalizedRecord:
        details = dict(record.details)
        contract = self.facet_labels.get("contractType", {}).get(str(details.get("contract_type_code") or ""))
        if contract:
            details["contract_type"] = contract
        zones = self.facet_labels.get("geographicalZones", {})
        names = [zones[c] for c in details.get("geographical_zone_codes") or [] if c in zones]
        if names:
            details["geographical_zones"] = names
        division = self.facet_labels.get("programmeDivisionProspect", {}).get(
            str(details.get("programme_division_code") or ""))
        if division:
            details["programme_division"] = division
        if not record.description:
            record = record.model_copy(update={"data_gaps": [*record.data_gaps, {
                "field": "description", "kind": "source_absent",
                "reason": "type-2 search payload has no description; see details.publication_documents",
            }]})
        return record.model_copy(update={"details": details})
