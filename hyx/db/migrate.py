"""Schema migration runner.

Reads hyx/db/migrations/NNN_*.sql in numeric order, applies any not yet recorded
in schema_migrations. Idempotent — rerunning is a no-op. Per architecture.md §3.2,
each slice owns its DDL additions; migrations are append-only and never edited
once applied.
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_FILENAME_RE = re.compile(r"^(\d{3,})_([a-z0-9_]+)\.sql$")


def _discover(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, str, Path]]:
    """Return sorted (id, name, path) for all migration files."""
    out: list[tuple[int, str, Path]] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _FILENAME_RE.match(path.name)
        if not m:
            raise RuntimeError(f"migration filename {path.name!r} does not match NNN_name.sql")
        out.append((int(m.group(1)), m.group(2), path))
    # Duplicate-ID check
    ids = [mid for mid, _, _ in out]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"duplicate migration ids in {migrations_dir}")
    return out


def _ensure_tracker(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _applied_ids(conn: duckdb.DuckDBPyConnection) -> set[int]:
    rows = conn.execute("SELECT id FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def migrate(conn: duckdb.DuckDBPyConnection, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply any unapplied migrations. Returns ids actually applied this run."""
    _ensure_tracker(conn)
    applied = _applied_ids(conn)
    applied_this_run: list[int] = []

    for mid, name, path in _discover(migrations_dir):
        if mid in applied:
            continue
        sql = path.read_text()
        # DuckDB's Python API wraps each execute() in its own statement group, so
        # a multi-statement migration is fine as a single execute() call.
        conn.execute("BEGIN")
        try:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (id, name) VALUES (?, ?)",
                [mid, name],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied_this_run.append(mid)

    return applied_this_run


if __name__ == "__main__":
    from hyx.config import Config
    from hyx.db import connection

    cfg = Config.load(require_alpaca=False)
    with connection(cfg.db_path) as conn:
        applied = migrate(conn)
        if applied:
            print(f"applied migrations: {applied}")
        else:
            print("schema up to date")
