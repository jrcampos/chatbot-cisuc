"""
The sources the store knows: how to build each plugin, and its licence/attribution metadata
(written into `extsrc.source`, shown to the chatbot through `current.source_freshness`).
Adding a source = one entry here + its plugin + its field spec (see README "How to add a source").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from external_sources.plugins.base import SourcePlugin

EC_LICENCE = ("EU Funding & Tenders Portal content: (c) European Union, reuse authorised under "
              "Commission Decision 2011/833/EU (CC BY 4.0) with acknowledgement of the source")


@dataclass(frozen=True)
class SourceInfo:
    name: str
    description: str
    base_url: str
    licence: str
    attribution: str
    build: Callable[..., SourcePlugin]
    with_detail: bool = False


def _ft_portal(**kwargs: Any) -> SourcePlugin:
    from external_sources.plugins.ft_portal import FTPortalPlugin
    return FTPortalPlugin(**kwargs)


def _ft_cascade(**kwargs: Any) -> SourcePlugin:
    from external_sources.plugins.ft_cascade import FTCascadePlugin
    return FTCascadePlugin(**kwargs)


def _ft_europeaid(**kwargs: Any) -> SourcePlugin:
    from external_sources.plugins.ft_europeaid import FTEuropeAidPlugin
    return FTEuropeAidPlugin(**kwargs)


SOURCES: dict[str, SourceInfo] = {
    "ft_portal": SourceInfo(
        "ft_portal", "EU Funding & Tenders Portal: open and forthcoming call topics (search type 1), "
        "all programmes, enriched from the per-topic detail endpoint and the linked documents",
        "https://ec.europa.eu/info/funding-tenders/opportunities/portal", EC_LICENCE,
        "Source: EU Funding & Tenders Portal (European Commission)", _ft_portal, with_detail=True),
    "ft_cascade": SourceInfo(
        "ft_cascade", "EU Funding & Tenders Portal: cascade-funding calls (financial support to third "
        "parties) launched by EU-funded projects (search type 8)",
        "https://ec.europa.eu/info/funding-tenders/opportunities/portal", EC_LICENCE,
        "Call text written by the organising EU-funded project (not an EU action); listed on the EU "
        "Funding & Tenders Portal", _ft_cascade),
    "ft_europeaid": SourceInfo(
        "ft_europeaid", "EU Funding & Tenders Portal: EU external-action calls for proposals "
        "(grants and twinning, search type 2, formerly EuropeAid)",
        "https://ec.europa.eu/info/funding-tenders/opportunities/portal", EC_LICENCE,
        "Source: EU Funding & Tenders Portal / EU external action (European Commission)", _ft_europeaid),
}
