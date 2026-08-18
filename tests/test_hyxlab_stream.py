"""Stream capture (B7), no network: WS message parsing for both venues,
Kalshi auth signing, seq-gap detection, and StreamStore persistence."""

import contextlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
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


def test_kalshi_sequenced_frame_with_no_book_rows_is_recorded_as_void():
    """A frame that carries sid/seq but archives no book row still consumed a
    wire sequence number. Dropping it leaves a hole in book_events that is
    indistinguishable from real data loss — 70 such seq in the 2026-07-30 26h
    window, against zero seq_gap rows from SeqTracker."""
    events, trades = kalshi_ws.parse_message(
        {"type": "market_lifecycle_v2", "sid": 1, "seq": 77, "msg": {"market_ticker": "M1"}}, RECV
    )
    (e,) = events
    assert (e.kind, e.seq, e.market_id) == ("void", 77, "M1")
    assert trades == []


def test_kalshi_empty_snapshot_is_recorded_as_void():
    """An orderbook_snapshot with empty ladders yields no level rows, so it
    hits the same hole-in-book_events problem as a control frame."""
    events, _ = kalshi_ws.parse_message(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 78,
            "msg": {"market_ticker": "M1", "yes_dollars_fp": [], "no_dollars_fp": []},
        },
        RECV,
    )
    assert [(e.kind, e.seq) for e in events] == [("void", 78)]


def test_void_rows_close_the_seq_hole_they_explain(tmp_path):
    """The point of the void row: the frame's seq is present in book_events,
    so the QA continuity check sees a contiguous run instead of a hole it
    cannot attribute."""
    from collector import qa

    store = StreamStore(tmp_path / "s.duckdb")
    for seq, typ in ((1, "delta"), (2, "void"), (3, "delta")):
        frame = (
            {"type": "orderbook_delta", "sid": 1, "seq": seq,
             "msg": {"market_ticker": "M1", "price_dollars": "0.40",
                     "delta_fp": "1.00", "side": "yes"}}
            if typ == "delta"
            else {"type": "market_lifecycle_v2", "sid": 1, "seq": seq, "msg": {}}
        )
        store.append_events(kalshi_ws.parse_message(frame, RECV)[0])
    store.flush()
    conn = qa._connect_ro(str(tmp_path / "s.duckdb"))
    seqs = [r[0] for r in conn.execute("SELECT seq FROM book_events ORDER BY seq").fetchall()]
    assert seqs == [1, 2, 3]


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


def test_kalshi_books_keeps_retrying_past_exhausted_ladder_never_subscribes_empty(
    tmp_path, monkeypatch
):
    """Regression (EXP-005): if every ladder rung returns empty (~220s REST
    outage), keep repeating the last rung until tickers exist. Subscribing
    with an EMPTY set captures nothing, and the hourly in-loop refresh may
    never fire if the venue drops the empty subscription."""
    import asyncio

    from collector import streamd

    calls: list[int] = []

    def fake_open_tickers(series):
        calls.append(1)
        if len(calls) > 20:  # bound: a broken retry loop must not spin forever
            raise AssertionError("unbounded ticker refetch")
        return set() if len(calls) < 8 else {"T1"}  # empty past the whole ladder

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

    assert len(calls) == 8  # kept fetching past the four-rung ladder
    assert waits == [10, 30, 60, 120, 120, 120, 120]  # last rung repeats
    assert '"T1"' in subscribed["books"]  # never subscribed with an empty set


def test_poly_books_retries_empty_initial_token_set_until_subscribed(tmp_path, monkeypatch):
    """Regression (EXP-005): an empty initial token set (watchlist empty AND
    Gamma unreachable at boot) must be retried — not left permanently idle
    until a manual daemon restart."""
    import asyncio

    import pytest

    from collector import streamd

    calls: list[int] = []

    def fake_token_set(self):
        calls.append(1)
        return [] if len(calls) < 3 else ["tok1", "tok2"]  # empty twice, then live

    monkeypatch.setattr(streamd.Daemon, "_poly_token_set", fake_token_set)

    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(secs):
        waits.append(secs)
        await real_sleep(0)

    monkeypatch.setattr(streamd.asyncio, "sleep", fake_sleep)

    sent: list[str] = []

    class FakeWS:
        async def send(self, msg):
            sent.append(msg)
            raise asyncio.CancelledError  # subscription observed; stop the loop

    class FakeConnect:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return FakeWS()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(streamd.websockets, "connect", FakeConnect)

    store = StreamStore(tmp_path / "s.duckdb")
    d = streamd.Daemon(store, watchlist={})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(d.poly_books())

    assert len(calls) == 3  # initial fetch + two retries, stops when non-empty
    assert waits == [10, 30]  # same short ladder as kalshi-books
    assert "tok1" in sent[0]  # ended up subscribed with the recovered set


