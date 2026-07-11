"""Stream capture (B7), no network: WS message parsing for both venues,
Kalshi auth signing, seq-gap detection, and StreamStore persistence."""

import json
from datetime import UTC, datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from collector.venues import kalshi_ws, polymarket_ws
from hyxlab.streamstore import StreamStore

RECV = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


# -- kalshi auth ------------------------------------------------------------


def test_kalshi_auth_signature_verifies_with_public_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    headers = kalshi_ws.auth_headers("key-id", pem, ts_ms=1751000000000)
    assert headers["KALSHI-ACCESS-KEY"] == "key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1751000000000"
    import base64

    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1751000000000GET/trade-api/ws/v2",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )  # raises InvalidSignature on mismatch


# -- kalshi parsing ----------------------------------------------------------


def test_kalshi_trade_message_parses_to_dollars():
    # Live shape (probed 2026-07-07): string dollars + fp strings + ts_ms.
    raw = json.dumps(
        {
            "type": "trade",
            "sid": 1,
            "seq": 7,
            "msg": {
                "trade_id": "2d87822c",
                "market_ticker": "KXHIGHNY-26JUL07-T82",
                "yes_price_dollars": "0.3700",
                "no_price_dollars": "0.6300",
                "count_fp": "76.92",
                "taker_side": "yes",
                "taker_outcome_side": "yes",
                "taker_book_side": "bid",
                "ts": 1751889600,
                "ts_ms": 1751889600578,
            },
        }
    )
    events, trades = kalshi_ws.parse_message(raw, RECV)
    assert events == []
    (t,) = trades
    assert t.price == 0.37
    assert t.qty == 76.92
    assert t.taker_side == "yes"
    assert t.seq == 7
    assert t.src_ts == datetime.fromtimestamp(1751889600.578, tz=UTC)


def test_kalshi_orderbook_snapshot_expands_levels():
    raw = {
        "type": "orderbook_snapshot",
        "sid": 2,
        "seq": 1,
        "msg": {
            "market_ticker": "M1",
            "market_id": "5015b3f7",
            "yes_dollars_fp": [["0.4000", "100.00"], ["0.3900", "50.00"]],
            "no_dollars_fp": [["0.5900", "30.00"]],
        },
    }
    events, trades = kalshi_ws.parse_message(raw, RECV)
    assert trades == []
    assert len(events) == 3
    assert all(e.kind == "snap" for e in events)
    yes = [e for e in events if e.side == "yes"]
    assert {(e.price, e.qty) for e in yes} == {(0.40, 100.0), (0.39, 50.0)}


def test_kalshi_orderbook_delta_keeps_signed_qty():
    raw = {
        "type": "orderbook_delta",
        "sid": 2,
        "seq": 9,
        "msg": {
            "market_ticker": "M1",
            "market_id": "624695c9",
            "price_dollars": "0.4000",
            "delta_fp": "-20.00",
            "side": "yes",
            "ts": "2026-07-07T19:13:19.229566Z",
            "ts_ms": 1783451599229,
        },
    }
    events, _ = kalshi_ws.parse_message(raw, RECV)
    (e,) = events
    assert e.kind == "delta"
    assert e.qty == -20.0
    assert e.price == 0.40
    assert e.src_ts == datetime.fromtimestamp(1783451599.229, tz=UTC)


def test_kalshi_control_frames_parse_to_nothing():
    assert kalshi_ws.parse_message({"type": "subscribed", "id": 1}, RECV) == ([], [])


def test_seq_tracker_flags_jump_once_per_sid():
    tr = kalshi_ws.SeqTracker()
    assert tr.observe(1, 1) is False  # first observation
    assert tr.observe(1, 2) is False  # consecutive
    assert tr.observe(2, 10) is False  # other sid, independent
    assert tr.observe(1, 5) is True  # jump 2 -> 5
    assert tr.observe(1, 6) is False  # recovers after the jump
    tr.reset()
    assert tr.observe(1, 99) is False  # fresh after reset


# -- polymarket parsing -------------------------------------------------------


def test_poly_book_snapshot_and_array_frames():
    frame = json.dumps(
        [
            {
                "event_type": "book",
                "asset_id": "tok1",
                "timestamp": "1751889600000",
                "bids": [{"price": "0.44", "size": "120"}],
                "asks": [{"price": "0.46", "size": "80"}],
            }
        ]
    )
    events, trades = polymarket_ws.parse_message(frame, RECV)
    assert trades == []
    assert len(events) == 2
    bid = next(e for e in events if e.side == "bid")
    assert (bid.kind, bid.price, bid.qty) == ("snap", 0.44, 120.0)
    assert bid.src_ts == datetime.fromtimestamp(1751889600, tz=UTC)


def test_poly_price_change_carries_absolute_size():
    frame = {
        "event_type": "price_change",
        "asset_id": "tok1",
        "timestamp": "1751889601000",
        "changes": [{"price": "0.45", "side": "SELL", "size": "0"}],
    }
    events, _ = polymarket_ws.parse_message(frame, RECV)
    (e,) = events
    assert (e.kind, e.side, e.qty) == ("delta", "ask", 0.0)  # 0 = level removed


