"""Historical backfill CLI — the Tier-1 data layer.

Pulls into the same DuckDB file the live collector uses:
- Kalshi: settled markets (metadata + results) and their hourly candles
  (price + yes bid/ask OHLC) for the requested series and window.
- IEM: archived NWS climate-report highs (settlement truth) and archived
  MOS forecast highs as-issued (no-lookahead model inputs).

Run:
    python -m collector.backfill --days 365
    python -m collector.backfill --days 365 --series KXHIGHNY --stations NYC

WRITER DISCIPLINE (EXP-1370): this module held a read-write `Store` on
the live archive for its ENTIRE multi-hour run, interleaving REST fetches
with inserts, and took `data/writer.lock` at no point. That is the H1
shape of the 2026-07-11 deep review verbatim — the one that dropped 421
of 3,706 capture cycles from `collector.sweep` — with the lock omitted on
top, so a concurrent `collect` cycle WINS the flock and then collides on
DuckDB's file lock instead of recording an honest skip. HTTP now happens
outside the lock and rows are flushed in bounded bursts.
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, date, datetime, timedelta

import requests

from collector.sweep import FLUSH_ROWS, writer_burst
from collector.venues import iem, kalshi
from hyxlab.store import open_retry

WEATHER_SERIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHDEN"]


def _parse_ts(v: str | None) -> int | None:
    if not v:
        return None
    return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())


def _candles_with_backoff(
    series: str,
    ticker: str,
    open_ts: int,
    close_ts: int,
    session: requests.Session,
    max_tries: int = 6,
) -> list[dict]:
    """The candlesticks endpoint rate-limits well below the documented
    30/s public cap; back off exponentially on 429 instead of dropping
    markets (a dropped market silently biases the backtest sample)."""
    for attempt in range(max_tries):
        try:
            return kalshi.get_candlesticks(
                series, ticker, open_ts, close_ts, period_interval=60, session=session
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429 and attempt < max_tries - 1:
                retry_after = e.response.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 2.0**attempt)
                continue
            raise
    return []


def backfill_kalshi_series(
    db: str,
    series: str,
    days: int,
    session: requests.Session,
    pause_s: float = 0.35,
) -> tuple[int, int]:
    """Backfill settled markets + hourly candles for one series.

    Takes a PATH, not a Store: an open Store is a held file lock, and the
    fetch loop below runs for hours. Each burst opens, writes and closes.
    """
    min_close = int(time.time()) - days * 86400
    markets = kalshi.get_markets(
        series_ticker=series,
        status="settled",
        max_pages=50,
        session=session,
        min_close_ts=min_close,
    )
    with writer_burst(db) as store:
        store.upsert_markets([kalshi.to_market_info(m) for m in markets])
    n_candles = 0
    buf: list[tuple] = []
    for i, m in enumerate(markets):
        open_ts = _parse_ts(m.get("open_time"))
        close_ts = _parse_ts(m.get("close_time"))
        if open_ts is None or close_ts is None:
            continue
        try:
            candles = _candles_with_backoff(series, m["ticker"], open_ts, close_ts, session)
        except requests.RequestException as e:
            print(f"[backfill] {series} {m['ticker']}: {e}")
            continue
        buf.extend(kalshi.candle_row(series, m, c, 3600) for c in candles)
        # Flush mid-loop rather than once at the end: a single end-of-series
        # burst is how KXBTC held the lock for ~21 min (sweep.FLUSH_ROWS).
        if len(buf) >= FLUSH_ROWS:
            with writer_burst(db) as store:
                n_candles += store.insert_candles(buf)
            buf.clear()
        if (i + 1) % 200 == 0:
            print(f"[backfill] {series}: {i + 1}/{len(markets)} markets, {n_candles} candles")
        time.sleep(pause_s)
    if buf:
        with writer_burst(db) as store:
            n_candles += store.insert_candles(buf)
    return len(markets), n_candles


def backfill_iem(
    db: str, stations: list[str], start: date, end: date, session: requests.Session
) -> tuple[int, int]:
    """One burst per station — the fetch is per-year and the write is not."""
    n_obs = n_fc = 0
    for station in stations:
        obs: list[tuple[str, date, int | None]] = []
        for year in range(start.year, end.year + 1):
            got = iem.get_observed_highs(station, year, session=session)
            obs.extend(o for o in got if start <= o[1] <= end)
        fcs = iem.get_mos_forecasts(station, start, end, session=session)
        with writer_burst(db) as store:
            store.upsert_observations(obs)
            store.insert_forecasts(fcs)
        n_obs += len(obs)
        n_fc += len(fcs)
        print(f"[backfill] IEM {station}: {n_obs} obs, {n_fc} forecasts so far")
    return n_obs, n_fc


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab historical backfill")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--series", nargs="*", default=WEATHER_SERIES)
    ap.add_argument("--stations", nargs="*", default=list(iem.STATION_ICAO))
    ap.add_argument("--skip-kalshi", action="store_true")
    ap.add_argument("--skip-iem", action="store_true")
    args = ap.parse_args()

    sess = requests.Session()
    end = datetime.now(UTC).date()
    start = end - timedelta(days=args.days)
    if not args.skip_iem:
        n_obs, n_fc = backfill_iem(args.db, args.stations, start, end, sess)
        print(f"[backfill] IEM done: {n_obs} observations, {n_fc} forecasts")
    if not args.skip_kalshi:
        for series in args.series:
            n_m, n_c = backfill_kalshi_series(args.db, series, args.days, sess)
            print(f"[backfill] {series} done: {n_m} settled markets, {n_c} candles")
    # The closing summary is a READ; it must not re-take the writer lock.
    store = open_retry(args.db, read_only=True)
    try:
        print(f"[backfill] db={store.counts()}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
