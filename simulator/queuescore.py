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

# Every value `_direction_tier` can return for `status`. Exhaustive on purpose:
# `direction_verdict` counts over this list, so the counts partition the four
# tier x bound readings by construction and a new status cannot be added
# without the partition test noticing.
DIRECTION_STATUSES = (
    "significant_over",
    "significant_under",
    "not_significant",
    "underpowered",
    "no_direction",
)


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

    `significant` is a BOOLEAN over outcomes that mean opposite things, which is
    the failure the atlas quoted tier hit (mistakes #32). It reads False for a
    run that tested a direction and found none (evidence AGAINST a fill-model
    bias) and for a run that could not have found one whatever the data did (NO
    evidence either way) — and the econ bracket has printed both: 2026-08-06 at
    4 underlyings, min_sign_p 0.0625 > 0.05, was structurally incapable of a
    verdict, while 2026-08-25 at 5 underlyings, min_sign_p 0.03125, genuinely
    tested and found nothing. `status` separates them and PARTITIONS the tier:

    - `no_direction`   — the aggregate is zero, so there is no sign to test;
    - `underpowered`   — `min_sign_p` > SIGN_ALPHA: no evidence either way;
    - `significant_over` / `significant_under` — tested, p <= SIGN_ALPHA;
    - `not_significant` — tested with the power to reject, and did not.

    `significant` itself is left exactly as it was, for cross-report
    comparability, per the day-tier / overlap-tier / quoted-tier precedent;
    `status` is a strict refinement of it, asserted as such in the tests.

    The `min_sign_p > SIGN_ALPHA` boundary is checked and UNREACHABLE: min_sign_p
    is 2^-k for integer k, which never equals 0.05, so `>` and `>=` are the same
    predicate here and a mutation between them survives the suite. Recorded
    rather than pinned with a fixture that cannot exist.
    """
    over = sum(1 for v in nets.values() if v > 0)
    under = sum(1 for v in nets.values() if v < 0)
    decisive = over + under
    agreeing = over if agg > 0 else under if agg < 0 else 0
    abs_total = sum(abs(v) for v in nets.values())
    sign_p = sign_test_p(agreeing, decisive) if agg != 0 else 1.0
    min_sign_p = sign_test_p(decisive, decisive) if decisive else None
    if agg == 0 or min_sign_p is None:
        status = "no_direction"
    elif min_sign_p > SIGN_ALPHA:
        status = "underpowered"
    elif sign_p <= SIGN_ALPHA:
        status = "significant_over" if agg > 0 else "significant_under"
    else:
        status = "not_significant"
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
        "status": status,
    }


def direction_verdict(concentration: dict, strict: dict) -> dict:
    """Collect the four direction readings into counts that PARTITION them.

    The bracket produces four direction readings — market and underlying, each
    against the pessimistic floor and the optimistic ceiling — and each of them
    used to be summarised by one boolean whose False collapsed "tested, no
    direction" into "could not have found one". Counting the tri-state statuses
    over `DIRECTION_STATUSES` makes the arithmetic itself the guard: the counts
    sum to 4, so a reading that carries no evidence can no longer be read as a
    measurement that came back empty. Same construction as the atlas
    `quoted_verdict` (mistakes #32).

    `powered` is the number of readings that could have rejected the coin-flip
    null at all. When it is 0, this run says nothing about fill-model direction
    however large `net_disagreement` looks — that is a property of the top-N
    market selection, not of the data.
    """
    readings = {
        "market_pess": concentration["direction_market_status"],
        "market_opt": strict["direction_market_status"],
        "underlying_pess": concentration["direction_underlying_status"],
        "underlying_opt": strict["direction_underlying_status"],
    }
    counts = dict.fromkeys(DIRECTION_STATUSES, 0)
    for status in readings.values():
        counts[status] += 1
    powered = sum(1 for v in readings.values() if v not in ("underpowered", "no_direction"))
    return {
        "readings": readings,
        "counts": counts,
        "readings_total": len(readings),
        "powered": powered,
        "significant": counts["significant_over"] + counts["significant_under"],
    }


def over_award(o: VirtualOrder, bound: str) -> bool:
    """Does the crossing rule award a fill the queue evidence does not support?

    The queue evidence is a BRACKET, not a point, so "unsupported" has two
    readings and they are not the same claim:

    - `bound="pess"` — the crossing rule fills where the PESSIMISTIC floor does
      not. This is an UPPER bound on over-award: it charges the sim for every
      order the floor misses, including those the optimistic ceiling fills,
      which lie INSIDE the bracket and are exactly the cases the bracket was
      built to call unknown.
    - `bound="opt"` — the crossing rule fills where even the OPTIMISTIC ceiling
      does not. This is a LOWER bound: no queue model supports these, so they
      are unambiguously invented.

    Because `filled_pess <= filled_opt` always (verified: 0 violations in
    21,168 archived orders), the pess reading is a superset of the opt reading
    for every order. The difference is therefore ONE-SIDED — the loose test can
    only ever read MORE over, never less — so a direction test run against the
    floor alone is biased toward "the sim over-awards" by construction, not by
    the data. Read both.

    The UNDER side needs no such split: an order the sim declines while even
    the floor fills it is a forgone real fill under either bound.
    """
    if o.crossed_at is None:
        return False
    return o.tracker.filled_opt == 0 if bound == "opt" else o.tracker.filled_pess == 0


def over_award_split(orders: list[VirtualOrder]) -> dict[str, int]:
    """Partition the crossing-vs-queue disagreement into its three real states.

    `crossing_but_not_pess` is the historical field and is NOT a fourth state —
    it is the union of the first two, kept because archived reports carry it:

    - `crossing_but_not_opt` — crossed, no queue model fills it. Unambiguously
      invented; the LOWER bound on over-award.
    - `inside_bracket` — crossed, the floor misses it, the ceiling fills it.
      Ambiguous. Charging these to the sim is what makes the floor-only reading
      an UPPER bound rather than a measurement.
    - `pess_but_not_crossing` — declined by the sim, filled by even the floor.
      Unambiguously forgone, under either bound.

    The first two partition the third-from-last exactly, which is the arithmetic
    the report's fields rest on and the reason this lives in one function rather
    than as three comprehensions at the call site.
    """
    cross_only = [o for o in orders if over_award(o, "pess")]
    return {
        "crossing_but_not_pess": len(cross_only),
        "crossing_but_not_opt": sum(1 for o in orders if over_award(o, "opt")),
        "inside_bracket": sum(1 for o in cross_only if o.tracker.filled_opt > 0),
        "pess_but_not_crossing": sum(
            1 for o in orders if o.crossed_at is None and o.tracker.filled_pess > 0
        ),
    }


def concentration_by_market(orders: list[VirtualOrder], bound: str = "pess") -> dict:
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

    `bound` selects which end of the queue bracket decides an over-award (see
    `over_award`). The default "pess" is kept so archived reports stay
    comparable; the report also carries the "opt" reading under
    `concentration_strict`, and the two are a bracket on the DIRECTION exactly
    as pess/opt are a bracket on the fill count. A direction that holds only at
    the loose end is a property of the floor, not of the fill models.
    """
    per: dict[str, list[int]] = {}
    for o in orders:
        e = per.setdefault(o.market_id, [0, 0, 0])
        e[0] += 1
        if over_award(o, bound):
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
        "direction_market_status": mkt["status"],
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
        "direction_underlying_status": und["status"],
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


def select_markets(conn, since: datetime, top_n: int, series: list[str] | None = None) -> list[str]:
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
    split = over_award_split(all_orders)
    composition = series_composition(all_orders)
    conc = concentration_by_market(all_orders)
    conc_strict = concentration_by_market(all_orders, bound="opt")
    out_dir = Path(args.out)
    report = {
        "generated_at": str(datetime.now(UTC).replace(tzinfo=None, microsecond=0)),
        "window_hours": args.hours,
        "orders": n,
        "crossing_filled": len(crossed),
        "queue_pess_filled": len(pess),
        "queue_opt_filled": len(opt),
        **split,
        "market_composition": composition,
        "concentration": conc,
        "concentration_strict": conc_strict,
        "direction_verdict": direction_verdict(conc, conc_strict),
        "independence": independence_vs_prior(out_dir, all_orders, composition),
        "note": (
            "crossing rule = what backtests award today; queue bounds ="
            " what L2+tape evidence supports (pess is the floor)."
            " crossing_but_not_pess counts fills the sim may be inventing;"
            " pess_but_not_crossing counts real fills the sim forgoes."
            " 'may be' is load-bearing: the queue evidence is a BRACKET, so an"
            " order the floor misses but the ceiling fills lies INSIDE it and is"
            " ambiguous, not invented. Read the three-way split —"
            " crossing_but_not_opt (no queue model fills it: unambiguously"
            " invented), inside_bracket (ambiguous), pess_but_not_crossing"
            " (unambiguously forgone). Since filled_pess <= filled_opt always,"
            " crossing_but_not_pess is an UPPER bound on over-award and"
            " crossing_but_not_opt is the LOWER bound, so any direction test run"
            " against the floor alone leans over BY CONSTRUCTION. concentration"
            " uses the floor (unchanged, for cross-report comparability);"
            " concentration_strict re-runs every tier against the ceiling. A"
            " direction significant in one and not the other is a property of"
            " which end of the bracket was charged, not of the fill models."
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
            " direction_*_status is that comparison made for you, because"
            " direction_*_significant is a boolean over outcomes that mean"
            " opposite things: False covers both a run that tested and found no"
            " direction and a run that could not have found one. Read"
            " direction_verdict — its counts PARTITION the four tier x bound"
            " readings, and powered=0 means this run says nothing about"
            " fill-model direction however large net_disagreement looks."
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
