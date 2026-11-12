"""
Quality policy, required-field gaps and the alert log for enriched records.

The policy (config/quality_policy.yaml) names the required fields per source; `compute_gaps`
records what could not be filled and why. The store loader applies the coverage gates
(store/loader.py); alerts go to alerts.log and NEVER block extraction or drop records.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from external_sources.plugins.base import NormalizedRecord

DEFAULT_POLICY_PATH = Path(__file__).parent / "config" / "quality_policy.yaml"


def load_quality_policy(plugin: str, path: str | Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """Load `plugin`'s policy; raises KeyError for an unknown plugin."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if plugin not in data:
        raise KeyError(f"No quality policy for plugin {plugin!r} in {path}")
    policy = data[plugin]
    if not isinstance(policy.get("required_fields"), list):
        raise ValueError(f"{path}: {plugin}.required_fields must be a list")
    return policy


def field_value(record: NormalizedRecord, field: str) -> Any:
    """Value of a top-level field or `details.<name>`."""
    if field.startswith("details."):
        return record.details.get(field[len("details."):])
    return getattr(record, field, None)


def is_filled(value: Any) -> bool:
    """True when `value` carries information (0 and False count as information)."""
    return not (value is None or value == "" or value == [] or value == {})




def is_filled_any(record: NormalizedRecord, fields: list[str]) -> bool:
    """True when at least one of `fields` carries information."""
    return any(is_filled(field_value(record, name)) for name in fields)




def compute_gaps(
    record: NormalizedRecord,
    required: list[str],
    tried: list[str],
    reason: str,
    required_any: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """
    One `missing_required` entry per required field that is still empty, and one per
    `required_any` label (e.g. "budget") none of whose member fields is filled.
    """
    gaps = [
        {"field": name, "kind": "missing_required", "tried": list(tried), "reason": reason}
        for name in required
        if not is_filled(field_value(record, name))
    ]
    for label, members in (required_any or {}).items():
        if not is_filled_any(record, members):
            gaps.append(
                {"field": label, "kind": "missing_required", "tried": list(tried),
                 "reason": reason, "any_of": list(members)}
            )
    return gaps


def _lines(alerts: list[dict[str, Any]], plugin: str) -> list[str]:
    return [f"[ALERT:{a['level']}] {plugin}: {a['message']}" for a in alerts]


class LogNotifier:
    """Prints alerts and optionally appends them to a log file."""

    def __init__(self, log_path: str | Path | None = None) -> None:
        self.log_path = Path(log_path) if log_path else None

    def notify(self, alerts: list[dict[str, Any]], plugin: str) -> bool:
        """Print (and log) every alert; never raises."""
        if not alerts:
            return True
        lines = _lines(alerts, plugin)
        for line in lines:
            print(line)
        if self.log_path:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(UTC).isoformat()
                with self.log_path.open("a", encoding="utf-8") as f:
                    for alert, line in zip(alerts, lines):
                        f.write(f"{stamp} {alert['code']} {line}\n")
            except OSError as exc:
                print(f"[WARN] could not write alert log: {exc}")
                return False
        return True


def alert_notifier(output_dir: str | Path) -> LogNotifier:
    """Alerts are printed and appended to `<output_dir>/alerts.log`."""
    return LogNotifier(Path(output_dir) / "alerts.log")
