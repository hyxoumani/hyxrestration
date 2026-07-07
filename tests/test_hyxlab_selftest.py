"""B3 environment self-tests: lookahead resistance, determinism, accounting
under random load, and a golden synthetic episode with known PnL.

(The golden *real-data* weather-week episode is added once the initial
sweep releases the DB writer lock.)
"""

import json
import random
from datetime import UTC, date, datetime, timedelta

from hyxlab.capabilities import INDEPENDENT_NO_BOOK
from hyxlab.models import Forecast, MarketInfo, Order, Snapshot
from hyxlab.sim import Simulator
from hyxlab.strategies import IntramarketRebalance
from hyxlab.strategy import Strategy

T0 = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
# Declared for tests whose synthetic snapshots carry genuinely independent
# YES/NO quotes (built directly, not via the complement snap() helper).
INDEP = {"kalshi": frozenset({INDEPENDENT_NO_BOOK})}


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


def test_adversarial_peeker_sees_nothing():
    """A strategy actively attempting lookahead must come up empty."""
    info = MarketInfo(
        venue="kalshi",
        market_id="M1",
        series="KXHIGHNY",
        strike_type="greater",
        floor_strike=80.0,
        target_date=date(2026, 7, 7),
        result="yes",  # settled!
    )
    future_fc = Forecast(
        station="NYC",
        fetched_at=T0 + timedelta(hours=2),
        target_date=date(2026, 7, 7),
        high_f=95,
    )

    class Peeker(Strategy):
        name = "peeker"
        leaks: list = []

        def on_snapshot(self, s, ctx):
            if ctx.market("kalshi", "M1").result:
                Peeker.leaks.append("settlement result visible")
            if ctx.forecast_high("NYC", date(2026, 7, 7)) is not None:
                Peeker.leaks.append("future forecast visible")
            try:  # attempt to mutate hidden state
                ctx._positions[("peeker", "kalshi", "M1", "yes")] = 1e9
                ctx._positions.pop(("peeker", "kalshi", "M1", "yes"))
            except Exception:
                pass
            return []

    Simulator({("kalshi", "M1"): info}, [Peeker()], forecasts=[future_fc]).run(
        [snap("M1", T0, 0.5, 0.51)]
    )
    assert Peeker.leaks == []


def test_determinism_same_inputs_same_metrics():
    # Independent YES/NO books (0.45 + 0.45 = 0.90) so the strategy really
    # fills — a determinism probe over zero fills would prove nothing.
    # (The complement-deriving snap() helper above caused exactly that
    # vacuousness before the capability guard existed.)
    def one_run():
        markets = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1", result="yes")}
        snaps = [
            Snapshot(
                venue="kalshi",
                market_id="M1",
                ts=T0 + timedelta(minutes=i),
                yes_bid=0.43,
                yes_ask=0.45,
                no_bid=0.43,
                no_ask=0.45,
                yes_bid_size=100,
                yes_ask_size=100,
                no_bid_size=100,
                no_ask_size=100,
            )
            for i in range(5)
        ]
        res = Simulator(markets, [IntramarketRebalance()], data_capabilities=INDEP).run(snaps)
        assert res.fills  # non-vacuous: the probe exercised real fills
        return json.dumps(res.metrics, sort_keys=True, default=str)

    assert one_run() == one_run()


def test_accounting_invariants_hold_under_random_load():
    """Seeded fuzz: random orders/closes against random books; the engine's
    own runtime invariants (I1/I2) must never trip."""
    rng = random.Random(42)

    class Chaos(Strategy):
        name = "chaos"

        def on_snapshot(self, s, ctx):
            orders = []
            for _ in range(rng.randint(0, 3)):
                side = rng.choice(["yes", "no"])
                action = rng.choice(["open", "open", "close"])
                limit = rng.choice([None, round(rng.uniform(0.05, 0.95), 2)])
                tif = rng.choice(["GTC", "IOC"])
                orders.append(
                    Order(
                        "kalshi",
                        s.market_id,
                        side,
                        rng.randint(1, 30),
                        limit_price=limit,
                        action=action,
                        tif=tif,
                    )
                )
            return orders

    markets = {
        ("kalshi", f"M{i}"): MarketInfo(
            venue="kalshi",
            market_id=f"M{i}",
            result=rng.choice(["", "yes", "no"]),
        )
        for i in range(4)
    }
    snaps = []
    for i in range(300):
        bid = round(rng.uniform(0.05, 0.90), 2)
        ask = min(round(bid + rng.uniform(0.01, 0.08), 2), 0.99)
        snaps.append(
            snap(
                f"M{rng.randint(0, 3)}",
                T0 + timedelta(minutes=i),
                bid,
                ask,
                size=rng.choice([5.0, 50.0, 0.0]),
            )
        )
    # run() checks I1/I2 after every fill and at settlement; no exception = pass.
    res = Simulator(markets, [Chaos()]).run(snaps)
    assert len(res.fills) > 50  # the fuzz actually exercised the engine


def test_golden_synthetic_arb_episode_exact_pnl():
    """Hand-built episode with PnL computable to the cent. Pins engine
    behavior: any change that moves this number is a reviewable event."""
    markets = {("kalshi", "G1"): MarketInfo(venue="kalshi", market_id="G1", result="no")}
    # Built directly (not via the complement helper): a genuine two-sided
    # discount needs independent books — yes_ask + no_ask = 0.90.
    snaps = [
        Snapshot(
            venue="kalshi",
            market_id="G1",
            ts=T0,
            yes_bid=0.43,
            yes_ask=0.45,
            no_bid=0.43,
            no_ask=0.45,
            yes_bid_size=100,
            yes_ask_size=100,
            no_bid_size=100,
            no_ask_size=100,
        )
    ]
    res = Simulator(markets, [IntramarketRebalance()], data_capabilities=INDEP).run(snaps)
    # 100 YES @0.45 + 100 NO @0.45; fees ceil(0.07*100*0.45*0.55)=1.74 each leg;
    # settlement pays the NO side 100. PnL = 100 - 90 - 3.48 = 6.52 exactly.
    assert round(res.metrics["rebalance"]["settled_net_pnl"], 2) == 6.52
