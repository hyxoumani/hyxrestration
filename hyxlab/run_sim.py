"""Replay stored snapshots through the baseline strategies and print metrics.

    python -m hyxlab.run_sim [--db data/hyxlab.duckdb]

Cross-venue arb runs only if pairs are configured in the watchlist (each
pair must be human-verified for identical resolution rules first).
"""

from __future__ import annotations

import argparse
import json

from hyxlab.collect import DEFAULT_WATCHLIST, load_watchlist
from hyxlab.sim import Simulator
from hyxlab.store import Store
from hyxlab.strategies import CrossVenueArb, IntramarketRebalance, WeatherNWS
from hyxlab.strategies.cross_venue import Pair


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab strategy replay")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    args = ap.parse_args()

    store = Store(args.db)
    markets = store.markets()
    snapshots = store.iter_snapshots()
    forecasts = store.forecasts()
    print(
        f"replaying {len(snapshots)} snapshots over {len(markets)} markets, "
        f"{len(forecasts)} forecast rows"
    )

    strategies = [IntramarketRebalance(), WeatherNWS()]
    pairs_cfg = load_watchlist(args.watchlist).get("cross_venue_pairs", [])
    if pairs_cfg:
        pairs = [Pair(*p) for p in pairs_cfg]
        strategies.append(CrossVenueArb(pairs))

    sim = Simulator(markets, strategies, forecasts=forecasts)
    result = sim.run(snapshots)

    print(json.dumps(result.metrics, indent=2, default=str))
    for f in result.fills[:50]:
        print(
            f"  {f.ts} {f.strategy:12s} {f.venue}:{f.market_id} "
            f"buy {f.side} {f.qty:g} @ {f.price:.3f} fee {f.fee:+.4f} "
            f"{'maker' if f.maker else 'taker'}"
        )
    if len(result.fills) > 50:
        print(f"  ... {len(result.fills) - 50} more fills")
    store.close()


if __name__ == "__main__":
    main()
