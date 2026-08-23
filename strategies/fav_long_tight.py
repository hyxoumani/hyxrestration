"""FavLongTight v1 — spread-conditioned favorite entry (Tier-1 kill-test).

Binding spec: docs/hyxpredict/prereg_favlong_tight_backtest.md (2026-08-22).
FavoriteLongshot v1 died with "the spread decides": it paid 89.0¢ at the
ask for a book realizing 85.2%, while the underpricing it was chasing
lives at the MID. This variant fixes that CONDITION rather than moving
the band — the entry is taken only when the book's spread is one tick,
so the ask sits within half a tick of the mid and the 3.8¢ ask-vs-mid
gap cannot open.

Everything else is copied from v1 unchanged (one look per market inside
[close−24h, close−12h], favorite side, 10 contracts IOC, held to
settlement) so the comparison is like-for-like; `band` is the only other
knob and both registered bands are run by simulator.run_favlong_tight.
"""

from __future__ import annotations

from datetime import timedelta

from hyxlab.models import Order, Snapshot
from simulator.strategy import Context, Strategy


class FavLongTight(Strategy):
    def __init__(
        self,
        band: tuple[float, float] = (0.80, 0.95),
        qty: float = 10.0,
        window_hours: tuple[float, float] = (24.0, 12.0),
        max_spread_ticks: int = 1,
    ) -> None:
        self.name = "fav_long_tight"
        self.band = band
        self.qty = qty
        self.window = (timedelta(hours=window_hours[0]), timedelta(hours=window_hours[1]))
        self.max_spread_ticks = max_spread_ticks
        self._done: set[tuple[str, str]] = set()

    def on_snapshot(self, snap: Snapshot, ctx: Context) -> list[Order]:
        key = (snap.venue, snap.market_id)
        if key in self._done:
            return []
        info = ctx.market(snap.venue, snap.market_id)
        if info is None or info.close_time is None:
            return []
        to_close = info.close_time - snap.ts
        if to_close > self.window[0]:
            return []  # too early; keep waiting
        self._done.add(key)  # first in-window look decides, once
        if to_close < self.window[1]:
            return []  # window missed (sparse candles)
        if snap.yes_bid is None or snap.yes_ask is None:
            return []
        # The gate. Ticks, not floats: candle closes carry cent-quantised
        # prices as binary floats, so 0.97 - 0.96 > 0.01 is a live risk.
        if round((snap.yes_ask - snap.yes_bid) * 100) > self.max_spread_ticks:
            return []
        mid = (snap.yes_bid + snap.yes_ask) / 2
        side, ask = ("yes", snap.yes_ask) if mid >= 0.5 else ("no", snap.no_ask)
        if ask is None or not (self.band[0] <= ask <= self.band[1]):
            return []
        return [Order(snap.venue, snap.market_id, side, self.qty, tif="IOC")]
