"""Trade-tape retro-pass (B3.5): pull public trade prints for every
settled market already in the archive, BEFORE Kalshi's ~64-day retention
purges them (probed 2026-07-07: markets closed ≤2026-05-01 are already
gone — the boundary advances daily, so this runs oldest-first).

    python -m hyxlab.trades_backfill [--db ...] [--rps 2] [--limit N]

Resumable and idempotent: per-market progress in trades_swept (purged
markets recorded as status='empty' so they aren't refetched), trade rows
dedup'd on trade_id. Plays nice with the 5-min collector: REST fetching
happens without any lock; the DB is touched in short flock-guarded
open→write→close bursts every FLUSH_MARKETS markets.
"""

from __future__ import annotations

import argparse
import fcntl
import time
from pathlib import Path

import requests

from hyxlab.store import Store
from hyxlab.venues import kalshi

FLUSH_MARKETS = 50
LOCK_FILE = "data/writer.lock"


def _flush(db: str, batch: list[tuple[str, list[tuple], str]]) -> int:
    """batch = [(market_id, rows, status)]; returns trades inserted."""
    inserted = 0
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = Store(db)
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
    store = Store(db, read_only=True)
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
    args = ap.parse_args()

    Path(LOCK_FILE).parent.mkdir(exist_ok=True)
    # Brief write-open under flock so the new trades tables exist before
    # the read-only pending query (read-only connects skip schema DDL).
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        Store(args.db).close()
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
        t_req = time.monotonic()
        try:
            raw = kalshi.get_trades(ticker, session=sess)
            rows = [kalshi.trade_row(t) for t in raw]
            # Only successes get marked in trades_swept — errored markets
            # stay pending so the next run retries them.
            batch.append((ticker, rows, "ok" if rows else "empty"))
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
    print(f"[tradepass] done: {totals}", flush=True)


if __name__ == "__main__":
    main()
