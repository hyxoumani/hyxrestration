"""Replay stored snapshots through the baseline strategies and print metrics.

    python -m simulator.run_sim [--db data/hyxlab.duckdb]

Cross-venue arb runs only if pairs are configured in the watchlist (each
pair must be human-verified for identical resolution rules first).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hyxlab.store import attach_wait_block, attach_wait_line, held_open, reset_attach_waits
from hyxlab.watchlist import DEFAULT_WATCHLIST, load_watchlist
from simulator.capabilities import live_feed_caps, partition_runnable
from simulator.sim import Simulator
from strategies import CrossVenueArb, IntramarketRebalance, WeatherNWS
from strategies.cross_venue import Pair


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="hyxlab strategy replay")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    args = ap.parse_args(argv)

    reset_attach_waits()
    # read_only + retry: this replay only READS. A read-write open takes
    # the archive's writer lock, which is what crashed the shadow daemon
    # mid-persist on 2026-07-12 (mistakes #20) — and this one is a
    # DOCUMENTED command, so the hazard is not ad-hoc.
    # `held_open`: the hold is the LOAD (markets + every snapshot +
    # forecasts) on the 5-minute collector's file, measured and printed
    # after the release (#91, #96) -- and released BEFORE the replay,
    # which needs nothing from the file; it used to stay open across it.
    with held_open(args.db, read_only=True) as store:
        markets = store.markets()
        snapshots = store.iter_snapshots()
        forecasts = store.forecasts()
    print(attach_wait_line(attach_wait_block(db=Path(args.db).name), prefix="[run_sim]"))
    print(
        f"replaying {len(snapshots)} snapshots over {len(markets)} markets, "
        f"{len(forecasts)} forecast rows"
    )

    strategies = [IntramarketRebalance(), WeatherNWS()]
    pairs_cfg = load_watchlist(args.watchlist).get("cross_venue_pairs", [])
    if pairs_cfg:
        pairs = [Pair(*p) for p in pairs_cfg]
        strategies.append(CrossVenueArb(pairs))

    caps = live_feed_caps(snapshots)
    strategies, refused = partition_runnable(strategies, caps)
    for strat in refused:
        print(
            f"SKIPPED {strat.name}: requires {sorted(strat.requires)}, feed provides "
            f"{ {v: sorted(c) for v, c in caps.items()} } — would be a vacuous run"
        )

    sim = Simulator(
        markets,
        strategies,
        forecasts=forecasts,
        data_capabilities=caps,
    )
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


if __name__ == "__main__":
    main()
