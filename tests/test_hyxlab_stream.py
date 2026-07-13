"""Stream capture (B7), no network: WS message parsing for both venues,
Kalshi auth signing, seq-gap detection, and StreamStore persistence."""

import json
from datetime import UTC, datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from collector.venues import kalshi_ws, polymarket_ws
from hyxlab.streamstore import BookEvent, StreamStore, StreamTrade

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


def test_kalshi_books_retries_empty_initial_ticker_set(tmp_path, monkeypatch):
    """Regression (review backlog 2026-07-11): an empty INITIAL open_tickers
    result (Kalshi REST down at boot) must be retried on a short ladder —
    not left dark until the hourly TICKER_REFRESH_SECS refresh."""
    import asyncio

    from collector import streamd

    calls: list[list[str]] = []

    def fake_open_tickers(series):
        calls.append(series)
        return set() if len(calls) < 3 else {"T1", "T2"}  # empty twice, then live

    monkeypatch.setattr(streamd, "open_tickers", fake_open_tickers)

    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(secs):
        waits.append(secs)
        await real_sleep(0)

    monkeypatch.setattr(streamd.asyncio, "sleep", fake_sleep)

    subscribed: dict[str, str] = {}

    async def fake_loop(self, channel, make_subscribe, refresh):
        subscribed[channel] = make_subscribe()

    monkeypatch.setattr(streamd.Daemon, "_kalshi_loop", fake_loop)

    store = StreamStore(tmp_path / "s.duckdb")
    d = streamd.Daemon(store, watchlist={"kalshi_series": ["S1"]})
    asyncio.run(d.kalshi_books())

    assert len(calls) == 3  # initial fetch + two short retries, stops when non-empty
    assert waits == [10, 30]  # short ladder, far below TICKER_REFRESH_SECS
    assert all(w < streamd.TICKER_REFRESH_SECS for w in waits)
    assert '"T1"' in subscribed["books"]  # loop got the recovered set


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


# -- spill-to-sidecar (multi-hour reader wedge cap) ----------------------------


def _seq_trade(seq):
    from datetime import timedelta

    return StreamTrade("kalshi", "M1", RECV + timedelta(seconds=seq), RECV, 0.4, 5.0, "yes", seq)


def _wedge(monkeypatch):
    """Same blocked-writer simulation as test_flush_failure_preserves_buffer."""
    import duckdb

    from hyxlab import streamstore as ss

    def locked(*args, **kwargs):
        raise duckdb.IOException("Could not set lock on file")

    monkeypatch.setattr(ss.duckdb, "connect", locked)


def test_wedged_flush_past_cap_spills_overflow_and_bounds_memory(tmp_path, monkeypatch):
    """A reader wedge lasting past SPILL_CAP pending rows must move the
    oldest overflow to the sidecar and keep the in-memory buffer at the cap."""
    import duckdb
    import pytest

    store = StreamStore(tmp_path / "s.duckdb")
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 10)
    store.append_trades([_seq_trade(i) for i in range(25)])
    _wedge(monkeypatch)
    with pytest.raises(duckdb.IOException):
        store.flush()
    assert store.pending <= 10  # memory bounded at the cap
    assert store._spill_path.exists()  # overflow landed on disk


def test_recovery_flush_drains_sidecar_before_memory_with_zero_loss(tmp_path, monkeypatch):
    """After a wedge that spilled, the next good flush must land every
    ingested row exactly once — sidecar (oldest) first, recv order intact."""
    import duckdb
    import pytest

    store = StreamStore(tmp_path / "s.duckdb")
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 10)
    store.append_trades([_seq_trade(i) for i in range(25)])
    _wedge(monkeypatch)
    with pytest.raises(duckdb.IOException):
        store.flush()
    monkeypatch.undo()

    store.append_trades([_seq_trade(i) for i in range(25, 30)])  # ingest continues post-wedge
    assert store.flush() == 30  # sidecar + memory drained in one transaction
    with duckdb.connect(str(tmp_path / "s.duckdb"), read_only=True) as conn:
        rows = conn.execute("SELECT seq FROM stream_trades ORDER BY rowid").fetchall()
    seqs = [r[0] for r in rows]
    assert seqs == list(range(30))  # zero loss, original ingest order
    assert not store._spill_path.exists()  # sidecar removed after commit


def test_spill_roundtrip_covers_events_trades_and_gaps(tmp_path, monkeypatch):
    """Every buffered row type (incl. None fields) must survive the
    sidecar round-trip faithfully."""
    import duckdb
    import pytest

    store = StreamStore(tmp_path / "s.duckdb")
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 0)  # spill everything on failure
    store.append_events([BookEvent("kalshi", "M1", RECV, None, None, 7, "snap", "yes", 0.4, 100.0)])
    store.append_trades([_seq_trade(1)])
    store.append_gap("kalshi", "books", RECV, RECV, "seq_gap")
    _wedge(monkeypatch)
    with pytest.raises(duckdb.IOException):
        store.flush()
    monkeypatch.undo()

    assert store.pending == 0  # all three rows spilled
    assert store.flush() == 3
    assert store.counts() == {"book_events": 1, "stream_trades": 1, "stream_gaps": 1}


def test_sidecar_drain_stores_original_event_timestamps(tmp_path, monkeypatch):
    """Rows drained from the sidecar must keep their ORIGINAL recv/src
    timestamps (naive UTC), never a drain-time restamp (mistakes #10)."""
    import duckdb
    import pytest

    store = StreamStore(tmp_path / "s.duckdb")
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 0)
    store.append_trades([_seq_trade(0)])
    _wedge(monkeypatch)
    with pytest.raises(duckdb.IOException):
        store.flush()
    monkeypatch.undo()

    store.flush()
    with duckdb.connect(str(tmp_path / "s.duckdb"), read_only=True) as conn:
        recv, src = conn.execute("SELECT recv_ts, src_ts FROM stream_trades").fetchone()
    assert recv == RECV.replace(tzinfo=None)  # original event time, naive UTC
    assert src == RECV.replace(tzinfo=None)


def test_sidecar_survives_daemon_restart_and_drains_on_first_flush(tmp_path, monkeypatch):
    """A daemon that crashed while wedged leaves a sidecar on disk; a
    fresh StreamStore's first good flush must recover those rows."""
    import duckdb
    import pytest

    store = StreamStore(tmp_path / "s.duckdb")
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 0)
    store.append_trades([_seq_trade(i) for i in range(3)])
    _wedge(monkeypatch)
    with pytest.raises(duckdb.IOException):
        store.flush()
    monkeypatch.undo()

    fresh = StreamStore(tmp_path / "s.duckdb")  # restart: empty memory buffers
    assert fresh.pending == 0
    assert fresh.flush() == 3  # first flush drains the crashed daemon's sidecar
    assert fresh.counts()["stream_trades"] == 3
