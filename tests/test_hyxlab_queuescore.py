"""queuescore end-to-end on a synthetic archive: the two fill models
disagree exactly where they should."""

from datetime import datetime, timedelta

import duckdb

from collector.venues.kalshi_ws import parse_message
from hyxlab.streamstore import StreamStore
from simulator.queuescore import score_market

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
