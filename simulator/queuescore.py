"""Maker calibration bracket: crossing rule vs queue-position bounds.

    python -m simulator.queuescore [--hours 24] [--markets N] [--qty 5]

Walks archived Kalshi windows with a deterministic join-the-touch
virtual maker (GTC bid at the current best yes-bid, one order per
market at a time, 30-min lifetime, 10-min re-arm) and scores every
resting interval under BOTH fill models:

- the sim's conservative crossing rule (fills when the ask reaches the
  limit; what backtests award today), and
- FIFO queue-position bounds from L2 deltas + the trade tape
  (simulator/queuebounds.py): pessimistic and optimistic fills.

The output bracket is the maker analogue of the taker divergence
report: how much the crossing rule under- or over-awards against what
the queue evidence supports. Ledger-only; nothing is traded.

Known v1 simplifications: coverage gaps aren't specially handled inside
an order's lifetime (orders are 30-min capped, and gap-heavy windows
show up as unmatched noise, not bias); the crossing rule is evaluated
once per snapshot at full remaining qty capped at displayed ask size.

Coverage note: by default markets are the top-N Kalshi series by stream
trade-print count. In practice these are dominated by `KXHIGH*` weather
high-temp markets (the most-active stream series), so the default
bracket's conclusions generalize to weather high-temp — the report's
`market_composition` field records the actual series mix per run. To
validate a maker registration in another category, pass `--series`
(e.g. `--series KXCPI,KXCPIYOY,KXFED`) to run the bracket against that
category's markets and close the coverage gap.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hyxlab.store import connect_retry
from hyxlab.streamstore import BookEvent
from simulator.bookreplay import BOOK_GAPS, BookReplayer, replay_snapshots
from simulator.queuebounds import QueueTracker, consuming_print

STREAM_DB = "data/hyxstream.duckdb"
LIFETIME = timedelta(minutes=30)
COOLDOWN = timedelta(minutes=10)


@dataclass
class VirtualOrder:
    market_id: str
    side: str
    price: float
    qty: float
    placed: datetime
    tracker: QueueTracker
    crossed_at: datetime | None = None
    crossed_qty: float = 0.0

    def summary(self) -> dict:
        t = self.tracker
        return {
            "market_id": self.market_id,
            "price": self.price,
            "placed": str(self.placed),
            "crossing_fill": self.crossed_qty,
            "crossing_at": str(self.crossed_at) if self.crossed_at else None,
            "fills_pess": t.filled_pess,
            "fills_opt": t.filled_opt,
            "pess_at": str(t.fill_events[0][0]) if t.fill_events else None,
        }


def order_key(o: VirtualOrder) -> list:
    """Identity of a virtual order across runs: same market, same seat time,
    same limit. Deterministic, so two runs whose windows overlap re-derive
    byte-identical keys for the orders they share."""
    return [o.market_id, str(o.placed), o.price]


def independence_vs_prior(out_dir: Path, orders: list[VirtualOrder], composition: dict) -> dict:
    """How much of this run's evidence is NEW since the last comparable run.

    The window is trailing (`max(recv_ts) - N hours`), so re-running sooner
    than `--hours` re-scores orders the prior run already counted. A 336h
    econ bracket re-run 18h later shares ~90% of its orders; consecutive
    readings then agree because they are largely the same measurement, not
    because a signal confirmed. Weather brackets escape this only
    incidentally — `KXHIGH*` markets expire daily, so the top-N market set
    churns on its own.

    Comparison is against the most recent prior report sharing a series with
    this run, which keeps weather and econ sequences from being compared to
    each other. Returns null counts when there is no comparable prior run.

    "New since the last run" is NOT "never scored before", because the scored
    market set is only the top-N by print count (`select_markets`) and that
    set churns: a strike that drops out of one run's top-N and returns in the
    next reads as fresh evidence against the immediate prior while an older
    run already counted it. Measured over the archive, this inflates
    `new_share` by up to ~1.9x on econ re-runs (07-24 0.265 vs 0.137 honest,
    driven by 262 `KXCPI-26JUL-T-0.1` orders absent from the prior top-N).
    `*_vs_all` therefore compares against the union of EVERY comparable prior
    and is the honest novelty read; `new_share` is kept unchanged for
    cross-report comparability with reports written before this tier.
    """
    keys = {tuple(order_key(o)) for o in orders}
    prior_name, prior_keys = None, None
    union: set[tuple] = set()
    n_priors = 0
    for path in sorted(out_dir.glob("*.json"), reverse=True):
        try:
            prior = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not set(prior.get("market_composition", {})) & set(composition):
            continue
        prior_keys_here = {
            (d["market_id"], d["placed"], d["price"]) for d in prior.get("orders_detail", [])
        }
        if prior_keys is None:
            prior_name, prior_keys = path.name, prior_keys_here
        union |= prior_keys_here
        n_priors += 1
    if prior_keys is None:
        return {
            "prior_report": None,
            "orders_new": None,
            "orders_shared": None,
            "new_share": None,
            "priors_compared": 0,
            "orders_new_vs_all": None,
            "new_share_vs_all": None,
        }
    new = len(keys - prior_keys)
    new_all = len(keys - union)
    return {
        "prior_report": prior_name,
        "orders_new": new,
        "orders_shared": len(keys & prior_keys),
        "new_share": round(new / len(keys), 4) if keys else None,
        "priors_compared": n_priors,
        "orders_new_vs_all": new_all,
        "new_share_vs_all": round(new_all / len(keys), 4) if keys else None,
    }


def event_ticker(market_id: str) -> str:
    """The Kalshi EVENT a market belongs to: `SERIES-EVENT-STRIKE` -> `SERIES-EVENT`.

    Every strike on one event resolves off a single underlying path — the three
    `KXHIGHNY-26JUL28-B{77.5,79.5,81.5}` brackets are one New York temperature,
    and `KXCPI-26JUL-T{-0.1,0.0,0.1}` is one CPI print. Split from the LEFT, not
    with `rsplit`: strike suffixes can themselves contain '-' (`...-T-0.1`),
    while Kalshi series tickers do not.
    """
    return "-".join(market_id.split("-", 2)[:2])


SIGN_ALPHA = 0.05


def sign_test_p(agreeing: int, decisive: int) -> float:
    """One-sided binomial sign test: P(X >= agreeing | X ~ Bin(decisive, 0.5)).

    The null is "the fill models disagree in a direction that is a coin flip per
    independent unit". Returns 1.0 when nothing leans, which is the honest read
    of a run with no direction to test.
    """
    if decisive <= 0:
        return 1.0
    agreeing = max(agreeing, 0)
    return sum(math.comb(decisive, i) for i in range(agreeing, decisive + 1)) / 2**decisive


def _direction_tier(nets: dict[str, int], agg: int) -> dict:
    """Shared over/under/tied split, majority test and sign test for one unit.

    `robust` is a BARE-MAJORITY test and that is much weaker than it reads: with
    an odd number of leaning units a strict majority always exists, so at k=3 —
    the default weather bracket's usual shape — it can only fail when the
    aggregate sign contradicts the unit majority. Measured over the 34-run
    archive it fires on 24 runs, 10 of them at a sign-test p of exactly 0.50.

    `sign_p` is therefore reported alongside it, and `min_sign_p` = 2^-k is the
    p this run would have produced had EVERY leaning unit agreed — the run's
    power ceiling. When `min_sign_p` > SIGN_ALPHA the run could not have
    produced a significant direction whatever the data did, which is a property
    of the bracket's configuration (top-N markets) and not of the fill models.
    """
    over = sum(1 for v in nets.values() if v > 0)
    under = sum(1 for v in nets.values() if v < 0)
    decisive = over + under
    agreeing = over if agg > 0 else under if agg < 0 else 0
    abs_total = sum(abs(v) for v in nets.values())
    sign_p = sign_test_p(agreeing, decisive) if agg != 0 else 1.0
    min_sign_p = sign_test_p(decisive, decisive) if decisive else None
    return {
        "units": len(nets),
        "abs_net": abs_total,
        "top_net_share": (
            round(max(abs(v) for v in nets.values()) / abs_total, 4) if abs_total else None
        ),
        "net_over": over,
        "net_under": under,
        "net_tied": len(nets) - decisive,
        "robust": agg != 0 and agreeing * 2 > decisive,
        "sign_p": round(sign_p, 6),
        "min_sign_p": None if min_sign_p is None else round(min_sign_p, 6),
        "significant": agg != 0 and sign_p <= SIGN_ALPHA,
    }


def concentration_by_market(orders: list[VirtualOrder]) -> dict:
    """Is the crossing-vs-queue disagreement a fill-model bias, or one market?

    The headline bracket compares `crossing_filled` against the queue bounds
    as if the run's N virtual orders were N independent draws. They are not:
    every order in a market is seated on the SAME book, so a market whose ask
    happens to walk down repeatedly contributes dozens of same-signed
    disagreements off one price path. This is the maker-side instance of the
    unit-of-independence class that the atlas settlement-day tier addresses
    (`docs/wiki/`): the coarser unit here is the market.

    Reported per run:

    - `net_disagreement` = crossing_but_not_pess - pess_but_not_crossing, the
      aggregate the headline verdict rests on;
    - `abs_net_by_market`, the same quantity summed WITHOUT letting markets
      cancel — when it dwarfs `net_disagreement`, a near-zero aggregate is
      cancellation of large opposing effects, not a tight measurement;
    - `top_market_net_share`, the tier-neutral concentration read;
    - `direction_market_robust`: the aggregate direction is supported by a
      strict majority of the markets that lean either way. False when the
      aggregate is zero (no direction to support). This is a BOUND on the
      headline verdict, deliberately harsh where markets genuinely differ —
      the headline fields are untouched for cross-report comparability, per
      the divergence-matcher and atlas-day-tier precedent.

    The market is not the coarsest unit, so the same fields are reported one
    level up over the EVENT (`direction_underlying_robust`, see
    `event_ticker`). A weather run's "8 markets" are in practice 3-4 city-days
    of strike ladders, and an econ run's are 4-5 prints; the tiers nest
    (underlyings <= markets <= orders), each strictly more conservative.

    Both tiers additionally carry `*_sign_p` / `*_min_sign_p` /
    `direction_*_significant` — a majority is not a measurement, and at the
    handful of independent units a bracket actually has, a bare majority is
    usually a coin flip. See `_direction_tier`.
    """
    per: dict[str, list[int]] = {}
    for o in orders:
        e = per.setdefault(o.market_id, [0, 0, 0])
        e[0] += 1
        if o.crossed_at is not None and o.tracker.filled_pess == 0:
            e[1] += 1
        elif o.crossed_at is None and o.tracker.filled_pess > 0:
            e[2] += 1

    nets = {m: v[1] - v[2] for m, v in per.items()}
    agg = sum(nets.values())
    mkt = _direction_tier(nets, agg)

    und_nets: dict[str, int] = {}
    for m, v in nets.items():
        u = event_ticker(m)
        und_nets[u] = und_nets.get(u, 0) + v
    und = _direction_tier(und_nets, agg)
    return {
        "markets": len(per),
        "top_market_order_share": (
            round(max(v[0] for v in per.values()) / len(orders), 4) if orders else None
        ),
        "net_disagreement": agg,
        "abs_net_by_market": mkt["abs_net"],
        "top_market_net_share": mkt["top_net_share"],
        "markets_net_over": mkt["net_over"],
        "markets_net_under": mkt["net_under"],
        "markets_net_tied": mkt["net_tied"],
        "direction_market_robust": mkt["robust"],
        "market_sign_p": mkt["sign_p"],
        "market_min_sign_p": mkt["min_sign_p"],
        "direction_market_significant": mkt["significant"],
        "underlyings": und["units"],
        "abs_net_by_underlying": und["abs_net"],
        "top_underlying_net_share": und["top_net_share"],
        "underlyings_net_over": und["net_over"],
        "underlyings_net_under": und["net_under"],
        "underlyings_net_tied": und["net_tied"],
        "direction_underlying_robust": und["robust"],
        "underlying_sign_p": und["sign_p"],
        "underlying_min_sign_p": und["min_sign_p"],
        "direction_underlying_significant": und["significant"],
        "per_underlying": [
            {"event_ticker": u, "net": und_nets[u]}
            for u in sorted(und_nets, key=lambda u: -abs(und_nets[u]))
        ],
        "per_market": [
            {"market_id": m, "orders": per[m][0], "cross_only": per[m][1], "pess_only": per[m][2]}
            for m in sorted(per, key=lambda m: -abs(nets[m]))
        ],
    }


def series_composition(orders: list[VirtualOrder]) -> dict[str, int]:
    """Count virtual orders per Kalshi series (market_id prefix before the
    first '-'), high-to-low. Surfaces the bracket's coverage: in practice
    the top-print markets are all `KXHIGH*` weather high-temp."""
    comp: dict[str, int] = {}
    for o in orders:
        series = o.market_id.split("-", 1)[0]
        comp[series] = comp.get(series, 0) + 1
    return dict(sorted(comp.items(), key=lambda kv: -kv[1]))


def select_markets(
    conn, since: datetime, top_n: int, series: list[str] | None = None
) -> list[str]:
    """Top-N Kalshi markets by stream-print count in the window that also
    carry L2 deltas (a bracket needs both tape and book). When `series` is
    given, restrict to markets whose series prefix (before the first '-')
    is in that set — this is how a bracket targets a non-weather category
    to close the coverage gap noted in the module docstring."""
    sql = (
        "SELECT t.market_id FROM stream_trades t WHERE t.venue='kalshi'"
        " AND t.recv_ts > ? AND EXISTS (SELECT 1 FROM book_events b"
        "   WHERE b.market_id = t.market_id AND b.kind='delta')"
    )
    params: list = [since]
    if series:
        placeholders = ",".join("?" for _ in series)
        sql += f" AND split_part(t.market_id, '-', 1) IN ({placeholders})"
        params.extend(series)
    sql += " GROUP BY 1 ORDER BY count(*) DESC LIMIT ?"
    params.append(top_n)
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def score_market(conn, market_id: str, since: datetime, qty: float) -> list[VirtualOrder]:
    events = conn.execute(
        "SELECT venue, market_id, recv_ts, src_ts, sid, seq, kind, side, price, qty"
        " FROM book_events WHERE market_id=? AND venue='kalshi' AND recv_ts > ?"
        " ORDER BY recv_ts, seq",
        [market_id, since],
    ).fetchall()
    trades = conn.execute(
        "SELECT recv_ts, price, qty, taker_side FROM stream_trades"
        " WHERE market_id=? AND venue='kalshi' AND recv_ts > ? ORDER BY recv_ts",
        [market_id, since],
    ).fetchall()
    gaps = conn.execute(
        f"SELECT started_at, ended_at FROM stream_gaps WHERE ended_at > ? AND {BOOK_GAPS}",
        [since],
    ).fetchall()

    orders: list[VirtualOrder] = []
    state: dict = {"open": None, "next_arm": since, "ti": 0}

    def feed():
        """Yield events in order, teeing deltas and prints into the
        open order's tracker as they stream past."""
        for e in events:
            row = BookEvent(*e)
            o: VirtualOrder | None = state["open"]
            # merge tape prints up to this event's recv_ts
            while state["ti"] < len(trades) and trades[state["ti"]][0] <= row.recv_ts:
                ts, p, q, taker = trades[state["ti"]]
                state["ti"] += 1
                if o is not None and consuming_print(o.side, o.price, taker, p):
                    o.tracker.on_print(ts, q)
            if (
                o is not None
                and row.kind == "delta"
                and row.side == o.side
                and abs(row.price - o.price) < 1e-9
            ):
                o.tracker.on_delta(row.recv_ts, row.qty)
            yield row

    replayer = BookReplayer()
    for snap in replay_snapshots(feed(), gaps=gaps, replayer=replayer):
        o: VirtualOrder | None = state["open"]
        if o is not None:
            # crossing rule: ask reached our bid → sim awards a maker fill
            if o.crossed_at is None and snap.yes_ask is not None and snap.yes_ask <= o.price + 1e-9:
                o.crossed_at = snap.ts
                o.crossed_qty = min(o.qty, snap.yes_ask_size or 0.0)
            if snap.ts - o.placed >= LIFETIME or (o.tracker.done and o.crossed_at):
                state["open"] = None
                state["next_arm"] = snap.ts + COOLDOWN
        if (
            state["open"] is None
            and snap.ts >= state["next_arm"]
            and snap.yes_bid is not None
            and (snap.yes_bid_size or 0) > 0
        ):
            tracker = QueueTracker(
                side="yes", price=snap.yes_bid, qty=qty, level_size=snap.yes_bid_size
            )
            o = VirtualOrder(market_id, "yes", snap.yes_bid, qty, snap.ts, tracker)
            orders.append(o)
            state["open"] = o
    return orders


