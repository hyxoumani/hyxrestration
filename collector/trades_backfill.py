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
from datetime import UTC, datetime
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
#: 429 backoff: first wait, cap, and attempts per market. Kalshi's public
#: read limit is a short per-second bucket -- probed 2026-10-08 during the
#: sweep: 9 back-to-back requests pass, the 10th is refused, and 3s later
#: six at 2 rps all pass. The old flat 30s sleep then SKIPPED the market:
#: from 10-06, with the daily sweep drawing ~1,500 429s/h on the same host,
#: the 10-08 run slept ~167 of its 210 minutes (335 x 30s), drained 4,035
#: of 38,931, and the backlog grew 259 -> 25,298 -> 38,931 in three days.
#: Now a 429 retries the SAME market, doubling from 2s to the old 30s cap;
#: a success resets the streak, so a sustained storm degrades to the old
#: pacing and a transient one costs seconds.
BACKOFF_429_S = 2.0
BACKOFF_429_CAP_S = 30.0
ATTEMPTS_429 = 5


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


#: Pending markets the ARCHIVED hourly candles prove never traded -- the
#: retro-pass twin of `sweep.tape_provably_empty`. Same two clauses (every
#: candle zero-volume, last candle ending at or after the close) plus one
#: the archive needs and the live API does not: `candle_row` stores an
#: absent `volume_fp` as 0.0, so a stored zero cannot tell "no trades" from
#: "field renamed". `live` demands that some market's candle on the close's
#: own UTC day carried volume, so a rename (every candle zero from then on)
#: proves nothing and every tape fetches. Measured on the 10-08 22:30
#: backup: 43,213 of 51,755 pending (83%) qualify, and across 3,380,000
#: already-fetched tapes the predicate marked empty, NONE had a trade.
PROVEN_EMPTY_SQL = """
WITH pend AS (
  SELECT m.market_id, m.close_time FROM markets m
  LEFT JOIN trades_swept s ON s.market_id = m.market_id
  WHERE m.venue = 'kalshi' AND m.result != '' AND s.market_id IS NULL),
cov AS (
  SELECT c.market_id, bool_and(c.volume = 0) AS all_zero, max(c.end_ts) AS last_end
  FROM candles c JOIN pend ON pend.market_id = c.market_id
  WHERE c.venue = 'kalshi' AND c.period_s = 3600 GROUP BY 1),
live AS (
  SELECT DISTINCT CAST(end_ts AS DATE) AS d FROM candles
  WHERE venue = 'kalshi' AND period_s = 3600 AND volume > 0
    AND end_ts >= (SELECT min(close_time) FROM pend) - INTERVAL 1 DAY)
SELECT pend.market_id, ?::TIMESTAMP, 0, 'candle_empty'
FROM pend JOIN cov ON cov.market_id = pend.market_id
WHERE cov.all_zero IS TRUE AND cov.last_end >= pend.close_time
  AND CAST(pend.close_time AS DATE) IN (SELECT d FROM live)
"""


def mark_proven_empty(db: str) -> int:
    """Mark every pending tape the archived candles prove empty, without a
    request, as 'candle_empty' (the sweep's status for the same proof).

    One statement inside one writer burst: the proof and the mark cannot
    disagree, and it ran in 0.13-0.22s on the 24 GB backup. Without it the
    pass spent its rate-limited budget fetching ~83% empty tapes."""
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        note_holder(LOCK_FILE)
        store = open_retry(db)
        try:
            row = store.conn.execute(
                f"INSERT OR REPLACE INTO trades_swept {PROVEN_EMPTY_SQL}",
                # naive UTC like mark_trades_swept; current_timestamp would
                # cast through the session zone (the host runs CDT)
                [datetime.now(UTC).replace(tzinfo=None)],
            ).fetchone()
        finally:
            store.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
    return int(row[0]) if row else 0


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
    # `wlock`, never `lock`: rebinding the instance lock's name dropped its
    # last reference, CPython closed it, and the flock was gone before the
    # first fetch (mistakes #107).
    with open(LOCK_FILE, "a") as wlock:
        fcntl.flock(wlock, fcntl.LOCK_EX)
        note_holder(LOCK_FILE)
        open_retry(args.db).close()
        fcntl.flock(wlock, fcntl.LOCK_UN)
    skipped = mark_proven_empty(args.db)
    print(f"[tradepass] {skipped} pending tapes candle-proven empty; marked, not fetched", flush=True)
    targets = pending_markets(args.db)
    if args.limit:
        targets = targets[: args.limit]
    print(f"[tradepass] {len(targets)} settled markets pending, oldest first", flush=True)

    sess = requests.Session()
    batch: list[tuple[str, list[tuple], str]] = []
    totals = {"markets": 0, "trades": 0, "empty": 0, "errors": 0, "rate_limited": 0}
    t0 = time.monotonic()
    backoff = BACKOFF_429_S
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
            for attempt in range(ATTEMPTS_429):
                try:
                    raw, truncated = kalshi.get_trades(ticker, session=sess)
                    backoff = BACKOFF_429_S
                    break
                except requests.HTTPError as exc:
                    resp = exc.response
                    if resp is None or resp.status_code != 429 or attempt == ATTEMPTS_429 - 1:
                        raise
                    totals["rate_limited"] += 1
                    print(
                        f"[tradepass] HTTP 429 at {ticker}; retrying in {backoff:g}s",
                        flush=True,
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_429_CAP_S)
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
            if code == 429:
                totals["rate_limited"] += 1
                wait = backoff
                backoff = min(backoff * 2, BACKOFF_429_CAP_S)
            else:
                wait = 5
            print(f"[tradepass] HTTP {code} at {ticker}; backing off {wait:g}s", flush=True)
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
                f" {totals['empty']} empty, {totals['errors']} errors,"
                f" {totals['rate_limited']} x 429 | ~{eta_h:.1f}h left",
                flush=True,
            )
        elapsed = time.monotonic() - t_req
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    if batch:
        _flush(args.db, batch)
    totals["candle_empty"] = skipped
    totals["elapsed_min"] = round((time.monotonic() - t0) / 60, 1)
    lock.close()  # a crash releases it too: flock dies with the process
    print(f"[tradepass] done: {totals}", flush=True)


if __name__ == "__main__":
    main()
