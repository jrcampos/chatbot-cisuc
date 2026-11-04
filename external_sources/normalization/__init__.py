"""Normalization helpers: transforms, declarative field specs, de-duplication."""

from external_sources.normalization.dedupe import dedupe_by_natural_key
from external_sources.normalization.spec import (
    TOP_LEVEL_TARGETS,
    FieldSpec,
    Rule,
    SpecError,
    Step,
    apply_spec,
    derive_status,
    finalizer,
    is_valid_target,
    load_spec,
)

__all__ = [
    "TOP_LEVEL_TARGETS",
    "FieldSpec",
    "Rule",
    "SpecError",
    "Step",
    "apply_spec",
    "dedupe_by_natural_key",
    "derive_status",
    "finalizer",
    "is_valid_target",
    "load_spec",
]
