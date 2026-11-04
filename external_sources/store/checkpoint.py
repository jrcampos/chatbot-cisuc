"""
Run directories: checkpoints, resume and the local spool.

Layout under `<work>/runs/<run_id>/`:

    run.json                 run state (status, started_at, sources, pid)
    <source>/state.json      stage: collecting | fetched | enriching | pending_load | done | failed
    <source>/fetched.json    plugin outcome (records, failures, partial reasons)   [checkpoint 1]
    <source>/enriched.jsonl  one enriched record per line, appended + fsynced     [checkpoint 2]
    <source>/batch.json      the LoadBatch ready for the database                 [spool]

A crashed or killed run resumes from the last checkpoint: fetched.json skips the network
fetch, enriched.jsonl skips records already enriched, and a batch.json in `pending_load`
(database unreachable) is loaded by the next run. Every write is atomic (temp + rename) or an
fsynced append; a disk-full error disables checkpointing for the rest of the run (logged and
reported) instead of crashing it.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TERMINAL = ("done", "failed")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class CheckpointWriter:
    """Writes checkpoints; on ENOSPC/EROFS it degrades to 'no checkpoint' and remembers why."""

    def __init__(self) -> None:
        self.disabled_reason: str | None = None

    def _guard(self, action: str, fn: Any, *args: Any) -> bool:
        if self.disabled_reason:
            return False
        try:
            fn(*args)
            return True
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EROFS, errno.EDQUOT):
                self.disabled_reason = f"{action}: {exc.strerror or exc} (checkpointing disabled for this run)"
                logger.error("checkpoint write failed: %s", self.disabled_reason)
                return False
            raise

    def write_json(self, path: Path, data: Any) -> bool:
        return self._guard(f"write {path.name}", atomic_write_json, path, data)

    def append_line(self, path: Path, data: Any) -> bool:
        def append() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return self._guard(f"append {path.name}", append)


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Valid JSON object lines; a torn last line (kill -9 mid-write) is skipped."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                logger.warning("%s: skipped a torn/corrupt checkpoint line", path.name)
                continue
            if isinstance(item, dict):
                yield item


@dataclass
class RunDir:
    root: Path
    run_id: str

    @property
    def path(self) -> Path:
        return self.root / "runs" / self.run_id

    def source_dir(self, source: str) -> Path:
        return self.path / source

    def state(self) -> dict[str, Any]:
        return read_json(self.path / "run.json") or {}

    def source_state(self, source: str) -> dict[str, Any]:
        return read_json(self.source_dir(source) / "state.json") or {}

    def set_state(self, writer: CheckpointWriter, **update: Any) -> None:
        writer.write_json(self.path / "run.json", {**self.state(), **update, "updated_at": _now()})

    def set_source_state(self, writer: CheckpointWriter, source: str, stage: str, **extra: Any) -> None:
        state = {**self.source_state(source), **extra, "stage": stage, "updated_at": _now()}
        writer.write_json(self.source_dir(source) / "state.json", state)


def list_runs(root: Path) -> list[RunDir]:
    runs_dir = root / "runs"
    if not runs_dir.exists():
        return []
    runs = []
    for path in runs_dir.iterdir():
        if path.is_dir() and (path / "run.json").exists():
            runs.append(RunDir(root, path.name))
    return sorted(runs, key=lambda r: str(r.state().get("started_at") or ""))


def resumable_run(root: Path, max_age_hours: float) -> RunDir | None:
    """The newest unfinished run younger than `max_age_hours`; older unfinished runs are abandoned."""
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    candidate = None
    for run in list_runs(root):
        state = run.state()
        if state.get("status") not in ("running", "interrupted"):
            continue
        try:
            started = datetime.fromisoformat(str(state.get("started_at")))
        except ValueError:
            continue
        if started < cutoff:
            run.set_state(CheckpointWriter(), status="abandoned")
            logger.warning("run %s is older than %sh: abandoned (its pending batches still load)",
                           run.run_id, max_age_hours)
            continue
        candidate = run
    return candidate


def pending_batches(root: Path, exclude_run: str | None = None) -> list[tuple[RunDir, str, Path]]:
    """(run, source, batch path) of every spooled batch waiting for the database, oldest first."""
    out = []
    for run in list_runs(root):
        if run.run_id == exclude_run:
            continue
        for source_dir in sorted(p for p in run.path.iterdir() if p.is_dir()):
            state = run.source_state(source_dir.name)
            batch = source_dir / "batch.json"
            if state.get("stage") == "pending_load" and batch.exists():
                out.append((run, source_dir.name, batch))
    return out


def prune_run_dirs(root: Path, keep_days: float) -> int:
    """Delete finished run directories older than `keep_days` (never ones with pending batches)."""
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    removed = 0
    for run in list_runs(root):
        state = run.state()
        if state.get("status") in ("running", "interrupted"):
            continue
        if any(run.source_state(p.name).get("stage") == "pending_load"
               for p in run.path.iterdir() if p.is_dir()):
            continue
        try:
            started = datetime.fromisoformat(str(state.get("started_at")))
        except ValueError:
            continue
        if started < cutoff:
            shutil.rmtree(run.path, ignore_errors=True)
            removed += 1
    return removed