def duckdb_reason(path):
    import duckdb

    with duckdb.connect(str(path), read_only=True) as conn:
        return conn.execute("SELECT reason FROM stream_gaps").fetchone()[0]


# -- seq-reset synthetic gap rows (S-190; S-188 loss class 3) ------------------
#
# Every (re)connect resets the Kalshi ws sequence. In-process reconnects
# already logged a 'reconnect' row, but a PROCESS RESTART lost `last_recv`
# and wrote nothing per-channel — the 2026-08-02 17:16 restart's misses sat
# outside the coarse '*' daemon_start row. The loop must now write a
# 'seq_reset' row bounded [last persisted/in-process recv_ts -> first
# post-reset frame recv_ts] on each connection's first frame, and nothing
# extra while the sequence flows normally.


def _scripted_kalshi_loop(tmp_path, monkeypatch, connections, db=None):
    """Run _kalshi_loop against scripted per-connection frame lists.

    `connections` is a list of lists of raw frames; each inner list is one
    ws connection's recv() sequence. Exhausting the LAST connection raises
    CancelledError (test over); exhausting an earlier one raises
    ConnectionError (forces a reconnect). Returns the store."""
    import asyncio

    from collector import streamd

    class ScriptedWS:
        def __init__(self, frames, last):
            self._frames = list(frames)
            self._last = last

        async def send(self, msg):
            pass

        async def recv(self):
            if not self._frames:
                raise asyncio.CancelledError if self._last else ConnectionError("scripted drop")
            return self._frames.pop(0)

    remaining = [list(c) for c in connections]

    class FakeConnect:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return ScriptedWS(remaining.pop(0), last=not remaining)

        async def __aexit__(self, *exc):
            return False

    async def no_sleep(secs):
        pass

    monkeypatch.setattr(streamd.websockets, "connect", FakeConnect)
    monkeypatch.setattr(streamd.kalshi_ws, "auth_headers", lambda kid, pem: {})
    monkeypatch.setattr(streamd.asyncio, "sleep", no_sleep)

    store = StreamStore(db or tmp_path / "s.duckdb")
    d = streamd.Daemon(store, watchlist={})
    d.key_id, d.pem = "k", b"p"
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(d._kalshi_loop("trades", lambda: "{}", None))
    store.flush()
    return store


def _gap_rows(path):
    import duckdb

    with duckdb.connect(str(path), read_only=True) as conn:
        return conn.execute(
            "SELECT venue, channel, started_at, ended_at, reason"
            " FROM stream_gaps ORDER BY ended_at"
        ).fetchall()


def test_restart_writes_seq_reset_row_bounded_by_persisted_recv_ts(tmp_path, monkeypatch):
    """Fresh process over a DB with history (the restart-blackout class):
    the first frame must produce a per-channel seq_reset row starting at
    the channel's last PERSISTED recv_ts and ending at that frame."""
    db = tmp_path / "s.duckdb"
    prior = StreamStore(db)  # the previous daemon's life
    prior.append_trades(kalshi_ws.parse_message(_trade_frame(), RECV)[1])
    prior.flush()

    before = datetime.now(UTC).replace(tzinfo=None)
    _scripted_kalshi_loop(tmp_path, monkeypatch, [[json.dumps(_trade_frame(seq=1))]], db=db)

    rows = [r for r in _gap_rows(db) if r[4] == "seq_reset"]
    assert len(rows) == 1
    venue, channel, started, ended, _ = rows[0]
    assert (venue, channel) == ("kalshi", "trades")
    assert started == RECV.replace(tzinfo=None)  # last persisted recv_ts
    assert ended >= before  # first post-reset frame, not connect/start time


