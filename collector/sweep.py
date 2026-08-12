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

from collector.lockid import note_holder
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

# Per-series, per-run market budget. NOT a coverage cap: get_markets_ascending
# returns a contiguous oldest-first prefix, so stopping here advances the
# watermark honestly and the next run resumes exactly where this one stopped.
# What it bounds is TIME. Measured (EXP-931, 2026-08-02): sweep_series costs
# ~0.5s per market (candles + trade tape + CANDLES_PAUSE_S), and the widened
# category list put three sub-hourly BNB series near the front of the
# alphabetical target order. Each burned ~1.5h, so a 3.5h run reached
# KXBNB15M and died there — every KXBTC*/KXETH*/KXSOL* series and all ten
# Exotics series were never requested at all, and the resulting archive
# ("crypto == BNB only") looked like a finding rather than a starved run.
# 2,000 caps one series at ~17min so no single series can eat the run.
MAX_MARKETS_PER_SERIES = 2000

# Consecutive-failure circuit breaker. Measured 2026-08-06: Kalshi's
# /markets endpoint degraded mid-run (503s, hour-long 429 storm) and the
# sweep fail-fasted through 2,569 CONSECUTIVE series errors — ~10k
# useless requests against a venue already refusing service, sharing the
# rate budget with capture daemons — then reported success to systemd.
# Organic series errors are rare (recent runs: 0) and never contiguous;
# a long unbroken error run is a venue-side outage. Aborting leaves
# every unfinished watermark untouched, so the next timer firing resumes
# exactly where this one stopped: a delayed sweep, never a lost one.
ABORT_CONSEC_ERRORS = 25

# Mid-series flush threshold, in buffered rows (candles + trades).
# "All writes for the series land in a single writer_burst" assumed the
# burst is short; measured 2026-08-07, KXBTC's recovery backlog buffered
# ~3.85M trade rows and that single burst held the writer lock for ~21
# min — four consecutive collect cycles skipped (a 20-min capture gap,
# QA FAIL). Every insert in the burst is idempotent (trade_id/candle-key
# dedup, OR REPLACE marks) and the watermark still advances ONLY in the
# final burst, so flushing mid-series is crash-safe: a crash between
# bursts re-fetches and dedups exactly as before. 250k rows keeps each
# burst well under collect's 300s open budget (measured ~3k rows/s
# against the 165M-row trades table).
FLUSH_ROWS = 250_000

# Resume floor for series with a watermark. Kalshi purges settled-market
# data ~60-90 days out (module docstring), so resuming from a watermark
# older than this buys nothing — the range is gone. It bounds the cost of
# a long-dormant series to a handful of empty adaptive-window probes.
# 60 matches the initial `--days 60` retention-capture depth.
PURGE_HORIZON_DAYS = 60

