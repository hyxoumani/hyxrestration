"""queuescore end-to-end on a synthetic archive: the two fill models
disagree exactly where they should."""

import json
from datetime import datetime, timedelta

import duckdb

from collector.venues.kalshi_ws import parse_message
from hyxlab.streamstore import StreamStore
from simulator.queuescore import (
    VirtualOrder,
    concentration_by_market,
    event_ticker,
    independence_vs_prior,
    score_market,
    select_markets,
    series_composition,
    sign_test_p,
)

T0 = datetime(2026, 7, 11, 12, 0)


def _image(mid, seq, ts, yes=("0.4000", "30.00"), no=("0.5500", "50.00")):
    return parse_message(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": seq,
            "msg": {
                "market_ticker": mid,
                "yes_dollars_fp": [list(yes)],
                "no_dollars_fp": [list(no)],
            },
        },
        ts,
    )[0]


def _delta(mid, seq, ts, side, price, qty):
    return parse_message(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": seq,
            "msg": {
                "market_ticker": mid,
                "price_dollars": price,
                "delta_fp": qty,
                "side": side,
            },
        },
        ts,
    )[0]


def _trade(mid, seq, ts, price, qty, taker):
    return parse_message(
        {
            "type": "trade",
            "sid": 2,
            "seq": seq,
            "msg": {
                "market_ticker": mid,
                "yes_price_dollars": price,
                "count_fp": qty,
                "taker_side": taker,
                "ts_ms": int(ts.timestamp() * 1000),
            },
        },
        ts,
    )[1]


def test_queue_fill_without_crossing_and_crossing_without_queue(tmp_path):
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)

    # Market A: heavy prints chew through the 30 ahead and fill us
    # pessimistically — but the ask never reaches our bid, so the
    # crossing rule awards nothing (a real fill the sim forgoes).
    store.append_events(_image("A", 10, T0))
    store.append_events(_delta("A", 11, T0 + timedelta(seconds=1), "yes", "0.1000", "1.00"))
    store.append_trades(_trade("A", 12, T0 + timedelta(seconds=60), "0.4000", "35.00", "no"))
    store.append_events(_delta("A", 13, T0 + timedelta(seconds=60), "yes", "0.4000", "-30.00"))
    store.append_events(_delta("A", 14, T0 + timedelta(seconds=90), "yes", "0.1000", "1.00"))

    # Market B: the ask walks down to our bid with NO prints at our
    # level — the crossing rule awards a fill the queue evidence
    # doesn't support (a fill the sim may be inventing).
    store.append_events(_image("B", 20, T0))
    store.append_events(_delta("B", 21, T0 + timedelta(seconds=1), "yes", "0.1000", "1.00"))
    store.append_events(_delta("B", 22, T0 + timedelta(seconds=60), "no", "0.6000", "40.00"))
    store.append_events(_delta("B", 23, T0 + timedelta(seconds=90), "yes", "0.1000", "1.00"))
    store.flush()

    conn = duckdb.connect(str(db), read_only=True)
    a = score_market(conn, "A", T0 - timedelta(minutes=1), qty=5.0)
    b = score_market(conn, "B", T0 - timedelta(minutes=1), qty=5.0)
    conn.close()

    assert len(a) == 1
    assert a[0].price == 0.40 and a[0].tracker.level_size == 30.0
    assert a[0].tracker.filled_pess == 5.0  # 35 traded through 30 ahead
    assert a[0].crossed_at is None  # ask stayed at 0.45

    assert len(b) == 1
    assert b[0].crossed_at is not None  # no-bid 0.60 -> ask 0.40 = our bid
    assert b[0].crossed_qty == 5.0
    assert b[0].tracker.filled_pess == 0.0  # no prints: no queue evidence
    assert b[0].tracker.filled_opt == 0.0


def test_series_composition_groups_by_prefix_high_to_low():
    def vo(mid):
        return VirtualOrder(mid, "yes", 0.5, 5.0, T0, tracker=None)

    orders = [
        vo("KXHIGHNY-26JUL13-B84.5"),
        vo("KXHIGHNY-26JUL13-B85.5"),
        vo("KXHIGHMIA-26JUL13-B90.5"),
        vo("KXFED-26DEC-T4.50"),
        vo("KXFED-26DEC-T4.75"),
        vo("KXFED-26DEC-T5.00"),
    ]
    comp = series_composition(orders)
    # grouped by the prefix before the first '-', ordered high-to-low
    assert comp == {"KXFED": 3, "KXHIGHNY": 2, "KXHIGHMIA": 1}
    assert list(comp)[0] == "KXFED"


