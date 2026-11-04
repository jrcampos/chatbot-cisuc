"""
Declarative field specs.

A spec (YAML, one per plugin) says what to do with every raw key a source
sends: either `{ignore: "<reason>"}` or a list of rules
`{to, parse: [steps], applies_to?}`. `to` is a normalized top-level field or
`details.<name>`. Steps are transform names (or `{name: {kwargs}}`) from
normalization.transforms. Top-level keys: `source`, `anchors` (free space for
YAML anchors), `not_applicable` ({field: [record_types]} for fields no raw key
feeds), `finalizers` ([names] run after all rules), `transform_modules`
([dotted module paths] imported before the steps are validated, so a spec
brings its own source-specific transforms), `fields`.

Adding a programme means writing a plugin plus one spec — the engine, the
transforms and the audit stay untouched.
"""

from __future__ import annotations

import copy
import importlib
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from external_sources.normalization.transforms import (
    get_transform,
    is_date_producer,
    registered_transforms,
)

TOP_LEVEL_TARGETS: tuple[str, ...] = (
    "natural_key",
    "title",
    "description",
    "status",
    "funding_amount",
    "start_date",
    "deadline_date",
    "source_updated_at",
    "source_url",
)
_DETAILS_PREFIX = "details."
_DETAILS_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_ALLOWED_TOP_KEYS = {
    "source",
    "anchors",
    "not_applicable",
    "finalizers",
    "fields",
    "transform_modules",
}
_ALLOWED_RULE_KEYS = {"to", "parse", "applies_to", "may_be_empty"}

Finalizer = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], None]
_FINALIZERS: dict[str, Finalizer] = {}


class SpecError(ValueError):
    """Raised when a field spec is malformed."""


@dataclass(frozen=True)
class Step:
    """One transform application: a registered name plus keyword arguments."""

    name: str
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class Rule:
    """How one raw key feeds one target."""

    raw_key: str
    to: str
    steps: tuple[Step, ...]
    applies_to: tuple[str, ...] | None
    may_be_empty: bool = False

    @property
    def produces_date(self) -> bool:
        """True when any step is a date-producing transform."""
        return any(is_date_producer(step.name) for step in self.steps)


@dataclass(frozen=True)
class FieldSpec:
    """A loaded, validated field spec."""

    source: str
    rules: dict[str, tuple[Rule, ...]]
    ignored: dict[str, str]
    not_applicable: dict[str, tuple[str, ...]]
    finalizers: tuple[str, ...]

    def declared_keys(self) -> set[str]:
        """Every raw key the spec knows about (mapped or ignored)."""
        return set(self.rules) | set(self.ignored)

    def rules_by_target(self) -> dict[str, tuple[Rule, ...]]:
        """Index rules by target (each target has exactly one rule)."""
        index: dict[str, list[Rule]] = {}
        for rules in self.rules.values():
            for rule in rules:
                index.setdefault(rule.to, []).append(rule)
        return {target: tuple(rules) for target, rules in index.items()}


def finalizer(name: str) -> Callable[[Finalizer], Finalizer]:
    """Register a finalizer `fn(fields, details, ctx)` under `name`."""

    def decorator(fn: Finalizer) -> Finalizer:
        if name in _FINALIZERS:
            raise ValueError(f"Finalizer {name!r} is already registered")
        _FINALIZERS[name] = fn
        return fn

    return decorator


@finalizer("derive_status")
def derive_status(
    fields: dict[str, Any], details: dict[str, Any], ctx: dict[str, Any]
) -> None:
    """
    Make `status` trustworthy: a call the source still flags open/forthcoming
    but whose deadline has passed becomes "closed". (The source's own label
    should be kept by a separate rule, e.g. details.source_status.)
    """
    status = fields.get("status")
    deadline = fields.get("deadline_date")
    if status in ("open", "forthcoming") and deadline:
        try:
            deadline_dt = datetime.fromisoformat(deadline)
            # Treat naive datetime as UTC
            if deadline_dt.tzinfo is None:
                deadline_dt = deadline_dt.replace(tzinfo=UTC)

            now = ctx.get("now")
            if now is None:
                now = datetime.now(UTC)
            # Treat naive now as UTC
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)

            if deadline_dt < now:
                fields["status"] = "closed"
        except (ValueError, TypeError):
            # If we can't parse the deadline, leave status untouched
            pass


def is_valid_target(target: Any) -> bool:
    """A target is a known top-level field or `details.<snake_case_name>`."""
    if not isinstance(target, str):
        return False
    if target in TOP_LEVEL_TARGETS:
        return True
    return target.startswith(_DETAILS_PREFIX) and bool(
        _DETAILS_NAME.match(target[len(_DETAILS_PREFIX):])
    )


