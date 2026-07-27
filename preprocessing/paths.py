import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

LOCAL_ROOT = Path(
    os.getenv("CISUC_LOCAL_ROOT", PROJECT_ROOT / ".local")
).expanduser().resolve()

RAW_DIR = LOCAL_ROOT / "raw"
ENRICHED_DIR = LOCAL_ROOT / "enriched"
CHROMA_DIR = LOCAL_ROOT / "chroma"
LOGS_DIR = LOCAL_ROOT / "logs"


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


def ensure_local_directories() -> None:
    for directory in (RAW_DIR, ENRICHED_DIR, CHROMA_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
