"""FavoriteLongshot: one-look-per-market semantics, band gating,
favorite-side selection — the pre-registered spec, mechanically."""

from datetime import datetime, timedelta

from hyxlab.models import MarketInfo, Snapshot
from simulator.sim import Simulator
from strategies.fav_long import FavoriteLongshot

CLOSE = datetime(2026, 7, 20, 12, 0)


def snap(mid_yes, ts, market_id="M1", spread=0.02):
    bid, ask = mid_yes - spread / 2, mid_yes + spread / 2
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


def run(snaps, result="yes"):
    markets = {
        ("kalshi", "M1"): MarketInfo(
            venue="kalshi", market_id="M1", result=result, close_time=CLOSE
        )
    }
    sim = Simulator(markets, [FavoriteLongshot()])
    return sim.run(snaps)


def test_buys_favorite_yes_inside_band_and_window():
    res = run([snap(0.86, CLOSE - timedelta(hours=20))])
    assert len(res.fills) == 1
    f = res.fills[0]
    assert f.side == "yes" and f.qty == 10 and f.price == 0.87  # taker at ask


def test_buys_no_side_when_yes_is_the_longshot():
    res = run([snap(0.13, CLOSE - timedelta(hours=20))], result="no")
    assert len(res.fills) == 1
    assert res.fills[0].side == "no"
    assert res.fills[0].price == 0.88  # no_ask = 1 - yes_bid = 1 - 0.12


def test_first_in_window_look_decides_once():
    # first in-window snapshot is OUT of band -> market done; a later
    # in-band snapshot must NOT trade (no optional stopping)
    res = run(
        [
            snap(0.60, CLOSE - timedelta(hours=23)),  # ask 0.61, out of band
            snap(0.86, CLOSE - timedelta(hours=15)),  # in band but too late
        ]
    )
    assert res.fills == []


def test_too_early_snapshot_keeps_waiting():
    res = run(
        [
            snap(0.86, CLOSE - timedelta(hours=30)),  # before window: no look
            snap(0.86, CLOSE - timedelta(hours=20)),  # first in-window: trades
        ]
    )
    assert len(res.fills) == 1


def test_window_missed_means_no_trade():
    res = run([snap(0.86, CLOSE - timedelta(hours=2))])  # inside 12h buffer
    assert res.fills == []
