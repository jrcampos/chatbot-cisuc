"""
Shared plugin contract for external_sources.

Every source-specific plugin (ft_portal.py, ft_cascade.py, ...) implements
`SourcePlugin` and returns a list of `NormalizedRecord` — a common shape
that hides each source's native schema differences behind one interface.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from external_sources.normalization.spec import FieldSpec


class NormalizedRecord(BaseModel):
    """
    A single funding record (a project or a call) normalized to a common
    shape, regardless of which source it came from.

    `raw` always carries the untouched original payload from the source —
    nothing is lost even when a source has fields this model doesn't cover.

    Date fields hold ISO-8601 UTC strings (`YYYY-MM-DDTHH:MM:SS+00:00`) when
    the plugin's field spec normalizes them (F&T Portal does); `details` holds
    prepared extras (cleaned text, parsed structures, link lists) that have no
    dedicated top-level field; `raw` remains the untouched source payload.

    `source_updated_at` is the source's own "last indexed/updated" timestamp
    (ISO UTC), used for change detection. `field_sources` maps a field
    (`description`, `details.publication_date`, ...) to where its value came
    from (`api`, `detail`, or `document:<doc_id>`). `data_gaps` lists what
    could not be filled or disagrees, as `{"field", "kind", "reason", ...}`
    dicts.
    """

    natural_key: str
    record_type: Literal["project", "call"]
    domain: str
    programme: str | None = None
    country_scope: str | None = None
    title: str
    description: str | None = None
    status: str | None = None
    funding_amount: float | None = None
    start_date: str | None = None
    deadline_date: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    source_url: str
    source_updated_at: str | None = None
    field_sources: dict[str, str] = Field(default_factory=dict)
    data_gaps: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any]


_HASH_EXCLUDE = {"raw", "field_sources", "data_gaps"}
# Annotations derived from the values (with run timestamps), not content.
_DETAILS_EXCLUDE = ("sources", "verification")


def record_hash(record: NormalizedRecord) -> str:
    """sha256 over the record's normalised content (excludes raw/provenance/gaps/annotations)."""
    payload = record.model_dump(exclude=_HASH_EXCLUDE)
    payload["details"] = {k: v for k, v in payload.get("details", {}).items() if k not in _DETAILS_EXCLUDE}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Failure:
    """
    One item that could not be processed. Nothing is dropped silently: the store keeps
    every failure (dead-letter table) with its raw payload so it can be retried or re-read.

    `stage`: fetch | normalize | detail | document | enrich | load | embed.
    `key`: what failed (a natural key, `programme:page3`, a URL).
    """

    stage: str
    key: str
    error: str
    payload: Any = None
    retryable: bool = True

    @classmethod
    def from_exception(cls, stage: str, key: str, exc: BaseException, payload: Any = None) -> Failure:
        from external_sources.resilience import redact
        return cls(stage, key, f"{type(exc).__name__}: {redact(str(exc))[:1000]}", payload)


@dataclass
class FetchOutcome:
    """
    What a plugin run produced: the records it could build, every failure, and why the
    result is incomplete (`partial_reasons`). `complete` is False when part of the source
    could not be read (a page, a programme, the whole API): absent records must then not be
    treated as evidence that a call disappeared.
    """

    records: list[NormalizedRecord]
    failures: list[Failure] = field(default_factory=list)
    partial_reasons: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.partial_reasons


def _never() -> bool:
    return False


class SourcePlugin(ABC):
    """
    Abstract contract every extraction plugin must implement.

    A plugin is responsible for one source. `fetch()` is the only required
    behaviour: go get the data, normalize it, return it. Plugins do not
    write to any storage themselves — that's the caller's job (currently
    `store/runner.py`). A plugin with
    a declarative field spec exposes it here so the audit can check its output;
    plugins without one are skipped.

    Resilient plugins also fill `self.failures` (items that could not be read or
    normalized) and `self.partial_reasons` during `fetch()` instead of raising, and
    check `self.stop_requested()` between units of work (SIGTERM: finish the current
    item). `collect()` wraps any plugin, resilient or not, into a `FetchOutcome`.
    """

    name: str
    field_spec: FieldSpec | None = None
    stop_requested: Callable[[], bool] = staticmethod(_never)

    @abstractmethod
    def fetch(self) -> list[NormalizedRecord]:
        """Fetch and normalize records from this plugin's source."""
        raise NotImplementedError

    def collect(self) -> FetchOutcome:
        """Run `fetch()` and report records, failures and partial reasons; never raises."""
        self.failures: list[Failure] = []
        self.partial_reasons: list[str] = []
        try:
            records = self.fetch()
        except Exception as exc:  # noqa: BLE001 — the source failed as a whole; report it
            failure = Failure.from_exception("fetch", self.name, exc)
            return FetchOutcome([], [*self.failures, failure],
                                [*self.partial_reasons, f"fetch failed: {failure.error[:200]}"])
        return FetchOutcome(list(records), list(self.failures), list(self.partial_reasons))


class MissingAPIKeyError(RuntimeError):
    """Raised when a plugin needs an API key that isn't set in the environment."""
