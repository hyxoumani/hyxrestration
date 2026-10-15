"""Tier-1 claims-ladder kill-test scorer (pre-registered, count-gated).

    python -m simulator.run_econ_claims [--db data/hyxlab.duckdb] [--pair ICSA|CCSA]
                                        [--second-look]

Executes exactly the configuration bound in
docs/hyxpredict/prereg_econ_claims_backtest.md. The registration scores
ONCE, at 26 SCORABLE events, and a scorable event is decided from the
markets, snapshots and vintage table alone -- settled, quoted inside the
decision window, prior-week print present as-of decision time. No price
is compared with any outcome to count them. Under the count this prints
the count and exits 0; it does not replay and prints no PnL. The single
pre-declared second read (`--second-look`) opens at 40.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime
from math import comb
from pathlib import Path
from statistics import median_low

from hyxlab.models import MarketInfo, Snapshot
from hyxlab.store import attach_wait_block, attach_wait_line, held_open, reset_attach_waits
from simulator.capabilities import live_feed_caps
from simulator.features import FeatureView
from simulator.harness import data_fingerprint, write_manifest
from simulator.iterate import deflated_sharpe
from simulator.sim import Simulator
from strategies.econ_claims import HIST_N, PAIRS, ClaimsPair, EconClaimsRW, fit_model

MIN_EVENTS = 26  # the registered read
SECOND_LOOK_EVENTS = 40  # the one pre-declared second read
MIN_TRADED = 20  # under this the read is UNDERPOWERED
MIN_ROI = 0.01
ALPHA = 0.05
N_TRIALS = 2  # two pairs registered


def sign_test_k(n: int, alpha: float = ALPHA) -> int:
    """Smallest k with P(Bin(n, 1/2) >= k) <= alpha."""
    total = 2**n
    tail = 0
    for k in range(n, -1, -1):
        tail += comb(n, k)
        if tail / total > alpha:
            return k + 1
    return 0


def decision_window(info: MarketInfo, strat: EconClaimsRW) -> tuple[datetime, datetime]:
    return info.close_time - strat.window[0], info.close_time - strat.window[1]


def scorable_events(
    markets: dict[tuple[str, str], MarketInfo],
    snapshots: list[Snapshot],
    fv: FeatureView,
    pair: ClaimsPair,
    strat: EconClaimsRW,
) -> dict[date, dict]:
    """Events (close dates) the registration can score, with the reason
    each excluded one is out. Reads NO outcome against NO price: settled
    is a yes/no presence test, the window is a timestamp test, the model
    is fit from the vintage table only."""
    by_event: dict[date, list[MarketInfo]] = defaultdict(list)
    for info in markets.values():
        if info.series == pair.market_series and info.close_time is not None:
            by_event[info.close_time.date()].append(info)
    in_window: dict[tuple[str, str], datetime] = {}
    for s in snapshots:
        info = markets.get((s.venue, s.market_id))
        if info is None or info.series != pair.market_series or info.close_time is None:
            continue
        lo, hi = decision_window(info, strat)
        if lo <= s.ts <= hi and (s.venue, s.market_id) not in in_window:
            in_window[(s.venue, s.market_id)] = s.ts
    out: dict[date, dict] = {}
    for ev, infos in sorted(by_event.items()):
        settled = all(i.result in ("yes", "no") for i in infos)
        quoted = [i for i in infos if (i.venue, i.market_id) in in_window]
        if not settled:
            out[ev] = {"scorable": False, "why": "not settled"}
            continue
        if not quoted:
            out[ev] = {"scorable": False, "why": "no snapshot in decision window"}
            continue
        ts = min(in_window[(i.venue, i.market_id)] for i in quoted)
        model, why = fit_model(fv.econ_series_asof(pair.series_id, ts, HIST_N), ev, pair)
        if model is None:
            out[ev] = {"scorable": False, "why": why}
            continue
        out[ev] = {"scorable": True, "strikes": len(quoted), "decision_ts": str(ts)}
    return out


def score(settled: list, n_scorable: int) -> dict:
    """Endpoints + registered verdict. Thresholds are the registration's."""
    per_event: dict[date, float] = defaultdict(float)
    cost = fees = payout = 0.0
    for f, info, p in settled:
        per_event[info.close_time.date()] += p - f.qty * f.price - f.fee
        cost += f.qty * f.price
        fees += f.fee
        payout += p
    n_t = len(per_event)
    wins = sum(1 for v in per_event.values() if v > 0.0)
    k = sign_test_k(n_t) if n_t else None
    pnl = payout - cost - fees
    roi = pnl / cost if cost else None
    halves = {"H1": 0.0, "H2": 0.0}
    if n_t:
        med = median_low(per_event)
        for ev, v in per_event.items():
            halves["H1" if ev <= med else "H2"] += v
    t_n = n_t >= MIN_TRADED
    t_sign = k is not None and wins >= k
    t_roi = roi is not None and roi > MIN_ROI
    t_halves = halves["H1"] > 0 and halves["H2"] > 0
    if not t_n:
        verdict = f"UNDERPOWERED ({n_t} < {MIN_TRADED} traded events)"
    elif t_sign and t_roi and t_halves:
        verdict = "SURVIVE (exploratory -- atlas contact, family size 2)"
    else:
        verdict = "FAIL (kill)"
    returns = [v for v in per_event.values()]
    dsr = deflated_sharpe(returns, n_trials=N_TRIALS) if n_t >= 2 else None
    return {
        "scorable_events": n_scorable,
        "traded_events": n_t,
        "event_wins": wins,
        "sign_test_k": k,
        "fills": len(settled),
        "cost": round(cost, 2),
        "fees": round(fees, 2),
        "payout": round(payout, 2),
        "pnl": round(pnl, 2),
        "roi": round(roi, 4) if roi is not None else None,
        "halves_pnl": {h: round(v, 2) for h, v in halves.items()},
        "per_event_pnl": {str(ev): round(v, 2) for ev, v in sorted(per_event.items())},
        "thresholds": {
            "traded_ge_20": t_n,
            "wins_ge_k": t_sign,
            "roi_gt_1pct": t_roi,
            "both_halves_positive": t_halves,
        },
        "psr_supplementary": (
            {kk: round(v, 4) for kk, v in dsr._asdict().items()} if dsr else None
        ),
        "verdict": verdict,
    }


