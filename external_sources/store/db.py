"""
Postgres connections with timeouts and bounded connect retries (psycopg 3, imported lazily so
the rest of the package and its unit tests work without the driver installed).

Every session runs in UTC with a statement timeout; reader sessions are also read-only
(`default_transaction_read_only`), on top of the read-only database role.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from external_sources.resilience import RetryPolicy, retry_call
from external_sources.store.settings import DBSettings

logger = logging.getLogger(__name__)


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached after the configured retries."""


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DatabaseUnavailable(
            "psycopg is not installed (pip install -r external_sources/requirements.txt)") from exc
    return psycopg


def is_connection_error(exc: BaseException) -> bool:
    """True for errors that mean 'the server is unreachable or the connection dropped'."""
    try:
        psycopg = _psycopg()
    except DatabaseUnavailable:
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


def connect(
    settings: DBSettings,
    read_only: bool = False,
    attempts: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """
    Open a connection or raise DatabaseUnavailable after `attempts` tries. Connections are in
    autocommit mode: every multi-statement unit of work uses an explicit `conn.transaction()`
    block, so nothing is left in an implicit, never-committed transaction.
    """
    psycopg = _psycopg()
    options = f"-c statement_timeout={int(settings.statement_timeout_ms)} -c TimeZone=UTC " \
              "-c idle_in_transaction_session_timeout=600000"
    if read_only:
        options += " -c default_transaction_read_only=on"

    def attempt() -> Any:
        return psycopg.connect(
            host=settings.host, port=settings.port, dbname=settings.dbname, user=settings.user,
            password=settings.password, connect_timeout=settings.connect_timeout_s,
            application_name=settings.application_name, options=options, autocommit=True,
        )

    try:
        return retry_call(attempt, RetryPolicy(max_attempts=attempts, base_delay=2.0, max_delay=30.0),
                          is_retryable=lambda e: isinstance(e, psycopg.OperationalError), sleep=sleep,
                          describe=f"connect {settings.describe()}")
    except psycopg.OperationalError as exc:
        # The message may contain the host, never the password; keep only the first line.
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise DatabaseUnavailable(f"cannot connect to {settings.describe()}: {first_line}") from exc
