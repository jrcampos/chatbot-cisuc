"""
Per-record, per-field verification: which layers (API, detail endpoint, documents) state a
key field, whether they agree, and the resulting verdict, with the URLs that prove it.

Declarative: the fields, how each is checked and what counts as independent come from
config/verification_policy.yaml; checked decisions for disagreements come from
config/verification_verdicts.yaml. Nothing here fetches anything or changes a value — the
result is written to `details.verification` next to the values it judges.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from external_sources.extraction.dates import _BRUSSELS, calendar_date
from external_sources.plugins.base import NormalizedRecord
from external_sources.sources import topic_page_url

CONFIG_DIR = Path(__file__).parent / "config"
DEFAULT_POLICY_PATH = CONFIG_DIR / "verification_policy.yaml"
DEFAULT_PROGRAMMES_PATH = CONFIG_DIR / "eu_programmes.yaml"
KINDS = ("numeric", "date", "text_presence", "text_overlap", "official_text", "programme")
STATUSES = ("confirmed", "single_source", "conflicting", "resolved", "absent")
_VERDICT_STATUSES = ("resolved", "confirmed", "conflicting")
_DECISIONS = ("api", "document", "both", "neither", "open")


def load_verification_policy(path: str | Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """Load and validate the verification policy; raises ValueError when malformed."""
    with open(path, encoding="utf-8") as f:
        policy = yaml.safe_load(f) or {}
    fields = policy.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError(f"{path}: 'fields' must be a non-empty mapping")
    for name, spec in fields.items():
        if not isinstance(spec, dict) or spec.get("kind") not in KINDS:
            raise ValueError(f"{path}: fields.{name}: kind must be one of {KINDS}")
    policy.setdefault("tolerance", 0.01)
    policy.setdefault("independent_groups", {"api": "portal", "detail": "portal", "document": "document"})
    policy["_dir"] = str(Path(path).parent)
    return policy


def load_verdicts(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load the checked verdicts; every entry needs key|key_pattern, field, status, sources, checked_at."""
    path = Path(path) if path else CONFIG_DIR / "verification_verdicts.yaml"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        entries = (yaml.safe_load(f) or {}).get("verdicts") or []
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'verdicts' must be a list")
    for i, entry in enumerate(entries):
        where = f"{path}: verdicts[{i}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a mapping")
        if not (entry.get("key") or entry.get("key_pattern")) or not entry.get("field"):
            raise ValueError(f"{where}: needs key or key_pattern, and field")
        if entry.get("status") not in _VERDICT_STATUSES:
            raise ValueError(f"{where}: status must be one of {_VERDICT_STATUSES}")
        if entry.get("decision") is not None and entry["decision"] not in _DECISIONS:
            raise ValueError(f"{where}: decision must be one of {_DECISIONS}")
        sources = entry.get("sources")
        if not isinstance(sources, list) or not sources or not all(
            isinstance(s, dict) and s.get("url") for s in sources
        ):
            raise ValueError(f"{where}: sources must list at least one {{url, quote?, page?}}")
        if not entry.get("checked_at"):
            raise ValueError(f"{where}: checked_at is required")
        if entry.get("key_pattern"):
            re.compile(entry["key_pattern"])
    return entries


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _value(record: NormalizedRecord, field: str) -> Any:
    if field.startswith("details."):
        return record.details.get(field[len("details."):])
    return getattr(record, field, None)


def _filled(value: Any) -> bool:
    return not (value is None or value == "" or value == [] or value == {})


def _layer_of(label: str | None) -> str:
    prefix = (label or "api").split(":", 1)[0]
    return prefix if prefix in ("api", "detail", "document") else "api"


