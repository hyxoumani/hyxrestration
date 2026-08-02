"""Exchange-wide archival sweep (proposal C8, corrected per data_contracts.md).

Enumerates Kalshi series by **category allowlist**, captures settled
markets + hourly candles since each series' watermark, and logs progress
to sweep_log. Idempotent: watermarks + anti-join inserts make re-runs and
crashes safe; a re-run resumes where the last one stopped.

    python -m collector.sweep --days 60            # initial retention capture
    python -m collector.sweep --days 2             # daily incremental
    python -m collector.sweep --days 2 --limit 20  # smoke test

Rationale: Kalshi purges market data ~60-90 days after settlement
(verified 2026-07-06); anything not swept is gone. The allowlist excludes
the ~8,200 sports/entertainment/politics series that dominate settle
volume but are outside our strategy domains — one line to revisit.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import requests

from collector.venues import kalshi
from hyxlab.store import Store, open_retry

LOCK_FILE = "data/writer.lock"

DEFAULT_CATEGORIES = [
    "Economics",
    "Financials",
    "Climate and Weather",
    "Companies",
    "Commodities",
    "Science and Technology",
    "Health",
    "World",
    # Widened 2026-08-02 (breadth audit, owner infrastructure-first
    # directive): Crypto alone is ~29% of exchange notional ($7.7M/wk, 77
    # series) with ZERO archive coverage — uncalibratable forever without
    # settlement+candle capture. Collection is measurement, not trading:
    # CLAUDE.md still marks these families UNCALIBRATED = UNTRADEABLE for
    # the live agent. Sports/Entertainment/Politics stay excluded — that
    # is a standing USER-CONFIRMED decision (2026-07-08), not a default.
    "Crypto",
    "Exotics",
    "Mentions",
]

MARKETS_PAUSE_S = 0.2  # empirical safe pacing (data_contracts.md)
CANDLES_PAUSE_S = 0.35


@contextmanager
def writer_burst(db: str, lock_file: str | None = None):
    """Hold the writer lock + DB connection for ONE short write, then release.

    2026-08-02: this sweep used to run under a unit-level
    `flock data/writer.lock python -m collector.sweep`, holding both the
    advisory lock and the DuckDB file lock for its ENTIRE multi-hour run
    while interleaving REST fetches with inserts. `hyxlab-collect` runs
    `flock -n`, so every 5-min capture cycle that landed in that window
    was DROPPED, not delayed — 421 of 3,706 cycles over the 14 days to
    08-02 (11.4%), clustered in the daily 06:10 sweep window. A dropped
    cycle is an unrecoverable hole in the 5-min tape: unlike a sweep,
    the collector cannot backfill a snapshot it never took.

    This is fix direction (a) of H1 in docs/reviews/2026-07-11-deep-review.md
    ("one rule for everybody: all writers touch the DB only in
    open -> write -> close bursts"), which poly_sweep, trades_backfill and
    signals already follow and this module never did. HTTP happens
    OUTSIDE the lock; the DB is touched once per series for ~ms.

    `lock_file` resolves at CALL time, not as a default argument: a
    default binds the module constant at definition and silently ignores
    a monkeypatched LOCK_FILE, which made the tests block on the live
    production lock.
    """
    lock_file = lock_file or LOCK_FILE
    with open(lock_file, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = open_retry(db)  # readers (QA/doctor/backtest) don't take the flock
        try:
            yield store
        finally:
            store.close()
            fcntl.flock(lock, fcntl.LOCK_UN)


def refresh_series(
    store: Store, session: requests.Session, series: list[dict] | None = None
) -> list[dict]:
    """Persist series metadata, return the full set.

    `series` lets the caller fetch OUTSIDE the writer burst; when omitted
    the fetch happens here, which is the convenient shape for ad-hoc use
    but holds the lock across a REST call.
    """
    if series is None:
        series = kalshi.get_series_list(session)
    store.upsert_series(
        [
            (
                "kalshi",
                s["ticker"],
                s.get("title", ""),
                s.get("category", ""),
                s.get("fee_type", ""),
                s.get("fee_multiplier"),
                s.get("frequency", ""),
            )
            for s in series
        ]
    )
    return series


def sweep_series(
    db: str, series_ticker: str, days: int, session: requests.Session
) -> tuple[int, int]:
    """Capture settled markets + candles for one series since its watermark.

    Every REST call happens with NO lock and NO open connection; all
    writes for the series land in a single `writer_burst` at the end.
    Buffering is what makes the burst short — the fetch loop below runs
    for minutes on a large series, and holding the lock across it is the
    exact defect this refactor removes. Crash-safety is unchanged and
    slightly better: a crash mid-series now leaves the watermark
    unmoved AND nothing half-written, so the re-run is exact.
    """
    now = datetime.now(UTC)
    floor_ts = now - timedelta(days=days)
    with writer_burst(db) as store:
        wm = store.watermark(series_ticker)
    if wm is not None:
        floor_ts = max(floor_ts, wm.replace(tzinfo=UTC) + timedelta(seconds=1))

    markets = kalshi.get_markets(
        series_ticker=series_ticker,
        status="settled",
        max_pages=50,
        session=session,
        min_close_ts=int(floor_ts.timestamp()),
    )
    time.sleep(MARKETS_PAUSE_S)
    if not markets:
        with writer_burst(db) as store:
            store.log_sweep(series_ticker, floor_ts, None, 0, 0, "ok", "no settled markets")
        return 0, 0

    infos = [kalshi.to_market_info(m) for m in markets]
    candle_rows: list[tuple] = []
    trade_rows: list[tuple] = []
    swept: list[tuple[str, int, str]] = []
    max_close = floor_ts
    for m in markets:
        open_ts = _ts(m.get("open_time"))
        close_ts = _ts(m.get("close_time"))
        if open_ts is None or close_ts is None:
            # The watermark may advance past this market via its
            # siblings' closes — without a log line that's a permanent
            # invisible hole in a system built on marking what it missed.
            print(f"[sweep] {m.get('ticker', '?')} skipped: missing open/close time", flush=True)
            continue
        try:
            candles = kalshi.get_candlesticks(
                series_ticker, m["ticker"], open_ts, close_ts, 60, session=session
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                retry_after = e.response.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 5.0)
                candles = kalshi.get_candlesticks(
                    series_ticker, m["ticker"], open_ts, close_ts, 60, session=session
                )
            else:
                raise
        candle_rows.extend(kalshi.candle_row(series_ticker, m, c, 3600) for c in candles)
        # Trade tape rides along (B3.5): prints purge on the same
        # retention clock as candles, so capture them at first sight.
        try:
            raw, truncated = kalshi.get_trades(m["ticker"], session=session)
            rows = [kalshi.trade_row(t) for t in raw]
            trade_rows.extend(rows)
            status = "truncated" if truncated else ("ok" if rows else "empty")
            swept.append((m["ticker"], len(rows), status))
        except requests.HTTPError as e:
            # Stays unmarked here (watermark advances past it regardless,
            # so a later sweep won't retry) — hyxlab-tradepass.timer's daily
            # retro-pass is what actually catches it.
            code = e.response.status_code if e.response is not None else "?"
            print(f"[sweep] {m.get('ticker', '?')} trade tape fetch HTTP {code}", flush=True)
        close_dt = datetime.fromtimestamp(close_ts, tz=UTC)
        max_close = max(max_close, close_dt)
        time.sleep(CANDLES_PAUSE_S)

    with writer_burst(db) as store:
        store.upsert_markets(infos)
        n_candles = store.insert_candles(candle_rows)
        store.insert_trades(trade_rows)
        for ticker, n_trades, status in swept:
            store.mark_trades_swept(ticker, n_trades, status)
        store.set_watermark(series_ticker, max_close)
        store.log_sweep(series_ticker, floor_ts, max_close, len(markets), n_candles, "ok")
    return len(markets), n_candles


def _ts(v: str | None) -> int | None:
    if not v:
        return None
    return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())


def run_sweep(
    db: str,
    days: int,
    categories: list[str],
    session: requests.Session | None = None,
    limit: int | None = None,
) -> dict:
    sess = session or requests.Session()
    series = kalshi.get_series_list(sess)  # HTTP outside the lock
    with writer_burst(db) as store:
        all_series = refresh_series(store, sess, series=series)
    targets = [s["ticker"] for s in all_series if s.get("category") in categories]
    targets.sort()
    if limit:
        targets = targets[:limit]
    totals = {"series": len(targets), "markets": 0, "candles": 0, "errors": 0}
    t0 = time.monotonic()
    for i, ticker in enumerate(targets):
        try:
            n_m, n_c = sweep_series(db, ticker, days, sess)
            totals["markets"] += n_m
            totals["candles"] += n_c
        except requests.RequestException as e:
            totals["errors"] += 1
            with writer_burst(db) as store:
                store.log_sweep(ticker, None, None, 0, 0, "error", str(e)[:200])
        if (i + 1) % 100 == 0:
            rate = (i + 1) / (time.monotonic() - t0)
            eta_min = (len(targets) - i - 1) / rate / 60
            print(
                f"[sweep] {i + 1}/{len(targets)} series | "
                f"{totals['markets']} markets, {totals['candles']} candles, "
                f"{totals['errors']} errors | ~{eta_min:.0f} min left"
            )
    totals["elapsed_min"] = round((time.monotonic() - t0) / 60, 1)
    return totals


def doctor(store: Store) -> None:
    """Archive health at a glance."""
    print(json.dumps(store.counts(), indent=1))
    mv = store.mirror_violations()
    print(f"kalshi mirror violations: {mv}" + (" <-- PIPELINE CORRUPTION" if mv else ""))
    stream_db = Path("data/hyxstream.duckdb")
    if stream_db.exists():
        size_mb = stream_db.stat().st_size / 1e6
        try:
            with duckdb.connect(str(stream_db), read_only=True) as sconn:
                counts = {
                    t: sconn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                    for t in ("book_events", "stream_trades", "stream_gaps")
                }
            print(f"stream archive: {counts} ({size_mb:.0f} MB)")
        except Exception as exc:  # daemon mid-flush holds the writer lock
            print(f"stream archive: busy ({type(exc).__name__}) ({size_mb:.0f} MB)")
    rows = store.conn.execute(
        "SELECT status, count(*) FROM sweep_log"
        " WHERE swept_at > now() - INTERVAL 2 DAY GROUP BY status"
    ).fetchall()
    print("sweep_log (48h):", dict(rows))
    rows = store.conn.execute(
        "SELECT s.category, count(DISTINCT m.market_id) AS markets"
        " FROM markets m JOIN series s ON s.ticker = m.series AND s.venue = m.venue"
        " GROUP BY s.category ORDER BY markets DESC"
    ).fetchall()
    print("archived markets by category:")
    for cat, n in rows:
        print(f"  {cat or '?'}: {n}")


def acquire_sweep_lock(path: str) -> object | None:
    """Exclusive non-blocking flock; None if another sweep holds it.
    flock releases on process death — no stale-file failure mode (the
    old touch()/exists() lock survived SIGKILL and blocked every later
    sweep until removed by hand)."""
    f = open(path, "a")  # noqa: SIM115 — handle must outlive this call
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab exchange-wide archival sweep")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--categories", nargs="*", default=DEFAULT_CATEGORIES)
    ap.add_argument("--limit", type=int, default=None, help="max series (smoke tests)")
    ap.add_argument("--doctor", action="store_true", help="print archive health and exit")
    args = ap.parse_args()

    if args.doctor:
        store = None
        for attempt in range(5):
            try:
                store = Store(args.db, read_only=True)
                break
            except duckdb.Error:
                # A writer (collector/tradepass flush) holds the file;
                # those bursts last ~seconds.
                if attempt == 4:
                    # Nonzero so systemd records a failed run instead of a
                    # silent no-op success only QA would notice 36h later.
                    print("archive busy (writer active); try again in a few seconds")
                    sys.exit(75)  # EX_TEMPFAIL
                time.sleep(2)
        try:
            doctor(store)
        finally:
            store.close()
        return

    # The sweep itself never holds a connection between bursts, so there
    # is nothing to open here — `writer_burst` opens and closes per write.
    lock = acquire_sweep_lock(args.db + ".lock")
    if lock is None:
        print("[sweep] another sweep holds the lock; aborting")
        sys.exit(75)
    try:
        totals = run_sweep(args.db, args.days, args.categories, limit=args.limit)
        print(f"[sweep] done: {totals}")
        with writer_burst(args.db) as store:
            print(f"[sweep] db={store.counts()}")
    finally:
        lock.close()


if __name__ == "__main__":
    main()
