"""Strategy interface and the read-only Context the simulator provides.

A Strategy sees one Snapshot at a time (in strict timestamp order) plus a
Context limited to information available at that moment: market metadata,
last-seen quotes for other markets, positions, fee models, and NWS
forecasts fetched at-or-before the snapshot's timestamp. Settlement
results are deliberately NOT exposed — they exist only for the
simulator's settlement pass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

from hyxlab.fees import FEE_MODELS, FeeModel
from hyxlab.models import Forecast, MarketInfo, Order, Snapshot


class Context:
    def __init__(
        self,
        markets: dict[tuple[str, str], MarketInfo],
        forecasts: list[Forecast] | None = None,
        fee_models: dict[str, FeeModel] | None = None,
        features=None,  # simulator.features.FeatureView (duck-typed; optional)
    ) -> None:
        self._markets = markets
        self._features = features
        self._fee_models = fee_models or FEE_MODELS
        self._last: dict[tuple[str, str], Snapshot] = {}
        self._positions: dict[tuple[str, str, str, str], float] = {}
        # Index forecasts by (station, target_date), each list sorted by
        # fetched_at, so the as-of lookup scans a handful of runtimes
        # instead of the whole archive (matters at backtest scale).
        self._forecasts: dict[tuple[str, date], list[Forecast]] = {}
        for f in sorted(forecasts or [], key=lambda f: f.fetched_at):
            self._forecasts.setdefault((f.station, f.target_date), []).append(f)
        self._sanitized: dict[tuple[str, str], MarketInfo] = {}
        self.now: datetime | None = None

    # -- market data ----------------------------------------------------

    def market(self, venue: str, market_id: str) -> MarketInfo | None:
        info = self._markets.get((venue, market_id))
        if info is None:
            return None
        # Hide settlement results from strategies (no lookahead).
        if info.result:
            key = (venue, market_id)
            cached = self._sanitized.get(key)
            if cached is not None:
                return cached
            self._sanitized[key] = MarketInfo(
                venue=info.venue,
                market_id=info.market_id,
                title=info.title,
                series=info.series,
                close_time=info.close_time,
                strike_type=info.strike_type,
                floor_strike=info.floor_strike,
                cap_strike=info.cap_strike,
                result="",
                target_date=info.target_date,
            )
            return self._sanitized[key]
        return info

    def last(self, venue: str, market_id: str) -> Snapshot | None:
        return self._last.get((venue, market_id))

    def forecast_high(self, station: str, target_date: date) -> int | None:
        """Most recent forecast for (station, target_date) as of self.now."""
        for f in reversed(self._forecasts.get((station, target_date), ())):
            if self.now is None or f.fetched_at <= self.now:
                return f.high_f
        return None

    # -- signals (delegate to FeatureView; every answer is as-of now) ----

    def econ_latest(self, series_id: str):
        """Most recent econ print (latest revision) knowable at now."""
        if self._features is None or self.now is None:
            return None
        return self._features.econ_latest(series_id, self.now)

    def econ_series(self, series_id: str, n: int) -> list:
        """Last n periods as known at now, newest period first."""
        if self._features is None or self.now is None:
            return []
        return self._features.econ_series_asof(series_id, self.now, n)

    def news_window(self, topic: str, window):
        """NewsAgg(count, mean_tone) over (now-window, now]; None w/o feed."""
        if self._features is None or self.now is None:
            return None
        return self._features.news_window(topic, self.now, window)

    # -- portfolio ------------------------------------------------------

    def position(self, strategy: str, venue: str, market_id: str, side: str) -> float:
        return self._positions.get((strategy, venue, market_id, side), 0.0)

    def fee_model(self, venue: str) -> FeeModel:
        return self._fee_models[venue]

    def open_orders(self, strategy: str) -> list[tuple[int, object]]:
        """(order_id, Order) for this strategy's resting orders — feed ids
        to Cancel(). Backed by the simulator's live resting book."""
        ref = getattr(self, "_resting_ref", None) or {}
        return [(r.order_id, r.order) for rs in ref.values() for r in rs if r.strategy == strategy]

    # -- simulator-side mutation (not for strategies) --------------------

    def _observe(self, snap: Snapshot) -> None:
        self._last[(snap.venue, snap.market_id)] = snap
        self.now = snap.ts

    def _add_position(
        self, strategy: str, venue: str, market_id: str, side: str, qty: float
    ) -> None:
        key = (strategy, venue, market_id, side)
        self._positions[key] = self._positions.get(key, 0.0) + qty


class Strategy(ABC):
    name: str = "strategy"
    # Book-structure capabilities this strategy's trigger depends on (see
    # simulator.capabilities). The Simulator refuses to run a strategy whose
    # requirements no venue in the declared data feed satisfies — a
    # backtest that cannot fill is vacuous, not a null result.
    requires: frozenset[str] = frozenset()

    @abstractmethod
    def on_snapshot(self, snap: Snapshot, ctx: Context) -> list[Order]:
        """React to one snapshot; return orders (possibly for other markets)."""
