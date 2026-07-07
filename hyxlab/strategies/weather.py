"""NWS-forecast weather strategy for Kalshi daily-high markets.

Model: reported daily high ~ Normal(forecast_high + bias, sigma), with
sigma defaulting to 2.7°F — NWS 24h highs land within ±3.5°F about 80% of
the time, and 3.5/1.28 ≈ 2.7. Strike math follows Kalshi's integer
conventions ("greater" floor 82 = 83°+, i.e. threshold 82.5).

Trades only when the model's probability beats the ask by min_edge after
taker fees, on either side. Positions held to settlement. The point of
running this on recorded data is to measure whether the naive public
version of the weather edge still exists AFTER fees and AFTER the 2026
proliferation of public edge-finder tools — i.e., to try to falsify the
memo's top thesis cheaply.

Honest caveats: bias/sigma should ultimately be fit per-city from NWS
forecast-vs-climate-report history, and same-day trading needs
intraday temperature pace, not just the morning forecast. v1 tests the
dumbest defensible version first.
"""

from __future__ import annotations

import math
from datetime import timedelta

from hyxlab.models import MarketInfo, Order, Snapshot
from hyxlab.strategy import Context, Strategy
from hyxlab.venues.nws import SERIES_TO_STATION


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def prob_yes(info: MarketInfo, mu: float, sigma: float) -> float | None:
    """P(market resolves YES) given reported-high ~ N(mu, sigma)."""
    if info.strike_type == "greater" and info.floor_strike is not None:
        return 1.0 - _phi((info.floor_strike + 0.5 - mu) / sigma)
    if info.strike_type == "less" and info.cap_strike is not None:
        return _phi((info.cap_strike - 0.5 - mu) / sigma)
    if (
        info.strike_type == "between"
        and info.floor_strike is not None
        and info.cap_strike is not None
    ):
        return _phi((info.cap_strike + 0.5 - mu) / sigma) - _phi(
            (info.floor_strike - 0.5 - mu) / sigma
        )
    return None


class WeatherNWS(Strategy):
    def __init__(
        self,
        sigma: float = 2.7,
        bias: float = 0.0,
        min_edge: float = 0.05,
        max_qty: float = 20.0,
        trade_same_day: bool = False,
    ) -> None:
        self.name = "weather_nws"
        self.sigma = sigma
        self.bias = bias
        self.min_edge = min_edge
        self.max_qty = max_qty
        self.trade_same_day = trade_same_day

    def on_snapshot(self, snap: Snapshot, ctx: Context) -> list[Order]:
        info = ctx.market(snap.venue, snap.market_id)
        if info is None or info.target_date is None:
            return []
        if not self.trade_same_day:
            # Once the measured day has started, the market has watched the
            # realized intraday high and a morning-forecast gaussian hasn't —
            # the model is wrong by construction there. Fixed UTC-5 offset
            # approximates US-city local dates well enough for a day gate.
            local_date = (snap.ts - timedelta(hours=5)).date()
            if local_date >= info.target_date:
                return []
        station = SERIES_TO_STATION.get(info.series)
        if station is None:
            return []
        high = ctx.forecast_high(station, info.target_date)
        if high is None:
            return []
        p = prob_yes(info, high + self.bias, self.sigma)
        if p is None:
            return []
        for side in ("yes", "no"):
            if ctx.position(self.name, snap.venue, snap.market_id, side) > 0:
                return []  # already positioned in this market
        model = ctx.fee_model(snap.venue)
        orders: list[Order] = []
        if snap.yes_ask is not None and snap.yes_ask_size > 0:
            edge = p - snap.yes_ask - model.taker_frac(snap.yes_ask)
            if edge > self.min_edge:
                orders.append(
                    Order(snap.venue, snap.market_id, "yes", min(self.max_qty, snap.yes_ask_size))
                )
        if not orders and snap.no_ask is not None and snap.no_ask_size > 0:
            edge = (1.0 - p) - snap.no_ask - model.taker_frac(snap.no_ask)
            if edge > self.min_edge:
                orders.append(
                    Order(snap.venue, snap.market_id, "no", min(self.max_qty, snap.no_ask_size))
                )
        return orders