class _Tracker:
    """Minimal stand-in carrying only the field concentration reads."""

    def __init__(self, filled_pess):
        self.filled_pess = filled_pess


def _disagreeing(mid, kind, i):
    """A virtual order that disagrees in one direction: `cross` = the sim
    awards a fill the queue evidence doesn't support, `pess` = the reverse."""
    return VirtualOrder(
        mid,
        "yes",
        0.4,
        5.0,
        T0 + timedelta(minutes=i),
        tracker=_Tracker(0.0 if kind == "cross" else 5.0),
        crossed_at=T0 + timedelta(minutes=i) if kind == "cross" else None,
    )


def test_concentration_refuses_a_direction_carried_by_one_market():
    """A large aggregate net that comes from a single market is that market's
    idiosyncrasy, not a fill-model bias: every order in one market rides one
    book, so 30 orders there are nowhere near 30 independent draws."""
    orders = [_disagreeing("KXHIGHNY-26JUL27-B83.5", "cross", i) for i in range(30)]
    for j, mid in enumerate(("KXHIGHMIA-26JUL27-B90.5", "KXHIGHCHI-26JUL27-B88.5")):
        orders.append(_disagreeing(mid, "pess", 100 + j))

    c = concentration_by_market(orders)

    assert c["net_disagreement"] == 28  # +30 / -1 / -1
    assert c["markets_net_over"] == 1 and c["markets_net_under"] == 2
    assert c["top_market_net_share"] == 0.9375  # 30 of 32
    # majority of decisive markets lean the OTHER way -> direction not robust
    assert c["direction_market_robust"] is False


def test_concentration_keeps_a_direction_agreed_across_markets():
    """The same aggregate net spread same-sign across markets is a real
    directional reading — the coarser unit must not swallow it."""
    orders = [
        _disagreeing(f"KXHIGHNY-26JUL27-B8{m}.5", "cross", m * 10 + i)
        for m in range(4)
        for i in range(7)
    ]
    orders.append(_disagreeing("KXHIGHMIA-26JUL27-B90.5", "pess", 99))

    c = concentration_by_market(orders)

    assert c["net_disagreement"] == 27
    assert c["markets_net_over"] == 4 and c["markets_net_under"] == 1
    assert c["direction_market_robust"] is True


def test_underlying_tier_refuses_a_direction_carried_by_one_strike_ladder():
    """The market tier's own "agreed across markets" case is four strikes on
    ONE New York day — one temperature path, not four draws. The strictest
    tier must refuse it while the market tier keeps it."""
    orders = [
        _disagreeing(f"KXHIGHNY-26JUL27-B8{m}.5", "cross", m * 10 + i)
        for m in range(4)
        for i in range(7)
    ]
    orders.append(_disagreeing("KXHIGHMIA-26JUL27-B90.5", "pess", 99))

    c = concentration_by_market(orders)

    assert c["direction_market_robust"] is True  # 4 markets vs 1
    assert c["underlyings"] == 2  # ...which are 2 city-days
    assert c["underlyings_net_over"] == 1 and c["underlyings_net_under"] == 1
    assert c["top_underlying_net_share"] == round(28 / 29, 4)
    assert c["direction_underlying_robust"] is False


def test_underlying_tier_keeps_a_direction_agreed_across_events():
    """The same net spread over separate city-days survives — the tier
    discriminates rather than killing every reading."""
    orders = [
        _disagreeing(f"KXHIGH{city}-26JUL27-B80.5", "cross", c0 * 10 + i)
        for c0, city in enumerate(("NY", "MIA", "CHI"))
        for i in range(7)
    ]
    orders.append(_disagreeing("KXHIGHDEN-26JUL27-T93", "pess", 99))

    c = concentration_by_market(orders)

    assert c["underlyings"] == 4
    assert c["underlyings_net_over"] == 3 and c["underlyings_net_under"] == 1
    assert c["direction_underlying_robust"] is True


