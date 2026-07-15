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
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    report = {
        "generated_at": str(datetime.now().replace(microsecond=0)),
        "window_hours": args.hours,
        "orders": n,
        "crossing_filled": len(crossed),
        "queue_pess_filled": len(pess),
        "queue_opt_filled": len(opt),
        "crossing_but_not_pess": len(cross_only),
        "pess_but_not_crossing": len(pess_only),
        "market_composition": composition,
        "note": (
            "crossing rule = what backtests award today; queue bounds ="
            " what L2+tape evidence supports (pess is the floor)."
            " crossing_but_not_pess counts fills the sim may be inventing;"
            " pess_but_not_crossing counts real fills the sim forgoes."
        ),
        "orders_detail": [o.summary() for o in all_orders],
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    for k, v in report.items():
        if k != "orders_detail":
            print(f"  {k}: {v}")
    print(f"[queuescore] written to {out}")


if __name__ == "__main__":
    main()
