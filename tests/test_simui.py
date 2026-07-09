"""simui ReplaySession: manual orders through the real sim, seek/reset
semantics, gap honesty, settlement sanitization, catalog/loader."""

from datetime import datetime, timedelta

import pytest

from hyxlab.models import MarketInfo
from hyxlab.streamstore import BookEvent, StreamStore, StreamTrade
from simulator.bookreplay import BookReplayer
from simulator.simui.session import ReplaySession, list_events, load_session

T0 = datetime(2026, 7, 7, 12, 0)  # naive UTC, as stored


def ev(kind, side, price, qty, seq, sid=1, mid="EV-M1", ts_off=0.0):
    return BookEvent(
        venue="kalshi",
        market_id=mid,
        recv_ts=T0 + timedelta(seconds=ts_off),
        src_ts=None,
        sid=sid,
        seq=seq,
        kind=kind,
        side=side,
        price=price,
        qty=qty,
    )


def image(seq=1, sid=1, mid="EV-M1", ts_off=0.0, yes=0.40, no=0.55):
    """Full book image: yes bid `yes` x100, no bid `no` x30."""
    return [
        ev("snap", "yes", yes, 100, seq, sid, mid, ts_off),
        ev("snap", "no", no, 30, seq, sid, mid, ts_off),
    ]


def make_session(events, trades=(), gaps=(), latency=0.0, result="", **kw):
    mids = sorted({e.market_id for e in events})
    markets = {("kalshi", m): MarketInfo("kalshi", m, title=f"t-{m}", result=result) for m in mids}
    return ReplaySession(mids, events, list(trades), list(gaps), markets, latency=latency, **kw)


def at(ts_off):
    return T0 + timedelta(seconds=ts_off)


# -- BookReplayer.depth -------------------------------------------------------


def test_depth_none_before_seed():
    assert BookReplayer().depth("EV-M1") is None


def test_depth_returns_sorted_ladders_after_advance():
    s = make_session(image() + [ev("delta", "yes", 0.38, 20, seq=2, ts_off=1)])
    s.advance(at(2))
    depth = s.replayer.depth("EV-M1")
    assert depth["yes"] == [(0.40, 100), (0.38, 20)]  # best first
    assert depth["no"] == [(0.55, 30)]


# -- manual trading -----------------------------------------------------------


def test_manual_ioc_buy_fills_at_ask():
    s = make_session(image())
    s.place_order("EV-M1", "yes", 5, tif="IOC")
    frame = s.advance(at(1))
    assert len(frame["fills"]) == 1
    f = frame["fills"][0]
    assert f["account"] == "you"
    assert f["price"] == pytest.approx(0.45)  # 1 - best no bid
    assert f["qty"] == 5


def test_latency_defers_manual_fill_to_later_quote():
    events = image() + [
        # decision-time quote: better no bid appears -> yes_ask = 0.40
        ev("delta", "no", 0.60, 40, seq=2, ts_off=1),
        # after latency window: no bid retreats -> yes_ask back to 0.45
        ev("delta", "no", 0.60, -40, seq=3, ts_off=4),
    ]
    s = make_session(events, latency=2.0)
    s.advance(at(0.5))  # book seeded
    s.place_order("EV-M1", "yes", 5, tif="IOC")  # drains at ts=1 snapshot
    frame = s.advance(at(10))
    prices = [f["price"] for f in frame["fills"] if f["account"] == "you"]
    assert prices == [pytest.approx(0.45)]  # decision-time 0.40 never fillable


def test_account_ledger_tracks_cash_and_equity():
    s = make_session(image(), start_cash=100.0)
    s.place_order("EV-M1", "yes", 10, tif="IOC")
    frame = s.advance(at(1))
    acct = frame["accounts"]["you"]
    fill = frame["fills"][0]
    assert acct["cash"] == pytest.approx(100.0 - 10 * 0.45 - fill["fee"])
    pos = acct["positions"][0]
    assert (pos["side"], pos["qty"]) == ("yes", 10)
    assert acct["equity"] == pytest.approx(acct["cash"] + 10 * pos["mark"])


def test_resting_limit_shows_in_open_orders_and_cancels():
    events = image() + [
        ev("delta", "yes", 0.39, 5, seq=2, ts_off=1),
        ev("delta", "yes", 0.40, -50, seq=3, ts_off=3),  # top-size change: emits
    ]
    s = make_session(events)
    s.place_order("EV-M1", "yes", 5, limit_price=0.30)  # not marketable -> rests
    frame = s.advance(at(2))
    orders = frame["accounts"]["you"]["open_orders"]
    assert len(orders) == 1 and orders[0]["limit_price"] == 0.30
    s.cancel_order(orders[0]["order_id"])  # drains at the next snapshot
    frame = s.advance(at(4))
    assert frame["accounts"]["you"]["open_orders"] == []


