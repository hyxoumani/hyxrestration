"""Diagnostic probe: tiny taker orders into tight-spread books.

NOT a money thesis. Its job is to exercise the shadow harness and the
fill model with realistic order flow: buy a few contracts at the touch
when the spread is one tick, rate-limited per market. Shadow runs score
its hypothetical fills against subsequent reality; sim runs measure
latency sensitivity. Any PnL it shows is incidental and unauthorized
for capital by definition.
"""

from __future__ import annotations

from datetime import timedelta

from hyxlab.models import Order, Snapshot
from simulator.strategy import Context, Strategy


class TightSpreadProbe(Strategy):
    def __init__(
        self,
        qty: float = 5.0,
        max_spread: float = 0.011,
        max_mid: float = 0.5,
        cooldown_min: float = 10.0,
    ) -> None:
        self.name = "probe"
        self.qty = qty
        self.max_spread = max_spread
        self.max_mid = max_mid
        self.cooldown = timedelta(minutes=cooldown_min)
        self._last: dict[str, object] = {}

    def on_snapshot(self, snap: Snapshot, ctx: Context) -> list[Order]:
        if snap.yes_bid is None or snap.yes_ask is None:
            return []
        if snap.yes_ask - snap.yes_bid > self.max_spread:
            return []
        if (snap.yes_bid + snap.yes_ask) / 2 >= self.max_mid:
            return []
        last = self._last.get(snap.market_id)
        if last is not None and snap.ts - last < self.cooldown:
            return []
        self._last[snap.market_id] = snap.ts
        return [Order(snap.venue, snap.market_id, "yes", self.qty, tif="IOC")]
