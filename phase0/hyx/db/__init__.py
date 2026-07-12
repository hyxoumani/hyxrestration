"""DuckDB connection helpers. One writer at a time — see architecture.md §2.1."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open the DuckDB file, creating parent dirs if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


@contextmanager
def connection(db_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Context-managed DuckDB connection."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