def test_in_seq_flow_writes_no_further_gap_rows(tmp_path, monkeypatch):
    """Consecutive in-seq frames after the seed frame add nothing: one
    seq_reset per connection, zero seq_gap."""
    db = tmp_path / "s.duckdb"
    prior = StreamStore(db)
    prior.append_trades(kalshi_ws.parse_message(_trade_frame(), RECV)[1])
    prior.flush()

    frames = [json.dumps(_trade_frame(seq=s)) for s in (1, 2, 3, 4)]
    store = _scripted_kalshi_loop(tmp_path, monkeypatch, [frames], db=db)

    reasons = [r[4] for r in _gap_rows(db)]
    assert reasons == ["seq_reset"]  # exactly one, no seq_gap/reconnect noise
    assert store.counts()["stream_trades"] == 5  # prior + all four frames kept


def test_empty_db_first_ever_run_writes_no_seq_reset(tmp_path, monkeypatch):
    """Nothing persisted -> nothing was being covered -> no synthetic row
    (mirrors mark_startup_gap's no-op on an empty DB)."""
    store = _scripted_kalshi_loop(tmp_path, monkeypatch, [[json.dumps(_trade_frame(seq=1))]])
    assert _gap_rows(store.path) == []


def test_reconnect_seq_reset_row_spans_last_frame_to_first_new_frame(tmp_path, monkeypatch):
    """In-process reconnect: the seq_reset row must start at the LAST frame
    of the dying connection (in-process last_recv, not the DB) and end at
    the new connection's first frame — extending the 'reconnect' row, which
    ends at connect time, through the actual resume."""
    store = _scripted_kalshi_loop(
        tmp_path,
        monkeypatch,
        [[json.dumps(_trade_frame(seq=1))], [json.dumps(_trade_frame(seq=1))]],
    )

    rows = _gap_rows(store.path)
    reasons = [r[4] for r in rows]
    assert reasons == ["reconnect", "seq_reset"]  # first connection seeded none (empty DB)
    reconnect, seq_reset = rows
    assert seq_reset[:2] == ("kalshi", "trades")
    assert seq_reset[2] == reconnect[2]  # both start at the dying conn's last recv
    assert seq_reset[3] >= reconnect[3]  # ...but seq_reset covers through first frame


def test_last_recv_ts_reads_per_channel_table(tmp_path):
    """trades channel -> stream_trades; book channels -> book_events;
    empty tables -> None."""
    store = StreamStore(tmp_path / "s.duckdb")
    assert store.last_recv_ts("kalshi", "trades") is None
    store.append_trades(kalshi_ws.parse_message(_trade_frame(), RECV)[1])
    store.append_events(
        [BookEvent("kalshi", "M1", RECV.replace(hour=13), None, 1, 1, "snap", "yes", 0.4, 1.0)]
    )
    store.flush()
    assert store.last_recv_ts("kalshi", "trades") == RECV.replace(tzinfo=None)
    assert store.last_recv_ts("kalshi", "books") == RECV.replace(hour=13, tzinfo=None)
    assert store.last_recv_ts("polymarket", "trades") is None  # venue-scoped


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


def test_spilled_counter_tracks_overflow_and_resets_on_drain(tmp_path, monkeypatch):
    """During a wedge past the cap, `pending` plateaus at SPILL_CAP — the
    `spilled` counter must expose the sidecar growth, and reset once a
    good flush drains the sidecar (EXP-005)."""
    import duckdb
    import pytest

    store = StreamStore(tmp_path / "s.duckdb")
    assert store.spilled == 0
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 10)
    store.append_trades([_seq_trade(i) for i in range(25)])
    _wedge(monkeypatch)
    with pytest.raises(duckdb.IOException):
        store.flush()
    assert store.spilled == 15  # overflow past the cap is visible
    assert store.pending == 10  # ...while pending sits at the plateau
    monkeypatch.undo()

    assert store.flush() == 25  # sidecar + memory drained
    assert store.spilled == 0  # counter reset with the sidecar gone


