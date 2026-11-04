"""
Store configuration from the environment and `config/store_policy.yaml`.

Connection settings come only from environment variables (committed defaults in
`config/chatbot.env`, passwords in the git-ignored `secrets/chatbot.env`).
Passwords are never part of `repr()` or of any log line.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_POLICY_PATH = PACKAGE_DIR / "config" / "store_policy.yaml"
DEFAULT_WORK_DIR = PACKAGE_DIR / "output" / "store"


class ConfigError(RuntimeError):
    """A required setting is missing or invalid (names only, never values)."""


@dataclass(frozen=True)
class DBSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)
    statement_timeout_ms: int = 600_000
    connect_timeout_s: int = 10
    application_name: str = "external_sources"

    @classmethod
    def from_env(cls, role: str = "loader", env: Mapping[str, str] | None = None) -> DBSettings:
        """`role` is 'loader' (read/write) or 'reader' (the chatbot's read-only login)."""
        env = os.environ if env is None else env
        prefix = "EXTSRC_DB_READER_" if role == "reader" else "EXTSRC_DB_"
        names = {"host": "EXTSRC_DB_HOST", "port": "EXTSRC_DB_PORT", "dbname": "EXTSRC_DB_NAME",
                 "user": f"{prefix}USER", "password": f"{prefix}PASSWORD"}
        missing = [name for name in names.values() if not env.get(name)]
        if missing:
            raise ConfigError(
                "missing environment variable(s): " + ", ".join(missing)
                + " (defaults in config/chatbot.env, passwords in secrets/chatbot.env)")
        try:
            port = int(env["EXTSRC_DB_PORT"])
            timeout_name = "EXTSRC_DB_READER_STATEMENT_TIMEOUT_MS" if role == "reader" else "EXTSRC_DB_LOAD_TIMEOUT_MS"
            timeout = int(env.get(timeout_name) or (5_000 if role == "reader" else 600_000))
        except ValueError as exc:
            raise ConfigError(f"EXTSRC_DB_PORT / statement timeout must be integers ({exc.__class__.__name__})") from None
        return cls(env["EXTSRC_DB_HOST"], port, env["EXTSRC_DB_NAME"], env[names["user"]],
                   env[names["password"]], statement_timeout_ms=timeout,
                   application_name=f"external_sources_{role}")

    def describe(self) -> str:
        """host:port/dbname as user — safe to log."""
        return f"{self.user}@{self.host}:{self.port}/{self.dbname}"


def load_store_policy(path: str | Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        policy = yaml.safe_load(f) or {}
    for section in ("validation", "retention", "tracking", "runner"):
        if not isinstance(policy.get(section), dict):
            raise ConfigError(f"{path}: section {section!r} missing")
    return policy


def work_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where run checkpoints, spooled batches, documents and logs live."""
    env = os.environ if env is None else env
    return Path(env.get("EXTSRC_WORK_DIR") or DEFAULT_WORK_DIR)
