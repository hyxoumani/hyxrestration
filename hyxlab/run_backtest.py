"""Tier-1 historical backtest: candles → simulator → per-city report.

    python -m hyxlab.run_backtest [--db ...] [--sigma 2.7] [--bias 0]
                                  [--min-edge 0.05] [--max-qty 20]

Also prints the forecast-quality diagnostic (MAE/bias of day-ahead MOS
forecasts vs the settled climate-report highs) — if that MAE isn't in the
~2.5–4.5°F range the MOS parsing convention is wrong and the backtest
result is meaningless; the runner refuses to print PnL in that case.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import timedelta

from hyxlab.capabilities import candle_feed_caps
from hyxlab.sim import Simulator
from hyxlab.store import Store
from hyxlab.strategies import WeatherNWS


def forecast_diagnostic(store: Store) -> dict[str, dict[str, float]]:
    """Day-ahead forecast error vs observed highs, per station."""
    obs = store.observations()
    best: dict[tuple[str, object], tuple] = {}
    for f in store.forecasts():
        # Day-ahead = issued the calendar day before the target (UTC).
        if f.fetched_at.date() != f.target_date - timedelta(days=1):
            continue
        key = (f.station, f.target_date)
        cur = best.get(key)
        if cur is None or f.fetched_at > cur[0]:
            best[key] = (f.fetched_at, f.high_f)
    errs: dict[str, list[float]] = defaultdict(list)
    for (station, target), (_, high_f) in best.items():
        actual = obs.get((station, target))
        if actual is not None:
            errs[station].append(high_f - actual)
    out: dict[str, dict[str, float]] = {}
    for station, es in errs.items():
        n = len(es)
        out[station] = {
            "n": n,
            "mae": sum(abs(e) for e in es) / n,
            "bias": sum(es) / n,
            "exact_match": sum(1 for e in es if e == 0) / n,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab Tier-1 backtest")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--sigma", type=float, default=2.7)
    ap.add_argument("--bias", type=float, default=0.0)
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--max-qty", type=float, default=20.0)
    args = ap.parse_args()

    store = Store(args.db)
    markets = store.markets()
    snapshots = store.candles_as_snapshots()
    forecasts = store.forecasts()

    diag = forecast_diagnostic(store)
    print("== forecast diagnostic (day-ahead MOS vs climate report) ==")
    print(json.dumps(diag, indent=2))
    # Gate per prereg_weather_backtest.md Addendum 1: MAE in [1, 5] °F and
    # exact-match < 60% (a forecast←observation leak would be ~100%).
    bad = [s for s, d in diag.items() if not 1.0 <= d["mae"] <= 5.0 or d["exact_match"] >= 0.60]
    if not diag or bad:
        print(
            f"ABORT: forecast MAE out of sane range for {bad or 'all stations'} — "
            "check the MOS parsing convention before trusting any backtest."
        )
        store.close()
        return

    print(
        f"\n== replaying {len(snapshots)} candle-snapshots over "
        f"{len(markets)} markets, {len(forecasts)} forecasts =="
    )
    weather = WeatherNWS(
        sigma=args.sigma, bias=args.bias, min_edge=args.min_edge, max_qty=args.max_qty
    )
    # No IntramarketRebalance here: candle snapshots derive NO as the YES
    # complement, so its trigger can never fire — the capability guard
    # would (rightly) refuse the run.
    sim = Simulator(
        markets,
        [weather],
        forecasts=forecasts,
        data_capabilities=candle_feed_caps(snapshots),
    )
    result = sim.run(snapshots)
    print(json.dumps(result.metrics, indent=2, default=str))

    # Per-series (city) settled PnL breakdown for the weather strategy.
    by_series: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n_fills": 0, "cost": 0.0, "fees": 0.0, "payout": 0.0}
    )
    for f in result.fills:
        if f.strategy != "weather_nws":
            continue
        info = markets.get((f.venue, f.market_id))
        if info is None or info.result not in ("yes", "no"):
            continue
        b = by_series[info.series]
        b["n_fills"] += 1
        b["cost"] += f.qty * f.price
        b["fees"] += f.fee
        b["payout"] += f.qty * (1.0 if info.result == f.side else 0.0)
    print("\n== weather_nws settled PnL by city ==")
    for series, b in sorted(by_series.items()):
        pnl = b["payout"] - b["cost"] - b["fees"]
        roi = pnl / b["cost"] if b["cost"] else 0.0
        print(
            f"  {series:12s} fills={b['n_fills']:5.0f} cost=${b['cost']:9.2f} "
            f"pnl=${pnl:+9.2f} roi={roi:+.1%}"
        )
    store.close()


if __name__ == "__main__":
    main()