class Verifier:
    """Computes `details.verification` for a record from its values, provenance and documents."""

    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        verdicts: list[dict[str, Any]] | None = None,
        now: str | None = None,
        programmes_path: str | Path = DEFAULT_PROGRAMMES_PATH,
    ) -> None:
        self.policy = policy or load_verification_policy()
        self.verdicts = load_verdicts(Path(self.policy["_dir"]) / self.policy["verdicts_file"]) \
            if verdicts is None and self.policy.get("verdicts_file") else (verdicts or [])
        self.now = now or datetime.now(UTC).isoformat()
        with open(programmes_path, encoding="utf-8") as f:
            programmes = (yaml.safe_load(f) or {}).get("programmes") or {}
        self._programme_key = {str(p.get("label", k)).lower(): k for k, p in programmes.items()}
        self._norm_cache: dict[str, str] = {}

    # ---- helpers ------------------------------------------------------------------------
    def _group(self, layer: str) -> str:
        return str(self.policy["independent_groups"].get(layer, layer))

    def _doc_norm(self, doc_id: str, text: str) -> str:
        if doc_id not in self._norm_cache:
            self._norm_cache[doc_id] = " " + _norm(text) + " "
        return self._norm_cache[doc_id]

    def _portal_source(self, record: NormalizedRecord) -> dict[str, Any]:
        return {"layer": "api", "url": topic_page_url(record) or record.source_url}

    def _verdict(self, record: NormalizedRecord, status: str, value: Any, layers: dict[str, Any],
                 agreeing: list[str], sources: list[dict[str, Any]], basis: str, note: str | None = None,
                 **extra: Any) -> dict[str, Any]:
        groups = sorted({self._group(layer) for layer in agreeing})
        out = {
            "status": status, "value": value, "layers": layers, "agreeing": agreeing,
            "agree_count": len(groups), "sources": sources, "basis": basis, "checked_at": self.now,
        }
        if note:
            out["note"] = note
        out.update(extra)
        return out

    # ---- kinds --------------------------------------------------------------------------
    def _numeric(self, record: NormalizedRecord, field: str) -> dict[str, Any]:
        value = _value(record, field)
        tolerance = float(self.policy["tolerance"])
        layer = _layer_of(record.field_sources.get(field))
        if not _filled(value):
            return self._verdict(record, "absent", None, {}, [], [], "none")
        layers: dict[str, Any] = {layer: value}
        agreeing = [layer]
        sources = [self._evidence_source(record, field) if layer == "document" else self._portal_source(record)]
        for entry in (record.details.get("corroboration") or {}).get(field, []):
            agreeing.append("document")
            layers.setdefault("document", entry.get("value"))
            sources.append({"layer": "document", "url": entry.get("url"), "page": entry.get("page")})
        if field == "details.call_budget_eur":
            for entry in record.details.get("value_history") or []:
                old = entry.get("value")
                if (entry.get("reason") == "api_is_call_total" and isinstance(old, (int, float))
                        and abs(old - value) <= tolerance * max(abs(value), 1.0)):
                    agreeing.append(entry.get("source") or "api")
                    layers.setdefault(entry.get("source") or "api", old)
                    sources.insert(0, self._portal_source(record))
        extra: dict[str, Any] = {}
        call_budget = record.details.get("call_budget_eur")
        if (field == "funding_amount" and isinstance(call_budget, (int, float))
                and _layer_of(record.field_sources.get("details.call_budget_eur")) == "document"
                and layer != "document"
                and abs(call_budget - value) <= tolerance * max(abs(value), 1.0)):
            agreeing.append("document")
            layers.setdefault("document", call_budget)
            sources.append(self._evidence_source(record, "details.call_budget_eur"))
            extra["level"] = "call_total"
            extra["note"] = "the topic value equals the call budget stated by the call document: it is the CALL total"
        conflict = False
        own = sources[0]
        values = [_entry(value, layer, own.get("url"), own.get("page"))]
        for gap in record.data_gaps:
            if gap.get("field") == field and gap.get("kind") == "discrepancy":
                conflict = True
                layers["document"] = gap.get("document")
                sources.append({"layer": "document", "url": gap.get("url")})
                values.append(_entry(gap.get("document"), "document", gap.get("url")))
        for entry in record.details.get("value_history") or []:
            if entry.get("field") == field and entry.get("confirmation") == "pending":
                conflict = True
                old_layer = _layer_of(entry.get("source"))
                layers[old_layer] = entry.get("value")
                portal = self._portal_source(record)
                sources.insert(0, portal)
                values.append(_entry(entry.get("value"), old_layer, portal["url"]))
        if conflict:
            extra["range"] = _range(values, value)
        groups = {self._group(layer_name) for layer_name in agreeing}
        status = "conflicting" if conflict else ("confirmed" if len(groups) >= 2 else "single_source")
        note = extra.pop("note", None)
        return self._verdict(record, status, value, layers, agreeing, _dedupe(sources), "agreement", note, **extra)

    def _evidence_source(self, record: NormalizedRecord, field: str) -> dict[str, Any]:
        evidence = (record.details.get("evidence") or {}).get(field) or {}
        return {"layer": "document", "url": evidence.get("url"), "page": evidence.get("page")}

    def _date(self, record: NormalizedRecord, field: str, spec: dict[str, Any]) -> dict[str, Any]:
        value = _value(record, field)
        candidates = []
        if spec.get("all_dates"):
            listed = _value(record, spec["all_dates"])
            candidates = [d for d in listed if isinstance(d, str) and d] if isinstance(listed, list) else []
        if not candidates and isinstance(value, str) and value:
            candidates = [value]
        document = _value(record, spec["document"]) if spec.get("document") else None
        if not candidates and not _filled(document):
            return self._verdict(record, "absent", None, {}, [], [], "none")
        api_days = [calendar_date(c) for c in candidates]
        layers: dict[str, Any] = {}
        if candidates:
            layers["api"] = [d.isoformat() for d in api_days if d]
        sources = [self._portal_source(record)] if candidates else []
        if not _filled(document):
            return self._verdict(record, "single_source", value, layers, ["api"], sources, "agreement")
        doc_day = calendar_date(str(document))
        layers["document"] = doc_day.isoformat() if doc_day else document
        sources.append(self._evidence_source(record, spec["document"]))
        if not candidates:
            return self._verdict(record, "single_source", document, layers, ["document"], sources, "agreement")
        note = None
        if "T" in str(document):
            local = datetime.fromisoformat(str(document).replace("Z", "+00:00")).astimezone(_BRUSSELS)
            note = f"document states {local:%H:%M} Brussels time"
        if doc_day in api_days:
            return self._verdict(record, "confirmed", value, layers, ["api", "document"], sources,
                                 "agreement", note)
        portal = self._portal_source(record)["url"]
        doc_source = self._evidence_source(record, spec["document"])
        values = [_entry(d.isoformat(), "api", portal) for d in dict.fromkeys(api_days) if d]
        values.append(_entry(layers["document"], "document", doc_source.get("url"), doc_source.get("page")))
        primary = calendar_date(value).isoformat() if calendar_date(value) else values[0]["value"]
        verdict = self._verdict(record, "conflicting", value, layers, ["api"], sources, "agreement", note,
                                range=_range(values, primary))
        notice = self._update_notice(record, spec, api_days)
        if notice is not None:
            verdict = {**verdict, "computed_status": "conflicting", "status": "resolved", "check": notice}
        return verdict

    def _update_notice(self, record: NormalizedRecord, spec: dict[str, Any], api_days: list[Any]) -> dict[str, Any] | None:
        """
        An official update on the topic page ('latest updates') that announces one of the API dates
        and is newer than the document: the document predates the change, so the API is right.
        """
        if not spec.get("update_notices"):
            return None
        updates = _value(record, spec["update_notices"])
        if not isinstance(updates, list):
            return None
        evidence = (record.details.get("evidence") or {}).get(spec.get("document", ""), {}) or {}
        published = str(evidence.get("published_at") or "")
        pattern = re.compile(spec.get("notice_pattern", r"(?i)deadline"))
        for update in updates:
            if not isinstance(update, dict):
                continue
            text, when = str(update.get("text") or ""), str(update.get("date") or "")
            if not pattern.search(text) or (published and when and when <= published):
                continue
            for day in api_days:
                if day is None:
                    continue
                month = day.strftime("%B")
                if re.search(rf"(?i)\b0?{day.day}\s+{month}\b|\b{month}\s+0?{day.day}\b", text):
                    return {
                        "status": "resolved", "decision": "api", "value": day.isoformat(), "basis": "update_notice",
                        "sources": [{"url": topic_page_url(record) or record.source_url,
                                     "quote": " ".join(text.split())[:300], "date": when}],
                        "checked_at": self.now,
                        "note": "official topic-page update announces the API date; the call document predates it",
                    }
        return None

    def _texts(self, documents: dict[str, tuple[str, str]]) -> list[tuple[str, str, str]]:
        return [(doc_id, url, self._doc_norm(doc_id, text)) for doc_id, (url, text) in documents.items()]

    def _text_presence(self, record: NormalizedRecord, field: str,
                       documents: dict[str, tuple[str, str]]) -> dict[str, Any]:
        value = _value(record, field)
        if not _filled(value):
            return self._verdict(record, "absent", None, {}, [], [], "none")
        layer = _layer_of(record.field_sources.get(field))
        needle = " " + _norm(str(value)) + " "
        hits = [url for doc_id, url, text in self._texts(documents)
                if layer != "document" and len(needle.split()) >= 2 and needle in text]
        sources = [self._portal_source(record)] + [{"layer": "document", "url": u} for u in hits]
        status = "confirmed" if hits else "single_source"
        agreeing = [layer] + (["document"] if hits else [])
        return self._verdict(record, status, value, {layer: value}, agreeing, _dedupe(sources), "text_presence")

    def _text_overlap(self, record: NormalizedRecord, field: str, spec: dict[str, Any],
                      documents: dict[str, tuple[str, str]]) -> dict[str, Any]:
        value = _value(record, field)
        if not _filled(value):
            return self._verdict(record, "absent", None, {}, [], [], "none")
        layer = _layer_of(record.field_sources.get(field))
        if layer == "document":
            return self._verdict(record, "single_source", None, {"document": True}, ["document"],
                                 [self._evidence_source(record, field)], "text_overlap", probes_found=0)
        sentences = [_norm(s) for s in re.split(r"(?<=[.!?])\s+|\n+", str(value))]
        sentences = [s[: int(spec.get("probe_max_chars", 120))] for s in sentences
                     if len(s) >= int(spec.get("probe_min_chars", 60))]
        count = int(spec.get("probes", 5))
        if len(sentences) > count:
            step = len(sentences) / count
            sentences = [sentences[int(i * step)] for i in range(count)]
        texts = self._texts(documents)
        found, urls = 0, []
        for probe in sentences:
            hit = next((url for _, url, text in texts if probe in text), None)
            if hit:
                found += 1
                urls.append(hit)
        needed = min(int(spec.get("min_hits", 2)), len(sentences)) if sentences else 1
        confirmed = bool(sentences) and found >= needed
        sources = [self._portal_source(record)] + [{"layer": "document", "url": u} for u in urls]
        return self._verdict(
            record, "confirmed" if confirmed else "single_source", None, {layer: True},
            [layer] + (["document"] if confirmed else []), _dedupe(sources), "text_overlap",
            probes_found=found, probes=len(sentences),
        )

    def _official_text(self, record: NormalizedRecord, spec: dict[str, Any]) -> dict[str, Any]:
        api_text = _value(record, spec["api"]) if spec.get("api") else None
        doc_fields = spec.get("document") or []
        doc_fields = [doc_fields] if isinstance(doc_fields, str) else list(doc_fields)
        doc_field = next((f for f in doc_fields if _filled(_value(record, f))), None)
        doc_text = _value(record, doc_field) if doc_field else None
        layers = {name: True for name, v in (("api", api_text), ("document", doc_text)) if _filled(v)}
        if _filled(doc_text):
            sources = ([self._portal_source(record)] if _filled(api_text) else []) + [
                self._evidence_source(record, doc_field)]
            return self._verdict(record, "confirmed", None, layers, list(layers), sources, "official_document")
        if _filled(api_text):
            return self._verdict(record, "single_source", None, layers, ["api"],
                                 [self._portal_source(record)], "api_only")
        return self._verdict(record, "absent", None, {}, [], [], "none")

    def _programme(self, record: NormalizedRecord, documents: dict[str, tuple[str, str]]) -> dict[str, Any]:
        label = record.programme
        if not label:
            return self._verdict(record, "absent", None, {}, [], [], "none")
        key = self._programme_key.get(label.lower())
        markers_cfg = self.policy.get("programme_markers") or {}
        markers = []
        if key:
            if markers_cfg.get("default"):
                markers.append(re.escape(markers_cfg["default"].format(key=key)).replace("\\{", "{"))
            markers += list((markers_cfg.get("extra") or {}).get(key, []))
        hits = [url for url, _ in documents.values() if any(re.search(m, url) for m in markers)]
        sources = [self._portal_source(record)] + [{"layer": "document", "url": u} for u in hits[:3]]
        if hits:
            return self._verdict(record, "confirmed", label, {"api": label, "document": True},
                                 ["api", "document"], _dedupe(sources), "programme_document")
        return self._verdict(record, "single_source", label, {"api": label}, ["api"], sources, "api_only")

    # ---- entry point --------------------------------------------------------------------
    def verify(
        self, record: NormalizedRecord, documents: dict[str, tuple[str, str]]
    ) -> dict[str, dict[str, Any]]:
        """Verdict per policy field. `documents` = {doc_id: (url, markdown)} the record used."""
        out: dict[str, dict[str, Any]] = {}
        for field, spec in self.policy["fields"].items():
            kind = spec["kind"]
            if kind == "numeric":
                verdict = self._numeric(record, field)
            elif kind == "date":
                verdict = self._date(record, field, spec)
            elif kind == "text_presence":
                verdict = self._text_presence(record, field, documents)
            elif kind == "text_overlap":
                verdict = self._text_overlap(record, field, spec, documents)
            elif kind == "official_text":
                verdict = self._official_text(record, spec)
            else:
                verdict = self._programme(record, documents)
            out[field] = _finish_range(self._apply_checked(record, field, verdict))
        return out

    def _apply_checked(self, record: NormalizedRecord, field: str, verdict: dict[str, Any]) -> dict[str, Any]:
        for entry in self.verdicts:
            if entry["field"] != field:
                continue
            if entry.get("key") and entry["key"] != record.natural_key:
                continue
            if entry.get("key_pattern") and not re.search(entry["key_pattern"], record.natural_key):
                continue
            check = {k: entry[k] for k in ("status", "decision", "value", "sources", "checked_at", "note")
                     if k in entry}
            return {**verdict, "computed_status": verdict["status"], "status": entry["status"], "check": check}
        return verdict