def test_flusher_log_shows_spilled_rows_during_wedge(tmp_path, monkeypatch, capsys):
    """The flusher's failure line must include sidecar growth when rows
    have spilled — pending alone plateaus at the cap and hides it."""
    import asyncio

    import pytest

    from collector import streamd

    store = StreamStore(tmp_path / "s.duckdb")
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 10)
    store.append_trades([_seq_trade(i) for i in range(25)])
    _wedge(monkeypatch)

    sleeps: list[float] = []

    async def fake_sleep(secs):
        if sleeps:
            raise asyncio.CancelledError  # one flush round is enough
        sleeps.append(secs)

    monkeypatch.setattr(streamd.asyncio, "sleep", fake_sleep)

    d = streamd.Daemon(store, watchlist={})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(d.flusher())

    out = capsys.readouterr().out
    assert "flush FAILED" in out
    assert "10 rows held for retry" in out
    assert "15 spilled to sidecar" in out  # growth visible past the plateau


# -- spill under a REAL writer-lock wedge (EXP-936) ----------------------------
#
# Everything above wedges by monkeypatching duckdb.connect to raise. That
# proves the bookkeeping but not that the mechanism survives the actual
# production failure — a concurrent read_only connection holding the DuckDB
# file lock, which is what all 68 observed `flush FAILED` events were. These
# hold a genuine lock from a SECOND PROCESS instead.

_READER = (
    "import duckdb,sys;"
    "c=duckdb.connect(sys.argv[1],read_only=True);"
    "c.execute('SELECT count(*) FROM book_events').fetchone();"
    "print('LOCKED',flush=True);sys.stdin.readline()"
)


@contextlib.contextmanager
def _real_reader_lock(db):
    """Hold a real DuckDB read lock on `db` from another process."""
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "-c", _READER, str(db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "LOCKED"
        yield
    finally:
        proc.stdin.write("\n")
        proc.stdin.flush()
        proc.wait(timeout=30)


def test_real_reader_lock_wedge_spills_and_reconciles_every_row(tmp_path, monkeypatch):
    """A REAL concurrent reader (not a patched connect) must wedge the
    writer, drive the overflow to the sidecar, and lose nothing on drain."""
    import duckdb

    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 40)

    injected = 0
    with _real_reader_lock(db):
        with pytest.raises(duckdb.IOException):  # the lock is genuinely held
            duckdb.connect(str(db)).close()
        for batch in range(5):  # five wedged flush rounds, like the 15 s flusher
            store.append_trades([_seq_trade(injected + i) for i in range(20)])
            injected += 20
            with contextlib.suppress(Exception):
                store.flush()

    assert store.pending == 40 == StreamStore.SPILL_CAP  # memory bounded
    spilled = sum(1 for _ in store._spill_path.open())
    assert store.pending + spilled == injected  # nothing lost mid-wedge

    store.append_trades([_seq_trade(injected)])  # ingest continues post-wedge
    injected += 1
    assert store.flush() == injected
    with duckdb.connect(str(db), read_only=True) as conn:
        seqs = [r[0] for r in conn.execute("SELECT seq FROM stream_trades").fetchall()]
    assert seqs == list(range(injected))  # sidecar first, recv order, zero holes
    assert not store._spill_path.exists()


@pytest.mark.skipif(
    os.environ.get("EXP936_FULL_CAP") != "1",
    reason="~95 s: drives the real 400k SPILL_CAP; set EXP936_FULL_CAP=1",
)
def test_real_wedge_past_the_unpatched_spill_cap(tmp_path):
    """The same run at the SHIPPED SPILL_CAP, no monkeypatching: 600k rows
    injected under a real lock must come back as exactly 600k rows."""
    import duckdb

    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    injected = 0
    with _real_reader_lock(db):
        for batch in range(24):
            store.append_trades([_seq_trade(injected + i) for i in range(25_000)])
            injected += 25_000
            with contextlib.suppress(Exception):
                store.flush()
    assert injected == 600_000
    assert store.pending == StreamStore.SPILL_CAP == 400_000
    assert store.flush() == injected
    with duckdb.connect(str(db), read_only=True) as conn:
        n, distinct = conn.execute(
            "SELECT count(*), count(DISTINCT seq) FROM stream_trades"
        ).fetchone()
    assert n == distinct == injected  # zero duplicates, zero holes


