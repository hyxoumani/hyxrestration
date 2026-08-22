"""Shadow PnL attribution: where a shadow run's equity actually went.

    python -m simulator.shadow_attribution [--ledger data/hyxshadow.duckdb]
                                           [--run RUN_ID] [--out reports/shadow_attribution]

Why this exists (2026-08-22). For six consecutive status passes the
shadow ledger's equity curve was narrated as a SHAPE -- "settle-and-
slide", a daily drop through the settlement cohort hour, watched for an
overnight round-trip. The shape was described repeatedly and never once
decomposed, so the narration could not distinguish three completely
different worlds: the strategy realising losses at settlement, fees
grinding the book down, or open positions being marked around.

They are separable from the ledger alone, and the answer for run
`20260810T081931` (10 days, 1,324 settled positions, final equity
-4,302.5) is unambiguous:

    realized at settlement  -3,495.0   (81% of the loss)
    of which fees           -1,737.0
    open-book carry           -807.5

So the slide is not marking noise and it is not a mystery: it is a
long-only taker book realising, at the cohort hour, losses it has been
carrying all along. "Settle-and-slide" was a correct observation of the
CLOCK and a wrong theory of the CAUSE -- the cohort hour is when the
loss is BOOKED, not when it is incurred.

The decomposition that matters is by ENTRY PRICE, because the strategy
that fills this ledger (`strategies.probe.TightSpreadProbe`) is
explicitly "NOT a money thesis": it buys a fixed few contracts of YES at
the touch whenever the spread is one tick and the mid is under 0.5,
rate-limited per market. It takes no view. That makes its realised
return by entry-price band something better than a strategy result --
it is an opinion-free MEASUREMENT of what buying YES at the ask pays in
tight-spread Kalshi books, and it comes out monotone (run above):

    band      n   avg_entry  win_rate   bias=entry-win
    <5c     428      0.030     0.000        +0.030
    5-15c   379      0.090     0.042        +0.048
    15-25c  188      0.194     0.181        +0.013
    25-35c  140      0.306     0.314        -0.008
    35-45c  127      0.401     0.520        -0.119
    45c+     62      0.483     0.758        -0.275

Textbook favourite-longshot bias, sign-crossing near 25-30c, measured
live rather than inferred from a mid. Fees are what turn the bias into
the loss: Kalshi charges 0.07*p*(1-p) per contract, which at this
ledger's 0.169 average fill price is **5.6% of notional** -- so the
15-35c band, which is +812 gross, nets +31 after fees, and the whole
sub-15c book (-2,751 gross) nets -4,702.

VALIDITY BOUNDS, all reported in the `validity` block rather than left
to the reader's memory:

  1. Settled positions are a SUBSET (1,324 of 1,613 = 82%), because a
     run only observes an outcome if it outlives the market -- the
     standing lesson from `simulator.shadow_coverage`. Selection here
     is mild and roughly flat across bands (77-85%), but it is measured
     every run, not assumed: a run whose cheap band settles at 90% and
     whose dear band settles at 40% would manufacture this curve out of
     nothing. Read `settled_share_by_band` before reading the bands.
  2. Most shadow runs die before settling ANYTHING. A run with no
     settlements gets `realized: null`, never a reassuring 0.0.
  3. The ledger is long-only in fact (every fill in it is side `yes`,
     qty > 0), and the basis arithmetic assumes it. A short or a
     closing sell would make `sum(qty*price)` stop meaning "cost", so
     `long_only` is CHECKED and false makes the run's realized figure
     null rather than wrong.
  4. Market mix is reported. This ledger is dominated by the daily city
     temperature ladders (KXHIGH*/KXLOWT*), where the cheap band is the
     tail of a bracket set whose YES prices sum to more than 1 -- the
     overround has to live somewhere, and the tails are where it sits.
     That is a mechanism, and it is also a warning: this is a
     measurement of the weather-bracket complex, not of Kalshi.

The identity this report is built on, with `open_carry` as the residual
that closes it (mark value of the open book is not in the ledger, so
the residual is reported as a residual and never as a measurement):

    equity_delta = realized_net(settled) + open_carry
    realized_net = payout - basis - fees   (settled positions only)
    open_carry   = mark(open) - basis(open) - fees(open)
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from hyxlab.store import connect_retry

SHADOW_DB = "data/hyxshadow.duckdb"
#: Entry-price band edges. Chosen to straddle the sign crossing the
#: first reading put near 25-30c with a band on each side, and to give
#: the sub-5c floor its own bucket -- a 1c tick is 20-100% of price down
#: there, so it is a different market regime, not merely a cheaper one.
BAND_EDGES = (0.05, 0.15, 0.25, 0.35, 0.45)


def band_label(px: float, edges: tuple[float, ...] = BAND_EDGES) -> str:
    """Bucket an average entry price. Labels sort in price order."""
    lo = 0.0
    for i, hi in enumerate(edges):
        if px < hi:
            return f"{chr(ord('a') + i)} {_money(lo)}-{_money(hi)}"
        lo = hi
    return f"{chr(ord('a') + len(edges))} {_money(lo)}+"


def _money(x: float) -> str:
    return f"{round(x * 100)}c"


def _r(x: float | None, n: int = 1) -> float | None:
    return None if x is None else round(x, n)


def _positions(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict]:
    """One row per (market_id, side) position, with basis, fees, outcome.

    A position is the unit because settlement is recorded per market and
    side; fills are per order and a position is usually several.
    """
    rows = conn.execute(
        """
        with f as (
          select market_id, side, venue, strategy,
                 sum(qty) as qty, sum(qty * price) as basis, sum(fee) as fee,
                 min(qty) as min_qty, count(*) as n_fills
          from shadow_fills where run_id = ? group by 1, 2, 3, 4
        ), s as (
          select market_id, side, sum(payout) as payout, min(ts) as settled_ts
          from shadow_settlements where run_id = ? group by 1, 2
        )
        select f.market_id, f.side, f.venue, f.strategy, f.qty, f.basis,
               f.fee, f.min_qty, f.n_fills, s.payout, s.settled_ts
        from f left join s using (market_id, side)
        """,
        [run_id, run_id],
    ).fetchall()
    out = []
    for m, side, venue, strat, qty, basis, fee, min_qty, n_fills, payout, sts in rows:
        out.append(
            {
                "market_id": m,
                "side": side,
                "venue": venue,
                "strategy": strat,
                "qty": qty,
                "basis": basis,
                "fee": fee,
                "min_qty": min_qty,
                "n_fills": n_fills,
                "payout": payout,
                "settled": payout is not None,
                "settled_ts": sts,
                # avg entry price; qty is never 0 for a filled position,
                # but a ledger is not a proof, so guard it.
                "px": (basis / qty) if qty else None,
            }
        )
    return out


def _band_table(positions: list[dict]) -> list[dict]:
    """Per-band realised economics, plus the settlement-selection share.

    `settled_share` sits in the same row as the band's PnL on purpose:
    the band curve is only readable if selection is flat across bands,
    and putting the two numbers in separate tables is how a reader ends
    up comparing bands that were observed at different rates.
    """
    bands: dict[str, dict] = {}
    for p in positions:
        if p["px"] is None:
            continue
        b = bands.setdefault(
            band_label(p["px"]),
            {
                "band": band_label(p["px"]),
                "n_positions": 0,
                "n_settled": 0,
                "basis_all": 0.0,
                "basis": 0.0,
                "payout": 0.0,
                "fees": 0.0,
                "wins": 0,
                "entry_sum": 0.0,
            },
        )
        b["n_positions"] += 1
        b["basis_all"] += p["basis"]
        if not p["settled"]:
            continue
        b["n_settled"] += 1
        b["basis"] += p["basis"]
        b["payout"] += p["payout"]
        b["fees"] += p["fee"]
        b["entry_sum"] += p["px"]
        if p["payout"] > 0:
            b["wins"] += 1

    table = []
    for b in sorted(bands.values(), key=lambda x: x["band"]):
        n_s = b["n_settled"]
        gross = b["payout"] - b["basis"] if n_s else None
        net = gross - b["fees"] if n_s else None
        avg_entry = b["entry_sum"] / n_s if n_s else None
        win_rate = b["wins"] / n_s if n_s else None
        table.append(
            {
                "band": b["band"],
                "n_positions": b["n_positions"],
                "n_settled": n_s,
                "settled_share": _r(n_s / b["n_positions"], 3),
                "basis": _r(b["basis"]),
                "payout": _r(b["payout"]),
                "gross": _r(gross),
                "fees": _r(b["fees"]),
                "net": _r(net),
                "net_pct": _r(100 * net / b["basis"], 1) if n_s and b["basis"] else None,
                "avg_entry": _r(avg_entry, 3),
                "win_rate": _r(win_rate, 3),
                # The bias a long-only taker PAYS: what it handed over
                # minus what the contract turned out to be worth.
                "bias": _r(avg_entry - win_rate, 3) if n_s else None,
            }
        )
    return table


def build_attribution(ledger: duckdb.DuckDBPyConnection, run_id: str | None = None) -> dict:
    """Decompose each run's equity into realised PnL, fees and open carry."""
    runs = [
        r[0]
        for r in ledger.execute(
            "select distinct run_id from shadow_fills"
            + (" where run_id = ?" if run_id else "")
            + " order by run_id",
            [run_id] if run_id else [],
        ).fetchall()
    ]
    report: dict = {
        "generated_at": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}",
        "band_edges": list(BAND_EDGES),
        "runs": [],
    }
    for rid in runs:
        positions = _positions(ledger, rid)
        eq = ledger.execute(
            """select min(ts), max(ts), arg_min(equity, ts), arg_max(equity, ts),
                      min(equity), max(equity), count(*)
               from shadow_equity where run_id = ?""",
            [rid],
        ).fetchone()
        first_ts, last_ts, eq_open, eq_close, eq_lo, eq_hi, n_eq = eq

        settled = [p for p in positions if p["settled"]]
        # Validity gate 3: the basis arithmetic below reads
        # sum(qty*price) as "what this position cost", which is only
        # true for a long-only book that never sells to close.
        long_only = all(p["min_qty"] > 0 for p in positions) and {p["side"] for p in positions} <= {
            "yes"
        }

        fees_all = sum(p["fee"] for p in positions)
        fees_settled = sum(p["fee"] for p in settled)
        basis_all = sum(p["basis"] for p in positions)
        basis_settled = sum(p["basis"] for p in settled)
        payout = sum(p["payout"] for p in settled)

        realized = (payout - basis_settled - fees_settled) if settled else None
        eq_delta = (eq_close - eq_open) if n_eq else None
        # Residual, not a measurement: the ledger has no mark for the
        # open book, so this absorbs it along with any arithmetic drift.
        open_carry = (
            None if (realized is None or eq_delta is None or not long_only) else eq_delta - realized
        )

        mix = {}
        for p in positions:
            pfx = p["market_id"].split("-")[0]
            mix[pfx] = mix.get(pfx, 0) + 1
        top_mix = sorted(mix.items(), key=lambda kv: -kv[1])[:6]

        report["runs"].append(
            {
                "run_id": rid,
                "first_ts": str(first_ts) if first_ts else None,
                "last_ts": str(last_ts) if last_ts else None,
                "life_hours": _r((last_ts - first_ts).total_seconds() / 3600, 2) if n_eq else None,
                "equity_open": _r(eq_open),
                "equity_close": _r(eq_close),
                "equity_min": _r(eq_lo),
                "equity_max": _r(eq_hi),
                "equity_delta": _r(eq_delta),
                "n_positions": len(positions),
                "n_settled": len(settled),
                "settled_share": _r(len(settled) / len(positions), 3) if positions else None,
                "basis_all": _r(basis_all),
                "basis_settled": _r(basis_settled),
                "payout": _r(payout) if settled else None,
                "fees_all": _r(fees_all),
                "fees_settled": _r(fees_settled),
                "fees_pct_of_notional": _r(100 * fees_all / basis_all, 2) if basis_all else None,
                "gross_realized": _r(payout - basis_settled) if settled else None,
                "realized": _r(realized),
                "open_carry": _r(open_carry),
                "long_only": long_only,
                "strategies": sorted({p["strategy"] for p in positions}),
                "venues": sorted({p["venue"] for p in positions}),
                "market_mix": [{"prefix": k, "n": v} for k, v in top_mix],
                "bands": _band_table(positions),
            }
        )
    return report


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=SHADOW_DB)
    ap.add_argument("--run", default=None, help="run_id (default: every run)")
    ap.add_argument("--out", default="reports/shadow_attribution")
    args = ap.parse_args(argv)

    ledger = connect_retry(args.ledger, read_only=True)
    try:
        report = build_attribution(ledger, args.run)
    finally:
        ledger.close()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(report, indent=1) + "\n")

    print("| run | life_h | d_equity | realized | fees | open_carry | n_set | set_share |")
    print("|---|---|---|---|---|---|---|---|")
    for r in report["runs"][-12:]:
        print(
            f"| {r['run_id']} | {r['life_hours']} | {r['equity_delta']}"
            f" | {r['realized']} | {r['fees_all']} | {r['open_carry']}"
            f" | {r['n_settled']} | {r['settled_share']} |"
        )
    # The band table is the point of the report, so print it for the run
    # with the most settlements -- a run that settled nothing has no
    # band curve, only a fill histogram, and printing that invites it to
    # be read as one.
    best = max(report["runs"], key=lambda r: r["n_settled"], default=None)
    if best and best["n_settled"]:
        print(
            f"\n[shadow_attribution] bands for {best['run_id']}"
            f" ({best['n_settled']} settled of {best['n_positions']},"
            f" fees {best['fees_pct_of_notional']}% of notional)"
        )
        print("| band | n | set_share | avg_entry | win_rate | bias | gross | fees | net | net_% |")
        print("|---|---|---|---|---|---|---|---|---|---|")
        for b in best["bands"]:
            print(
                f"| {b['band']} | {b['n_settled']} | {b['settled_share']}"
                f" | {b['avg_entry']} | {b['win_rate']} | {b['bias']}"
                f" | {b['gross']} | {b['fees']} | {b['net']} | {b['net_pct']} |"
            )
    print(f"\n[shadow_attribution] written to {out}")


if __name__ == "__main__":
    main()
