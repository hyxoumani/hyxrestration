"""FavoriteLongshot v1 — pre-registered Tier-1 kill-test strategy.

Binding spec: docs/hyxpredict/prereg_favlong_backtest.md (2026-07-11).
One look per market: at the FIRST snapshot inside [close−24h, close−12h],
buy 10 contracts of the favorite side IOC if its ask is in [0.80, 0.95];
otherwise the market is done — no re-checking, no optional stopping.
Buying the favorite subsumes shorting the longshot (NO of a 0.15-yes
market is an 0.85-favorite).
"""

from __future__ import annotations

from datetime import timedelta

from hyxlab.models import Order, Snapshot
from simulator.strategy import Context, Strategy


class FavoriteLongshot(Strategy):
    def __init__(
        self,
        band: tuple[float, float] = (0.80, 0.95),
        qty: float = 10.0,
        window_hours: tuple[float, float] = (24.0, 12.0),
    ) -> None:
        self.name = "fav_long"
        self.band = band
        self.qty = qty
        self.window = (timedelta(hours=window_hours[0]), timedelta(hours=window_hours[1]))
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
        mid = (snap.yes_bid + snap.yes_ask) / 2
        side, ask = ("yes", snap.yes_ask) if mid >= 0.5 else ("no", snap.no_ask)
        if ask is None or not (self.band[0] <= ask <= self.band[1]):
            return []
        return [Order(snap.venue, snap.market_id, side, self.qty, tif="IOC")]