class _DuplicateKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate keys while preserving anchors/aliases."""



def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[str, Any]:
    """Construct a mapping, rejecting duplicate keys."""
    loader.flatten_mapping(node)
    pairs = []
    seen: set[Any] = set()
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if key in seen:
            raise SpecError(f"duplicate key {key!r} in mapping")
        seen.add(key)
        value = loader.construct_object(value_node, deep=False)
        pairs.append((key, value))
    return dict(pairs)


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _parse_step(raw_step: Any, where: str) -> Step:
    if isinstance(raw_step, str):
        name, kwargs = raw_step, {}
    elif isinstance(raw_step, dict) and len(raw_step) == 1:
        ((name, args),) = raw_step.items()
        if args is None:
            kwargs = {}
        elif isinstance(args, dict):
            kwargs = dict(args)
        else:
            raise SpecError(f"{where}: arguments of step {name!r} must be a mapping")
    else:
        raise SpecError(f"{where}: a step must be a name or a single-key mapping")
    if name not in registered_transforms():
        raise SpecError(f"{where}: unknown transform {name!r}")

    # Validate kwargs match transform signature
    try:
        transform = get_transform(name)
        sig = inspect.signature(transform)
        sig.bind(None, {}, **kwargs)
    except TypeError as e:
        raise SpecError(
            f"{where}: step {name!r} invalid arguments: {e}"
        ) from None

    # Additional validation for 'map' step: all mapping keys must be str
    if name == "map" and "mapping" in kwargs:
        mapping = kwargs["mapping"]
        if not isinstance(mapping, dict):
            raise SpecError(f"{where}: step {name!r} mapping must be a dict")
        if not all(isinstance(k, str) for k in mapping.keys()):
            raise SpecError(
                f"{where}: step {name!r} mapping keys must be strings (quote the codes)"
            )

    return Step(name=name, kwargs=kwargs)


def _parse_rule(raw_key: str, raw_rule: Any, where: str) -> Rule:
    if not isinstance(raw_rule, dict) or "to" not in raw_rule:
        raise SpecError(f"{where}: each rule needs a 'to' target")
    unknown = set(raw_rule) - _ALLOWED_RULE_KEYS
    if unknown:
        raise SpecError(f"{where}: unknown rule keys {sorted(unknown)}")
    target = raw_rule["to"]
    if not is_valid_target(target):
        raise SpecError(f"{where}: invalid target {target!r}")

    # Validate parse is a list if present
    parse_value = raw_rule.get("parse")
    if parse_value is not None and not isinstance(parse_value, list):
        raise SpecError(f"{where}: 'parse' must be a list, not {type(parse_value).__name__}")

    steps = tuple(_parse_step(step, where) for step in parse_value or ())

    # Date targets need a date-producing step (e.g. iso_datetime)
    date_targets = {"start_date", "deadline_date", "source_updated_at"}
    if target in date_targets and not any(is_date_producer(step.name) for step in steps):
        raise SpecError(
            f"{where}: date field {target!r} must include a date-producing step "
            "(such as 'iso_datetime') in its parse chain"
        )

    applies_to = raw_rule.get("applies_to")
    if applies_to is not None:
        if not (
            isinstance(applies_to, list)
            and applies_to
            and all(isinstance(a, str) for a in applies_to)
        ):
            raise SpecError(f"{where}: applies_to must be a non-empty list of strings")
        applies_to = tuple(applies_to)

    may_be_empty = raw_rule.get("may_be_empty", False)
    if not isinstance(may_be_empty, bool):
        raise SpecError(f"{where}: 'may_be_empty' must be a boolean")
    return Rule(
        raw_key=raw_key,
        to=target,
        steps=steps,
        applies_to=applies_to,
        may_be_empty=may_be_empty,
    )


def load_spec(path: str | Path) -> FieldSpec:
    """Load and validate a field spec YAML; raises SpecError when malformed."""
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.load(f, Loader=_DuplicateKeyLoader)
    except FileNotFoundError as e:
        raise SpecError(f"spec file not found: {path}") from e
    except yaml.YAMLError as e:
        raise SpecError(f"{path}: invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise SpecError(f"{path}: top level must be a mapping")
    unknown = set(data) - _ALLOWED_TOP_KEYS
    if unknown:
        raise SpecError(f"{path}: unknown top-level keys {sorted(unknown)}")

    # Import plugin transform modules first so their @transform registrations
    # exist when the steps below are validated.
    modules = data.get("transform_modules")
    if modules is not None:
        if not (isinstance(modules, list) and all(isinstance(m, str) for m in modules)):
            raise SpecError(f"{path}: 'transform_modules' must be a list of module paths")
        for module_name in modules:
            try:
                importlib.import_module(module_name)
            except ImportError as e:
                raise SpecError(
                    f"{path}: cannot import transform module {module_name!r}: {e}"
                ) from e

    # Validate source is str if present
    source_value = data.get("source")
    if source_value is not None and not isinstance(source_value, str):
        raise SpecError(f"{path}: 'source' must be a string")

    fields = data.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise SpecError(f"{path}: 'fields' must be a non-empty mapping")

    # Validate all field keys are strings
    for key in fields.keys():
        if not isinstance(key, str):
            raise SpecError(f"{path}: all field keys must be strings, got {type(key).__name__}")

    rules: dict[str, tuple[Rule, ...]] = {}
    ignored: dict[str, str] = {}
    fed_by: dict[str, str] = {}
    for raw_key, entry in fields.items():
        where = f"{path}: field {raw_key!r}"
        if isinstance(entry, dict):
            reason = entry.get("ignore")
            if set(entry) != {"ignore"} or not isinstance(reason, str) or not reason.strip():
                raise SpecError(
                    f"{where}: an ignore entry must be exactly {{ignore: '<non-empty reason>'}}"
                )
            ignored[raw_key] = reason.strip()
            continue
        if not isinstance(entry, list) or not entry:
            raise SpecError(f"{where}: must be an ignore entry or a non-empty list of rules")
        parsed: list[Rule] = []
        for raw_rule in entry:
            rule = _parse_rule(raw_key, raw_rule, where)
            if rule.to in fed_by:
                raise SpecError(
                    f"{where}: target {rule.to!r} is already fed by raw key {fed_by[rule.to]!r}"
                )
            fed_by[rule.to] = raw_key
            parsed.append(rule)
        rules[raw_key] = tuple(parsed)

    # Validate not_applicable
    not_applicable_raw = data.get("not_applicable")
    if not_applicable_raw is not None and not isinstance(not_applicable_raw, dict):
        raise SpecError(f"{path}: 'not_applicable' must be a mapping")

    not_applicable: dict[str, tuple[str, ...]] = {}
    for target, record_types in (not_applicable_raw or {}).items():
        if not is_valid_target(target):
            raise SpecError(f"{path}: not_applicable has invalid target {target!r}")
        if target in fed_by:
            raise SpecError(
                f"{path}: not_applicable target {target!r} is also fed by field {fed_by[target]!r}"
            )
        if not (isinstance(record_types, list) and record_types):
            raise SpecError(f"{path}: not_applicable[{target!r}] must be a non-empty list")
        not_applicable[target] = tuple(str(r) for r in record_types)

    # Validate finalizers
    finalizers_raw = data.get("finalizers")
    if finalizers_raw is not None:
        if not isinstance(finalizers_raw, list):
            raise SpecError(f"{path}: 'finalizers' must be a list")
        if not all(isinstance(name, str) for name in finalizers_raw):
            raise SpecError(f"{path}: all finalizer names must be strings")
        # Check for duplicates
        if len(finalizers_raw) != len(set(finalizers_raw)):
            raise SpecError(f"{path}: duplicate finalizers not allowed")
        for name in finalizers_raw:
            if name not in _FINALIZERS:
                raise SpecError(f"{path}: unknown finalizer {name!r}")
        finalizers = tuple(finalizers_raw)
    else:
        finalizers = ()

    return FieldSpec(
        source=str(source_value or path.stem),
        rules=rules,
        ignored=ignored,
        not_applicable=not_applicable,
        finalizers=finalizers,
    )


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _run_steps(value: Any, steps: tuple[Step, ...], ctx: dict[str, Any]) -> Any:
    """Chain transforms; stop with None as soon as the value becomes empty."""
    for step in steps:
        if _is_empty(value):
            return None
        value = get_transform(step.name)(value, ctx, **step.kwargs)
    return None if _is_empty(value) else value


def apply_spec(spec: FieldSpec, raw: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Apply `spec` to one raw payload.

    `ctx` must hold `record_type`; `now` (aware datetime) defaults to the
    current UTC time. Returns {"fields": {...top-level...}, "details": {...}};
    targets whose value is empty are omitted.
    """
    ctx = dict(ctx)
    # Set now when missing or None
    if "now" not in ctx or ctx["now"] is None:
        ctx["now"] = datetime.now(UTC)

    fields: dict[str, Any] = {}
    details: dict[str, Any] = {}
    for raw_key, rules in spec.rules.items():
        if raw_key not in raw:
            continue
        for rule in rules:
            if rule.applies_to is not None and ctx["record_type"] not in rule.applies_to:
                continue
            # Deep-copy raw value to prevent aliasing
            value = copy.deepcopy(raw[raw_key])
            value = _run_steps(value, rule.steps, ctx)
            if value is None:
                continue
            if rule.to.startswith(_DETAILS_PREFIX):
                details[rule.to[len(_DETAILS_PREFIX):]] = value
            else:
                fields[rule.to] = value
    for name in spec.finalizers:
        _FINALIZERS[name](fields, details, ctx)
    return {"fields": fields, "details": details}