def test_torn_sidecar_record_does_not_stall_archiving(tmp_path, monkeypatch):
    """Regression (EXP-936, measured): a truncated trailing record — host
    crash or ENOSPC mid-append — used to raise JSONDecodeError inside every
    subsequent flush, permanently stopping ALL archiving and funnelling live
    ingest into the same poisoned file. The drain must skip the torn record,
    count it, and keep writing."""
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 0)
    store.append_trades([_seq_trade(i) for i in range(10)])
    _wedge(monkeypatch)
    with contextlib.suppress(Exception):
        store.flush()
    monkeypatch.undo()

    raw = store._spill_path.read_bytes()
    store._spill_path.write_bytes(raw[:-30])  # tear the last record

    store.append_trades([_seq_trade(99)])
    assert store.flush() == 10  # 9 readable spill rows + the live one
    assert store.spill_corrupt == 1  # the hole is counted, not silent
    assert not store._spill_path.exists()
    assert store.counts()["stream_trades"] == 10
    assert store.flush() == 0  # and the poisoned-file loop is gone
    store.append_trades([_seq_trade(100)])
    assert store.flush() == 1  # archiving continues normally


def test_failed_sidecar_append_rewinds_and_keeps_rows_in_memory(tmp_path, monkeypatch):
    """A part-written sidecar append (ENOSPC) must rewind to the last record
    boundary and leave every row in the buffer — a torn record is a hole and
    a half-written one must not be paid for twice."""
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 5)
    store.append_trades([_seq_trade(i) for i in range(10)])
    _wedge(monkeypatch)
    with contextlib.suppress(Exception):
        store.flush()
    monkeypatch.undo()  # undo() is wholesale — re-arm the cap below
    good = store._spill_path.stat().st_size
    assert good > 0 and store.pending == 5
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 5)

    class _HalfWriter:
        """Writes 40 bytes of the batch, then the disk fills up."""

        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            return False

        def writelines(self, lines):
            self._f.write("".join(lines)[:40])
            self._f.flush()
            raise OSError(28, "No space left on device")

    real_open = Path.open

    def flaky_open(self, *a, **kw):
        f = real_open(self, *a, **kw)
        mode = a[0] if a else kw.get("mode", "r")
        return _HalfWriter(f) if "a" in mode else f

    monkeypatch.setattr(Path, "open", flaky_open)
    store.append_trades([_seq_trade(i) for i in range(10, 20)])
    _wedge(monkeypatch)
    with pytest.raises(OSError):
        store.flush()
    monkeypatch.undo()

    assert store._spill_path.stat().st_size == good  # rewound to the boundary
    assert store.pending == 15  # all 15 rows still buffered, none dropped
    assert store.flush() == 20
    assert store.spill_corrupt == 0  # nothing torn, so nothing lost


def test_crash_between_commit_and_unlink_redrains_duplicates_not_holes(tmp_path, monkeypatch):
    """The sidecar is unlinked only AFTER the transaction commits. A crash in
    that window must replay it — duplicates are the stated design intent,
    holes are not."""
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 0)
    store.append_trades([_seq_trade(i) for i in range(4)])
    _wedge(monkeypatch)
    with contextlib.suppress(Exception):
        store.flush()
    monkeypatch.undo()

    survived = store._spill_path.read_bytes()  # what a crash would leave behind
    assert store.flush() == 4
    assert store.counts()["stream_trades"] == 4
    store._spill_path.write_bytes(survived)  # ...died before the unlink

    fresh = StreamStore(db)
    assert fresh.flush() == 4  # re-drained
    assert fresh.counts()["stream_trades"] == 8  # duplicated, not lost
    assert fresh.spill_corrupt == 0
    assert not fresh._spill_path.exists()


def test_flusher_logs_sidecar_holes(tmp_path, monkeypatch, capsys):
    """A skipped sidecar record is a real archive hole; the store no longer
    stalls on it, so the journal is the only place it can surface."""
    import asyncio

    from collector import streamd

    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 0)
    store.append_trades([_seq_trade(i) for i in range(3)])
    _wedge(monkeypatch)
    with contextlib.suppress(Exception):
        store.flush()
    monkeypatch.undo()
    raw = store._spill_path.read_bytes()
    store._spill_path.write_bytes(raw[:-30])

    async def fake_sleep(secs):
        if getattr(fake_sleep, "done", False):
            raise asyncio.CancelledError
        fake_sleep.done = True

    monkeypatch.setattr(streamd.asyncio, "sleep", fake_sleep)
    d = streamd.Daemon(store, watchlist={})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(d.flusher())

    out = capsys.readouterr().out
    assert "CRITICAL sidecar drain skipped 1 unreadable row(s)" in out


