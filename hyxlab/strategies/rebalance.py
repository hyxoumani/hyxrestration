"""Intramarket rebalancing: buy YES+NO when their asks sum below $1 − fees.

Guaranteed $1 payout at settlement regardless of outcome, so any
fee-adjusted discount is locked-in profit. The IMDEA study (arXiv
2508.03474) found ~$10.6M/yr extracted this way in the zero-fee era; the
question this strategy answers on recorded data is whether anything
survives the 2026 fee schedules at polling-scale latency. Expected
result: near-zero opportunities. A nonzero result is a data point, not an
invitation to chase millisecond bots.
"""

from __future__ import annotations

from hyxlab.models import Order, Snapshot
from hyxlab.strategy import Context, Strategy


class IntramarketRebalance(Strategy):
    def __init__(self, min_edge: float = 0.005, max_qty: float = 100.0) -> None:
        self.name = "rebalance"
        self.min_edge = min_edge
        self.max_qty = max_qty

    def on_snapshot(self, snap: Snapshot, ctx: Context) -> list[Order]:
        if snap.yes_ask is None or snap.no_ask is None:
            return []
        # One shot per market: guaranteed-PnL positions don't need re-entry
        # logic, and re-entering on every poll would double-count one book.
        if ctx.position(self.name, snap.venue, snap.market_id, "yes") > 0:
            return []
        model = ctx.fee_model(snap.venue)
        fees = model.taker_frac(snap.yes_ask) + model.taker_frac(snap.no_ask)
        if snap.yes_ask + snap.no_ask + fees >= 1.0 - self.min_edge:
            return []
        qty = min(self.max_qty, snap.yes_ask_size or self.max_qty, snap.no_ask_size or self.max_qty)
        if qty <= 0:
            return []
        return [
            Order(snap.venue, snap.market_id, "yes", qty),
            Order(snap.venue, snap.market_id, "no", qty),
        ]
