"""
Tiny migration runner: plain `NNNN_name.sql` files applied in order, each in its own
transaction, recorded in `extsrc.schema_migrations` with a checksum. An applied migration whose
file changed is an error (write a new migration instead). A transaction-level advisory lock
serialises concurrent runners.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LOCK_KEY = 0x45585453  # 'EXTS'


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise MigrationError(f"bad migration file name: {path.name} (expected NNNN_name.sql)")
        migrations.append(Migration(match.group(1), match.group(2), path.read_text(encoding="utf-8")))
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration versions in {directory}")
    return migrations


BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS extsrc;
CREATE TABLE IF NOT EXISTS extsrc.schema_migrations (
    version     text PRIMARY KEY,
    name        text NOT NULL,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def applied(conn: Any) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('extsrc.schema_migrations')")
        if cur.fetchone()[0] is None:
            return {}
        cur.execute("SELECT version, checksum FROM extsrc.schema_migrations ORDER BY version")
        return {row[0]: row[1] for row in cur.fetchall()}


def migrate(conn: Any, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations; returns the versions applied now."""
    migrations = discover(directory)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        cur.execute(BOOTSTRAP)
    done = applied(conn)
    for migration in migrations:
        if migration.version in done and done[migration.version] != migration.checksum:
            raise MigrationError(
                f"migration {migration.version}_{migration.name} changed after it was applied; "
                "add a new migration instead")
    newly: list[str] = []
    for migration in migrations:
        if migration.version in done:
            continue
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                cur.execute("SELECT 1 FROM extsrc.schema_migrations WHERE version = %s", (migration.version,))
                if cur.fetchone():
                    continue  # applied concurrently
                cur.execute(migration.sql)
                cur.execute(
                    "INSERT INTO extsrc.schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
        newly.append(migration.version)
    return newly


