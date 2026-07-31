"""v2 order lifecycle: closes, IOC, cancel, expiry, oversell cap, invariants."""

from datetime import UTC, datetime

import pytest

from hyxlab.models import Cancel, MarketInfo, Order, Snapshot
from simulator.sim import SimAccountingError, Simulator
from simulator.strategy import Strategy

T = [datetime(2026, 7, 6, 12, i * 5, tzinfo=UTC) for i in range(4)]


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


class Script(Strategy):
    """Emits a fixed list of commands per snapshot index."""

    name = "script"

    def __init__(self, steps):
        self.steps = steps
        self.i = 0

    def on_snapshot(self, s, ctx):
        cmds = self.steps.get(self.i, [])
        self.i += 1
        return cmds


def run(steps, snaps, markets=None):
    markets = markets or {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1")}
    sim = Simulator(markets, [Script(steps)])
    return sim, sim.run(snaps)


def test_close_realizes_pnl_before_settlement():
    steps = {
        0: [Order("kalshi", "M1", "yes", 10)],  # buy 10 @ ask 0.40
        1: [Order("kalshi", "M1", "yes", 10, action="close")],  # sell 10 @ bid 0.55
    }
    sim, res = run(steps, [snap("M1", T[0], 0.39, 0.40), snap("M1", T[1], 0.55, 0.56)])
    assert [f.qty for f in res.fills] == [10, -10]
    # kalshi taker fees: buy 0.07*10*0.4*0.6=0.168->0.17; sell 0.07*10*.55*.45=0.1732->0.18
    assert res.cash == pytest.approx(10 * 0.55 - 10 * 0.40 - 0.17 - 0.18)
    assert res.metrics["script"]["settled_net_pnl"] == 0.0  # market never settled


def test_close_capped_at_held_qty():
    steps = {
        0: [Order("kalshi", "M1", "yes", 10)],
        1: [Order("kalshi", "M1", "yes", 25, action="close")],  # only 10 held
    }
    _, res = run(steps, [snap("M1", T[0], 0.39, 0.40), snap("M1", T[1], 0.55, 0.56)])
    assert res.fills[-1].qty == -10  # I2: no shorting via oversized close


def test_zero_displayed_size_fills_nothing():
    """A real quote with displayed size 0 is NO liquidity, not 'size
    unknown' — filling into it flatters takers beyond the documented
    bias. Unknown size is represented as +inf (candle snapshots)."""
    steps = {0: [Order("kalshi", "M1", "yes", 10, tif="IOC")]}
    _, res = run(steps, [snap("M1", T[0], 0.39, 0.40, size=0.0)])
    assert res.fills == []


def test_infinite_size_caps_fill_at_order_qty():
    steps = {0: [Order("kalshi", "M1", "yes", 10, tif="IOC")]}
    _, res = run(steps, [snap("M1", T[0], 0.39, 0.40, size=float("inf"))])
    assert [f.qty for f in res.fills] == [10]


def test_ioc_drops_unfilled_remainder():
    steps = {0: [Order("kalshi", "M1", "yes", 50, limit_price=0.40, tif="IOC")]}
    snaps = [snap("M1", T[0], 0.39, 0.40, size=20), snap("M1", T[1], 0.39, 0.40, size=100)]
    sim, res = run(steps, snaps)
    assert len(res.fills) == 1 and res.fills[0].qty == 20  # 30 not resting
    assert sim._resting == {}


def test_gtc_remainder_rests_and_fills_later():
    steps = {0: [Order("kalshi", "M1", "yes", 50, limit_price=0.40)]}
    snaps = [snap("M1", T[0], 0.39, 0.40, size=20), snap("M1", T[1], 0.38, 0.39, size=100)]
    _, res = run(steps, snaps)
    assert [f.qty for f in res.fills] == [20, 30]
    assert res.fills[1].maker is True and res.fills[1].price == 0.40


def test_cancel_removes_resting_order():
    class Canceller(Strategy):
        name = "script"

        def __init__(self):
            self.i = 0

        def on_snapshot(self, s, ctx):
            self.i += 1
            if self.i == 1:
                return [Order("kalshi", "M1", "yes", 10, limit_price=0.30)]
            if self.i == 2:
                return [Cancel(ctx.open_orders("script")[0][0])]
            return []

    markets = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1")}
    sim = Simulator(markets, [Canceller()])
    res = sim.run(
        [snap("M1", T[0], 0.44, 0.45), snap("M1", T[1], 0.44, 0.45), snap("M1", T[2], 0.25, 0.29)]
    )  # would have crossed 0.30
    assert res.fills == []


def test_resting_order_expires_at_market_close():
    info = MarketInfo(venue="kalshi", market_id="M1", close_time=T[2].replace(tzinfo=None))
    steps = {0: [Order("kalshi", "M1", "yes", 10, limit_price=0.30)]}
    snaps = [
        snap("M1", T[0], 0.44, 0.45),
        Snapshot(
            venue="kalshi",
            market_id="M1",
            ts=T[2].replace(tzinfo=None),
            yes_bid=0.25,
            yes_ask=0.29,
            no_bid=0.71,
            no_ask=0.75,
            yes_bid_size=9,
            yes_ask_size=9,
            no_bid_size=9,
            no_ask_size=9,
        ),
    ]
    sim, res = run(steps, snaps, markets={("kalshi", "M1"): info})
    assert res.fills == []  # expired before the crossing snapshot could fill it
    assert sim._resting == {}


def test_accounting_invariant_trips_on_tampering():
    sim, _ = run({}, [snap("M1", T[0], 0.44, 0.45)])
    sim.result.cash += 1.0  # corrupt the books
    with pytest.raises(SimAccountingError):
        sim._check_invariants()


def test_order_rejects_typoed_fields():
    """A typo'd side keys positions under a string that never matches
    info.result — settled at 0 silently. Fail at construction instead."""
    with pytest.raises(ValueError):
        Order("kalshi", "M1", "Yes", 5)
    with pytest.raises(ValueError):
        Order("kalshi", "M1", "yes", 5, action="sell")
    with pytest.raises(ValueError):
        Order("kalshi", "M1", "yes", 5, tif="FOK")


# -- settlement retires the contract ---------------------------------------
#
# `_settle` paid a winning position out into cash but left it standing in
# `ctx._positions`, and `_compute_metrics` then calls `_equity()`, whose
# `_mark` returns 1.0 for a position on the winning side of a settled
# market. So every settled winner was counted twice. Measured on the
# archived fav-long pre-reg run (data/runs/20260711T230707_e7ba056d):
# `final_equity` +67,561.66 against a true -3,718.34, overstated by exactly
# the 71,280.00 of settled payout. These assert the NUMBERS, so an
# implementation that pays out without retiring fails on the arithmetic
# rather than on a missing key.

_BUY10 = {0: [Order("kalshi", "M1", "yes", 10)]}  # 10 yes @ ask 0.40, fee 0.17
_SNAPS = [snap("M1", T[0], 0.39, 0.40), snap("M1", T[1], 0.55, 0.56)]


def test_settled_winner_is_not_marked_on_top_of_its_payout():
    """Load-bearing: buy 10 @ 0.40 (fee 0.17), settles YES. Cash is
    -4.17 + 10 = 5.83 and that IS the final equity. The pre-fix value was
    15.83 — cash plus a second 1.0 mark on the un-retired position."""
    mk = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1", result="yes")}
    sim, res = run(_BUY10, _SNAPS, markets=mk)
    assert res.cash == pytest.approx(5.83)
    assert res.metrics["_portfolio"]["final_equity"] == pytest.approx(5.83)
    assert res.metrics["_portfolio"]["final_equity"] != pytest.approx(15.83)
    assert sim.ctx._positions[("script", "kalshi", "M1", "yes")] == 0.0


def test_settled_loser_equity_is_unchanged():
    """Control: a loser marks 0.0 either way, so the overstatement is
    exactly `settled_payout` and never touches a losing run. If this moves,
    the fix shifted equity generally instead of retiring a double count."""
    mk = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1", result="no")}
    sim, res = run(_BUY10, _SNAPS, markets=mk)
    assert res.cash == pytest.approx(-4.17)
    assert res.metrics["_portfolio"]["final_equity"] == pytest.approx(-4.17)
    # A settled LOSER must be retired too. Its 0.0 mark makes the equity
    # arithmetic identical either way, so nothing above can catch a
    # winners-only retirement — assert the book state directly.
    assert sim.ctx._positions[("script", "kalshi", "M1", "yes")] == 0.0


def test_unsettled_position_is_still_marked():
    """Discrimination control: the fix must retire SETTLED positions only.
    An open market has no result, so the position survives finalize and
    equity carries its mid mark: -4.17 + 10 * 0.555 = 1.38. A fix that
    simply stopped counting positions would read -4.17 here."""
    _, res = run(_BUY10, _SNAPS)  # default M1 has no result
    assert res.cash == pytest.approx(-4.17)
    assert res.metrics["_portfolio"]["final_equity"] == pytest.approx(1.38)


def test_settlement_is_selective_within_one_run():
    """M1 settles YES, M2 stays open, in the same portfolio. Cash is
    -4.17 - 4.17 + 10 = 1.66 and equity adds only M2's mark: 1.66 + 5.55 =
    7.21. Pre-fix this read 17.21."""
    mk = {
        ("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1", result="yes"),
        ("kalshi", "M2"): MarketInfo(venue="kalshi", market_id="M2"),
    }
    steps = {0: [Order("kalshi", "M1", "yes", 10)], 1: [Order("kalshi", "M2", "yes", 10)]}
    snaps = [
        snap("M1", T[0], 0.39, 0.40),
        snap("M2", T[1], 0.39, 0.40),
        snap("M2", T[2], 0.55, 0.56),
    ]
    sim, res = run(steps, snaps, markets=mk)
    assert res.cash == pytest.approx(1.66)
    assert res.metrics["_portfolio"]["final_equity"] == pytest.approx(7.21)
    assert sim.ctx._positions[("script", "kalshi", "M1", "yes")] == 0.0
    assert sim.ctx._positions[("script", "kalshi", "M2", "yes")] == 10.0


def test_retiring_positions_keeps_the_cash_ledger_intact():
    """Retiring a position must not touch the I1 cash ledger — settlement
    moves value from the position into cash, it does not create any."""
    mk = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1", result="yes")}
    sim, _ = run(_BUY10, _SNAPS, markets=mk)
    sim._check_invariants()  # raises SimAccountingError if the ledger drifted
