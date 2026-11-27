"""Tier-1 historical backtest: candles → simulator → per-city report.

    python -m simulator.run_backtest [--db ...] [--sigma 2.7] [--bias 0]
                                  [--min-edge 0.05] [--max-qty 20]
                                  [--forecast-since ISO] [--target-from D --target-to D]

`--forecast-since` bounds the forecast SOURCE (the table holds backfilled
MEX MOS, then the live gridpoint pull from 2026-07-06 19:36Z);
`--target-from/--target-to` (inclusive, both or neither) bound the weather
markets by target date and make the load proportional to the window --
without them the load is the whole candle table, as v1 ran it.

Also prints the forecast-quality diagnostic (MAE/bias of day-ahead MOS
forecasts vs the settled climate-report highs) — if that MAE isn't in the
~2.5–4.5°F range the MOS parsing convention is wrong and the backtest
result is meaningless; the runner refuses to print PnL in that case.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from hyxlab.models import Forecast
from hyxlab.stations import SERIES_TO_STATION
from hyxlab.store import Store, attach_wait_block, attach_wait_line, held_open, reset_attach_waits
from simulator.capabilities import candle_feed_caps
from simulator.sim import Simulator
from strategies import WeatherNWS


def forecast_diagnostic(
    store: Store, forecasts: list[Forecast] | None = None
) -> dict[str, dict[str, float]]:
    """Day-ahead forecast error vs observed highs, per station.

    `forecasts` is the list the replay will be served, so a source bound
    and a target window reach the gate too."""
    obs = store.observations()
    best: dict[tuple[str, object], tuple] = {}
    for f in store.forecasts() if forecasts is None else forecasts:
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


def _print_buckets(buckets: dict[str, dict[str, float]]) -> None:
    for name, b in sorted(buckets.items()):
        pnl = b["payout"] - b["cost"] - b["fees"]
        roi = pnl / b["cost"] if b["cost"] else 0.0
        print(
            f"  {name:12s} fills={b['n_fills']:5.0f} cost=${b['cost']:9.2f} "
            f"fees=${b['fees']:8.2f} pnl=${pnl:+9.2f} roi={roi:+.1%}"
        )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="hyxlab Tier-1 backtest")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--sigma", type=float, default=2.7)
    ap.add_argument("--bias", type=float, default=0.0)
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--max-qty", type=float, default=20.0)
    ap.add_argument("--forecast-since", type=datetime.fromisoformat, default=None)
    ap.add_argument("--target-from", type=date.fromisoformat, default=None)
    ap.add_argument("--target-to", type=date.fromisoformat, default=None)
    args = ap.parse_args(argv)
    if (args.target_from is None) != (args.target_to is None):
        ap.error("--target-from and --target-to go together")
    windowed = args.target_from is not None

    reset_attach_waits()
    # read_only + retry: this backtest only READS. A read-write open takes
    # the archive's writer lock, which is what crashed the shadow daemon
    # mid-persist on 2026-07-12 (mistakes #20) — and this one is a
    # DOCUMENTED command, so the hazard is not ad-hoc.
    # `held_open`: the hold is the WHOLE load (markets + every candle +
    # forecasts + observations) on the 5-minute collector's file, so it is
    # measured and printed after the release (#91, #96) -- and released
    # BEFORE the replay, which needs nothing from the file.
    with held_open(args.db, read_only=True) as store:
        if windowed:
            ids = [
                r[0]
                for r in store.conn.execute(
                    "SELECT market_id FROM markets WHERE venue='kalshi'"
                    " AND series = ANY(?::VARCHAR[]) AND target_date BETWEEN ? AND ? ORDER BY 1",
                    [sorted(SERIES_TO_STATION), args.target_from, args.target_to],
                ).fetchall()
            ]
            markets = store.markets(venue="kalshi", market_ids=ids)
            snapshots = store.candles_as_snapshots(market_ids=ids)
        else:
            markets = store.markets()
            snapshots = store.candles_as_snapshots()
        forecasts = store.forecasts(
            since=args.forecast_since, target_from=args.target_from, target_to=args.target_to
        )
        diag = forecast_diagnostic(store, forecasts)
    print(attach_wait_line(attach_wait_block(db=Path(args.db).name), prefix="[backtest]"))

    if windowed or args.forecast_since is not None:
        with_candles = {s.market_id for s in snapshots}
        settled = sum(1 for m in markets.values() if m.result in ("yes", "no"))
        print(
            f"[backtest] scope: target dates {args.target_from}..{args.target_to}, "
            f"forecasts fetched since {args.forecast_since} | {len(markets)} markets, "
            f"{settled} settled, {len(with_candles)} with usable candles, "
            f"{len(forecasts)} forecast rows"
        )
    print("== forecast diagnostic (day-ahead forecast vs climate report) ==")
    print(json.dumps(diag, indent=2))
    # Gate per prereg_weather_backtest.md Addendum 1: MAE in [1, 5] °F and
    # exact-match < 60% (a forecast←observation leak would be ~100%).
    bad = [s for s, d in diag.items() if not 1.0 <= d["mae"] <= 5.0 or d["exact_match"] >= 0.60]
    if not diag or bad:
        print(
            f"ABORT: forecast MAE out of sane range for {bad or 'all stations'} — "
            "check the MOS parsing convention before trusting any backtest."
        )
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
    def _bucket() -> dict[str, float]:
        return {"n_fills": 0, "cost": 0.0, "fees": 0.0, "payout": 0.0}

    by_series: dict[str, dict[str, float]] = defaultdict(_bucket)
    # The window's two halves by TARGET date (the registered R3 split); the
    # first half takes the odd day.
    by_half: dict[str, dict[str, float]] = defaultdict(_bucket)
    split = args.target_from + (args.target_to - args.target_from) // 2 if windowed else None
    for f in result.fills:
        if f.strategy != "weather_nws":
            continue
        info = markets.get((f.venue, f.market_id))
        if info is None or info.result not in ("yes", "no"):
            continue
        buckets = [by_series[info.series]]
        if split is not None and info.target_date is not None:
            first = info.target_date <= split
            buckets.append(by_half[f"..{split}" if first else f"{split + timedelta(days=1)}.."])
        for b in buckets:
            b["n_fills"] += 1
            b["cost"] += f.qty * f.price
            b["fees"] += f.fee
            b["payout"] += f.qty * (1.0 if info.result == f.side else 0.0)
    print("\n== weather_nws settled PnL by city ==")
    _print_buckets(by_series)
    if by_half:
        print("\n== weather_nws settled PnL by half of the target window ==")
        _print_buckets(by_half)


if __name__ == "__main__":
    main()
