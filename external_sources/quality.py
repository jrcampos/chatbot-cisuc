"""
Quality policy, required-field gaps and alert notifiers for enriched records.

The policy (config/quality_policy.yaml) names the required fields per source; `compute_gaps`
records what could not be filled and why. The store loader applies the coverage gates
(store/loader.py); alerts inform people (log file, optional webhook) and NEVER block
extraction or drop records.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
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


def source_of(record: NormalizedRecord, field: str) -> str:
    """Provenance label of a field: api, detail, document or unknown."""
    label = record.field_sources.get(field)
    return label.split(":", 1)[0] if label else "unknown"


def is_filled_any(record: NormalizedRecord, fields: list[str]) -> bool:
    """True when at least one of `fields` carries information."""
    return any(is_filled(field_value(record, name)) for name in fields)


def source_of_any(record: NormalizedRecord, fields: list[str]) -> str:
    """Provenance label of the first filled member of `fields`."""
    for name in fields:
        if is_filled(field_value(record, name)):
            return source_of(record, name)
    return "unknown"


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


class WebhookNotifier:
    """Posts alerts as a JSON `{"text": ...}` message (Slack/Discord-compatible shape)."""

    def __init__(self, url: str, session: Any = None, timeout: float = 15.0) -> None:
        self.url = url
        self.session = session or requests.Session()
        self.timeout = timeout

    def notify(self, alerts: list[dict[str, Any]], plugin: str) -> bool:
        """POST one summary message; returns False (never raises) on failure."""
        if not alerts:
            return True
        try:
            response = self.session.post(
                self.url, json={"text": "\n".join(_lines(alerts, plugin))}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            print(f"[WARN] alert webhook failed: {exc}")
            return False
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and status >= 400:
            print(f"[WARN] alert webhook returned HTTP {status}")
            return False
        return True


class CompositeNotifier:
    """Fans an alert list out to several notifiers."""

    def __init__(self, notifiers: list[Any]) -> None:
        self.notifiers = notifiers

    def notify(self, alerts: list[dict[str, Any]], plugin: str) -> bool:
        """True only when every notifier succeeded."""
        return all([n.notify(alerts, plugin) for n in self.notifiers])


def notifier_from_env(output_dir: str | Path, env: Mapping[str, str] | None = None) -> Any:
    """Log notifier, plus a webhook when `ALERT_WEBHOOK_URL` is set (never in code/config)."""
    import os

    environment = os.environ if env is None else env
    log = LogNotifier(Path(output_dir) / "alerts.log")
    url = environment.get("ALERT_WEBHOOK_URL")
    return CompositeNotifier([log, WebhookNotifier(url)]) if url else log
