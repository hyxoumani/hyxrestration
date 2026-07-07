"""Numbered migrations for the hyxlab store.

    python -m hyxlab.migrate [--db data/hyxlab.duckdb]

Migration 1 — legacy timestamps to naive UTC. Before store._naive_utc
landed (2026-07-06), DuckDB silently converted tz-aware inserts to the
box's local time (America/Chicago on the dev box). Data written by the
old code is uniformly local-naive; this migration reinterprets it as
America/Chicago and rewrites it as naive UTC in a single atomic UPDATE
per column (DuckDB ICU `timezone()` — DST-aware; a per-row Python loop
would risk double-shifting values that collide after the shift).
Fresh databases are created at the current version and never migrate.
"""

from __future__ import annotations

import argparse

from hyxlab.store import SCHEMA_VERSION, Store

LEGACY_TZ = "America/Chicago"

_M1_COLUMNS = [
    ("markets", "close_time"),
    ("markets", "updated_at"),
    ("snapshots", "ts"),
    ("nws_forecasts", "fetched_at"),
    ("candles", "end_ts"),
]


def migration_1(store: Store) -> None:
    for table, col in _M1_COLUMNS:
        n = store.conn.execute(f"SELECT count(*) FROM {table} WHERE {col} IS NOT NULL").fetchone()[
            0
        ]
        if not n:
            continue
        # timezone(tz, TIMESTAMP)   -> TIMESTAMPTZ (interpret naive in tz)
        # timezone(tz, TIMESTAMPTZ) -> TIMESTAMP   (instant to naive in tz)
        store.conn.execute(
            f"UPDATE {table} SET {col} = timezone('UTC', timezone('{LEGACY_TZ}', {col}))"
            f" WHERE {col} IS NOT NULL"
        )
        print(f"[migrate] 1: {table}.{col}: rewrote {n} rows {LEGACY_TZ}->UTC")


MIGRATIONS = {1: migration_1}


def migrate(store: Store) -> int:
    v = store.schema_version()
    while v < SCHEMA_VERSION:
        v += 1
        print(f"[migrate] applying migration {v}")
        MIGRATIONS[v](store)
        store.set_schema_version(v)
    return v


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab store migrations")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    args = ap.parse_args()
    store = Store(args.db)
    v = migrate(store)
    print(f"[migrate] schema at version {v}; counts={store.counts()}")
    store.close()


if __name__ == "__main__":
    main()
