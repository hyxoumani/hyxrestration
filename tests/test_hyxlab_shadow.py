"""Shadow harness: step/run equivalence, live-tail cursor semantics,
ledger persistence — all against synthetic archives, no network."""

import json
from datetime import UTC, datetime, timedelta

import duckdb

from collector.venues.kalshi_ws import parse_message
from hyxlab.models import MarketInfo, Order, Snapshot
from hyxlab.streamstore import StreamStore
from simulator.shadow import ShadowLedger, ShadowRunner
from simulator.sim import Simulator
from simulator.strategy import Strategy

T0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)


def snap(mid, ts, yes_bid, yes_ask):
    return Snapshot(
        venue="kalshi",
        market_id=mid,
        ts=ts,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=1 - yes_ask,
        no_ask=1 - yes_bid,
        yes_bid_size=100,
        yes_ask_size=100,
        no_bid_size=100,
        no_ask_size=100,
    )


class BuyFirst(Strategy):
    name = "buy_first"
    done = False

    def on_snapshot(self, s, ctx):
        if self.done:
            return []
        self.done = True
        return [Order(s.venue, s.market_id, "yes", 5, tif="IOC")]


def test_step_loop_equals_run():
    markets = {("kalshi", "M1"): MarketInfo(venue="kalshi", market_id="M1", result="yes")}
    snaps = [snap("M1", T0 + timedelta(seconds=i), 0.40, 0.41) for i in range(5)]

    r1 = Simulator(markets, [BuyFirst()], latency=1.0).run(snaps)
    sim2 = Simulator(markets, [BuyFirst()], latency=1.0)
    for s in snaps:
        sim2.step(s)
    r2 = sim2.finalize()
    assert json.dumps(r1.metrics, sort_keys=True, default=str) == json.dumps(
        r2.metrics, sort_keys=True, default=str
    )


def _snapshot_frame(mid, seq, bid_cents, ask_no_cents, ts):
    return parse_message(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": seq,
            "msg": {
                "market_ticker": mid,
                "yes_dollars_fp": [[f"{bid_cents / 100:.4f}", "100.00"]],
                "no_dollars_fp": [[f"{ask_no_cents / 100:.4f}", "100.00"]],
            },
        },
        ts,
    )[0]


def test_shadow_tails_only_the_future_and_persists_fills(tmp_path):
    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"
    shadow_db = tmp_path / "shadow.duckdb"

    from hyxlab.store import Store

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    # History BEFORE shadow starts: must never be traded.
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))  # yes 0.40/0.41
    sstore.flush()

    runner = ShadowRunner(
        [BuyFirst()],
        latency=0.0,
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        ledger=ShadowLedger(shadow_db),
    )
    assert runner.poll_once() == 0  # first poll only anchors the cursor
    assert runner.sim.result.fills == []

    # New events arrive after anchoring -> processed, filled, persisted.
    sstore.append_events(_snapshot_frame("M1", 2, 44, 55, T0 + timedelta(seconds=30)))
    sstore.flush()
    n = runner.poll_once()
    assert n == 1
    assert len(runner.sim.result.fills) == 1
    assert runner.sim.result.fills[0].price == 0.45  # 1 - 0.55 no bid
    with duckdb.connect(str(shadow_db), read_only=True) as conn:
        fills = conn.execute("SELECT strategy, price, qty FROM shadow_fills").fetchall()
        runs = conn.execute("SELECT count(*) FROM shadow_runs").fetchone()[0]
    assert fills == [("buy_first", 0.45, 5.0)]
    assert runs == 1

    # Idempotent persistence: nothing new -> no duplicate rows.
    runner.poll_once()
    with duckdb.connect(str(shadow_db), read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_fills").fetchone()[0] == 1


def test_shadow_gap_invalidates_books(tmp_path):
    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"

    from hyxlab.store import Store

    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="M1")])
    store.close()

    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    sstore.flush()
    runner = ShadowRunner(
        [BuyFirst()],
        latency=0.0,
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        ledger=ShadowLedger(tmp_path / "shadow.duckdb"),
    )
    runner.poll_once()  # anchor

    # A delta after a coverage gap must NOT produce a snapshot (book
    # unknown until re-seeded).
    t1 = T0 + timedelta(seconds=60)
    sstore.append_gap("kalshi", "books", t1, t1 + timedelta(seconds=1), "reconnect")
    delta = parse_message(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 5,
            "msg": {
                "market_ticker": "M1",
                "price_dollars": "0.4400",
                "delta_fp": "10.00",
                "side": "yes",
                "ts_ms": int((t1 + timedelta(seconds=2)).timestamp() * 1000),
            },
        },
        t1 + timedelta(seconds=2),
    )[0]
    sstore.append_events(delta)  # parse_message returns the events LIST
    sstore.flush()
    assert runner.poll_once() == 0  # suppressed: unknown book
    # Fresh image re-seeds and flows again.
    sstore.append_events(_snapshot_frame("M1", 6, 44, 55, t1 + timedelta(seconds=10)))
    sstore.flush()
    assert runner.poll_once() == 1
