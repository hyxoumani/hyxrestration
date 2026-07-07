"""Snapshot collector CLI.

Each cycle:
- Kalshi: for every watchlist series, pull open markets (metadata upsert +
  top-of-book snapshot) and recently settled markets (results, so the sim
  can settle positions).
- Polymarket: batch-fetch CLOB books for configured token pairs.
- NWS: pull the 7-day forecast per station; every pull is stored with
  fetched_at for no-lookahead replay.

Run:
    python -m hyxlab.collect --once
    python -m hyxlab.collect --interval 300
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from hyxlab.store import Store
from hyxlab.venues import kalshi, nws, polymarket

DEFAULT_WATCHLIST = Path(__file__).parent / "watchlist.json"


def load_watchlist(path: str | Path = DEFAULT_WATCHLIST) -> dict:
    with open(path) as f:
        return json.load(f)


def collect_once(store: Store, watchlist: dict, session: requests.Session | None = None) -> dict:
    sess = session or requests.Session()
    counts = {"kalshi_snaps": 0, "kalshi_markets": 0, "poly_snaps": 0, "forecasts": 0, "errors": 0}
    ts = datetime.now(timezone.utc)

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
        except requests.RequestException as e:
            counts["errors"] += 1
            print(f"[collect] kalshi {series}: {e}")

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
        except requests.RequestException as e:
            counts["errors"] += 1
            print(f"[collect] polymarket books: {e}")

    for station in watchlist.get("nws_stations", []):
        try:
            fcs = nws.get_daily_highs(station, session=sess)
            store.insert_forecasts(fcs)
            counts["forecasts"] += len(fcs)
        except requests.RequestException as e:
            counts["errors"] += 1
            print(f"[collect] nws {station}: {e}")

    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab market-data collector")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    ap.add_argument("--interval", type=int, default=300, help="seconds between cycles")
    ap.add_argument("--once", action="store_true", help="one cycle, then exit")
    args = ap.parse_args()

    store = Store(args.db)
    watchlist = load_watchlist(args.watchlist)
    sess = requests.Session()
    try:
        while True:
            counts = collect_once(store, watchlist, session=sess)
            print(
                f"[collect] {datetime.now(timezone.utc).isoformat()} {counts} db={store.counts()}"
            )
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        store.close()


if __name__ == "__main__":
    main()