def test_sign_test_reads_a_bare_majority_of_three_underlyings_as_a_coin_flip():
    """The load-bearing case, and it is the production shape: the 07-31 weather
    run is 3 city-days splitting 1 over / 2 under, and `direction_underlying_
    robust` certifies it. Both halves are asserted on one fixture — the old
    majority tier must still say True (proving it is the thing being corrected,
    not something already strict) while the sign test reads exactly 0.50, i.e.
    no evidence at all."""
    orders = [
        _disagreeing(f"KXHIGH{city}-26JUL30-B80.5", kind, c0 * 10 + i)
        for c0, (city, kind, n) in enumerate((("NY", "pess", 3), ("MIA", "pess", 8)))
        for i in range(n)
    ]
    orders += [_disagreeing("KXHIGHCHI-26JUL30-B85.5", "cross", 90 + i) for i in range(2)]

    c = concentration_by_market(orders)

    assert c["net_disagreement"] == -9  # -3 NY, -8 MIA, +2 CHI
    assert c["underlyings"] == 3
    assert c["underlyings_net_over"] == 1 and c["underlyings_net_under"] == 2
    assert c["direction_underlying_robust"] is True  # bare majority: 2 of 3
    assert c["underlying_sign_p"] == 0.5  # ...which is a coin flip
    assert c["direction_underlying_significant"] is False
    # the ceiling is a property of the run's WIDTH, not of how it leaned:
    # this run observed 0.5 but could not have beaten 0.125 either way
    assert c["underlying_min_sign_p"] == 0.125


def test_three_underlyings_cannot_reach_significance_however_they_lean():
    """The power ceiling, and it is a property of the bracket's configuration:
    at 3 leaning underlyings the best attainable p is 2^-3 = 0.125, so even a
    unanimous run is not significant. `min_sign_p` must say so, otherwise a
    future pass reads an underpowered run as a failed test of the fill model
    rather than as a test that was never able to run."""
    orders = [
        _disagreeing(f"KXHIGH{city}-26JUL30-B80.5", "pess", c0 * 10 + i)
        for c0, city in enumerate(("NY", "MIA", "CHI"))
        for i in range(4)
    ]

    c = concentration_by_market(orders)

    assert c["underlyings_net_under"] == 3 and c["underlyings_net_over"] == 0
    assert c["direction_underlying_robust"] is True  # unanimous
    assert c["underlying_sign_p"] == 0.125  # ...and still not significant
    assert c["underlying_min_sign_p"] == 0.125  # the ceiling: p == best possible
    assert c["direction_underlying_significant"] is False


def test_sign_test_certifies_a_wide_unanimous_run():
    """The discrimination control: the tier must not be merely always-false.
    Six city-days all leaning the same way is 2^-6 = 0.015625 and clears 0.05,
    so the bracket CAN produce a significant direction — it just needs a top-N
    wide enough to reach six underlyings."""
    orders = [
        _disagreeing(f"KXHIGH{city}-26JUL30-B80.5", "cross", c0 * 10 + i)
        for c0, city in enumerate(("NY", "MIA", "CHI", "DEN", "AUS", "PHIL"))
        for i in range(3)
    ]

    c = concentration_by_market(orders)

    assert c["underlyings"] == 6 and c["underlyings_net_over"] == 6
    assert c["underlying_sign_p"] == 0.015625
    assert c["underlying_min_sign_p"] == 0.015625
    assert c["direction_underlying_significant"] is True


def test_market_tier_carries_the_sign_test_too():
    """The market tier is the same bare-majority test one level down, so it
    gets the same treatment — 4 markets over / 1 under is p=0.1875, not a
    verdict, even though `direction_market_robust` certifies it."""
    orders = [
        _disagreeing(f"KXHIGHNY-26JUL27-B8{m}.5", "cross", m * 10 + i)
        for m in range(4)
        for i in range(7)
    ]
    orders.append(_disagreeing("KXHIGHMIA-26JUL27-B90.5", "pess", 99))

    c = concentration_by_market(orders)

    assert c["direction_market_robust"] is True
    assert c["market_sign_p"] == sign_test_p(4, 5) == 0.1875
    assert c["direction_market_significant"] is False


def test_sign_test_of_an_undirected_run_is_one():
    """No aggregate direction means nothing to test — p must be 1.0, not the
    p of whichever side happens to have more units."""
    orders = [_disagreeing("KXHIGHNY-26JUL27-B83.5", "cross", i) for i in range(3)]
    orders += [_disagreeing("KXHIGHMIA-26JUL27-B90.5", "pess", 50 + i) for i in range(3)]

    c = concentration_by_market(orders)

    assert c["net_disagreement"] == 0
    assert c["underlying_sign_p"] == 1.0
    assert c["direction_underlying_significant"] is False


