"""FavLongTight: the one-tick spread gate, plus the v1 semantics it
inherits — pre-registered spec (prereg_favlong_tight_backtest.md),
mechanically."""

from datetime import datetime, timedelta

from hyxlab.models import MarketInfo, Snapshot
from simulator.sim import Simulator
from strategies.fav_long_tight import FavLongTight

CLOSE = datetime(2026, 7, 20, 12, 0)


def snap(bid, ask, ts, market_id="M1"):
    return Snapshot(
        venue="kalshi",
        market_id=market_id,
        ts=ts,
        yes_bid=bid,
        yes_ask=ask,
        no_bid=1 - ask,
        no_ask=1 - bid,
        yes_bid_size=float("inf"),
        yes_ask_size=float("inf"),
        no_bid_size=float("inf"),
        no_ask_size=float("inf"),
    )


def run(snaps, result="yes", band=(0.80, 0.95)):
    markets = {
        ("kalshi", "M1"): MarketInfo(
            venue="kalshi", market_id="M1", result=result, close_time=CLOSE
        )
    }
    return Simulator(markets, [FavLongTight(band=band)]).run(snaps)


def test_one_tick_spread_inside_band_trades():
    res = run([snap(0.86, 0.87, CLOSE - timedelta(hours=20))])
    assert len(res.fills) == 1
    f = res.fills[0]
    assert f.side == "yes" and f.qty == 10 and f.price == 0.87  # taker at ask


def test_zero_spread_passes_the_gate():
    res = run([snap(0.87, 0.87, CLOSE - timedelta(hours=20))])
    assert len(res.fills) == 1


def test_two_tick_spread_is_gated_out():
    # v1's exact entry, minus the gate: this is the trade FavLongTight
    # exists NOT to take.
    res = run([snap(0.85, 0.87, CLOSE - timedelta(hours=20))])
    assert res.fills == []


def test_gate_uses_ticks_not_float_subtraction():
    # 0.97 - 0.96 == 0.010000000000000009 in binary float; a naive
    # `<= 0.01` comparison would reject a genuine one-tick book.
    assert 0.97 - 0.96 > 0.01
    res = run([snap(0.96, 0.97, CLOSE - timedelta(hours=20))], band=(0.95, 0.99))
    assert len(res.fills) == 1
    assert res.fills[0].price == 0.97


def test_buys_no_side_when_yes_is_the_longshot():
    res = run([snap(0.12, 0.13, CLOSE - timedelta(hours=20))], result="no")
    assert len(res.fills) == 1
    assert res.fills[0].side == "no"
    assert res.fills[0].price == 0.88  # no_ask = 1 - yes_bid


def test_first_in_window_look_decides_once_even_when_gated_out():
    # A wide first look burns the market's single look: no optional
    # stopping until the spread happens to tighten.
    res = run(
        [
            snap(0.85, 0.87, CLOSE - timedelta(hours=23)),  # in band, gate fails
            snap(0.86, 0.87, CLOSE - timedelta(hours=15)),  # tight, but too late
        ]
    )
    assert res.fills == []


def test_pre_window_snapshots_do_not_burn_the_look():
    res = run(
        [
            snap(0.85, 0.87, CLOSE - timedelta(hours=30)),  # before the window
            snap(0.86, 0.87, CLOSE - timedelta(hours=20)),  # first in-window look
        ]
    )
    assert len(res.fills) == 1


def test_band_b_excludes_band_a_prices():
    res = run([snap(0.86, 0.87, CLOSE - timedelta(hours=20))], band=(0.95, 0.99))
    assert res.fills == []
