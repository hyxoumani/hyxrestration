"""Trade-tape retro-pass (B3.5): pull public trade prints for every
settled market already in the archive, BEFORE Kalshi's ~64-day retention
purges them (probed 2026-07-07: markets closed ≤2026-05-01 are already
gone — the boundary advances daily, so this runs oldest-first).

    python -m collector.trades_backfill [--db ...] [--rps 2] [--limit N]

Resumable and idempotent: per-market progress in trades_swept (purged
markets recorded as status='empty' so they aren't refetched), trade rows
dedup'd on trade_id. Plays nice with the 5-min collector: REST fetching
happens without any lock; the DB is touched in short flock-guarded
open→write→close bursts every FLUSH_MARKETS markets.
"""

from __future__ import annotations

import argparse
import fcntl
import sys
import time
from pathlib import Path

import requests

from collector.venues import kalshi
from hyxlab.lockid import instance_lock_or_reason, note_holder
from hyxlab.store import open_retry

FLUSH_MARKETS = 50
LOCK_FILE = "data/writer.lock"
#: Wall-clock deadline (minutes). The worklist is unbounded — a sweep that
#: opens a new asset class can queue tens of thousands of tapes overnight
#: (2026-08-03: the first crypto pass turned a 5-minute run into 15h06m,
#: 3.69h of it inside the 23:00Z fade window). The pass is resumable per
#: market via trades_swept and ordered oldest-close-first, so stopping at
#: the deadline costs nothing but calendar days; QA's BATCH_RUN_BUDGET_H
#: (4.0h) stays true by construction. 0 disables (manual full drains).
DEADLINE_MIN = 210.0


def _flush(db: str, batch: list[tuple[str, list[tuple], str]]) -> int:
    """batch = [(market_id, rows, status)]; returns trades inserted."""
    inserted = 0
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        note_holder(LOCK_FILE)
        # open_retry, not Store: the flock excludes other WRITERS, but DuckDB
        # also refuses a read-write open while any read-only holder (QA,
        # doctor, simui — none of which flock) is attached. A bare open here
        # killed the 2026-08-04 run at 26,000/42,978 markets.
        store = open_retry(db)
        try:
            for market_id, rows, status in batch:
                inserted += store.insert_trades(rows)
                store.mark_trades_swept(market_id, len(rows), status)
        finally:
            store.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
    return inserted


def pending_markets(db: str) -> list[str]:
    """Settled markets without a trades sweep, oldest close first (the
    retention clock eats oldest-settled markets first)."""
    # open_retry, not a bare Store, and for the mirror-image reason to
    # _flush's: this read runs OUTSIDE the flock, so a writer (poly_sweep
    # holds the archive for ~7h) can take the file in the gap after the
    # schema burst above closes. DuckDB refuses a read-only open against
    # a read-write holder, and a bare open here kills the whole pass at
    # its first statement.
    store = open_retry(db, read_only=True)
    try:
        rows = store.conn.execute(
            "SELECT m.market_id FROM markets m"
            " LEFT JOIN trades_swept s ON s.market_id = m.market_id"
            " WHERE m.venue = 'kalshi' AND m.result != '' AND s.market_id IS NULL"
            " ORDER BY m.close_time ASC"
        ).fetchall()
    finally:
        store.close()
    return [r[0] for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab trade-tape retro-pass")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--rps", type=float, default=2.0, help="request pacing")
    ap.add_argument("--limit", type=int, default=None, help="max markets (smoke tests)")
    ap.add_argument(
        "--deadline-min",
        type=float,
        default=DEADLINE_MIN,
        help="stop cleanly after this many minutes; 0 disables",
    )
    args = ap.parse_args()

    Path(LOCK_FILE).parent.mkdir(exist_ok=True)
    # Single-INSTANCE guard: the worklist is unbounded (the 08-03 crypto
    # pass ran 15h06m), resumable per market, and paced at --rps. Two
    # copies fetch the same oldest-first tapes twice at twice the rate.
    lock, why = instance_lock_or_reason("trades_backfill")
    if lock is None:
        print(f"[tradepass] {why}; aborting", flush=True)
        sys.exit(75)  # EX_TEMPFAIL — the next timer firing resumes
    # Brief write-open under flock so the new trades tables exist before
    # the read-only pending query (read-only connects skip schema DDL).
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        note_holder(LOCK_FILE)
        open_retry(args.db).close()
        fcntl.flock(lock, fcntl.LOCK_UN)
    targets = pending_markets(args.db)
    if args.limit:
        targets = targets[: args.limit]
    print(f"[tradepass] {len(targets)} settled markets pending, oldest first", flush=True)

    sess = requests.Session()
    batch: list[tuple[str, list[tuple], str]] = []
    totals = {"markets": 0, "trades": 0, "empty": 0, "errors": 0}
    t0 = time.monotonic()
    min_interval = 1.0 / args.rps

    for i, ticker in enumerate(targets):
        if args.deadline_min and time.monotonic() - t0 > args.deadline_min * 60:
            totals["remaining"] = len(targets) - i
            print(
                f"[tradepass] deadline {args.deadline_min:g}min reached at {i}/"
                f"{len(targets)}; {totals['remaining']} markets stay pending for "
                f"the next run",
                flush=True,
            )
            break
        t_req = time.monotonic()
        try:
            raw, truncated = kalshi.get_trades(ticker, session=sess)
            rows = [kalshi.trade_row(t) for t in raw]
            # Only successes get marked in trades_swept — errored markets
            # stay pending so the next run retries them. A page-capped
            # tape is recorded as 'truncated', never 'ok'.
            if truncated:
                print(f"[tradepass] {ticker} tape TRUNCATED at {len(rows)} prints", flush=True)
            status = "truncated" if truncated else ("ok" if rows else "empty")
            batch.append((ticker, rows, status))
            totals["trades"] += len(rows)
            totals["empty"] += 0 if rows else 1
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            wait = 30 if code == 429 else 5
            print(f"[tradepass] HTTP {code} at {ticker}; backing off {wait}s", flush=True)
            totals["errors"] += 1
            time.sleep(wait)
        except Exception as exc:
            totals["errors"] += 1
            print(f"[tradepass] {type(exc).__name__} at {ticker}: {exc}", flush=True)
            time.sleep(5)
        totals["markets"] += 1

        if len(batch) >= FLUSH_MARKETS or i == len(targets) - 1:
            _flush(args.db, batch)
            batch = []
        if (i + 1) % 500 == 0:
            rate = (i + 1) / (time.monotonic() - t0)
            eta_h = (len(targets) - i - 1) / rate / 3600
            print(
                f"[tradepass] {i + 1}/{len(targets)} | {totals['trades']} trades,"
                f" {totals['empty']} empty, {totals['errors']} errors | ~{eta_h:.1f}h left",
                flush=True,
            )
        elapsed = time.monotonic() - t_req
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    if batch:
        _flush(args.db, batch)
    totals["elapsed_min"] = round((time.monotonic() - t0) / 60, 1)
    lock.close()  # a crash releases it too: flock dies with the process
    print(f"[tradepass] done: {totals}", flush=True)


if __name__ == "__main__":
    main()
