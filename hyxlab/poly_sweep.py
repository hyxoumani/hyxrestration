"""Polymarket archival sweep: metadata, volume series, price history,
trade tails — everything the venue still serves (probed 2026-07-07).

    python -m hyxlab.poly_sweep [--db ...] [--min-volume 10000] [--limit N]

Retention facts driving the design:
- prices-history: ~60-day ROLLING window; closed markets purge ~1-2
  months after close → capture incrementally (per-token watermark),
  daily, before it rolls off.
- data-api trades: hard cap of the LAST 3,000 prints per market — a
  tail sample, never a tape. The forward tape is the WS stream; these
  tails fill in low-volume markets completely and busy ones partially.
- Gamma metadata + volumeNum/liquidityNum: full history, cheap — swept
  volume-desc down to --min-volume, plus recently-closed markets for
  settlement results.

Plays nice with the 5-min collector: REST fetching holds no lock; DB is
touched in short flock-guarded bursts (same pattern as trades_backfill).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from hyxlab.store import Store
from hyxlab.venues import polymarket as poly

FLUSH_MARKETS = 25
LOCK_FILE = "data/writer.lock"
REQUEST_PAUSE_S = 0.25  # CLOB/data-api pacing


def _flush(db: str, batch: dict) -> None:
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = Store(db)
        try:
            if batch["infos"]:
                store.upsert_markets(batch["infos"])
            if batch["stats"]:
                store.insert_poly_stats(batch["stats"])
            if batch["prices"]:
                batch["n_prices"] += store.insert_poly_prices(batch["prices"])
            if batch["trades"]:
                batch["n_trades"] += store.insert_trades(batch["trades"])
        finally:
            store.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
    batch["infos"], batch["stats"], batch["prices"], batch["trades"] = [], [], [], []


def sweep(
    db: str,
    min_volume: float,
    limit: int | None = None,
    include_closed_days: int = 7,
) -> dict:
    sess = requests.Session()
    sess.headers["User-Agent"] = "hyxlab-research"

    now = datetime.now(UTC)
    print("[poly] enumerating markets via Gamma (volume-desc)...", flush=True)
    markets = poly.iter_markets_by_volume(min_volume, closed=False, session=sess)
    closed_floor = (now - timedelta(days=include_closed_days)).strftime("%Y-%m-%d")
    markets += poly.iter_markets_by_volume(
        min_volume, closed=True, session=sess, end_date_min=closed_floor
    )
    if limit:
        markets = markets[:limit]
    print(f"[poly] {len(markets)} markets >= ${min_volume:g} volume", flush=True)

    with open(LOCK_FILE, "a") as lock:  # ensure schema + read watermarks
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = Store(db)
        watermarks = store.poly_price_watermarks()
        store.close()
        fcntl.flock(lock, fcntl.LOCK_UN)

    batch: dict = {
        "infos": [],
        "stats": [],
        "prices": [],
        "trades": [],
        "n_prices": 0,
        "n_trades": 0,
    }
    totals = {"markets": 0, "errors": 0}
    t0 = time.monotonic()

    for i, m in enumerate(markets):
        cond = m.get("conditionId")
        pair = poly.token_pair(m)
        if not cond or pair is None:
            continue
        try:
            batch["infos"].append(poly.gamma_market_info(m))
            batch["stats"].append(
                (cond, now, float(m.get("volumeNum") or 0), float(m.get("liquidityNum") or 0))
            )
            for token, outcome in zip(pair, ("yes", "no"), strict=True):
                wm = watermarks.get(token)
                # First capture reaches the full ~60d retention window;
                # later sweeps resume from the watermark. Ranges are
                # chunked (API rejects spans over ~30d).
                start = (
                    int(wm.replace(tzinfo=UTC).timestamp()) + 1
                    if wm
                    else int((now - timedelta(days=62)).timestamp())
                )
                hist = poly.prices_history_range(token, start, session=sess)
                batch["prices"].extend(poly.price_rows(token, cond, outcome, hist))
                time.sleep(REQUEST_PAUSE_S)
            batch["trades"].extend(
                poly.poly_trade_row(t) for t in poly.trades_tail(cond, session=sess)
            )
            time.sleep(REQUEST_PAUSE_S)
        except Exception as exc:
            totals["errors"] += 1
            print(f"[poly] {type(exc).__name__} at {cond[:16]}: {str(exc)[:80]}", flush=True)
            time.sleep(2)
        totals["markets"] += 1
        if len(batch["infos"]) >= FLUSH_MARKETS or i == len(markets) - 1:
            _flush(db, batch)
        if (i + 1) % 200 == 0:
            rate = (i + 1) / (time.monotonic() - t0)
            eta = (len(markets) - i - 1) / rate / 60
            print(
                f"[poly] {i + 1}/{len(markets)} | {batch['n_prices']} prices,"
                f" {batch['n_trades']} trades | ~{eta:.0f} min left",
                flush=True,
            )
    _flush(db, batch)
    totals["n_prices"], totals["n_trades"] = batch["n_prices"], batch["n_trades"]
    totals["elapsed_min"] = round((time.monotonic() - t0) / 60, 1)
    return totals


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab polymarket archival sweep")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--min-volume", type=float, default=10000.0)
    ap.add_argument("--limit", type=int, default=None, help="max markets (smoke tests)")
    ap.add_argument("--closed-days", type=int, default=7, help="recently-closed lookback")
    args = ap.parse_args()
    Path(LOCK_FILE).parent.mkdir(exist_ok=True)
    totals = sweep(args.db, args.min_volume, args.limit, args.closed_days)
    print(f"[poly] done: {json.dumps(totals)}", flush=True)


if __name__ == "__main__":
    main()
