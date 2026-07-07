"""Capability guard: backtests that cannot produce a fill must refuse to
run (mistakes log #3 — vacuous rebalance-on-Kalshi PoC returned a polite
zero twice)."""

from datetime import UTC, datetime

import pytest

from hyxlab.capabilities import (
    INDEPENDENT_NO_BOOK,
    VacuousBacktestError,
    candle_feed_caps,
    live_feed_caps,
)
from hyxlab.models import MarketInfo, Snapshot
from hyxlab.sim import Simulator
from hyxlab.store import Store
from hyxlab.strategies import IntramarketRebalance, WeatherNWS

T0 = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def snap(venue, mid="M1"):
    return Snapshot(
        venue=venue,
        market_id=mid,
        ts=T0,
        yes_bid=0.44,
        yes_ask=0.45,
        no_bid=0.44,
        no_ask=0.45,
        yes_bid_size=100,
        yes_ask_size=100,
        no_bid_size=100,
        no_ask_size=100,
    )


def test_rebalance_refuses_undeclared_feed():
    markets = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1")}
    with pytest.raises(VacuousBacktestError, match="rebalance"):
        Simulator(markets, [IntramarketRebalance()])


def test_rebalance_refuses_complement_derived_candle_feed():
    # The original failure: rebalance replayed over Kalshi candle
    # snapshots, whose NO quotes are 1 − YES by construction.
    markets = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1")}
    caps = candle_feed_caps([snap("kalshi")])
    with pytest.raises(VacuousBacktestError, match="independent_no_book"):
        Simulator(markets, [IntramarketRebalance()], data_capabilities=caps)


def test_rebalance_refuses_kalshi_only_live_feed():
    # Even live Kalshi data can't trigger it: one mirrored book by venue design.
    markets = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1")}
    caps = live_feed_caps([snap("kalshi")])
    with pytest.raises(VacuousBacktestError):
        Simulator(markets, [IntramarketRebalance()], data_capabilities=caps)


def test_rebalance_runs_on_live_polymarket_feed():
    markets = {("polymarket", "M1"): MarketInfo(venue="polymarket", market_id="M1")}
    snaps = [snap("polymarket")]
    res = Simulator(markets, [IntramarketRebalance()], data_capabilities=live_feed_caps(snaps)).run(
        snaps
    )
    assert len(res.fills) == 2  # 0.45 + 0.45 = 0.90 discount -> both legs


def test_strategy_without_requirements_runs_undeclared():
    # WeatherNWS buys one side outright; complement books serve it fine.
    markets = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1")}
    res = Simulator(markets, [WeatherNWS()]).run([snap("kalshi")])
    assert res.fills == []  # no forecast -> no trade; the point is no raise


def test_candle_feed_caps_strip_independence_even_for_polymarket():
    # Candle-derived NO is synthesized as the complement regardless of the
    # venue's live book structure.
    caps = candle_feed_caps([snap("polymarket")])
    assert INDEPENDENT_NO_BOOK not in caps["polymarket"]


def test_store_candle_snapshots_never_satisfy_rebalance(tmp_path):
    # End-to-end: the store's own candle replay path must be refused.
    store = Store(tmp_path / "t.duckdb")
    store.insert_candles(
        [("kalshi", "M1", T0, 3600, None, None, None, 0.30, 0.29, 0.31, None, None, 10.0, 5.0)]
    )
    snaps = store.candles_as_snapshots()
    store.close()
    assert snaps  # the gate let the clean candle through
    markets = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1")}
    with pytest.raises(VacuousBacktestError):
        Simulator(markets, [IntramarketRebalance()], data_capabilities=candle_feed_caps(snaps))
