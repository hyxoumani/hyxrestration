"""Snapshot collector CLI.

Each cycle:
- Kalshi: for every watchlist series, pull open markets (metadata upsert +
  top-of-book snapshot) and recently settled markets (results, so the sim
  can settle positions).
- Polymarket: batch-fetch CLOB books for configured token pairs.
- NWS: pull the 7-day forecast per station; every pull is stored with
  fetched_at for no-lookahead replay.

Run:
    python -m collector.collect --once
    python -m collector.collect --interval 300
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from collector.venues import kalshi, nws, polymarket
from hyxlab.store import Store, open_retry
from hyxlab.watchlist import DEFAULT_WATCHLIST, load_watchlist

__all__ = [
    "DEFAULT_WATCHLIST",
    "LOCK_WAIT_S",
    "SKIP_LOG",
    "acquire_writer_lock",
    "collect_once",
    "load_watchlist",
    "main",
    "record_skip",
]

LOCK_FILE = "data/writer.lock"
SKIP_LOG = "data/collect_skips.jsonl"
# Bounded well under the 300s timer period: a cycle that waits longer than
# this would still be running when the next one fires, so runs would stack.
LOCK_WAIT_S = 240.0


def acquire_writer_lock(lock_file: str | None = None, wait_s: float | None = None):
    """Exclusive flock, waiting up to `wait_s`; None on timeout.

    2026-08-02: this wait used to be `flock -n` in the unit file, so a
    cycle that found the lock held was DROPPED — and dropped before
    python started (3ms CPU), which is why nothing in the archive ever
    recorded it. A dropped cycle is an unrecoverable hole in the 5-min
    tape; the collector cannot backfill a snapshot it never took. Waiting
    is almost always right here, because every other writer touches the
    DB in short bursts (poly_sweep, trades_backfill, signals, and since
    this change collector.sweep too).

    `lock_file`/`wait_s` resolve at CALL time: a default argument binds
    the module constant at definition and silently ignores a patched
    value, which is how the first version of the test blocked on the
    live production lock.
    """
    lock_file = lock_file or LOCK_FILE
    wait_s = LOCK_WAIT_S if wait_s is None else wait_s
    Path(lock_file).parent.mkdir(exist_ok=True)
    f = open(lock_file, "a")  # noqa: SIM115 — handle must outlive this call
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def record_skip(reason: str, waited_s: float, path: str | None = None) -> None:
    """Append a skipped cycle to the sidecar journal.

    A skip cannot be recorded in the archive — the archive is precisely
    what could not be opened — so it goes to a file beside it. QA reads
    this (`collector cycles are not being skipped`) and fails on a rate,
    because the 07-20..08-02 outage was invisible for 14 days exactly
    because a skip left no trace an archive-reading instrument could see.
    """
    path = path or SKIP_LOG
    Path(path).parent.mkdir(exist_ok=True)
    rec = {
        "at": datetime.now(UTC).isoformat(),
        "reason": reason,
        "waited_s": round(waited_s, 1),
    }
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def collect_once(store: Store, watchlist: dict, session: requests.Session | None = None) -> dict:
    sess = session or requests.Session()
    counts = {"kalshi_snaps": 0, "kalshi_markets": 0, "poly_snaps": 0, "forecasts": 0, "errors": 0}
    ts = datetime.now(UTC)

    for series in watchlist.get("kalshi_series", []):
        try:
            open_markets = kalshi.get_markets(series_ticker=series, status="open", session=sess)
            settled = kalshi.get_markets(
                series_ticker=series, status="settled", max_pages=1, session=sess
            )
            infos = [kalshi.to_market_info(m) for m in open_markets + settled]
            snaps = [kalshi.to_snapshot(m, ts) for m in open_markets]
            store.upsert_markets(infos)
            store.insert_snapshots(snaps)
            counts["kalshi_markets"] += len(infos)
            counts["kalshi_snaps"] += len(snaps)
        except Exception as e:  # isolation: one bad source must not kill the cycle
            counts["errors"] += 1
            print(f"[collect] kalshi {series}: {type(e).__name__}: {e}")

    pairs = watchlist.get("polymarket_pairs", [])
    if pairs:
        try:
            tokens = [t for _, yes_t, no_t in pairs for t in (yes_t, no_t)]
            books = polymarket.get_books(tokens, session=sess)
            snaps = [
                polymarket.pair_snapshot(mid, books.get(yes_t), books.get(no_t), ts)
                for mid, yes_t, no_t in pairs
            ]
            store.insert_snapshots(snaps)
            counts["poly_snaps"] += len(snaps)
        except Exception as e:  # Gamma serves error objects with HTTP 200
            counts["errors"] += 1
            print(f"[collect] polymarket books: {type(e).__name__}: {e}")

    for station in watchlist.get("nws_stations", []):
        try:
            fcs = nws.get_daily_highs(station, session=sess)
            store.insert_forecasts(fcs)
            counts["forecasts"] += len(fcs)
        except Exception as e:
            counts["errors"] += 1
            print(f"[collect] nws {station}: {type(e).__name__}: {e}")

    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab market-data collector")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    ap.add_argument("--interval", type=int, default=300, help="seconds between cycles")
    ap.add_argument("--once", action="store_true", help="one cycle, then exit")
    ap.add_argument(
        "--lock-wait",
        type=float,
        default=LOCK_WAIT_S,
        help="seconds to wait for the archive writer lock before skipping the cycle",
    )
    args = ap.parse_args()

    t0 = time.monotonic()
    lock = acquire_writer_lock(wait_s=args.lock_wait)
    if lock is None:
        waited = time.monotonic() - t0
        record_skip("writer lock held", waited)
        # Nonzero so systemd records it, AND a durable record so an
        # instrument that never sees systemd can still count the hole.
        print(f"[collect] skipped: writer lock held for {waited:.0f}s")
        sys.exit(75)  # EX_TEMPFAIL

    watchlist = load_watchlist(args.watchlist)
    sess = requests.Session()
    store = open_retry(args.db, retries=5)
    try:
        while True:
            counts = collect_once(store, watchlist, session=sess)
            print(f"[collect] {datetime.now(UTC).isoformat()} {counts} db={store.counts()}")
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        store.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
