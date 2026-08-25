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
import fcntl

from hyxlab.lockid import note_holder
from hyxlab.store import SCHEMA_VERSION, Store, open_retry

LEGACY_TZ = "America/Chicago"
LOCK_FILE = "data/writer.lock"

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


def migration_2(store: Store) -> None:
    """poly_market_stats.ts was written straight from an aware-UTC datetime,
    which DuckDB converts to the box's local time on insert — the exact
    convention migration_1 exists to undo. The table postdates that
    migration, so it kept the bug while every other writer moved to
    _naive_utc: its rows sat LEGACY_TZ behind the rest of the archive, and
    any window comparing them against another table was silently skewed."""
    n = store.conn.execute("SELECT count(*) FROM poly_market_stats").fetchone()[0]
    if not n:
        return
    store.conn.execute(
        f"UPDATE poly_market_stats SET ts = timezone('UTC', timezone('{LEGACY_TZ}', ts))"
    )
    print(f"[migrate] 2: poly_market_stats.ts: rewrote {n} rows {LEGACY_TZ}->UTC")


MIGRATIONS = {1: migration_1, 2: migration_2}


def migrate(store: Store) -> int:
    v = store.schema_version()
    while v < SCHEMA_VERSION:
        v += 1
        print(f"[migrate] applying migration {v}")
        MIGRATIONS[v](store)
        store.set_schema_version(v)
    return v


def main() -> None:
    """Migrate under `data/writer.lock`, like every other archive writer.

    EXP-1370: this opened the live archive read-write with no flock, so a
    concurrent `collect` cycle won the advisory lock and then collided on
    DuckDB's file lock — a dropped capture cycle with no honest skip
    record. A migration is the most exclusive write in the repo; it is
    the last one that should have been taking the lock on trust.

    Raw flock rather than `collector.sweep.writer_burst`: `hyxlab` is the
    kernel and may import nothing above it (tests/test_boundaries.py).
    `hyxlab.lockid` moved here for the same reason — the lock guards the
    ARCHIVE, which is kernel-owned, so its witness belongs in the kernel.
    The shape below is `collector.signals`'s, line for line.
    """
    ap = argparse.ArgumentParser(description="hyxlab store migrations")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--lock-file", default=LOCK_FILE)
    args = ap.parse_args()
    with open(args.lock_file, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        note_holder(args.lock_file)
        store = open_retry(args.db)
        try:
            v = migrate(store)
            print(f"[migrate] schema at version {v}; counts={store.counts()}")
        finally:
            store.close()
            fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
