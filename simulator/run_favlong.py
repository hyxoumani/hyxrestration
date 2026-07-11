"""Tier-1 favorite-longshot backtest runner (pre-registered kill-test).

    python -m simulator.run_favlong [--db data/hyxlab.duckdb]

Executes exactly the configuration bound in
docs/hyxpredict/prereg_favlong_backtest.md: candles → Simulator →
endpoints. Validity gates G1/G2 are enforced mechanically before any
PnL line prints. Emits the endpoint block verbatim for appending to
the registration, plus a manifest (family size 1).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from statistics import median

from hyxlab.store import open_retry
from simulator.capabilities import candle_feed_caps
from simulator.harness import data_fingerprint, write_manifest
from simulator.iterate import deflated_sharpe
from simulator.sim import Simulator
from strategies.fav_long import FavoriteLongshot

BAND = (0.80, 0.95)
SUB_BAND_SPLIT = 0.875


def main() -> None:
    ap = argparse.ArgumentParser(description="Tier-1 favorite-longshot kill-test")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    args = ap.parse_args()

    store = open_retry(args.db, read_only=True)
    markets = store.markets()
    snapshots = store.candles_as_snapshots()
    categories = dict(
        store.conn.execute(
            "SELECT ticker, coalesce(category, '?') FROM series WHERE venue='kalshi'"
        ).fetchall()
    )
    store.close()

    print(f"== replaying {len(snapshots)} candle-snapshots over {len(markets)} markets ==")
    strat = FavoriteLongshot()  # binding defaults per registration
    sim = Simulator(markets, [strat], data_capabilities=candle_feed_caps(snapshots))
    result = sim.run(snapshots)

    settled = []
    for f in result.fills:
        info = markets.get((f.venue, f.market_id))
        if info is None or info.result not in ("yes", "no"):
            continue
        payout = f.qty * (1.0 if info.result == f.side else 0.0)
        settled.append((f, info, payout))

    # -- validity gates (before any PnL is printed) ----------------------
    n = len(settled)
    if n:
        prices = [f.price for f, _, _ in settled]
        g2_ok = all(BAND[0] <= p <= BAND[1] for p in prices)
        mean_price = sum(prices) / n
    else:
        g2_ok, mean_price = False, float("nan")
    if not g2_ok:
        print(f"ABORT G2: entry prices out of band (mean {mean_price}); run invalid.")
        return
    if n < 300:
        print(f"INCONCLUSIVE G1: only {n} settled fills (< 300); no PnL verdict read.")
        return

    # -- endpoints --------------------------------------------------------
    cost = sum(f.qty * f.price for f, _, _ in settled)
    fees = sum(f.fee for f, _, _ in settled)
    payout = sum(p for _, _, p in settled)
    pnl = payout - cost - fees
    roi = pnl / cost

    by_cat: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for f, info, p in settled:
        cat = categories.get(info.series, "?")
        by_cat[cat]["n"] += 1
        by_cat[cat]["pnl"] += p - f.qty * f.price - f.fee

    med_close = median(info.close_time for _, info, _ in settled)
    halves = {"H1": 0.0, "H2": 0.0}
    for f, info, p in settled:
        halves["H1" if info.close_time <= med_close else "H2"] += p - f.qty * f.price - f.fee
    bands = {"low": 0.0, "high": 0.0}
    for f, _info, p in settled:
        bands["low" if f.price < SUB_BAND_SPLIT else "high"] += p - f.qty * f.price - f.fee

    big_cats = {c: v for c, v in by_cat.items() if v["n"] >= 50}
    pos_cats = [c for c, v in big_cats.items() if v["pnl"] > 0]
    total_pos = sum(v["pnl"] for v in by_cat.values() if v["pnl"] > 0)
    r1 = len(pos_cats) >= 3
    r2 = total_pos > 0 and all(v["pnl"] <= 0.8 * total_pos for v in by_cat.values())
    r3 = halves["H1"] > 0 and halves["H2"] > 0
    r4 = bands["low"] > 0 and bands["high"] > 0

    returns = [(p - f.qty * f.price - f.fee) / (f.qty * f.price) for f, _, p in settled]
    dsr = deflated_sharpe(returns, n_trials=1)

    if roi <= 0:
        verdict = "FAIL (kill)"
    elif roi >= 0.02:
        verdict = "SURVIVE" if (r1 and r2 and r3 and r4) else "WEAK SURVIVE"
    else:
        verdict = "INCONCLUSIVE"

    block = {
        "settled_fills": n,
        "mean_entry_price": round(mean_price, 4),
        "cost": round(cost, 2),
        "fees": round(fees, 2),
        "payout": round(payout, 2),
        "pnl": round(pnl, 2),
        "roi": round(roi, 4),
        "fee_share_of_gross": round(fees / (payout - cost), 4) if payout != cost else None,
        "by_category": {
            c: {"n": v["n"], "pnl": round(v["pnl"], 2)} for c, v in sorted(by_cat.items())
        },
        "halves_pnl": {k: round(v, 2) for k, v in halves.items()},
        "sub_bands_pnl": {k: round(v, 2) for k, v in bands.items()},
        "robustness": {"R1": r1, "R2": r2, "R3": r3, "R4": r4},
        "psr_supplementary": {k: round(v, 4) for k, v in dsr._asdict().items()},
        "verdict": verdict,
    }
    print(json.dumps(block, indent=1))
    manifest = write_manifest(
        result,
        strategies=[
            {
                "class": "FavoriteLongshot",
                "params": {"band": BAND, "qty": 10, "window_hours": [24, 12]},
            }
        ],
        fingerprint=data_fingerprint(snapshots),
        trial_context={
            "sweep_id": None,
            "n_trials_in_family": 1,
            "prereg": "prereg_favlong_backtest.md",
        },
    )
    print(f"[favlong] manifest {manifest}")


if __name__ == "__main__":
    main()