def main() -> None:
    ap = argparse.ArgumentParser(description="maker fill-model calibration bracket")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--markets", type=int, default=8, help="top-N by prints")
    ap.add_argument(
        "--series",
        default=None,
        help="comma-separated Kalshi series prefixes to restrict to"
        " (e.g. KXCPI,KXCPIYOY,KXFED); default = all, which is weather-dominated",
    )
    ap.add_argument("--qty", type=float, default=5.0)
    ap.add_argument("--stream-db", default=STREAM_DB)
    ap.add_argument("--out", default="reports/maker_bracket")
    args = ap.parse_args()

    conn = connect_retry(args.stream_db)
    since = conn.execute(
        "SELECT max(recv_ts) - INTERVAL 1 HOUR * CAST(? AS INTEGER) FROM book_events",
        [int(args.hours)],
    ).fetchone()[0]
    series = [s.strip() for s in args.series.split(",") if s.strip()] if args.series else None
    markets = select_markets(conn, since, args.markets, series)
    print(
        f"[queuescore] window since {since}, {len(markets)} markets"
        + (f", series={series}" if series else "")
    )

    all_orders: list[VirtualOrder] = []
    for m in markets:
        orders = score_market(conn, m, since, args.qty)
        all_orders.extend(orders)
        print(f"  {m}: {len(orders)} virtual maker orders")
    conn.close()

    n = len(all_orders)
    crossed = [o for o in all_orders if o.crossed_at]
    pess = [o for o in all_orders if o.tracker.filled_pess > 0]
    opt = [o for o in all_orders if o.tracker.filled_opt > 0]
    cross_only = [o for o in all_orders if o.crossed_at and o.tracker.filled_pess == 0]
    pess_only = [o for o in all_orders if not o.crossed_at and o.tracker.filled_pess > 0]
    composition = series_composition(all_orders)
    out_dir = Path(args.out)
    report = {
        "generated_at": str(datetime.now(UTC).replace(tzinfo=None, microsecond=0)),
        "window_hours": args.hours,
        "orders": n,
        "crossing_filled": len(crossed),
        "queue_pess_filled": len(pess),
        "queue_opt_filled": len(opt),
        "crossing_but_not_pess": len(cross_only),
        "pess_but_not_crossing": len(pess_only),
        "market_composition": composition,
        "concentration": concentration_by_market(all_orders),
        "independence": independence_vs_prior(out_dir, all_orders, composition),
        "note": (
            "crossing rule = what backtests award today; queue bounds ="
            " what L2+tape evidence supports (pess is the floor)."
            " crossing_but_not_pess counts fills the sim may be inventing;"
            " pess_but_not_crossing counts real fills the sim forgoes."
            " independence.new_share is the fraction of orders not scored by"
            " the prior comparable run — a low share means this reading is"
            " mostly the prior one re-measured, not a confirmation of it."
            " Read new_share_vs_all, not new_share: the scored market set is"
            " only the top-N by print count and it churns, so a strike absent"
            " from the immediate prior but present in an older run reads as"
            " fresh against new_share while never being new evidence at all."
            " concentration treats the MARKET as the independent unit (all"
            " orders in a market ride one book): read"
            " direction_market_robust before calling any over/under verdict,"
            " and compare abs_net_by_market against net_disagreement — a"
            " near-zero aggregate built from large opposing per-market nets"
            " is cancellation, not precision. Markets are not the coarsest"
            " unit either: every strike on one EVENT (city-day, CPI print)"
            " rides one underlying path, so underlyings is the honest sample"
            " size — a weather run's 8 markets are 3-4 city-days. But"
            " direction_*_robust is only a BARE-MAJORITY test: with an odd"
            " number of leaning units a strict majority always exists, so read"
            " underlying_sign_p (one-sided binomial on the leaning underlyings)"
            " before calling any over/under verdict —"
            " direction_underlying_robust is routinely true at p=0.50. Read"
            " underlying_min_sign_p too: it is the p this run would have"
            " produced had EVERY underlying agreed, so when it exceeds 0.05 the"
            " run could not have shown a direction whatever the data did. The"
            " default --markets 8 reaches only ~3 city-days (min_sign_p 0.125),"
            " so a directional verdict needs a wider top-N, not more runs."
        ),
        "orders_detail": [o.summary() for o in all_orders],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    for k, v in report.items():
        if k != "orders_detail":
            print(f"  {k}: {v}")
    print(f"[queuescore] written to {out}")


if __name__ == "__main__":
    main()
