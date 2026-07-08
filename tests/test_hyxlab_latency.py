"""Latency-aware fills: decide at t, execute at the first snapshot of the
order's market at/after t+Δ — the decision-time quote is never fillable."""

from datetime import UTC, datetime, timedelta

from hyxlab.models import Cancel, MarketInfo, Order, Snapshot
from hyxlab.sim import Simulator
from hyxlab.strategy import Strategy

T0 = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def snap(mid, ts, yes_bid, yes_ask, size=100.0):
    return Snapshot(
        venue="kalshi",
        market_id=mid,
        ts=ts,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=1 - yes_ask,
        no_ask=1 - yes_bid,
        yes_bid_size=size,
        yes_ask_size=size,
        no_bid_size=size,
        no_ask_size=size,
    )


class BuyOnce(Strategy):
    name = "buy_once"

    def __init__(self, qty=10, limit=None, tif="GTC"):
        self.done = False
        self.qty, self.limit, self.tif = qty, limit, tif

    def on_snapshot(self, s, ctx):
        if self.done:
            return []
        self.done = True
        return [Order("kalshi", "M1", "yes", self.qty, limit_price=self.limit, tif=self.tif)]


def _markets():
    return {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1", result="yes")}


def test_latency_fills_at_execution_snapshot_price():
    # Decide at t0 seeing ask 0.40; by t0+2s the ask is 0.45 -> pay 0.45.
    snaps = [
        snap("M1", T0, 0.39, 0.40),
        snap("M1", T0 + timedelta(seconds=2), 0.44, 0.45),
    ]
    res = Simulator(_markets(), [BuyOnce()], latency=1.0).run(snaps)
    assert len(res.fills) == 1
    assert res.fills[0].price == 0.45  # never the decision-time 0.40
    assert res.fills[0].ts == snaps[1].ts


def test_latency_skips_snapshots_inside_the_window():
    # A snapshot 0.5s after decision (inside Δ=1s) must NOT fill.
    snaps = [
        snap("M1", T0, 0.39, 0.40),
        snap("M1", T0 + timedelta(seconds=0.5), 0.30, 0.31),  # tempting, unreachable
        snap("M1", T0 + timedelta(seconds=3), 0.49, 0.50),
    ]
    res = Simulator(_markets(), [BuyOnce()], latency=1.0).run(snaps)
    assert len(res.fills) == 1
    assert res.fills[0].price == 0.50


def test_zero_latency_is_exactly_legacy_behavior():
    snaps = [snap("M1", T0, 0.39, 0.40)]
    res = Simulator(_markets(), [BuyOnce()], latency=0.0).run(snaps)
    assert len(res.fills) == 1
    assert res.fills[0].price == 0.40  # immediate, decision-time fill
    assert "n_dropped_pending" not in res.metrics["buy_once"]


def test_order_with_no_later_snapshot_is_dropped_and_counted():
    snaps = [snap("M1", T0, 0.39, 0.40)]  # nothing after the decision
    res = Simulator(_markets(), [BuyOnce()], latency=1.0).run(snaps)
    assert res.fills == []
    assert res.metrics["buy_once"]["n_dropped_pending"] == 1


def test_latency_applies_to_cross_market_orders():
    # Strategy reacts to M2's snapshot but orders M1: executes at M1's
    # next snapshot after t+Δ, not at M2's.
    class CrossBuyer(Strategy):
        name = "cross"
        done = False

        def on_snapshot(self, s, ctx):
            if self.done or s.market_id != "M2":
                return []
            self.done = True
            return [Order("kalshi", "M1", "yes", 10)]

    markets = {
        ("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1", result="yes"),
        ("kalshi", "M2"): MarketInfo(venue="kalshi", market_id="M2"),
    }
    snaps = [
        snap("M1", T0, 0.39, 0.40),
        snap("M2", T0 + timedelta(seconds=1), 0.50, 0.51),  # trigger
        snap("M2", T0 + timedelta(seconds=3), 0.50, 0.51),  # M2 snap after Δ: no fill
        snap("M1", T0 + timedelta(seconds=4), 0.42, 0.43),  # M1 snap: fills here
    ]
    res = Simulator(markets, [CrossBuyer()], latency=1.0).run(snaps)
    assert len(res.fills) == 1
    assert (res.fills[0].market_id, res.fills[0].price) == ("M1", 0.43)


def test_cancel_latency_lets_touch_fill_first():
    # Resting maker order; strategy cancels at t1, but the touch crosses
    # at t1+0.5s — inside the cancel's Δ=1s — so the fill happens.
    class RestThenCancel(Strategy):
        name = "rest_cancel"
        step = 0

        def on_snapshot(self, s, ctx):
            self.step += 1
            if self.step == 1:
                return [Order("kalshi", "M1", "yes", 10, limit_price=0.35)]
            if self.step == 2:
                return [Cancel(ctx.open_orders("rest_cancel")[0][0])]
            return []

    snaps = [
        snap("M1", T0, 0.33, 0.40),  # rests at 0.35
        snap("M1", T0 + timedelta(seconds=5), 0.33, 0.40),  # cancel decided
        snap("M1", T0 + timedelta(seconds=5.5), 0.30, 0.34),  # ask crosses 0.35
        snap("M1", T0 + timedelta(seconds=8), 0.30, 0.34),  # cancel lands (too late)
    ]
    res = Simulator(_markets(), [RestThenCancel()], latency=1.0).run(snaps)
    assert len(res.fills) == 1  # the cancel could not outrun the market
    assert res.fills[0].maker is True and res.fills[0].price == 0.35
