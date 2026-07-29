import os
from pathlib import Path


workspace = os.environ.get("WORKSPACE")
assert workspace, "WORKSPACE must be defined"

WORKSPACE = Path(workspace).expanduser().resolve()

RAW_DIR = WORKSPACE / "raw"
ENRICHED_DIR = WORKSPACE / "enriched"
CHROMA_DIR = WORKSPACE / "chroma"
LOGS_DIR = WORKSPACE / "logs"


def resolve_raw_path(relative_path: str | Path) -> Path:
    path = Path(relative_path)

    if path.is_absolute():
        return path

    return RAW_DIR / path


def resolve_log_path(relative_path: str | Path) -> Path:
    path = Path(relative_path)

    if path.is_absolute():
        return path

    return LOGS_DIR / path


def ensure_workspace_directories() -> None:
    for directory in (
        RAW_DIR,
        ENRICHED_DIR,
        CHROMA_DIR,
        LOGS_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)