def test_poly_last_trade_price_becomes_trade():
    frame = {
        "event_type": "last_trade_price",
        "asset_id": "tok1",
        "price": "0.45",
        "size": "33",
        "side": "BUY",
        "timestamp": "1751889602000",
    }
    _, trades = polymarket_ws.parse_message(frame, RECV)
    (t,) = trades
    assert (t.price, t.qty, t.taker_side) == (0.45, 33.0, "buy")


# -- daemon clock tripwire ----------------------------------------------------


def test_clock_step_logged_as_gap(tmp_path):
    from datetime import timedelta

    from collector.streamd import Daemon

    store = StreamStore(tmp_path / "s.duckdb")
    d = Daemon(store, watchlist={})
    d._clock_check("kalshi", "trades", RECV, RECV - timedelta(seconds=1))  # forward: fine
    assert store.pending == 0
    d._clock_check("kalshi", "trades", RECV - timedelta(seconds=20), RECV)  # backward step
    assert store.pending == 1
    store.flush()
    reason = duckdb_reason(tmp_path / "s.duckdb")
    assert reason.startswith("clock_step_-20")


def duckdb_reason(path):
    import duckdb

    with duckdb.connect(str(path), read_only=True) as conn:
        return conn.execute("SELECT reason FROM stream_gaps").fetchone()[0]


# -- stream store -------------------------------------------------------------


def _trade_frame(seq=1):
    return {
        "type": "trade",
        "sid": 2,
        "seq": seq,
        "msg": {
            "market_ticker": "M1",
            "yes_price_dollars": "0.4000",
            "count_fp": "5.00",
            "taker_side": "yes",
            "ts": 1751889600,
            "ts_ms": 1751889600000,
        },
    }


def test_streamstore_flush_roundtrip(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    events, trades = kalshi_ws.parse_message(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {"market_ticker": "M1", "yes_dollars_fp": [["0.4000", "100.00"]]},
        },
        RECV,
    )
    store.append_events(events)
    store.append_trades(kalshi_ws.parse_message(_trade_frame(seq=3), RECV)[1])
    assert store.pending == 2
    assert store.flush() == 2
    assert store.pending == 0
    assert store.flush() == 0  # idempotent on empty buffer
    assert store.counts() == {"book_events": 1, "stream_trades": 1, "stream_gaps": 0}


def test_streamstore_timestamps_stored_naive_utc(tmp_path):
    import duckdb

    store = StreamStore(tmp_path / "s.duckdb")
    store.append_trades(kalshi_ws.parse_message(_trade_frame(), RECV)[1])
    store.flush()
    with duckdb.connect(str(tmp_path / "s.duckdb"), read_only=True) as conn:
        recv, src = conn.execute("SELECT recv_ts, src_ts FROM stream_trades").fetchone()
    assert recv == RECV.replace(tzinfo=None)  # naive UTC, not box-local
    assert src == datetime.fromtimestamp(1751889600, tz=UTC).replace(tzinfo=None)


def test_flush_failure_preserves_buffer(tmp_path, monkeypatch):
    """Regression: a failed flush (e.g. a reader holds the file lock) must
    keep the batch buffered for the next attempt. Dropping it leaves silent
    unmarked holes in the archive — root cause of the 2026-07 negative
    reconstructed-book-levels QA failures."""
    import duckdb
    import pytest

    from hyxlab import streamstore as ss

    store = StreamStore(tmp_path / "s.duckdb")
    events, _ = kalshi_ws.parse_message(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {"market_ticker": "M1", "yes_dollars_fp": [["0.4000", "100.00"]]},
        },
        RECV,
    )
    store.append_events(events)
    store.append_trades(kalshi_ws.parse_message(_trade_frame(seq=3), RECV)[1])
    store.append_gap("kalshi", "books", RECV, RECV, "seq_gap")
    n = store.pending

    def locked(*args, **kwargs):
        raise duckdb.IOException("Could not set lock on file")

    monkeypatch.setattr(ss.duckdb, "connect", locked)
    with pytest.raises(duckdb.IOException):
        store.flush()
    monkeypatch.undo()

    assert store.pending == n  # batch survived the failure
    assert store.flush() == n
    assert store.counts() == {"book_events": 1, "stream_trades": 1, "stream_gaps": 1}


def test_streamstore_gap_rows(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_gap("kalshi", "books", RECV, RECV, "seq_gap")
    store.flush()
    assert store.counts()["stream_gaps"] == 1


def test_startup_gap_marks_downtime_only_when_history_exists(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    store.mark_startup_gap(now=RECV)
    assert store.pending == 0  # empty DB -> nothing was being covered
    store.append_trades(kalshi_ws.parse_message(_trade_frame(), RECV)[1])
    store.flush()
    store.mark_startup_gap(now=datetime(2026, 7, 7, 13, 0, tzinfo=UTC))
    assert store.pending == 1  # downtime gap buffered
    store.flush()
    assert store.counts()["stream_gaps"] == 1
