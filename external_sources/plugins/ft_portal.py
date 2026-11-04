"""
EU Funding & Tenders Portal plugin.

Covers ~20 EU programmes' Open and Forthcoming calls in a single connector
(see external_sources/config/eu_programmes.yaml for the full list). This is
the same search-api platform used by the portal's own website — apiKey
"SEDIA" is the portal's own public key, not a secret.

Two things that will silently break this plugin if changed carelessly:
1. Both the `query` and `languages` multipart parts need an explicit
   Content-Type: application/json header. Without it on `query`, the API
   returns HTTP 500. Without `languages` present at all, it returns HTTP 200
   with zero results — no error, just silently wrong.
2. Every metadata value in a result is a single-element array (e.g.
   `metadata["title"] == ["Some title"]`, not `"Some title"`). Always unwrap
   in the field spec with the `first` step.

Raw results are prepared through a declarative field spec
(external_sources/config/ft_portal_fields.yaml): every raw key is either mapped
(ISO dates, cleaned text, derived status, `details` extras) or explicitly
ignored, and the untouched payload stays in `raw`.

Detail enrichment: when built with `FTPortalPlugin.with_detail(cache_dir)`,
every in-scope call is also looked up on the per-call detail endpoint (config
`detail:` block; conditional-GET cached). Its payload is injected as a `detail`
raw key and mapped by the spec (the payload is first normalised into the search
shape, see ft_portal_detail); failures become `data_gaps` entries. Each
record's `field_sources` labels every populated field as "api" or "detail".

Scope is enforced at fetch time: the search index still flags ~564 long-expired
"call-family" records as open/forthcoming, which the spec's `derive_status`
turns into status="closed"; `fetch()` drops them (see `_filter_in_scope`).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import yaml

from external_sources.enrichment.detail import DetailEnricher
from external_sources.http_cache import USER_AGENT, CachedFetcher
from external_sources.normalization import apply_spec, dedupe_by_natural_key, load_spec
from external_sources.normalization.transforms import first, strip
from external_sources.plugins.base import Failure, NormalizedRecord, SourcePlugin
from external_sources.plugins.ft_portal_detail import (
    EXTRA_DETAIL_KEYS,
    fill_from_detail,
)
from external_sources.resilience import RetryingHTTP, RetryPolicy, retry_call

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "eu_programmes.yaml"
_DEFAULT_SPEC_PATH = Path(__file__).parent.parent / "config" / "ft_portal_fields.yaml"


class FTPortalPlugin(SourcePlugin):
    """
    Fetches Open/Forthcoming calls from the EU Funding & Tenders Portal for
    every programme listed in eu_programmes.yaml.

    Only records whose derived status is in the requested statuses leave
    fetch(); this is the guard that keeps expired records out of downstream
    storage. The comparison uses the same vocabulary as `self.statuses` and
    the spec's status map ("open"/"forthcoming"); a record whose derived
    status is None (unknown) is out of scope too.
    """

    name = "ft_portal"
    BASE_URL: str = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
    FACET_URL: str = "https://api.tech.ec.europa.eu/search-api/prod/rest/facet"
    API_KEY: str = "SEDIA"
    PAGE_SIZE: int = 50
    MAX_PAGES: int = 20  # safety cap — 50*20 = 1000 results per programme/status
    SEARCH_TYPE: str = "1"
    # Whole "request + parse" attempts per page (each request also has its own HTTP retries).
    PAGE_RETRY = RetryPolicy(max_attempts=2, base_delay=5.0, max_delay=30.0)

    def __init__(
        self,
        config_path: str | Path = _DEFAULT_CONFIG_PATH,
        statuses: list[str] | None = None,
        spec_path: str | Path = _DEFAULT_SPEC_PATH,
        now: datetime | None = None,
        http: RetryingHTTP | None = None,
        discover_all: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        Args:
            config_path: path to the YAML file listing programme ids/labels
                and status codes. Defaults to the bundled
                config/eu_programmes.yaml.
            statuses: which call statuses to fetch, as the human-readable
                keys from the config's status_codes map (e.g. "open",
                "forthcoming"). Defaults to both — Closed is out of scope.
            spec_path: path to the field spec YAML. Defaults to the bundled
                config/ft_portal_fields.yaml.
            now: fixed clock used for status derivation (tests only).
                Defaults to the current UTC time.
            http: retrying HTTP client (tests inject one with a fake transport).
                Defaults to `requests.post` behind retries, timeouts and a
                per-host circuit breaker.
            discover_all: also query every programme NOT in the config (one
                catch-all query) and label it from the portal's facet endpoint,
                so a call in an unconfigured programme is never missed.
            sleep: sleep function used between retries (tests: no-op).
        """
        with open(config_path, encoding="utf-8") as f:
            config: dict[str, Any] = yaml.safe_load(f)

        self.programmes: dict[str, dict[str, str]] = config["programmes"]
        self._status_codes: dict[str, str] = config["status_codes"]
        self.statuses: list[str] = statuses or ["open", "forthcoming"]
        self.field_spec = load_spec(spec_path)
        self._now = now
        self.detail_config: dict[str, Any] | None = config.get("detail")
        self.detail_enricher: DetailEnricher | None = None
        self.discover_all = discover_all
        self._sleep = sleep
        self._http = http or RetryingHTTP(
            lambda method, url, **kw: requests.post(url, **kw),
            RetryPolicy(max_attempts=4, base_delay=2.0, max_delay=60.0), sleep=sleep,
        )
        self.failures: list[Failure] = []
        self.partial_reasons: list[str] = []
        self.programme_labels: dict[str, str] = {}

    @classmethod
    def with_detail(cls, cache_dir: str | Path, **kwargs: Any) -> FTPortalPlugin:
        """Build a plugin that also enriches every in-scope call from the detail endpoint."""
        plugin = cls(**kwargs)
        if plugin.detail_config:
            plugin.detail_enricher = DetailEnricher(CachedFetcher(cache_dir), **plugin.detail_config)
        return plugin

    def _build_query(self, programme_id: str, status_codes: list[str]) -> dict[str, Any]:
        """
        Build the `query` multipart part's JSON body: grant-type calls,
        filtered by status and framework programme.
        """
        return {
            "bool": {
                "must": [
                    {"terms": {"type": [self.SEARCH_TYPE]}},
                    {"terms": {"status": status_codes}},
                    {"terms": {"frameworkProgramme": [programme_id]}},
                ]
            }
        }

    def _post_search(self, query: dict[str, Any], page_number: int, url: str | None = None,
                     paged: bool = True) -> dict[str, Any]:
        """
        POST one search (or facet) request and return its JSON object. Network errors and
        retryable statuses are retried by the HTTP client; a malformed or non-object JSON body
        is retried once more as a whole (it is usually a truncated response).
        """
        params: dict[str, Any] = {"apiKey": self.API_KEY, "text": "***"}
        if paged:
            params.update({"pageSize": self.PAGE_SIZE, "pageNumber": page_number})
        files = {
            "query": (None, json.dumps(query), "application/json"),
            "languages": (None, json.dumps(["en"]), "application/json"),
        }

        def attempt() -> dict[str, Any]:
            response = self._http.post(url or self.BASE_URL, params=params, files=files,
                                       headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError(f"search response is a {type(data).__name__}, not a JSON object")
            return data

        return retry_call(attempt, self.PAGE_RETRY, sleep=self._sleep,
                          describe=f"{self.name} search page {page_number}")

    def _fetch_page(
        self, programme_id: str, status_codes: list[str], page_number: int
    ) -> dict[str, Any]:
        """Make one paginated request to the search API and return its parsed JSON."""
        return self._post_search(self._build_query(programme_id, status_codes), page_number)

    def _fail(self, stage: str, key: str, exc: BaseException | str, payload: Any = None) -> None:
        failure = (Failure.from_exception(stage, key, exc, payload) if isinstance(exc, BaseException)
                   else Failure(stage, key, exc, payload))
        self.failures.append(failure)
        print(f"[WARN] {self.name}: {stage} failed for {key}: {failure.error[:200]}")

    def _paged_results(self, fetch_page: Callable[[int], dict[str, Any]], scope: str,
                       label: str | None = None, query: Any = None) -> list[dict[str, Any]]:
        """
        Every raw result of a paged search. A page that still fails after its retries is
        recorded (failure + partial reason) and ends this scope only; other scopes go on.
        """
        results: list[dict[str, Any]] = []
        page_number = 1
        total_results = 0
        while page_number <= self.MAX_PAGES:
            if self.stop_requested():
                self.partial_reasons.append(f"{scope}: interrupted before page {page_number}")
                break
            try:
                response = fetch_page(page_number)
                if not isinstance(response, dict):
                    raise ValueError(f"search response is a {type(response).__name__}, not a JSON object")
            except Exception as exc:  # noqa: BLE001 — isolate the failing scope
                self._fail("fetch", f"{scope}:page{page_number}", exc, {"query": query, "page": page_number})
                self.partial_reasons.append(f"{scope}: page {page_number} failed ({type(exc).__name__})")
                break
            page = response.get("results")
            if not isinstance(page, list):
                self._fail("fetch", f"{scope}:page{page_number}",
                           f"schema drift: 'results' is {type(page).__name__}", response)
                self.partial_reasons.append(f"{scope}: page {page_number} had no result list")
                break
            results.extend(page)
            raw_total = response.get("totalResults", 0)
            total_results = raw_total if isinstance(raw_total, int) else 0
            if not page or page_number * self.PAGE_SIZE >= total_results:
                break
            page_number += 1
        else:
            print(
                f"[WARN] {label or scope}: hit MAX_PAGES cap ({self.MAX_PAGES}), "
                f"results may be truncated (total_results={total_results}, collected so far)"
            )
            self.partial_reasons.append(f"{scope}: MAX_PAGES cap ({self.MAX_PAGES}) reached")
        return results

    def _safe_record(self, result: Any, programme_label: str) -> NormalizedRecord | None:
        """Normalize one raw result; a failure is recorded with the raw payload, never raised."""
        metadata = result.get("metadata") if isinstance(result, dict) else None
        guess = str(first((metadata or {}).get("identifier"), {}) or (metadata or {}).get("ccm2Id") or "?")
        if not isinstance(metadata, dict):
            self._fail("normalize", guess, "schema drift: result has no 'metadata' object", result)
            return None
        try:
            record = self._record_from_result(result, programme_label)
        except Exception as exc:  # noqa: BLE001 — one bad record never stops the source
            self._fail("normalize", guess, exc, result)
            return None
        if not record.natural_key:
            self._fail("normalize", guess, "empty natural_key after normalization", result)
            return None
        return self._check_required(record)

    @staticmethod
    def _check_required(record: NormalizedRecord) -> NormalizedRecord:
        """Keep a record whose title/source_url is missing, with a placeholder and a gap."""
        gaps = list(record.data_gaps)
        update: dict[str, Any] = {}
        if not record.title:
            update["title"] = record.natural_key
            gaps.append({"field": "title", "kind": "schema_drift",
                         "reason": "title missing in payload; natural key used as placeholder"})
        if not record.source_url:
            gaps.append({"field": "source_url", "kind": "schema_drift", "reason": "url missing in payload"})
        if update or len(gaps) != len(record.data_gaps):
            return record.model_copy(update={**update, "data_gaps": gaps})
        return record

    def _drift_gaps(self, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Raw keys the spec neither maps nor ignores: recorded per record, never fatal."""
        unknown = sorted(set(metadata) - self.field_spec.declared_keys() - {"detail"})
        if not unknown:
            return []
        return [{"field": "raw", "kind": "schema_drift", "reason": "unmapped raw keys", "keys": unknown}]

    def fetch_facet_labels(self, names: tuple[str, ...] = ("frameworkProgramme",)) -> dict[str, dict[str, str]]:
        """facet name -> {raw code -> portal label}, from the facet endpoint ({} on failure).
        Labels are optional decoration: a failure is recorded but never makes the run partial."""
        query = {"bool": {"must": [{"terms": {"type": [self.SEARCH_TYPE]}}]}}
        try:
            data = self._post_search(query, 1, url=self.FACET_URL, paged=False)
        except Exception as exc:  # noqa: BLE001 — labels are optional
            self._fail("fetch", "facet:" + ",".join(names), exc)
            return {}
        labels: dict[str, dict[str, str]] = {}
        for facet in data.get("facets") or []:
            if isinstance(facet, dict) and facet.get("name") in names:
                bucket = labels.setdefault(str(facet["name"]), {})
                for value in facet.get("values") or []:
                    if isinstance(value, dict) and value.get("rawValue"):
                        bucket[str(value["rawValue"])] = re.sub(r"\s+", " ", str(value.get("value") or "")).strip()
        return labels

    def fetch_programme_labels(self) -> dict[str, str]:
        """frameworkProgramme id -> portal label ({} on failure)."""
        return self.fetch_facet_labels().get("frameworkProgramme", {})

    def _label_for(self, programme_id: str) -> str:
        configured = {str(p["id"]): p["label"] for p in self.programmes.values()}
        return configured.get(programme_id) or self.programme_labels.get(programme_id) \
            or f"EU programme {programme_id}".strip()

    def _record_from_result(
        self, result: dict[str, Any], programme_label: str
    ) -> NormalizedRecord:
        """Map one raw API result to a NormalizedRecord via the field spec."""
        metadata = result.get("metadata", {})
        # Same normalization as the spec's natural_key rule ([first, strip]) so
        # the ctx key used by budget matching equals the record's natural_key.
        identifier = first(metadata.get("identifier"), {})
        ctx = {
            "record_type": "call",
            "natural_key": (strip(identifier, {}) if identifier is not None else "") or "",
            "now": self._now or datetime.now(UTC),
        }
        applied = apply_spec(self.field_spec, metadata, ctx)
        by_target = self.field_spec.rules_by_target()
        targets = [*applied["fields"], *(f"details.{k}" for k in applied["details"])]
        detail_meta = metadata.get("detail")
        injected = set(
            detail_meta.get("_filled_from_detail", []) if isinstance(detail_meta, dict) else []
        )
        field_sources = {
            target: "detail"
            if by_target.get(target)
            and by_target[target][0].raw_key in ({"detail"} | injected)
            else "api"
            for target in targets
        }
        defaults: dict[str, Any] = {"natural_key": "", "title": "", "source_url": ""}
        fixed: dict[str, Any] = {
            "record_type": "call",
            "domain": "projects",
            "programme": programme_label,
            "country_scope": "EU",
            "details": applied["details"],
            "raw": metadata,
            "field_sources": field_sources,
            "data_gaps": self._drift_gaps(metadata),
        }
        # Every top-level target the spec produced reaches the record.
        return NormalizedRecord(**{**defaults, **applied["fields"], **fixed})

    def _filter_in_scope(
        self, records: list[NormalizedRecord]
    ) -> tuple[list[NormalizedRecord], dict[str | None, int]]:
        """
        Split records into those whose derived status is in `self.statuses`
        and a count of the dropped ones keyed by derived status (None for
        records without a status). Order of kept records is preserved.
        """
        wanted = set(self.statuses)
        kept: list[NormalizedRecord] = []
        dropped: dict[str | None, int] = {}
        for record in records:
            if record.status in wanted:
                kept.append(record)
            else:
                dropped[record.status] = dropped.get(record.status, 0) + 1
        return kept, dropped

    def _add_detail(self, records: list[NormalizedRecord]) -> list[NormalizedRecord]:
        """Enrich every in-scope record from the detail endpoint; failures become data_gaps."""
        assert self.detail_enricher is not None
        enriched: list[NormalizedRecord] = []
        for position, record in enumerate(records):
            if self.stop_requested():
                self.partial_reasons.append(
                    f"detail: interrupted after {position} of {len(records)} records")
                enriched.extend(records[position:])
                break
            try:
                enriched.append(self._detail_one(record))
            except Exception as exc:  # noqa: BLE001 — keep the search-only record
                self._fail("detail", record.natural_key, exc)
                record.data_gaps.append({"field": "detail", "kind": "detail_error",
                                         "reason": f"{type(exc).__name__}: {str(exc)[:300]}"})
                enriched.append(record)
        return enriched

    def _detail_one(self, record: NormalizedRecord) -> NormalizedRecord:
        """One record enriched from the detail endpoint; unavailable detail becomes a gap."""
        assert self.detail_enricher is not None
        result = self.detail_enricher.fetch(record.natural_key)
        if result.payload is None:
            record.data_gaps.append(
                {"field": "detail", "kind": "detail_unavailable",
                 "reason": result.status, "error": result.error}
            )
            return record
        filled, injected = fill_from_detail(record.raw, result.payload)
        extras: dict[str, Any] = {
            k: result.payload[k] for k in EXTRA_DETAIL_KEYS if k in result.payload
        }
        extras["_filled_from_detail"] = injected
        metadata = {**filled, "detail": extras}
        rebuilt = self._record_from_result({"metadata": metadata}, record.programme or "")
        if result.status == "error":
            rebuilt.data_gaps.append(
                {"field": "detail", "kind": "detail_stale",
                 "reason": "refresh failed, cached copy used", "error": result.error}
            )
        return self._check_required(rebuilt)

    def build_records(
        self, results: list[dict[str, Any]], programme_label: str, enrich: bool = True
    ) -> list[NormalizedRecord]:
        """
        Convert raw search results to records without the scope filter or
        de-duplication (used by validation tooling, e.g. for closed topics).
        With `enrich` and a detail enricher, applies the same detail
        enrichment as fetch().
        """
        records = [self._record_from_result(r, programme_label) for r in results]
        if enrich and self.detail_enricher is not None:
            records = self._add_detail(records)
        return records

    def fetch(self) -> list[NormalizedRecord]:
        """
        Fetch Open/Forthcoming calls for every configured programme.

        Loops every programme in eu_programmes.yaml, paginating each one
        until either all results are collected or MAX_PAGES is hit (a
        safety cap — this plugin has no business pulling unbounded data in
        a validation pass). With `discover_all`, one more query collects every
        programme the config does not list. Duplicates are removed, then records whose
        derived status is not in `self.statuses` (e.g. expired calls the
        source still flags open) are dropped.

        Failures never abort the run: a page that fails after its retries ends that
        programme only (`partial_reasons`), a result that cannot be normalized is recorded
        with its raw payload (`failures`), and unmapped raw keys become `schema_drift` gaps.
        """
        self.failures = []
        self.partial_reasons = []
        status_codes = [self._status_codes[s] for s in self.statuses]
        records: list[NormalizedRecord] = []

        for programme_key, programme in self.programmes.items():
            def programme_page(number: int, pid: str = programme["id"]) -> dict[str, Any]:
                return self._fetch_page(pid, status_codes, number)

            query = self._build_query(programme["id"], status_codes)
            for result in self._paged_results(programme_page, programme_key, programme["label"], query):
                record = self._safe_record(result, programme["label"])
                if record is not None:
                    records.append(record)

        if self.discover_all:
            self.programme_labels = self.fetch_programme_labels()
            known = [str(p["id"]) for p in self.programmes.values()]
            query = {"bool": {
                "must": [{"terms": {"type": [self.SEARCH_TYPE]}}, {"terms": {"status": status_codes}}],
                "must_not": [{"terms": {"frameworkProgramme": known}}],
            }}
            for result in self._paged_results(lambda n: self._post_search(query, n),
                                              "unconfigured_programmes", query=query):
                metadata = result.get("metadata") if isinstance(result, dict) else None
                programme_id = str(first((metadata or {}).get("frameworkProgramme"), {}) or "")
                record = self._safe_record(result, self._label_for(programme_id))
                if record is not None:
                    records.append(record)

        records, dropped = dedupe_by_natural_key(records)
        if dropped:
            print(f"[INFO] {self.name}: dropped {dropped} duplicate record(s) by natural_key")

        records, out_of_scope = self._filter_in_scope(records)
        total_dropped = sum(out_of_scope.values())
        if total_dropped:
            if len(out_of_scope) == 1:
                breakdown = f"status={next(iter(out_of_scope))}"
            else:
                breakdown = ", ".join(
                    f"status={status}: {count}"
                    for status, count in sorted(out_of_scope.items(), key=lambda kv: str(kv[0]))
                )
            print(
                f"[INFO] {self.name}: dropped {total_dropped} out-of-scope record(s) "
                f"({breakdown} after deadline check)"
            )
        if self.detail_enricher is not None:
            records = self._add_detail(records)
        return records