def test_place_order_validates_inputs():
    s = make_session(image())
    with pytest.raises(ValueError):
        s.place_order("NOPE", "yes", 1)
    with pytest.raises(ValueError):
        s.place_order("EV-M1", "maybe", 1)
    with pytest.raises(ValueError):
        s.place_order("EV-M1", "yes", 0)
    with pytest.raises(ValueError):
        s.place_order("EV-M1", "yes", 1, limit_price=1.5)


# -- seek / reset -------------------------------------------------------------


def test_seek_seeds_books_without_stepping_sim():
    s = make_session(image())
    s.seek(at(30))
    assert s.replayer.depth("EV-M1") is not None  # book seeded from history
    assert s.sim.result.fills == [] and s.sim.result.equity_curve == []


def test_seek_resets_portfolio_flat():
    s = make_session(image() + image(seq=2, sid=2, ts_off=60))
    s.place_order("EV-M1", "yes", 5, tif="IOC")
    frame = s.advance(at(1))
    assert frame["accounts"]["you"]["positions"]
    s.seek(at(30))
    frame = s.advance(at(61))
    assert frame["accounts"]["you"]["positions"] == []
    assert frame["accounts"]["you"]["cash"] == pytest.approx(s.start_cash)


# -- honesty ------------------------------------------------------------------


def test_settlement_result_is_blanked_in_session_state():
    s = make_session(image(), result="yes")
    assert all(info.result == "" for info in s.markets.values())


def test_ensure_metadata_retries_and_sanitizes(monkeypatch):
    s = make_session(image())
    s.meta_loaded = False
    late = {("kalshi", "EV-M1"): MarketInfo("kalshi", "EV-M1", title="Late", result="yes")}
    monkeypatch.setattr("simulator.simui.session._try_load_markets", lambda _db: late)
    assert s.ensure_metadata() is True
    info = s.sim.markets[("kalshi", "EV-M1")]
    assert info.title == "Late"
    assert info.result == ""  # settlement stays hidden
    assert s.ensure_metadata() is False  # one-shot


def test_gap_invalidates_book_and_delta_after_gap_is_ignored():
    events = image() + [ev("delta", "yes", 0.40, -100, seq=2, ts_off=25)]
    gaps = [(at(10), at(20))]
    s = make_session(events, gaps=gaps)
    s.advance(at(5))
    assert s.replayer.depth("EV-M1") is not None
    frame = s.advance(at(15))
    assert frame["in_gap"] is True  # UI greys the (stale) ladder
    s.advance(at(30))  # gap then delta: book must be unknown, delta ignored
    assert s.replayer.depth("EV-M1") is None


def test_gap_pending_across_empty_batches_still_applies():
    events = image() + image(seq=9, sid=9, ts_off=40, yes=0.30, no=0.60)
    gaps = [(at(10), at(20))]
    s = make_session(events, gaps=gaps)
    s.advance(at(5))
    s.advance(at(15))  # gap due, no events yet -> stays pending
    s.advance(at(25))  # still no events
    s.advance(at(45))  # fresh image re-seeds after invalidation
    depth = s.replayer.depth("EV-M1")
    assert depth["yes"] == [(0.30, 100)]


def test_trade_tape_slices_by_cursor():
    trades = [
        StreamTrade("kalshi", "EV-M1", at(1), None, 0.44, 3, "yes", 1),
        StreamTrade("kalshi", "EV-M1", at(8), None, 0.46, 2, "no", 2),
    ]
    s = make_session(image(), trades=trades)
    f1 = s.advance(at(2))
    assert [t["price"] for t in f1["trades"]] == [0.44]
    f2 = s.advance(at(9))
    assert [t["price"] for t in f2["trades"]] == [0.46]


# -- catalog / loader ---------------------------------------------------------


