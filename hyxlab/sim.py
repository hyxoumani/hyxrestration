"""Replay simulator: snapshots in, fills + settlement PnL + metrics out.

v2 engine (proposal C5): full order lifecycle, runtime accounting
invariants, per-series fee resolution.

Order lifecycle:
    submit → marketable? fill now (taker, capped at displayed size;
             IOC drops remainder) : rest
    resting → later snapshot crosses limit → fill at limit (maker)
            → Cancel(order_id) → gone
            → market close_time reached → expired
    action="close" sells out of a held position at the side's bid
    (taker) or via resting limit (maker); capped at held qty.

Fill model biases (Tier-1/2): taker fills assume the displayed quote is
still there (optimistic); maker fills require the touch to cross the
limit (conservative). Unknown displayed size (candle grain) caps at
order qty.

Accounting invariants — checked after EVERY event, hard abort on
violation (an accounting bug must never be reportable as PnL):
    I1  cash == proceeds − purchases − fees + payouts   (component ledger)
    I2  every position qty ≥ 0
    I3  at settlement: payout − net_cost − fees == realized pnl (ledger-implied)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from hyxlab.capabilities import check_capabilities
from hyxlab.fees import FEE_MODELS, FeeModel
from hyxlab.models import Cancel, Fill, MarketInfo, Order, Snapshot
from hyxlab.strategy import Context, Strategy


class SimAccountingError(AssertionError):
    pass


@dataclass
class _Resting:
    order_id: int
    strategy: str
    order: Order
    qty_left: float


@dataclass
class SimResult:
    fills: list[Fill] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    cash: float = 0.0
    metrics: dict[str, dict[str, float]] = field(default_factory=dict)


class Simulator:
    def __init__(
        self,
        markets: dict[tuple[str, str], MarketInfo],
        strategies: list[Strategy],
        forecasts: list | None = None,
        fee_models: dict[str, FeeModel] | None = None,
        fee_resolver: Callable[[str, str], FeeModel] | None = None,
        data_capabilities: dict[str, frozenset[str]] | None = None,
    ) -> None:
        # Capability guard: refuse vacuous backtests up front. Strategies
        # with requirements need a feed declaration (hyxlab.capabilities
        # helpers) — undeclared counts as absent.
        check_capabilities(strategies, data_capabilities)
        self.markets = markets
        self.strategies = strategies
        self.fee_models = fee_models or FEE_MODELS
        self.fee_resolver = fee_resolver
        self.ctx = Context(markets, forecasts=forecasts, fee_models=fee_models)
        self.result = SimResult()
        self._resting: dict[tuple[str, str], list[_Resting]] = {}
        self.ctx._resting_ref = self._resting  # read-only view for open_orders()
        self._next_id = 1
        # Component ledger for I1 and per-market I3.
        self._purchases = self._proceeds = self._fees = self._payouts = 0.0
        self._by_market: dict[tuple[str, str, str], dict[str, float]] = {}

    # -- fee & book helpers ----------------------------------------------

    def _fee_model(self, venue: str, market_id: str) -> FeeModel:
        if self.fee_resolver is not None:
            return self.fee_resolver(venue, market_id)
        return self.fee_models[venue]

    @staticmethod
    def _quote(snap: Snapshot, side: str, buying: bool) -> tuple[float | None, float]:
        if buying:
            return (
                (snap.yes_ask, snap.yes_ask_size)
                if side == "yes"
                else (snap.no_ask, snap.no_ask_size)
            )
        return (
            (snap.yes_bid, snap.yes_bid_size) if side == "yes" else (snap.no_bid, snap.no_bid_size)
        )

    # -- fills -------------------------------------------------------------

    def _fill(
        self, strategy: str, order: Order, price: float, qty: float, ts: datetime, *, maker: bool
    ) -> None:
        model = self._fee_model(order.venue, order.market_id)
        fee = model.fee(price, qty, taker=not maker)
        buying = order.action == "open"
        key = (strategy, order.venue, order.market_id)
        led = self._by_market.setdefault(key, {"cost": 0.0, "fees": 0.0, "payout": 0.0})
        if buying:
            self.result.cash -= qty * price
            self._purchases += qty * price
            led["cost"] += qty * price
            self.ctx._add_position(strategy, order.venue, order.market_id, order.side, qty)
        else:
            self.result.cash += qty * price
            self._proceeds += qty * price
            led["cost"] -= qty * price
            self.ctx._add_position(strategy, order.venue, order.market_id, order.side, -qty)
        self.result.cash -= fee
        self._fees += fee
        led["fees"] += fee
        self.result.fills.append(
            Fill(
                strategy=strategy,
                venue=order.venue,
                market_id=order.market_id,
                side=order.side,
                qty=qty if buying else -qty,
                price=price,
                fee=fee,
                ts=ts,
                maker=maker,
            )
        )
        self._check_invariants()

    def _executable_qty(self, strategy: str, order: Order, avail: float) -> float:
        qty = order.qty if avail <= 0 else min(order.qty, avail)
        if order.action == "close":
            held = self.ctx.position(strategy, order.venue, order.market_id, order.side)
            resting_closes = sum(
                r.qty_left
                for rs in self._resting.values()
                for r in rs
                if r.strategy == strategy
                and r.order.action == "close"
                and (r.order.venue, r.order.market_id, r.order.side)
                == (order.venue, order.market_id, order.side)
            )
            qty = min(qty, held - resting_closes)  # I2: never oversell
        return max(qty, 0.0)

    def _submit(self, strategy: str, order: Order, snap: Snapshot | None) -> None:
        target = snap
        if target is None or (target.venue, target.market_id) != (order.venue, order.market_id):
            target = self.ctx.last(order.venue, order.market_id)
        if target is None:
            return
        buying = order.action == "open"
        px, avail = self._quote(target, order.side, buying)
        qty = self._executable_qty(strategy, order, avail if px is not None else 0.0)
        if qty <= 0:
            return
        marketable = px is not None and (
            order.limit_price is None
            or (buying and px <= order.limit_price)
            or (not buying and px >= order.limit_price)
        )
        if marketable:
            self._fill(strategy, order, px, qty, target.ts, maker=False)
            remainder = order.qty - qty
            if remainder > 0 and order.tif == "GTC" and order.limit_price is not None:
                self._rest(strategy, order, remainder)
        elif order.tif == "GTC" and order.limit_price is not None:
            self._rest(strategy, order, qty if order.action == "close" else order.qty)

    def _rest(self, strategy: str, order: Order, qty_left: float) -> None:
        key = (order.venue, order.market_id)
        self._resting.setdefault(key, []).append(_Resting(self._next_id, strategy, order, qty_left))
        self._next_id += 1

    def _maker_check_and_expire(self, snap: Snapshot) -> None:
        key = (snap.venue, snap.market_id)
        info = self.markets.get(key)
        still: list[_Resting] = []
        for r in self._resting.get(key, []):
            if info is not None and info.close_time is not None and snap.ts >= info.close_time:
                continue  # expired at market close
            buying = r.order.action == "open"
            px, avail = self._quote(snap, r.order.side, buying)
            limit = r.order.limit_price
            crossed = (
                px is not None
                and limit is not None
                and ((buying and px <= limit) or (not buying and px >= limit))
            )
            if crossed:
                probe = Order(**{**r.order.__dict__, "qty": r.qty_left})
                qty = self._executable_qty(r.strategy, probe, avail)
                if qty > 0:
                    self._fill(r.strategy, r.order, limit, qty, snap.ts, maker=True)
                    r.qty_left -= qty
            if r.qty_left > 1e-12:
                still.append(r)
        if still:
            self._resting[key] = still
        else:
            self._resting.pop(key, None)

    def _cancel(self, order_id: int) -> None:
        for key, rs in list(self._resting.items()):
            kept = [r for r in rs if r.order_id != order_id]
            if kept:
                self._resting[key] = kept
            elif key in self._resting:
                del self._resting[key]

    # -- main loop ---------------------------------------------------------

    def run(self, snapshots: Iterable[Snapshot]) -> SimResult:
        for snap in snapshots:
            self.ctx._observe(snap)
            self._maker_check_and_expire(snap)
            for strat in self.strategies:
                for cmd in strat.on_snapshot(snap, self.ctx) or []:
                    if isinstance(cmd, Cancel):
                        self._cancel(cmd.order_id)
                    else:
                        self._submit(strat.name, cmd, snap)
            self.result.equity_curve.append((snap.ts, self._equity()))
        self._settle()
        self._compute_metrics()
        return self.result

    # -- marking, settlement, invariants ------------------------------------

    def _mark(self, venue: str, market_id: str, side: str) -> float:
        info = self.markets.get((venue, market_id))
        if info is not None and info.result in ("yes", "no"):
            return 1.0 if info.result == side else 0.0
        snap = self.ctx.last(venue, market_id)
        mid = snap.mid() if snap is not None else None
        if mid is None:
            return 0.0
        return mid if side == "yes" else 1.0 - mid

    def _equity(self) -> float:
        pos = sum(
            qty * self._mark(v, m, side) for (_, v, m, side), qty in self.ctx._positions.items()
        )
        return self.result.cash + pos

    def _settle(self) -> None:
        for (strat, venue, market_id, side), qty in self.ctx._positions.items():
            info = self.markets.get((venue, market_id))
            if info is not None and info.result in ("yes", "no") and qty > 0:
                payout = qty * (1.0 if info.result == side else 0.0)
                self.result.cash += payout
                self._payouts += payout
                self._by_market.setdefault(
                    (strat, venue, market_id), {"cost": 0.0, "fees": 0.0, "payout": 0.0}
                )["payout"] += payout
        self._check_invariants()

    def _check_invariants(self) -> None:
        expected = self._proceeds - self._purchases - self._fees + self._payouts
        if abs(self.result.cash - expected) > 1e-6:
            raise SimAccountingError(f"I1 cash {self.result.cash} != ledger {expected}")
        for key, qty in self.ctx._positions.items():
            if qty < -1e-9:
                raise SimAccountingError(f"I2 negative position {key}: {qty}")

    def _compute_metrics(self) -> None:
        by_strat: dict[str, dict[str, float]] = {}
        for (strat, venue, market_id), led in self._by_market.items():
            m = by_strat.setdefault(
                strat,
                {
                    "n_fills": 0,
                    "fees": 0.0,
                    "cost": 0.0,
                    "settled_payout": 0.0,
                    "settled_net_pnl": 0.0,
                    "settled_cost": 0.0,
                    "open_cost": 0.0,
                },
            )
            m["fees"] += led["fees"]
            m["cost"] += led["cost"]
            m["settled_payout"] += led["payout"]
            info = self.markets.get((venue, market_id))
            if info is not None and info.result in ("yes", "no"):
                m["settled_net_pnl"] += led["payout"] - led["cost"] - led["fees"]
                m["settled_cost"] += max(led["cost"], 0.0)
            else:
                m["open_cost"] += led["cost"]
        for f in self.result.fills:
            by_strat[f.strategy]["n_fills"] += 1
        peak, max_dd = float("-inf"), 0.0
        for _, eq in self.result.equity_curve:
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
        by_strat["_portfolio"] = {
            "final_equity": self._equity(),
            "cash": self.result.cash,
            "max_drawdown": max_dd,
            "n_fills": float(len(self.result.fills)),
        }
        self.result.metrics = by_strat
