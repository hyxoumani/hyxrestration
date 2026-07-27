"""hylshi longshot-fade replay strategy (EXP-423, 2026-07-13).

Encodes the live hylshi weather-fade rule so it can be run through the
replay simulator for the first time (the original studies were direct
outcome re-queries of the archive, never an execution sim):

    - Kalshi weather bracket markets only (series KXHIGH* / KXLOWT*)
    - YES ask in [0.22, 0.52]
    - tight book: yes_ask - yes_bid <= 0.02
    - 4-6 hours before market close_time
    - enter as TAKER buying NO at the displayed no_ask (= 1 - yes_bid on
      Kalshi's mirrored book), IOC, fixed qty per event
    - hold to settlement, no exits
    - at most one entry per EVENT (brackets of one event resolve together;
      hylshi sizes per event, not per bracket)

`require_volume=True` additionally demands the entry hour's candle printed
volume > 0 — a freshness proxy for the studies' "fresh quote / actively
traded" condition, which candle closes cannot express directly.
"""

from __future__ import annotations

from datetime import timedelta

from hyxlab.models import Order, Snapshot
from simulator.strategy import Context, Strategy

WEATHER_PREFIXES = ("KXHIGH", "KXLOWT")


class HylshiFade(Strategy):
    def __init__(
        self,
        name: str = "hylshi_fade",
        qty: float = 5.0,
        band_lo: float = 0.22,
        band_hi: float = 0.52,
        max_spread: float = 0.02,
        min_hours: float = 4.0,
        max_hours: float = 6.0,
        require_volume: bool = False,
    ) -> None:
        self.name = name
        self.qty = qty
        self.band_lo = band_lo
        self.band_hi = band_hi
        self.max_spread = max_spread
        self.min_tta = timedelta(hours=min_hours)
        self.max_tta = timedelta(hours=max_hours)
        self.require_volume = require_volume
        self._entered_events: set[str] = set()

    @staticmethod
    def event_of(market_id: str) -> str:
        # "KXHIGHNY-26JUL10-B87.5" -> "KXHIGHNY-26JUL10"
        return market_id.rsplit("-", 1)[0]

    def on_snapshot(self, snap: Snapshot, ctx: Context) -> list[Order]:
        if snap.venue != "kalshi":
            return []
        info = ctx.market(snap.venue, snap.market_id)
        if info is None or not info.series.startswith(WEATHER_PREFIXES):
            return []
        if info.close_time is None:
            return []
        tta = info.close_time - snap.ts
        if not (self.min_tta <= tta <= self.max_tta):
            return []
        if snap.yes_ask is None or snap.yes_bid is None:
            return []
        if not (self.band_lo <= snap.yes_ask <= self.band_hi):
            return []
        if snap.yes_ask - snap.yes_bid > self.max_spread + 1e-9:
            return []
        if self.require_volume and snap.volume <= 0:
            return []
        event = self.event_of(snap.market_id)
        if event in self._entered_events:
            return []
        self._entered_events.add(event)
        # Taker at the displayed NO ask; IOC drops any remainder.
        return [Order(snap.venue, snap.market_id, "no", self.qty, tif="IOC")]