def test_list_events_and_load_session_roundtrip(tmp_path):
    db = str(tmp_path / "stream.duckdb")
    store = StreamStore(db)
    store.append_events(image(mid="KXTEST-26JUL07-B1.5") + image(seq=2, mid="KXTEST-26JUL07-B2.5"))
    store.append_trades(
        [StreamTrade("kalshi", "KXTEST-26JUL07-B1.5", at(1), None, 0.45, 1, "yes", 1)]
    )
    store.flush()
    events = list_events(stream_db=db, archive_db=str(tmp_path / "missing.duckdb"))
    assert [e["event"] for e in events] == ["KXTEST-26JUL07"]
    assert len(events[0]["markets"]) == 2
    s = load_session(
        "KXTEST-26JUL07",
        stream_db=db,
        archive_db=str(tmp_path / "missing.duckdb"),
        latency=0.0,
    )
    assert s.market_ids == ["KXTEST-26JUL07-B1.5", "KXTEST-26JUL07-B2.5"]
    s.place_order("KXTEST-26JUL07-B1.5", "yes", 1, tif="IOC")
    frame = s.advance(s.t_max + timedelta(seconds=1))
    assert len(frame["fills"]) == 1


# -- equivalence: chunked session replay == canonical one-shot run ------------


def _synthetic_stream(seed=7, n=2500):
    """Deterministic interleaved stream: 3 markets, multi-row snap images,
    signed deltas, plus coverage gaps. Exercises re-seeds, top churn, and
    image groups the same way live capture does."""
    import random

    rng = random.Random(seed)
    mids = ["EV-A", "EV-B", "EV-C"]
    events, gaps = [], []
    seq = {m: 0 for m in mids}
    t = 0.0
    for i in range(n):
        t += rng.uniform(0.05, 2.0)
        m = rng.choice(mids)
        seq[m] += 1
        if rng.random() < 0.04:  # fresh full image
            yes_p = rng.randrange(20, 60) / 100
            no_p = rng.randrange(20, 60) / 100
            events += [
                ev("snap", "yes", yes_p, rng.randrange(5, 200), seq[m], 1, m, t),
                ev("snap", "yes", yes_p - 0.01, rng.randrange(5, 200), seq[m], 1, m, t),
                ev("snap", "no", no_p, rng.randrange(5, 200), seq[m], 1, m, t),
            ]
        else:
            side = rng.choice(["yes", "no"])
            price = rng.randrange(15, 65) / 100
            qty = rng.choice([-40, -10, -5, 5, 10, 40])
            events.append(ev("delta", side, price, qty, seq[m], 1, m, t))
        if rng.random() < 0.004:
            gaps.append((at(t + 0.01), at(t + 0.02)))
    return events, gaps


def test_chunked_session_replay_equals_one_shot_run():
    """The claim the UI rests on: feeding the sim through ReplaySession's
    incremental advance (arbitrary chunk sizes, pending-gap bookkeeping)
    produces EXACTLY the fills and equity of the canonical
    replay_snapshots -> Simulator.run backtest path."""
    import random

    from simulator.bookreplay import replay_snapshots
    from simulator.capabilities import LIVE_VENUE_CAPS
    from simulator.sim import Simulator
    from simulator.simui.session import ManualTrader
    from strategies.probe import TightSpreadProbe

    events, gaps = _synthetic_stream()
    mids = sorted({e.market_id for e in events})
    markets = {("kalshi", m): MarketInfo("kalshi", m) for m in mids}

    def probe():
        return TightSpreadProbe(qty=5, max_spread=0.02, max_mid=0.6, cooldown_min=0.2)

    # Path A: canonical one-shot backtest replay.
    ref = Simulator(
        dict(markets),
        [ManualTrader(), probe()],
        data_capabilities={"kalshi": LIVE_VENUE_CAPS["kalshi"]},
        latency=2.0,
    )
    for snap in replay_snapshots(list(events), gaps=list(gaps)):
        ref.step(snap)

    # Path B: session advanced in random-size time chunks.
    s = ReplaySession(
        mids, list(events), [], list(gaps), markets,
        strategies_factory=lambda: [probe()], latency=2.0,
    )
    rng = random.Random(99)
    cur = s.t_min
    while s.cursor < s.t_max:
        cur = cur + timedelta(seconds=rng.uniform(0.2, 45.0))
        s.advance(cur)

    key = lambda f: (f.ts, f.strategy, f.market_id, f.side, f.qty, f.price, f.fee, f.maker)  # noqa: E731
    assert [key(f) for f in s.sim.result.fills] == [key(f) for f in ref.result.fills]
    assert len(ref.result.fills) > 10  # the comparison must not be vacuous
    assert s.sim.result.equity_curve == ref.result.equity_curve
    # Per-account ledger cross-foot: account equities re-sum to the sim's.
    accts = s._accounts()
    total_delta = sum(a["equity"] - s.start_cash for a in accts.values())
    assert total_delta == pytest.approx(s.sim._equity())