def test_open_tickers_paces_between_series(monkeypatch):
    """EXP-936 follow-on: the hourly ticker refresh makes ONE /markets call
    per series with no spacing. get_markets' own `pause_s` paces between
    PAGES, which does nothing for a burst of N single-page series calls.
    Measured 2026-08-02 at 23 series: that burst drew 429s (KXPAYROLLS,
    KXU3). The watchlist is now 31 series, so pin that the calls are spaced.
    """
    from collector import streamd

    sleeps: list[float] = []
    monkeypatch.setattr(streamd._time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        streamd.kalshi, "get_markets", lambda **kw: [{"ticker": kw["series_ticker"] + "-X"}]
    )

    out = streamd.open_tickers(["A", "B", "C"], pause_s=0.25)

    assert out == {"A-X", "B-X", "C-X"}
    # N series => N-1 gaps: paced BETWEEN calls, never before the first.
    assert sleeps == [0.25, 0.25]


def test_open_tickers_pause_zero_disables_spacing(monkeypatch):
    """pause_s=0 must not sleep at all — keeps existing tests/callers fast."""
    from collector import streamd

    sleeps: list[float] = []
    monkeypatch.setattr(streamd._time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        streamd.kalshi, "get_markets", lambda **kw: [{"ticker": kw["series_ticker"] + "-X"}]
    )

    streamd.open_tickers(["A", "B"], pause_s=0)

    assert sleeps == []


def test_open_tickers_still_paces_when_a_series_raises(monkeypatch):
    """A failing series must not skip the pacing for the ones after it —
    otherwise a run of failures reproduces the very burst this prevents."""
    from collector import streamd

    sleeps: list[float] = []
    monkeypatch.setattr(streamd._time, "sleep", lambda s: sleeps.append(s))

    def boom(**kw):
        if kw["series_ticker"] == "B":
            raise RuntimeError("429")
        return [{"ticker": kw["series_ticker"] + "-X"}]

    monkeypatch.setattr(streamd.kalshi, "get_markets", boom)

    out = streamd.open_tickers(["A", "B", "C"], pause_s=0.1)

    assert out == {"A-X", "C-X"}
    assert sleeps == [0.1, 0.1]


def test_kalshi_dead_air_logs_gap_and_reconnects(tmp_path, monkeypatch):
    """Regression (2026-08-13): the trades channel reconnected half-dead —
    subscription gone but WS pings keeping TCP alive — and recv() blocked
    forever (timeout=None when refresh is None): zero trades archived for
    75+ minutes with no error line. Silence past DEAD_AIR_SECS must log a
    dead_air gap and force a reconnect."""
    import asyncio

    from collector import streamd

    monkeypatch.setattr(streamd, "DEAD_AIR_SECS", 0.0)
    monkeypatch.setattr(streamd.kalshi_ws, "auth_headers", lambda kid, pem: {})

    async def instant_timeout(awaitable, timeout):
        assert timeout is not None  # the old code passed None and blocked forever
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(streamd.asyncio, "wait_for", instant_timeout)

    async def no_sleep(secs):
        pass

    monkeypatch.setattr(streamd.asyncio, "sleep", no_sleep)

    class FakeWS:
        async def send(self, msg):
            pass

        async def recv(self):
            pass

    connects = 0

    class FakeConnect:
        def __init__(self, *a, **kw):
            nonlocal connects
            connects += 1
            if connects > 1:
                raise asyncio.CancelledError  # stop the test after one reconnect

        async def __aenter__(self):
            return FakeWS()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(streamd.websockets, "connect", FakeConnect)

    store = StreamStore(tmp_path / "s.duckdb")
    d = streamd.Daemon(store, watchlist={})
    d.key_id, d.pem = "k", b"p"
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(d._kalshi_loop("trades", lambda: "{}", None))

    assert connects == 2  # dead air forced a reconnect
    store.flush()
    assert duckdb_reason(tmp_path / "s.duckdb") == "dead_air"