def test_event_ticker_keeps_negative_strike_suffixes_out_of_the_key():
    """`KXCPI-26JUL-T-0.1` is one CPI print: the strike itself carries a '-',
    so the event key must be split from the left, never rsplit."""
    assert event_ticker("KXCPI-26JUL-T-0.1") == "KXCPI-26JUL"
    assert event_ticker("KXCPI-26JUL-T0.1") == "KXCPI-26JUL"
    assert event_ticker("KXHIGHNY-26JUL28-B79.5") == "KXHIGHNY-26JUL28"


def test_concentration_calls_a_cancelling_split_undirected():
    """Near-zero aggregate net built from large opposing per-market nets is
    the bracket's normal state — no direction to be robust about."""
    orders = [_disagreeing("KXHIGHNY-26JUL27-B83.5", "cross", i) for i in range(7)]
    orders += [_disagreeing("KXHIGHMIA-26JUL27-B90.5", "pess", 20 + i) for i in range(7)]

    c = concentration_by_market(orders)

    assert c["net_disagreement"] == 0
    assert c["abs_net_by_market"] == 14  # aggregate hides 14 of disagreement
    assert c["direction_market_robust"] is False


def _tape_market(store, mid, seq0):
    """A market with a print + a book delta so it qualifies for a bracket."""
    store.append_events(_image(mid, seq0, T0))
    store.append_events(_delta(mid, seq0 + 1, T0 + timedelta(seconds=1), "yes", "0.1000", "1.00"))
    store.append_trades(_trade(mid, seq0 + 2, T0 + timedelta(seconds=30), "0.4000", "5.00", "no"))


def test_select_markets_series_filter_restricts_to_category(tmp_path):
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    # two weather markets (more prints) and one financial market
    _tape_market(store, "KXHIGHNY-26JUL13-B84.5", 10)
    store.append_trades(
        _trade("KXHIGHNY-26JUL13-B84.5", 13, T0 + timedelta(seconds=40), "0.4000", "5.00", "no")
    )
    _tape_market(store, "KXHIGHMIA-26JUL13-B90.5", 20)
    _tape_market(store, "KXFED-26DEC-T4.50", 30)
    store.flush()

    conn = duckdb.connect(str(db), read_only=True)
    since = T0 - timedelta(minutes=1)
    # default: weather markets dominate by print count
    top = select_markets(conn, since, top_n=8)
    assert top[0] == "KXHIGHNY-26JUL13-B84.5"
    assert "KXFED-26DEC-T4.50" in top
    # --series restricts to the requested category only
    fed = select_markets(conn, since, top_n=8, series=["KXFED"])
    conn.close()
    assert fed == ["KXFED-26DEC-T4.50"]


def _order(mid, placed, price=0.4):
    """A VirtualOrder carrying only the fields the independence key reads."""
    return VirtualOrder(mid, "yes", price, 5.0, placed, tracker=None)


def _write_report(out_dir, name, composition, orders):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(
        json.dumps(
            {
                "market_composition": composition,
                "orders_detail": [
                    {"market_id": m, "placed": str(p), "price": pr} for m, p, pr in orders
                ],
            }
        )
    )


def test_independence_flags_overlapping_rerun_as_mostly_not_new(tmp_path):
    """A trailing-window re-run that re-scores the prior run's orders is not
    an independent reading — new_share must expose the overlap."""
    out = tmp_path / "maker_bracket"
    shared = [("KXCPI-26JUL", T0 + timedelta(minutes=i), 0.4) for i in range(9)]
    _write_report(out, "20260726T000000.json", {"KXCPI": 9}, shared)

    orders = [_order(m, p, pr) for m, p, pr in shared]
    orders.append(_order("KXCPI-26JUL", T0 + timedelta(minutes=99)))
    res = independence_vs_prior(out, orders, {"KXCPI": 10})

    assert res["prior_report"] == "20260726T000000.json"
    assert res["orders_new"] == 1
    assert res["orders_shared"] == 9
    assert res["new_share"] == 0.1


