"""
Structured logging for store runs: one JSON object per line in `<work>/logs/<run_id>.jsonl`
(run_id and source on every line, via context variables) plus a short human line on stderr.
Secrets never reach a log line: connection settings are logged with `DBSettings.describe()`.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("extsrc_run_id", default=None)
SOURCE: contextvars.ContextVar[str | None] = contextvars.ContextVar("extsrc_source", default=None)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = RUN_ID.get()
        record.source = SOURCE.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", None),
            "source": getattr(record, "source", None),
            "message": record.getMessage(),
        }
        for key in ("event", "count", "status", "stage", "key", "exit_code"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure(log_dir: Path | None, run_id: str | None, level: int = logging.INFO) -> Path | None:
    """Install the JSON file handler (when possible) and a stderr handler; returns the log path."""
    root = logging.getLogger("external_sources")
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_extsrc", False):
            root.removeHandler(handler)
    context = ContextFilter()
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter("[%(levelname)s] %(source)s %(message)s"))
    stderr.addFilter(context)
    stderr._extsrc = True  # type: ignore[attr-defined]
    root.addHandler(stderr)
    path = None
    if log_dir is not None and run_id:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"{run_id}.jsonl"
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(JsonFormatter())
            handler.addFilter(context)
            handler._extsrc = True  # type: ignore[attr-defined]
            root.addHandler(handler)
        except OSError as exc:  # disk full / read-only: keep going with stderr only
            root.warning("JSON log file unavailable (%s): stderr only", exc)
            path = None
    root.propagate = False
    return path
