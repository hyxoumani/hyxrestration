"""Cross-venue arb: buy YES on one venue + NO on the other under $1 − fees.

Pairs must reference the SAME resolution event with equivalent rules —
rule mismatch turns a "locked" arb into a possible double loss, so pairing
is a manual, human-verified config, never fuzzy title matching.

This doubles as the §4 measurement study from the deep-dive re-analysis:
run it over recorded snapshots and the fill log IS the fee-adjusted
opportunity record (frequency, size, persistence).
"""

from __future__ import annotations

from dataclasses import dataclass

from hyxlab.models import Order, Snapshot
from hyxlab.strategy import Context, Strategy


@dataclass(frozen=True)
class Pair:
    venue_a: str
    market_a: str
    venue_b: str
    market_b: str


class CrossVenueArb(Strategy):
    def __init__(self, pairs: list[Pair], min_edge: float = 0.01, max_qty: float = 100.0) -> None:
        self.name = "cross_venue"
        self.min_edge = min_edge
        self.max_qty = max_qty
        self._by_leg: dict[tuple[str, str], Pair] = {}
        for p in pairs:
            self._by_leg[(p.venue_a, p.market_a)] = p
            self._by_leg[(p.venue_b, p.market_b)] = p

    def _try(
        self,
        ctx: Context,
        yes_leg: Snapshot,
        no_leg: Snapshot,
    ) -> list[Order]:
        if yes_leg.yes_ask is None or no_leg.no_ask is None:
            return []
        fee_yes = ctx.fee_model(yes_leg.venue).taker_frac(yes_leg.yes_ask)
        fee_no = ctx.fee_model(no_leg.venue).taker_frac(no_leg.no_ask)
        cost = yes_leg.yes_ask + no_leg.no_ask + fee_yes + fee_no
        if cost >= 1.0 - self.min_edge:
            return []
        qty = min(
            self.max_qty,
            yes_leg.yes_ask_size or self.max_qty,
            no_leg.no_ask_size or self.max_qty,
        )
        if qty <= 0:
            return []
        return [
            Order(yes_leg.venue, yes_leg.market_id, "yes", qty),
            Order(no_leg.venue, no_leg.market_id, "no", qty),
        ]

    def on_snapshot(self, snap: Snapshot, ctx: Context) -> list[Order]:
        pair = self._by_leg.get((snap.venue, snap.market_id))
        if pair is None:
            return []
        a = ctx.last(pair.venue_a, pair.market_a)
        b = ctx.last(pair.venue_b, pair.market_b)
        if a is None or b is None:
            return []
        # One shot per pair, same rationale as rebalance.
        if (
            ctx.position(self.name, pair.venue_a, pair.market_a, "yes") > 0
            or ctx.position(self.name, pair.venue_b, pair.market_b, "yes") > 0
        ):
            return []
        orders = self._try(ctx, a, b)  # YES on A + NO on B
        if not orders:
            orders = self._try(ctx, b, a)  # YES on B + NO on A
        return orders