# Per-burst open budget, deliberately far above open_retry's 60s default.
# Releasing between series has a cost the old whole-run hold did not: a
# READER can now get in, and DuckDB excludes a writer while any reader is
# attached. A long atlas/backtest/simui read (minutes over a 4.5GB
# archive) would otherwise kill the sweep at its next flush. 5 minutes
# outlasts every standing report measured to date; past that the sweep
# exits 75 and the next timer firing resumes from the watermark, so the
# failure mode is a delayed sweep, never a lost one.
BURST_OPEN_RETRIES = 150
BURST_OPEN_DELAY_S = 2.0


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
        note_holder(lock_file)  # name the holder BEFORE open_retry can spin for 300s
        # readers (QA/doctor/backtest) don't take the flock, so the open
        # can still lose to one — hence the widened budget above.
        store = open_retry(db, retries=BURST_OPEN_RETRIES, delay=BURST_OPEN_DELAY_S)
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
    db: str,
    series_ticker: str,
    days: int,
    session: requests.Session,
    max_markets: int | None = MAX_MARKETS_PER_SERIES,
) -> tuple[int, int, bool]:
    """Capture settled markets + candles for one series since its watermark.

    Every REST call happens with NO lock and NO open connection; writes
    land in `writer_burst`s — one final burst for a normal series, plus
    intermediate bursts every FLUSH_ROWS buffered rows for giant ones
    (the single-burst shape held the lock ~21 min on KXBTC's 3.85M-row
    recovery backlog, 2026-08-07). Crash-safety: the watermark advances
    only in the final burst, and every intermediate write is idempotent,
    so a crash mid-series re-runs exactly — flushed rows dedup away.
    """
    now = datetime.now(UTC)
    floor_ts = now - timedelta(days=days)
    with writer_burst(db) as store:
        wm = store.watermark(series_ticker)
    if wm is not None:
        # Resume from the watermark, NEVER clamped forward by the --days
        # window. The old `max(now - days, wm + 1s)` silently jumped the
        # floor past any watermark more than `days` old, so a truncated
        # dense series (daily timer: --days 2) lost the wm -> now-2d range
        # every single day while its sweep_log note promised "resume from
        # <max_close>" — measured 08-03..08-12: 0.8h-24h of close-time
        # coverage lost per day on each of the 9 chronically-truncated
        # crypto/MVE series (EXP-1271; the EXP-931 range-loss shape).
        # The only forward bound is the venue purge horizon: below that
        # the data no longer exists to fetch.
        floor_ts = max(
            wm.replace(tzinfo=UTC) + timedelta(seconds=1),
            now - timedelta(days=PURGE_HORIZON_DAYS),
        )

    markets, markets_truncated = kalshi.get_markets_ascending(
        series_ticker=series_ticker,
        status="settled",
        min_close_ts=int(floor_ts.timestamp()),
        max_close_ts=int(now.timestamp()),
        max_markets=max_markets,
        session=session,
        pause_s=MARKETS_PAUSE_S,
    )
    time.sleep(MARKETS_PAUSE_S)
    if not markets:
        with writer_burst(db) as store:
            store.log_sweep(series_ticker, floor_ts, None, 0, 0, "ok", "no settled markets")
        return 0, 0, False

    infos = [kalshi.to_market_info(m) for m in markets]
    candle_rows: list[tuple] = []
    trade_rows: list[tuple] = []
    swept: list[tuple[str, int, str]] = []
    max_close = floor_ts
    n_candles = 0

    def flush_buffers(store: Store) -> None:
        nonlocal n_candles
        n_candles += store.insert_candles(candle_rows)
        store.insert_trades(trade_rows)
        for ticker, n_trades, status in swept:
            store.mark_trades_swept(ticker, n_trades, status)
        candle_rows.clear()
        trade_rows.clear()
        swept.clear()

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
        if len(candle_rows) + len(trade_rows) >= FLUSH_ROWS:
            print(
                f"[sweep] {series_ticker} mid-series flush at "
                f"{len(candle_rows)} candles + {len(trade_rows)} trades",
                flush=True,
            )
            with writer_burst(db) as store:
                flush_buffers(store)
        time.sleep(CANDLES_PAUSE_S)

    with writer_burst(db) as store:
        store.upsert_markets(infos)
        flush_buffers(store)
        store.set_watermark(series_ticker, max_close)
        # A truncated sweep is NOT 'ok'. It used to be — the only trace was a
        # print, which is how "crypto == BNB only" survived into an
        # experiment's premises (EXP-931). status='truncated' puts it in
        # sweep_log where doctor(), QA and any later analyst must trip over it.
        store.log_sweep(
            series_ticker,
            floor_ts,
            max_close,
            len(markets),
            n_candles,
            "truncated" if markets_truncated else "ok",
            f"budget {max_markets} markets reached; resume from {max_close:%Y-%m-%dT%H:%M}Z"
            if markets_truncated
            else "",
        )
    return len(markets), n_candles, markets_truncated


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
    only_series: list[str] | None = None,
    max_markets: int | None = MAX_MARKETS_PER_SERIES,
) -> dict:
    sess = session or requests.Session()
    series = kalshi.get_series_list(sess)  # HTTP outside the lock
    with writer_burst(db) as store:
        all_series = refresh_series(store, sess, series=series)
    targets = [s["ticker"] for s in all_series if s.get("category") in categories]
    if only_series:
        # Targeted repair pass: the alphabetical whole-category order is what
        # starved KXBTC*/KXETH*/KXSOL* (EXP-931), so recovering a specific
        # family must not have to wait its turn behind 200 unrelated series.
        wanted = set(only_series)
        targets = [t for t in targets if t in wanted]
    targets.sort()
    if limit:
        targets = targets[:limit]
    totals = {
        "series": len(targets),
        "markets": 0,
        "candles": 0,
        "errors": 0,
        "truncated": 0,
        "aborted": False,
    }
    t0 = time.monotonic()
    consec_errors = 0
    for i, ticker in enumerate(targets):
        try:
            n_m, n_c, truncated = sweep_series(db, ticker, days, sess, max_markets)
            totals["markets"] += n_m
            totals["candles"] += n_c
            totals["truncated"] += int(truncated)
            consec_errors = 0
            if truncated:
                print(
                    f"[sweep] {ticker} TRUNCATED at {n_m} markets"
                    f" (per-series budget); logged non-ok, resumes next run",
                    flush=True,
                )
        except requests.RequestException as e:
            totals["errors"] += 1
            consec_errors += 1
            with writer_burst(db) as store:
                store.log_sweep(ticker, None, None, 0, 0, "error", str(e)[:200])
            if consec_errors >= ABORT_CONSEC_ERRORS:
                totals["aborted"] = True
                print(
                    f"[sweep] ABORT after {consec_errors} consecutive series"
                    f" errors at {ticker} ({str(e)[:120]}) — venue degraded;"
                    f" watermarks intact, next run resumes",
                    flush=True,
                )
                break
        if (i + 1) % 100 == 0:
            rate = (i + 1) / (time.monotonic() - t0)
            eta_min = (len(targets) - i - 1) / rate / 60
            print(
                f"[sweep] {i + 1}/{len(targets)} series | "
                f"{totals['markets']} markets, {totals['candles']} candles, "
                f"{totals['errors']} errors, {totals['truncated']} truncated"
                f" | ~{eta_min:.0f} min left"
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
    ap.add_argument(
        "--series", nargs="*", default=None, help="only these series tickers (repair pass)"
    )
    ap.add_argument(
        "--max-markets",
        type=int,
        default=MAX_MARKETS_PER_SERIES,
        help="per-series, per-run market budget (0 = unbounded)",
    )
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
        totals = run_sweep(
            args.db,
            args.days,
            args.categories,
            limit=args.limit,
            only_series=args.series,
            max_markets=args.max_markets or None,
        )
        print(f"[sweep] done: {totals}")
        with writer_burst(args.db) as store:
            print(f"[sweep] db={store.counts()}")
        if totals.get("aborted"):
            # Nonzero so systemd records a failed run — today's outage run
            # said "Finished" with an 82%-error pass only the journal knew
            # about. EX_TEMPFAIL matches the lock/reader-contention exits:
            # the next timer firing resumes from the watermarks.
            sys.exit(75)
    finally:
        lock.close()


if __name__ == "__main__":
    main()
