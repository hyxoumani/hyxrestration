"""Simulator + baseline strategies on synthetic snapshots (no network)."""

from datetime import UTC, date, datetime

from hyxlab.capabilities import INDEPENDENT_NO_BOOK
from hyxlab.models import Forecast, MarketInfo, Order, Snapshot
from hyxlab.sim import Simulator
from hyxlab.strategies import IntramarketRebalance, WeatherNWS
from hyxlab.strategies.cross_venue import CrossVenueArb, Pair
from hyxlab.strategy import Strategy
from hyxlab.venues.kalshi import parse_event_date, to_market_info, to_snapshot

TS0 = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
TS1 = datetime(2026, 7, 6, 12, 5, tzinfo=UTC)
# The snap() helper below builds genuinely independent YES/NO quotes, so
# the synthetic feed provides what IntramarketRebalance requires — the
# capability declaration describes the data, not the venue label.
INDEP = {"kalshi": frozenset({INDEPENDENT_NO_BOOK})}


def snap(venue, mid, ts, yes_bid, yes_ask, no_bid, no_ask, size=100.0):
    return Snapshot(
        venue=venue,
        market_id=mid,
        ts=ts,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_bid_size=size,
        yes_ask_size=size,
        no_bid_size=size,
        no_ask_size=size,
    )


def market(venue, mid, result="", **kw):
    return MarketInfo(venue=venue, market_id=mid, result=result, **kw)


def test_rebalance_captures_discount_and_settles():
    # YES 0.45 + NO 0.45 = 0.90 -> guaranteed $1 payout; fees can't eat 10c.
    markets = {("kalshi", "M1"): market("kalshi", "M1", result="yes")}
    snaps = [snap("kalshi", "M1", TS0, 0.44, 0.45, 0.44, 0.45)]
    sim = Simulator(markets, [IntramarketRebalance()], data_capabilities=INDEP)
    res = sim.run(snaps)
    assert len(res.fills) == 2  # one YES + one NO
    m = res.metrics["rebalance"]
    assert m["settled_payout"] == 100.0  # 100 YES pay out; 100 NO pay 0
    assert m["cost"] == 90.0
    assert 0 < m["fees"] < 4.0
    assert m["settled_net_pnl"] > 6.0


def test_rebalance_skips_fee_negative_discount():
    # 0.49 + 0.50 = 0.99: 1c gross < ~3.5c fees -> no trade.
    markets = {("kalshi", "M1"): market("kalshi", "M1")}
    snaps = [snap("kalshi", "M1", TS0, 0.48, 0.49, 0.49, 0.50)]
    res = Simulator(markets, [IntramarketRebalance()], data_capabilities=INDEP).run(snaps)
    assert res.fills == []


def test_rebalance_fires_once_per_market():
    markets = {("kalshi", "M1"): market("kalshi", "M1")}
    snaps = [
        snap("kalshi", "M1", TS0, 0.44, 0.45, 0.44, 0.45),
        snap("kalshi", "M1", TS1, 0.44, 0.45, 0.44, 0.45),
    ]
    res = Simulator(markets, [IntramarketRebalance()], data_capabilities=INDEP).run(snaps)
    assert len(res.fills) == 2


def test_cross_venue_arb_two_legs():
    markets = {
        ("kalshi", "K1"): market("kalshi", "K1", result="no"),
        ("polymarket", "P1"): market("polymarket", "P1", result="no"),
    }
    pair = Pair("kalshi", "K1", "polymarket", "P1")
    # YES on kalshi @0.40 + NO on polymarket @0.50 = 0.90.
    snaps = [
        snap("kalshi", "K1", TS0, 0.39, 0.40, 0.59, 0.60),
        snap("polymarket", "P1", TS1, 0.49, 0.50, 0.49, 0.50),
    ]
    res = Simulator(markets, [CrossVenueArb([pair])]).run(snaps)
    assert len(res.fills) == 2
    venues = {f.venue for f in res.fills}
    assert venues == {"kalshi", "polymarket"}
    m = res.metrics["cross_venue"]
    # Result "no" on both: the NO leg pays 100, YES leg 0; cost 90 + fees.
    assert m["settled_payout"] == 100.0
    assert m["settled_net_pnl"] > 5.0


