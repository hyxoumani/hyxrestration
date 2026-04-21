"""Migration runner tests."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from hyx.db.migrate import MIGRATIONS_DIR, _discover, migrate


def test_discover_returns_sorted_unique_ids():
    migrations = _discover()
    ids = [m[0] for m in migrations]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    # Slice 1 baseline migration must exist
    assert 1 in ids


def test_migrate_applies_fresh_schema():
    conn = duckdb.connect(":memory:")
    applied = migrate(conn)
    assert 1 in applied

    # Every slice 1 table must be present
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }
    expected = {
        "schema_migrations",
        "ohlcv_daily",
        "news",
        "news_tickers",
        "news_sentiment",
        "fetch_state",
        "audit_log",
    }
    assert expected.issubset(tables)


def test_migrate_is_idempotent():
    conn = duckdb.connect(":memory:")
    first = migrate(conn)
    second = migrate(conn)
    assert first == [1]
    assert second == []


def test_migrate_rejects_bad_filename(tmp_path: Path):
    bad = tmp_path / "abc.sql"
    bad.write_text("SELECT 1;")
    conn = duckdb.connect(":memory:")
    with pytest.raises(RuntimeError, match="does not match"):
        migrate(conn, migrations_dir=tmp_path)


def test_migrate_rejects_duplicate_ids(tmp_path: Path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (x INT);")
    (tmp_path / "001_b.sql").write_text("CREATE TABLE b (x INT);")
    conn = duckdb.connect(":memory:")
    with pytest.raises(RuntimeError, match="duplicate migration"):
        migrate(conn, migrations_dir=tmp_path)


def test_migrations_dir_exists():
    assert MIGRATIONS_DIR.is_dir()