def _entry(value: Any, layer: str, source: Any, page: Any = None) -> dict[str, Any]:
    entry = {"value": value, "layer": layer, "source": source}
    if page is not None:
        entry["page"] = page
    return entry


def _range(values: list[dict[str, Any]], primary: Any) -> dict[str, Any]:
    """{min, max, primary, values} over every value a layer states (dates as ISO strings)."""
    seen: set[tuple[Any, ...]] = set()
    unique = []
    for entry in values:
        key = (entry["value"], entry["layer"], entry["source"])
        if entry["value"] is not None and key not in seen:
            seen.add(key)
            unique.append(entry)
    numbers = [e["value"] for e in unique]
    return {"min": min(numbers), "max": max(numbers), "primary": primary, "values": unique, "settled": False}


def _finish_range(verdict: dict[str, Any]) -> dict[str, Any]:
    """
    A range stays unsettled (status conflicting) unless the official source settled it (status
    resolved): then the settled value becomes primary; every other value stays listed.
    """
    rng = verdict.get("range")
    if not rng:
        return verdict
    settled = verdict.get("status") == "resolved"
    rng = {**rng, "settled": settled}
    check = verdict.get("check") or {}
    if settled and check.get("value") is not None:
        rng["primary"] = check["value"]
    if settled:
        rng["settled_by"] = [s.get("url") for s in check.get("sources") or [] if s.get("url")]
    return {**verdict, "range": rng}


def value_ranges(verification: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """`details.value_range`: every field whose sources disagree -> its range plus the verdict status."""
    return {
        field: {**verdict["range"], "status": verdict["status"]}
        for field, verdict in verification.items()
        if isinstance(verdict, dict) and verdict.get("range")
    }


def _dedupe(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out = []
    for source in sources:
        key = (source.get("layer"), source.get("url"), source.get("page"))
        if source.get("url") and key not in seen:
            seen.add(key)
            out.append(source)
    return out