def test_maker_limit_order_rests_then_fills():
    class LimitBuyer(Strategy):
        name = "limit_buyer"
        done = False

        def on_snapshot(self, s, ctx):
            if self.done:
                return []
            self.done = True
            return [Order("kalshi", "M1", "yes", 10, limit_price=0.40)]

    markets = {("kalshi", "M1"): market("kalshi", "M1")}
    snaps = [
        snap("kalshi", "M1", TS0, 0.44, 0.45, 0.54, 0.55),  # not marketable -> rests
        snap("kalshi", "M1", TS1, 0.38, 0.39, 0.60, 0.61),  # ask crosses 0.40 -> fills
    ]
    res = Simulator(markets, [LimitBuyer()]).run(snaps)
    assert len(res.fills) == 1
    f = res.fills[0]
    assert f.maker is True
    assert f.price == 0.40  # fills at the limit, not the new ask


def test_weather_strategy_buys_underpriced_yes():
    # Forecast high 90; strike "84 or above" (floor 83) -> p ~ 0.99;
    # market asks only 0.60 for YES -> huge edge -> buy.
    info = MarketInfo(
        venue="kalshi",
        market_id="W1",
        series="KXHIGHNY",
        strike_type="greater",
        floor_strike=83.0,
        target_date=date(2026, 7, 7),
        result="yes",
    )
    markets = {("kalshi", "W1"): info}
    fc = Forecast(station="NYC", fetched_at=TS0, target_date=date(2026, 7, 7), high_f=90)
    snaps = [snap("kalshi", "W1", TS1, 0.55, 0.60, 0.40, 0.45)]
    res = Simulator(markets, [WeatherNWS()], forecasts=[fc]).run(snaps)
    assert len(res.fills) == 1
    assert res.fills[0].side == "yes"
    assert res.metrics["weather_nws"]["settled_net_pnl"] > 7.0


def test_weather_strategy_no_lookahead_on_forecasts():
    # Forecast fetched AFTER the snapshot must be invisible -> no trade.
    info = MarketInfo(
        venue="kalshi",
        market_id="W1",
        series="KXHIGHNY",
        strike_type="greater",
        floor_strike=83.0,
        target_date=date(2026, 7, 7),
    )
    markets = {("kalshi", "W1"): info}
    fc = Forecast(station="NYC", fetched_at=TS1, target_date=date(2026, 7, 7), high_f=90)
    snaps = [snap("kalshi", "W1", TS0, 0.55, 0.60, 0.40, 0.45)]
    res = Simulator(markets, [WeatherNWS()], forecasts=[fc]).run(snaps)
    assert res.fills == []


def test_strategies_cannot_see_settlement_results():
    info = MarketInfo(venue="kalshi", market_id="M1", result="yes")

    class Peeker(Strategy):
        name = "peeker"
        seen = None

        def on_snapshot(self, s, ctx):
            Peeker.seen = ctx.market("kalshi", "M1").result
            return []

    Simulator({("kalshi", "M1"): info}, [Peeker()]).run(
        [snap("kalshi", "M1", TS0, 0.5, 0.51, 0.49, 0.50)]
    )
    assert Peeker.seen == ""


def test_kalshi_parsers_on_live_shape():
    m = {
        "ticker": "KXHIGHNY-26JUL07-T82",
        "event_ticker": "KXHIGHNY-26JUL07",
        "title": "Will the high temp in NYC be >82 on Jul 7, 2026?",
        "close_time": "2026-07-08T04:59:00Z",
        "strike_type": "greater",
        "floor_strike": 82,
        "result": "",
        "yes_bid_dollars": "0.0000",
        "yes_ask_dollars": "0.0100",
        "no_bid_dollars": "0.9900",
        "no_ask_dollars": "1.0000",
        "yes_bid_size_fp": "0.00",
        "yes_ask_size_fp": "9590.63",
        "last_price_dollars": "0.0100",
        "volume_fp": "2283.94",
        "open_interest_fp": "2266.90",
    }
    info = to_market_info(m)
    assert info.series == "KXHIGHNY"
    assert info.target_date == date(2026, 7, 7)
    assert info.floor_strike == 82
    s = to_snapshot(m, TS0)
    assert s.yes_ask == 0.01
    assert s.yes_ask_size == 9590.63
    assert s.no_bid == 0.99
    assert parse_event_date("KXHIGHNY-26DEC31") == date(2026, 12, 31)
    assert parse_event_date("NODATE") is None
