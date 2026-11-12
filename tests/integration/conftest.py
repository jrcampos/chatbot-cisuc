"""
Fixtures for integration tests against a REAL Postgres + pgvector.

A throwaway container (`pgvector/pgvector:pg16`, random localhost port, random password) is
started once per session with `docker run` (argument list, shell=False) and removed afterwards.
Every test gets freshly migrated schemas. Everything is skipped cleanly when psycopg, docker or
the image is not available. The image is never pulled by the tests (pull it once by hand).
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import pytest

IMAGE = "pgvector/pgvector:pg16"


def _docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, shell=False,
                          check=False)


@pytest.fixture(scope="session")
def pg_server() -> Iterator[dict[str, Any]]:
    pytest.importorskip("psycopg")
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    try:
        if _docker("image", "inspect", IMAGE).returncode != 0:
            pytest.skip(f"{IMAGE} not present (docker pull {IMAGE})")
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("docker is not usable")
    password = secrets.token_urlsafe(16)
    started = _docker("run", "-d", "--rm", "-e", f"POSTGRES_PASSWORD={password}", "-e", "POSTGRES_USER=extsrc_test",
                      "-e", "POSTGRES_DB=extsrc_test", "-p", "127.0.0.1::5432", IMAGE)
    if started.returncode != 0:
        pytest.skip(f"cannot start {IMAGE}: {started.stderr.strip()[:200]}")
    container = started.stdout.strip()
    try:
        port = None
        for _ in range(50):
            mapped = _docker("port", container, "5432/tcp").stdout.strip().splitlines()
            if mapped:
                port = int(mapped[0].rsplit(":", 1)[1])
                break
            time.sleep(0.2)
        assert port, "no port mapping"
        import psycopg
        import psycopg.sql
        deadline = time.time() + 90
        while True:
            try:
                with psycopg.connect(host="127.0.0.1", port=port, user="extsrc_test", password=password,
                                     dbname="extsrc_test", connect_timeout=3) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        reader_password = secrets.token_urlsafe(16)
        with psycopg.connect(host="127.0.0.1", port=port, user="extsrc_test", password=password,
                             dbname="extsrc_test", autocommit=True) as conn:
            conn.execute("CREATE ROLE extsrc_reader NOLOGIN")
            conn.execute(psycopg.sql.SQL("CREATE ROLE extsrc_chatbot LOGIN PASSWORD {} IN ROLE extsrc_reader")
                         .format(psycopg.sql.Literal(reader_password)))
        env = {"EXTSRC_DB_HOST": "127.0.0.1", "EXTSRC_DB_PORT": str(port), "EXTSRC_DB_NAME": "extsrc_test",
               "EXTSRC_DB_USER": "extsrc_test", "EXTSRC_DB_PASSWORD": password,
               "EXTSRC_DB_READER_USER": "extsrc_chatbot", "EXTSRC_DB_READER_PASSWORD": reader_password,
               "EXTSRC_DB_LOAD_TIMEOUT_MS": "600000", "EXTSRC_DB_READER_STATEMENT_TIMEOUT_MS": "5000"}
        yield {"container": container, "port": port, "env": env}
    finally:
        _docker("stop", container, timeout=120)


@pytest.fixture
def db(pg_server: dict[str, Any]) -> Iterator[Any]:
    """A loader connection on freshly migrated schemas."""
    from external_sources.store.db import connect
    from external_sources.store.migrate import migrate
    from external_sources.store.settings import DBSettings

    conn = connect(DBSettings.from_env("loader", pg_server["env"]))
    conn.execute("DROP SCHEMA IF EXISTS current CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS extsrc CASCADE")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def reader(pg_server: dict[str, Any], db: Any) -> Iterator[Any]:
    from external_sources.store.db import connect
    from external_sources.store.settings import DBSettings

    conn = connect(DBSettings.from_env("reader", pg_server["env"]), read_only=True)
    yield conn
    conn.close()
