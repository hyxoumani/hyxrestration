"""Tier-1 spread-conditioned favorite backtest runner (pre-registered).

    python -m simulator.run_favlong_tight [--db data/hyxlab.duckdb]

Executes exactly the configuration bound in
docs/hyxpredict/prereg_favlong_tight_backtest.md: candles → Simulator →
endpoints, for BOTH registered bands in one replay pass (A = v1's
[0.80, 0.95], B = its untested complement [0.95, 0.99]). Neither band is
the headline; both blocks print and both are appended to the
registration unmodified.

Thresholds are read off the registration, not chosen here:
SURVIVE requires net ROI > +1.0%, n >= 2,000 settled fills, positive in
>= 4 categories holding >= 100 fills, positive in BOTH time halves, and
positive net ROI in each of the band's two sub-halves. A band under
2,000 fills reads UNDERPOWERED — it neither survives nor kills.
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
from strategies.fav_long_tight import FavLongTight

# Binding: band, sub-band split, and the strategy name that carries its
# fills through the shared replay.
BANDS = {
    "A": {"band": (0.80, 0.95), "split": 0.875, "name": "fav_long_tight_a"},
    "B": {"band": (0.95, 0.99), "split": 0.97, "name": "fav_long_tight_b"},
}
MIN_FILLS = 2000  # below this the band reads UNDERPOWERED, not FAIL
MIN_ROI = 0.01
CAT_MIN_FILLS = 100
CAT_MIN_POSITIVE = 4


def _band_block(spec: dict, settled: list, categories: dict) -> dict:
    """Endpoints + registered verdict for one band. No thresholds decided here."""
    lo, hi = spec["band"]
    n = len(settled)
    if not n:
        return {"settled_fills": 0, "verdict": "UNDERPOWERED (no settled fills)"}

    prices = [f.price for f, _, _ in settled]
    mean_price = sum(prices) / n
    g2_ok = all(lo <= p <= hi for p in prices)
    if not g2_ok:
        return {
            "settled_fills": n,
            "mean_entry_price": round(mean_price, 4),
            "verdict": f"ABORT G2: entry prices outside [{lo}, {hi}]; band invalid.",
        }

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

    sub = {"low": [0.0, 0.0], "high": [0.0, 0.0]}  # [pnl, cost]
    for f, _info, p in settled:
        k = "low" if f.price < spec["split"] else "high"
        sub[k][0] += p - f.qty * f.price - f.fee
        sub[k][1] += f.qty * f.price

    big_cats = {c: v for c, v in by_cat.items() if v["n"] >= CAT_MIN_FILLS}
    pos_cats = sorted(c for c, v in big_cats.items() if v["pnl"] > 0)
    t_roi = roi > MIN_ROI
    t_n = n >= MIN_FILLS
    t_cat = len(pos_cats) >= CAT_MIN_POSITIVE
    t_halves = halves["H1"] > 0 and halves["H2"] > 0
    t_sub = all(v[0] > 0 for v in sub.values() if v[1] > 0)

    if not t_n:
        verdict = f"UNDERPOWERED ({n} < {MIN_FILLS} settled fills)"
    elif t_roi and t_cat and t_halves and t_sub:
        verdict = "SURVIVE (exploratory — family size 2)"
    else:
        verdict = "FAIL (kill)"

    returns = [(p - f.qty * f.price - f.fee) / (f.qty * f.price) for f, _, p in settled]
    dsr = deflated_sharpe(returns, n_trials=2)  # family size 2, both bands

    return {
        "band": [lo, hi],
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
        "sub_bands": {
            k: {"pnl": round(v[0], 2), "roi": round(v[0] / v[1], 4) if v[1] else None}
            for k, v in sub.items()
        },
        "thresholds": {
            "roi_gt_1pct": t_roi,
            "n_ge_2000": t_n,
            "cats_positive_ge_4": t_cat,
            "n_cats_ge_100_fills": len(big_cats),
            "positive_cats": pos_cats,
            "both_halves_positive": t_halves,
            "both_sub_bands_positive": t_sub,
        },
        "psr_supplementary": {k: round(v, 4) for k, v in dsr._asdict().items()},
        "verdict": verdict,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Tier-1 spread-conditioned favorite kill-test")
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
    strats = []
    for spec in BANDS.values():
        s = FavLongTight(band=spec["band"])  # binding defaults otherwise
        s.name = spec["name"]
        strats.append(s)
    sim = Simulator(markets, strats, data_capabilities=candle_feed_caps(snapshots))
    result = sim.run(snapshots)

    settled: dict[str, list] = {name: [] for name in (s["name"] for s in BANDS.values())}
    for f in result.fills:
        info = markets.get((f.venue, f.market_id))
        if info is None or info.result not in ("yes", "no"):
            continue
        payout = f.qty * (1.0 if info.result == f.side else 0.0)
        settled[f.strategy].append((f, info, payout))

    blocks = {
        label: _band_block(spec, settled[spec["name"]], categories)
        for label, spec in BANDS.items()
    }
    for label, block in blocks.items():
        lo, hi = BANDS[label]["band"]
        print(f"\n-- band {label} = [{lo}, {hi}] --")
        print(json.dumps(block, indent=1))

    verdicts = [b.get("verdict", "") for b in blocks.values()]
    if all(v.startswith("FAIL") for v in verdicts):
        print("\nBOTH BANDS FAIL -> the favorite-longshot family is CLOSED (binding).")

    manifest = write_manifest(
        result,
        strategies=[
            {
                "class": "FavLongTight",
                "params": {
                    "band": list(spec["band"]),
                    "qty": 10,
                    "window_hours": [24, 12],
                    "max_spread_ticks": 1,
                },
            }
            for spec in BANDS.values()
        ],
        fingerprint=data_fingerprint(snapshots),
        trial_context={
            "sweep_id": None,
            "n_trials_in_family": 2,
            "prereg": "prereg_favlong_tight_backtest.md",
        },
    )
    print(f"[favlong-tight] manifest {manifest}")


if __name__ == "__main__":
    main()