def gate(n_scorable: int, second_look: bool) -> str | None:
    """The registered count gate; a string is the refusal."""
    need = SECOND_LOOK_EVENTS if second_look else MIN_EVENTS
    if n_scorable < need:
        return f"GATED: {n_scorable} scorable events < {need} registered; no replay, no PnL"
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Tier-1 claims-ladder kill-test (count-gated)")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--pair", choices=sorted(PAIRS), default="ICSA")
    ap.add_argument("--second-look", action="store_true")
    args = ap.parse_args()
    pair = PAIRS[args.pair]
    strat = EconClaimsRW(pair=pair)

    reset_attach_waits()
    # `held_open`: this is the 5-minute collector's file, and the hold is
    # the whole load (markets + snapshots + the vintage table), not a
    # query -- measured and printed after the release (#91, #96).
    with held_open(args.db, read_only=True) as store:
        # Bounded load (test_markets_load_discipline): the series' ids only.
        ids = [
            r[0]
            for r in store.conn.execute(
                "SELECT market_id FROM markets WHERE venue='kalshi' AND series=? ORDER BY 1",
                [pair.market_series],
            ).fetchall()
        ]
        markets = store.markets(venue="kalshi", market_ids=ids)
        rows = store.conn.execute(
            "SELECT venue, market_id, ts, yes_bid, yes_ask, no_bid, no_ask, yes_bid_size,"
            " yes_ask_size, no_bid_size, no_ask_size, last_price, volume, open_interest"
            " FROM snapshots WHERE venue='kalshi' AND market_id IN"
            " (SELECT market_id FROM markets WHERE venue='kalshi' AND series=?)"
            " ORDER BY ts, venue, market_id",
            [pair.market_series],
        ).fetchall()
        snapshots = [Snapshot(*r) for r in rows]
        fv = FeatureView.from_store(store)
    # After the release: `held_s` is charged by the close.
    print(attach_wait_line(attach_wait_block(db=Path(args.db).name), prefix="[econ-claims]"))

    events = scorable_events(markets, snapshots, fv, pair, strat)
    n_scorable = sum(1 for e in events.values() if e["scorable"])
    print(f"[econ-claims] pair {args.pair}: {len(ids)} markets, {len(snapshots)} snapshots")
    print(json.dumps({str(k): v for k, v in events.items()}, indent=1))
    refusal = gate(n_scorable, args.second_look)
    if refusal:
        print(refusal)
        return

    sim = Simulator(
        markets, [strat], data_capabilities=live_feed_caps(snapshots), features=fv, latency=0.0
    )
    result = sim.run(snapshots)
    settled = []
    for f in result.fills:
        info = markets.get((f.venue, f.market_id))
        if info is None or info.result not in ("yes", "no"):
            continue
        settled.append((f, info, f.qty * (1.0 if info.result == f.side else 0.0)))
    block = score(settled, n_scorable)
    print(json.dumps(block, indent=1))
    manifest = write_manifest(
        result,
        strategies=[
            {
                "class": "EconClaimsRW",
                "params": {"pair": args.pair, "edge": 0.10, "qty": 10, "window_minutes": [60, 50]},
            }
        ],
        fingerprint=data_fingerprint(snapshots),
        trial_context={
            "sweep_id": None,
            "n_trials_in_family": N_TRIALS,
            "prereg": "prereg_econ_claims_backtest.md",
            "second_look": args.second_look,
        },
    )
    print(f"[econ-claims] manifest {manifest}")


if __name__ == "__main__":
    main()