def test_independence_compares_within_series_and_handles_first_run(tmp_path):
    """Weather and econ sequences must not be compared to each other, and a
    run with no comparable prior reports reports null rather than 100% new."""
    out = tmp_path / "maker_bracket"
    _write_report(
        out,
        "20260726T000000.json",
        {"KXHIGHNY": 2},
        [("KXHIGHNY-26JUL26", T0, 0.4), ("KXHIGHNY-26JUL26", T0 + timedelta(minutes=1), 0.4)],
    )

    econ = [_order("KXCPI-26JUL", T0)]
    # only a weather report exists, so the econ run has no comparable prior
    assert independence_vs_prior(out, econ, {"KXCPI": 1}) == {
        "prior_report": None,
        "orders_new": None,
        "orders_shared": None,
        "new_share": None,
        "priors_compared": 0,
        "orders_new_vs_all": None,
        "new_share_vs_all": None,
    }
    # a weather run does match the weather report, and is fully new
    weather = [_order("KXHIGHNY-26JUL27", T0 + timedelta(hours=5))]
    res = independence_vs_prior(out, weather, {"KXHIGHNY": 1})
    assert res["prior_report"] == "20260726T000000.json"
    assert res["orders_new"] == 1 and res["orders_shared"] == 0
    assert res["new_share"] == 1.0


def test_independence_vs_all_catches_top_n_churn(tmp_path):
    """The scored market set is only the top-N by print count, and it churns.
    A strike absent from the immediate prior run but scored by an OLDER one is
    not new evidence — new_share cannot see that, new_share_vs_all must."""
    out = tmp_path / "maker_bracket"
    churned = [("KXCPI-26JUL-T-0.1", T0 + timedelta(minutes=i), 0.4) for i in range(8)]
    # oldest run scored the churned strike; the immediate prior dropped it
    _write_report(out, "20260724T000000.json", {"KXCPI": 8}, churned)
    _write_report(out, "20260725T000000.json", {"KXCPI": 2}, [("KXCPI-26JUL-T0.0", T0, 0.4)])

    # this run re-scores the churned strike plus 2 genuinely new orders
    orders = [_order(m, p, pr) for m, p, pr in churned]
    orders += [
        _order("KXCPI-26JUL-T0.5", T0 + timedelta(hours=9) + timedelta(minutes=i)) for i in range(2)
    ]
    res = independence_vs_prior(out, orders, {"KXCPI": 10})

    assert res["prior_report"] == "20260725T000000.json"
    assert res["priors_compared"] == 2
    # against the immediate prior alone, the churned strike looks fresh: 10/10
    assert res["orders_new"] == 10 and res["new_share"] == 1.0
    # against every comparable prior, only the 2 truly-new orders survive
    assert res["orders_new_vs_all"] == 2 and res["new_share_vs_all"] == 0.2


def test_independence_vs_all_still_certifies_a_genuinely_new_run(tmp_path):
    """Control: the tier must discriminate, not merely read lower than
    new_share. A run sharing nothing with any prior stays 1.0 on both."""
    out = tmp_path / "maker_bracket"
    _write_report(
        out, "20260728T000000.json", {"KXHIGHNY": 2}, [("KXHIGHNY-26JUL28-B80.5", T0, 0.4)]
    )
    _write_report(
        out,
        "20260729T000000.json",
        {"KXHIGHNY": 2},
        [("KXHIGHNY-26JUL29-B80.5", T0 + timedelta(hours=24), 0.4)],
    )

    fresh = [_order("KXHIGHNY-26JUL30-B80.5", T0 + timedelta(hours=48))]
    res = independence_vs_prior(out, fresh, {"KXHIGHNY": 1})

    assert res["priors_compared"] == 2
    assert res["new_share"] == 1.0 and res["new_share_vs_all"] == 1.0


def test_independence_vs_all_unions_only_comparable_priors(tmp_path):
    """The union must stay series-scoped: pulling an unrelated category's
    orders into it would let weather runs suppress econ novelty."""
    out = tmp_path / "maker_bracket"
    _write_report(
        out, "20260726T000000.json", {"KXHIGHNY": 1}, [("KXCPI-26JUL-T0.0", T0, 0.4)]
    )  # weather run, econ-shaped order
    _write_report(
        out,
        "20260727T000000.json",
        {"KXCPI": 1},
        [("KXCPI-26JUL-T0.5", T0 + timedelta(hours=1), 0.4)],
    )

    econ = [_order("KXCPI-26JUL-T0.0", T0)]
    res = independence_vs_prior(out, econ, {"KXCPI": 1})

    # only the one econ prior is comparable, and it does not carry this order
    assert res["priors_compared"] == 1
    assert res["new_share"] == 1.0 and res["new_share_vs_all"] == 1.0
