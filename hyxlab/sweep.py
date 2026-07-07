"""Exchange-wide archival sweep (proposal C8, corrected per data_contracts.md).

Enumerates Kalshi series by **category allowlist**, captures settled
markets + hourly candles since each series' watermark, and logs progress
to sweep_log. Idempotent: watermarks + anti-join inserts make re-runs and
crashes safe; a re-run resumes where the last one stopped.

    python -m hyxlab.sweep --days 60            # initial retention capture
    python -m hyxlab.sweep --days 2             # daily incremental
    python -m hyxlab.sweep --days 2 --limit 20  # smoke test

Rationale: Kalshi purges market data ~60-90 days after settlement
(verified 2026-07-06); anything not swept is gone. The allowlist excludes
the ~8,200 sports/entertainment/politics series that dominate settle
volume but are outside our strategy domains — one line to revisit.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from hyxlab.store import Store
from hyxlab.venues import kalshi

DEFAULT_CATEGORIES = [
    "Economics",
    "Financials",
    "Climate and Weather",
    "Companies",
    "Commodities",
    "Science and Technology",
    "Health",
    "World",
]

MARKETS_PAUSE_S = 0.2  # empirical safe pacing (data_contracts.md)
CANDLES_PAUSE_S = 0.35


def refresh_series(store: Store, session: requests.Session) -> list[dict]:
    """Pull the full series list, persist metadata, return allowlisted set."""
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
    store: Store, series_ticker: str, days: int, session: requests.Session
) -> tuple[int, int]:
    """Capture settled markets + candles for one series since its watermark."""
    now = datetime.now(timezone.utc)
    floor_ts = now - timedelta(days=days)
    wm = store.watermark(series_ticker)
    if wm is not None:
        floor_ts = max(floor_ts, wm.replace(tzinfo=timezone.utc) + timedelta(seconds=1))

    markets = kalshi.get_markets(
        series_ticker=series_ticker,
        status="settled",
        max_pages=50,
        session=session,
        min_close_ts=int(floor_ts.timestamp()),
    )
    time.sleep(MARKETS_PAUSE_S)
    if not markets:
        store.log_sweep(series_ticker, floor_ts, None, 0, 0, "ok", "no settled markets")
        return 0, 0

    store.upsert_markets([kalshi.to_market_info(m) for m in markets])
    n_candles = 0
    max_close = floor_ts
    for m in markets:
        open_ts = _ts(m.get("open_time"))
        close_ts = _ts(m.get("close_time"))
        if open_ts is None or close_ts is None:
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
        n_candles += store.insert_candles(
            [kalshi.candle_row(series_ticker, m, c, 3600) for c in candles]
        )
        close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)
        max_close = max(max_close, close_dt)
        time.sleep(CANDLES_PAUSE_S)

    store.set_watermark(series_ticker, max_close)
    store.log_sweep(series_ticker, floor_ts, max_close, len(markets), n_candles, "ok")
    return len(markets), n_candles


def _ts(v: str | None) -> int | None:
    if not v:
        return None
    return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())


def run_sweep(
    store: Store,
    days: int,
    categories: list[str],
    session: requests.Session | None = None,
    limit: int | None = None,
) -> dict:
    sess = session or requests.Session()
    all_series = refresh_series(store, sess)
    targets = [s["ticker"] for s in all_series if s.get("category") in categories]
    targets.sort()
    if limit:
        targets = targets[:limit]
    totals = {"series": len(targets), "markets": 0, "candles": 0, "errors": 0}
    t0 = time.monotonic()
    for i, ticker in enumerate(targets):
        try:
            n_m, n_c = sweep_series(store, ticker, days, sess)
            totals["markets"] += n_m
            totals["candles"] += n_c
        except requests.RequestException as e:
            totals["errors"] += 1
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


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab exchange-wide archival sweep")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--categories", nargs="*", default=DEFAULT_CATEGORIES)
    ap.add_argument("--limit", type=int, default=None, help="max series (smoke tests)")
    ap.add_argument("--doctor", action="store_true", help="print archive health and exit")
    args = ap.parse_args()

    lock = Path(args.db + ".lock")
    store = Store(args.db, read_only=args.doctor)
    try:
        if args.doctor:
            doctor(store)
            return
        if lock.exists():
            print(f"[sweep] lock {lock} exists — another writer running? aborting")
            return
        lock.touch()
        try:
            totals = run_sweep(store, args.days, args.categories, limit=args.limit)
            print(f"[sweep] done: {totals}")
            print(f"[sweep] db={store.counts()}")
        finally:
            lock.unlink(missing_ok=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
