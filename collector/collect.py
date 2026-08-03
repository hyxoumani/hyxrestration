"""Snapshot collector CLI.

Each cycle:
- Kalshi: for every watchlist series, pull open markets (metadata upsert +
  top-of-book snapshot) and recently settled markets (results, so the sim
  can settle positions).
- Polymarket: batch-fetch CLOB books for configured token pairs.
- NWS: pull the 7-day forecast per station; every pull is stored with
  fetched_at for no-lookahead replay.

A cycle is FETCH -> acquire writer lock -> WRITE -> release (EXP-957).
The HTTP half never runs under the lock; see `fetch_cycle`.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import requests

from collector.lockid import note_holder, read_holder
from collector.venues import kalshi, nws, polymarket
from hyxlab.models import Forecast, MarketInfo, Snapshot
from hyxlab.store import Store, open_retry
from hyxlab.watchlist import DEFAULT_WATCHLIST, load_watchlist

__all__ = [
    "DEFAULT_WATCHLIST",
    "LOCK_WAIT_S",
    "SKIP_LOG",
    "Cycle",
    "acquire_writer_lock",
    "collect_once",
    "fetch_cycle",
    "load_watchlist",
    "main",
    "record_skip",
    "write_cycle",
]

LOCK_FILE = "data/writer.lock"
SKIP_LOG = "data/collect_skips.jsonl"
# Bounded well under the 300s timer period: a cycle that waits longer than
# this would still be running when the next one fires, so runs would stack.
# Since EXP-957 the fetch runs BEFORE the wait, so `main` spends this as a
# whole-cycle budget (fetch elapsed is subtracted from the wait) rather
# than as a wait that starts from zero after ~29s of HTTP.
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
            note_holder(lock_file)
            return f
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def record_skip(
    reason: str, waited_s: float, path: str | None = None, holder: dict | None = None
) -> None:
    """Append a skipped cycle to the sidecar journal.

    A skip cannot be recorded in the archive — the archive is precisely
    what could not be opened — so it goes to a file beside it. QA reads
    this (`collector cycles are not being skipped`) and fails on a rate,
    because the 07-20..08-02 outage was invisible for 14 days exactly
    because a skip left no trace an archive-reading instrument could see.

    EXP-944: `holder` names the blocker (see `collector.lockid`). The
    2026-08-03 12:54/12:59/13:04Z skips recorded the WAIT but not the
    WAITED-ON, so a 15-minute tape hole was attributable only by
    inference. A count of holes tells you the tape is damaged; only the
    holder tells you what to fix. `None` is written through rather than
    omitted — "nobody recorded a holder" is itself a finding, and a
    missing key would read as an older-format row instead.
    """
    path = path or SKIP_LOG
    Path(path).parent.mkdir(exist_ok=True)
    rec = {
        "at": datetime.now(UTC).isoformat(),
        "reason": reason,
        "waited_s": round(waited_s, 1),
        "holder": holder,
    }
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


@dataclass
class Cycle:
    """One cycle's worth of fetched rows, buffered for a single write burst.

    Held in memory between `fetch_cycle` and `write_cycle`. MEASURED
    (EXP-957): a full production cycle (5,913 market infos + 670
    snapshots + 35 forecasts) buffers to **1.8 MB** — 0.7% of the
    collector's 240.8M peak RSS, because the raw API dicts are converted
    to models per series and dropped, so only the models survive the
    fetch. The buffer is not the cost; the lock was.
    """

    ts: datetime
    infos: list[MarketInfo] = field(default_factory=list)
    kalshi_snaps: list[Snapshot] = field(default_factory=list)
    poly_snaps: list[Snapshot] = field(default_factory=list)
    forecasts: list[Forecast] = field(default_factory=list)
    errors: int = 0


def fetch_cycle(watchlist: dict, session: requests.Session | None = None) -> Cycle:
    """All HTTP for one cycle. **Must run with the writer lock FREE.**

    EXP-957: `collect` used to take `data/writer.lock` in `main()` and
    hold it across every fetch of the cycle — the lock is needed for the
    WRITE, never for the FETCH. Measured on 2026-08-03 against the live
    31-series/5-station watchlist: the fetch half costs **28.9 s** (28.6 s
    Kalshi over 62 paginated calls, 0.25 s NWS, 0.01 s model conversion)
    of a cycle whose whole hold was ~52 s. Those seconds are rival with
    every other archive writer, and during 23:00-04:00Z they are rival
    with the weather collection that feeds live trading.

    Per-source isolation is unchanged and stays HERE: a bad series or a
    Gamma error object costs its own rows, not the cycle. What moved is
    only WHEN the rows are written.
    """
    sess = session or requests.Session()
    cyc = Cycle(ts=datetime.now(UTC))

    for series in watchlist.get("kalshi_series", []):
        try:
            open_markets = kalshi.get_markets(series_ticker=series, status="open", session=sess)
            settled = kalshi.get_markets(
                series_ticker=series, status="settled", max_pages=1, session=sess
            )
            cyc.infos.extend(kalshi.to_market_info(m) for m in open_markets + settled)
            cyc.kalshi_snaps.extend(kalshi.to_snapshot(m, cyc.ts) for m in open_markets)
        except Exception as e:  # isolation: one bad source must not kill the cycle
            cyc.errors += 1
            print(f"[collect] kalshi {series}: {type(e).__name__}: {e}")

    pairs = watchlist.get("polymarket_pairs", [])
    if pairs:
        try:
            tokens = [t for _, yes_t, no_t in pairs for t in (yes_t, no_t)]
            books = polymarket.get_books(tokens, session=sess)
            cyc.poly_snaps.extend(
                polymarket.pair_snapshot(mid, books.get(yes_t), books.get(no_t), cyc.ts)
                for mid, yes_t, no_t in pairs
            )
        except Exception as e:  # Gamma serves error objects with HTTP 200
            cyc.errors += 1
            print(f"[collect] polymarket books: {type(e).__name__}: {e}")

    for station in watchlist.get("nws_stations", []):
        try:
            cyc.forecasts.extend(nws.get_daily_highs(station, session=sess))
        except Exception as e:
            cyc.errors += 1
            print(f"[collect] nws {station}: {type(e).__name__}: {e}")

    return cyc


def write_cycle(store: Store, cyc: Cycle) -> dict:
    """Persist a fetched cycle in ONE transaction. Lock-held section.

    Atomic on purpose: buffering the whole cycle turns 31+ independently
    committed statements into one burst, and a crash between two of them
    would leave a cycle half in the archive — a hole that reads like data.
    Losing a whole cycle is acceptable (the tape already tolerates a
    skip, and `record_skip` counts it); corrupting one is not. On any
    failure the transaction is rolled back and the cycle is reported as
    an error rather than half-written.

    MEASURED (EXP-957, production-scale scratch DB: 2.85M snapshots,
    324k markets, 260k forecasts): this half costs **15.8 s** — 15.3 s of
    it `upsert_markets` (5,913 INSERT OR REPLACE against the markets PK).
    That is the irreducible hold; the other 28.9 s used to be lock time
    for no reason.
    """
    counts = {
        "kalshi_snaps": 0,
        "kalshi_markets": 0,
        "poly_snaps": 0,
        "forecasts": 0,
        "errors": cyc.errors,
    }
    store.conn.execute("BEGIN TRANSACTION")
    try:
        store.upsert_markets(cyc.infos)
        store.insert_snapshots(cyc.kalshi_snaps)
        store.insert_snapshots(cyc.poly_snaps)
        store.insert_forecasts(cyc.forecasts)
        store.conn.execute("COMMIT")
    except Exception as e:
        store.conn.execute("ROLLBACK")
        counts["errors"] += 1
        print(f"[collect] write rolled back: {type(e).__name__}: {e}")
        return counts
    counts["kalshi_markets"] = len(cyc.infos)
    counts["kalshi_snaps"] = len(cyc.kalshi_snaps)
    counts["poly_snaps"] = len(cyc.poly_snaps)
    counts["forecasts"] = len(cyc.forecasts)
    return counts


def collect_once(store: Store, watchlist: dict, session: requests.Session | None = None) -> dict:
    """Fetch then write, against an ALREADY-OPEN store.

    Kept for callers that manage their own store. `main()` deliberately
    does NOT use it: holding an open `Store` spans the DuckDB file lock
    across the fetch, which is the same starvation in a different lock.
    """
    return write_cycle(store, fetch_cycle(watchlist, session=session))


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

    watchlist = load_watchlist(args.watchlist)
    sess = requests.Session()
    while True:
        t0 = time.monotonic()
        cyc = fetch_cycle(watchlist, session=sess)  # NO lock held here (EXP-957)
        # The lock budget is a budget for the CYCLE, not for the wait: the
        # fetch now runs first, and a wait of the full LOCK_WAIT_S on top
        # of it could still be running when the next 5-min firing arrives.
        wait_s = max(0.0, args.lock_wait - (time.monotonic() - t0))
        lock = acquire_writer_lock(wait_s=wait_s)
        if lock is None:
            waited = time.monotonic() - t0
            holder = read_holder(LOCK_FILE)
            record_skip("writer lock held", waited, holder=holder)
            # Nonzero so systemd records it, AND a durable record so an
            # instrument that never sees systemd can still count the hole.
            who = (
                f"{holder.get('unit') or 'no unit'} pid={holder['pid']}"
                f" since {holder.get('at')}{'' if holder.get('alive') else ' (DEAD/stale record)'}"
                if holder
                else "holder unrecorded"
            )
            print(f"[collect] skipped: writer lock held for {waited:.0f}s by {who}")
            if args.once:
                sys.exit(75)  # EX_TEMPFAIL
            time.sleep(args.interval)
            continue

        store = open_retry(args.db, retries=5)
        try:
            counts = write_cycle(store, cyc)
            print(f"[collect] {datetime.now(UTC).isoformat()} {counts} db={store.counts()}")
        finally:
            store.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
