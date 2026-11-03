"""EXP-2007: per-connection minute heartbeats and the subscription log, no
network.

The point of the rows is ONE distinction the archive could not make before:
a zero-frame minute on a connection that was up and whose subscription the
venue confirmed (quiet) versus one on a connection that was down or whose
subscription the venue had dropped (dead). Every test below is phrased as a
pair of those, or as a guard on how the rows reach disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime

import duckdb
import pytest

from collector import heartbeat as hbmod
from collector import streamd
from collector.bookcov import BookCoverage
from collector.heartbeat import ConnBeat, HeartbeatBook, classify
from collector.venues import kalshi_ws, polymarket_ws
from hyxlab.streamstore import _COLS, StreamHeartbeat, StreamStore, StreamSubEvent, _from_iso

T0 = datetime(2026, 11, 3, 21, 0, tzinfo=UTC).timestamp()  # a minute boundary


# -- the classification rule ---------------------------------------------------


@pytest.mark.parametrize(
    ("connected", "frames", "ok", "dropped", "unanswered", "want"),
    [
        (60.0, 5, 0, 0, 0, (True, "frames")),
        (60.0, 0, 1, 0, 0, (True, "probe")),  # QUIET, proven
        (60.0, 0, 0, 0, 0, (None, "transport")),  # socket up, nothing more known
        (60.0, 0, 0, 0, 1, (False, "probe_unanswered")),  # DEAD subscription
        (60.0, 3, 0, 1, 0, (False, "probe_dropped")),  # repair books do not rescue it
        (30.0, 9, 1, 0, 0, (False, "disconnected")),  # a partial count is not a count
        (0.0, 0, 0, 0, 0, (False, "disconnected")),  # DEAD transport
    ],
)
def test_classify_separates_quiet_from_dead(connected, frames, ok, dropped, unanswered, want):
    assert classify(connected, frames, ok, dropped, unanswered) == want


def test_one_second_of_slack_and_no_more():
    assert classify(hbmod.FULL_MINUTE_S, 0, 1, 0, 0) == (True, "probe")
    assert classify(hbmod.FULL_MINUTE_S - 0.01, 0, 1, 0, 0) == (False, "disconnected")


# -- the minute roll -----------------------------------------------------------


def _book(tmp_path, enabled=True):
    store = StreamStore(tmp_path / "s.duckdb")
    book = HeartbeatBook(store, enabled=enabled)
    book.minute_start = T0
    return store, book


def test_quiet_probed_minute_and_killed_minute_are_different_rows(tmp_path):
    store, book = _book(tmp_path)
    quiet = book.beat("polymarket", "market", "quiet")
    dead = book.beat("polymarket", "market", "dead")
    quiet.up(T0 - 5)
    dead.up(T0 - 5)
    quiet.probe_sent(T0 + 30)
    quiet.probe_ok(T0 + 30.2)
    book.down(dead, T0 + 20)  # the socket dies 20 s into the minute
    assert book.roll(T0 + 60.1) == 2
    rows = {r.conn: r for r in store._heartbeats}
    assert (rows["quiet"].alive, rows["quiet"].evidence, rows["quiet"].frames) == (True, "probe", 0)
    assert (rows["dead"].alive, rows["dead"].evidence, rows["dead"].frames) == (
        False,
        "disconnected",
        0,
    )
    assert rows["dead"].connected_s == pytest.approx(20.0)
    assert rows["quiet"].connected_s == 60.0


def test_counters_reset_each_minute_and_state_carries(tmp_path):
    store, book = _book(tmp_path)
    b = book.beat("kalshi", "trades", "trades")
    b.up(T0 - 1)
    b.sid = 1
    b.frame(datetime.fromtimestamp(T0 + 1, UTC), T0 + 1, seq=41)
    b.frame(datetime.fromtimestamp(T0 + 2, UTC), T0 + 2, seq=42)
    book.roll(T0 + 60)
    book.roll(T0 + 120)
    first, second = store._heartbeats
    assert (first.frames, first.last_seq, first.alive) == (2, 42, True)
    # Silent second minute: no frames, but sid/last_seq carry -- the seq
    # proof survives into the next frame's minute; a firehose has no probe.
    assert (second.frames, second.last_seq, second.sid) == (0, 42, 1)
    assert (second.alive, second.evidence) == (None, "transport")


def test_a_late_roll_still_writes_every_minute(tmp_path):
    store, book = _book(tmp_path)
    book.beat("kalshi", "trades", "trades").up(T0 - 1)
    assert book.roll(T0 + 3 * 60 + 5) == 3
    assert [r.minute.timestamp() for r in store._heartbeats] == [T0, T0 + 60, T0 + 120]


def test_a_clock_step_forward_does_not_backfill_hours_of_rows(tmp_path):
    store, book = _book(tmp_path)
    book.beat("kalshi", "trades", "trades").up(T0 - 1)
    n = book.roll(T0 + 86400 + 5)  # the wall clock jumped a day
    assert n == 1  # only the minute that just ended
    assert store._heartbeats[0].minute.timestamp() == T0 + 86400 - 60
    assert store._heartbeats[0].connected_s == 60.0


def test_a_retired_connection_writes_its_last_minute_then_stops(tmp_path):
    store, book = _book(tmp_path)
    b = book.beat("kalshi", "books", "books-2", 2)
    b.up(T0 - 1)
    b.down(T0 + 10, book.minute_start)
    b.retired = True
    book.roll(T0 + 60)
    book.roll(T0 + 120)
    assert [(r.conn, r.alive) for r in store._heartbeats] == [("books-2", False)]
    assert not book.beats


def test_disabled_book_writes_nothing(tmp_path):
    store, book = _book(tmp_path, enabled=False)
    b = book.beat("kalshi", "trades", "trades")
    b.up(T0 - 1)
    book.sub(b, "connect")
    assert book.roll(T0 + 60) == 0
    assert store.pending == 0


def test_shutdown_writes_the_partial_minute_as_not_alive(tmp_path):
    store, book = _book(tmp_path)
    b = book.beat("kalshi", "trades", "trades")
    b.up(T0 - 1)
    b.frame(datetime.fromtimestamp(T0 + 1, UTC), T0 + 1, seq=1)
    assert book.roll_partial(T0 + 60 + 15) == 2
    last = store._heartbeats[-1]
    assert last.minute.timestamp() == T0 + 60
    assert (last.alive, last.evidence) == (False, "disconnected")


def test_probe_expiry_tells_dropped_from_unanswered():
    b = ConnBeat("polymarket", "market", "poly-books", None)
    b.up(T0)
    b.probe_sent(T0 + 1)
    assert b.probe_expired(T0 + 5, 10.0) is None  # still in time
    b.probe_books += 1
    assert b.probe_expired(T0 + 12, 10.0) == "dropped"
    b.probe_sent(T0 + 20)
    assert b.probe_expired(T0 + 31, 10.0) == "unanswered"
    assert (b.probes_dropped, b.probes_unanswered, b.probes_sent) == (1, 1, 2)


# -- the store -----------------------------------------------------------------


def _hb_row(**kw):
    base = dict(
        venue="kalshi",
        channel="books",
        conn="books-3",
        shard=3,
        minute=datetime(2026, 11, 3, 21, 0, tzinfo=UTC),
        emitted_ts=datetime(2026, 11, 3, 21, 1, 0, 50000, tzinfo=UTC),
        connected_s=60.0,
        alive=None,
        evidence="transport",
        sid=3000001,
        last_seq=None,
        frames=0,
        control_frames=0,
        keepalives=0,
        probes_sent=0,
        probes_ok=0,
        probes_dropped=0,
        n_markets=None,
        last_frame_ts=None,
    )
    base.update(kw)
    return StreamHeartbeat(**base)


def _sub_row(**kw):
    base = dict(
        venue="polymarket",
        channel="market",
        conn="poly-books",
        shard=None,
        ts=datetime(2026, 11, 3, 21, 0, 5, tzinfo=UTC),
        action="disconnect",
        reason="dead_air",
        sid=None,
        n_markets=100,
        detail='quote " and, comma\nnewline',
    )
    base.update(kw)
    return StreamSubEvent(**base)


def test_heartbeat_and_subscription_rows_round_trip_with_nulls(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_heartbeats([_hb_row(), _hb_row(alive=True, evidence="frames", frames=7)])
    store.append_subevents([_sub_row(), _sub_row(reason=None, detail=None)])
    assert store.flush() == 4
    with duckdb.connect(str(store.path), read_only=True) as conn:
        hb = conn.execute(
            "SELECT alive, evidence, frames, last_seq, last_frame_ts FROM stream_heartbeats"
            " ORDER BY frames"
        ).fetchall()
        subs = conn.execute(
            "SELECT reason, detail FROM stream_subscriptions ORDER BY reason NULLS LAST"
        ).fetchall()
    assert hb == [(None, "transport", 0, None, None), (True, "frames", 7, None, None)]
    assert subs == [("dead_air", 'quote " and, comma\nnewline'), (None, None)]
    assert store.counts()["stream_heartbeats"] == 2


def test_heartbeat_rows_survive_the_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 0)
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_heartbeats([_hb_row(last_frame_ts=datetime(2026, 11, 3, 20, 59, tzinfo=UTC))])
    store.append_subevents([_sub_row()])
    assert store.spill_all() == 2
    assert store.pending == 0
    assert store.flush() == 2
    counts = store.counts()
    assert counts["stream_heartbeats"] == counts["stream_subscriptions"] == 1
    assert store.spill_corrupt == 0


def test_a_pre_heartbeat_daemon_skips_the_new_sidecar_records_instead_of_misfiling_them(
    tmp_path, monkeypatch
):
    """Rollback hazard: the old decoder sends every unknown tag down the gap
    branch, which parses field 2 as a timestamp. Field 2 of both new tables
    is the connection label, which must fail that parse (-> skipped and
    counted) rather than land a heartbeat in stream_gaps."""
    assert _COLS["stream_heartbeats"][2][0] == _COLS["stream_subscriptions"][2][0] == "conn"
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 0)
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_heartbeats([_hb_row()])
    store.append_subevents([_sub_row()])
    store.spill_all()
    for line in store._spill_path.read_text().splitlines():
        r = json.loads(line)["r"]
        with pytest.raises(ValueError):
            _from_iso(r[2])  # exactly what the old gap branch does first
    for label in ("trades", "ticker", "lifecycle", "books-0", "books-13", "poly-books"):
        with pytest.raises(ValueError):
            _from_iso(label)


def test_heartbeats_are_not_data_for_the_startup_gap(tmp_path):
    """mark_startup_gap and last_recv_ts bound holes by the last DATA frame;
    a heartbeat written at shutdown must not shorten the next boot's gap."""
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_heartbeats([_hb_row()])
    store.flush()
    store.mark_startup_gap()
    assert store._gaps == []  # empty archive of data: still nothing covered


# -- the daemon: kalshi --------------------------------------------------------


class _WS:
    def __init__(self, inbox: asyncio.Queue, sent: list) -> None:
        self.inbox, self.sent = inbox, sent

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item


def _install_connect(monkeypatch, inbox, sent, connects):
    class Connect:
        def __init__(self, *a, **kw):
            connects.append(1)

        async def __aenter__(self):
            return _WS(inbox, sent)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(streamd.websockets, "connect", Connect)


def _daemon(tmp_path, monkeypatch):
    store = StreamStore(tmp_path / "s.duckdb")
    d = streamd.Daemon(store, {})
    d.key_id, d.pem = "k", b"p"
    d.hb.enabled = True
    monkeypatch.setattr(streamd.kalshi_ws, "auth_headers", lambda kid, pem: {})
    return d, store


def _subs(store):
    return [(s.conn, s.action, s.reason) for s in store._subevents]


def test_kalshi_books_shard_logs_connect_subscribe_update_and_seq_gap(tmp_path, monkeypatch):
    d, store = _daemon(tmp_path, monkeypatch)
    inbox, sent, connects = asyncio.Queue(), [], []
    _install_connect(monkeypatch, inbox, sent, connects)
    cov = BookCoverage({"S"})
    d.books = cov
    (sh,) = cov.boot({"S-1"})

    def snap(t, seq):
        return json.dumps(
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "seq": seq,
                "msg": {"market_ticker": t, "yes_dollars_fp": [["0.4000", "10.00"]]},
            }
        )

    async def drive():
        task = asyncio.create_task(
            d._kalshi_loop("books", lambda: kalshi_ws.subscribe_books(sorted(sh.assigned)), sh)
        )
        await inbox.put(json.dumps({"type": "subscribed", "id": 1, "msg": {"sid": 1}}))
        await inbox.put(snap("S-1", 1))
        await asyncio.sleep(0.05)
        cov.on_lifecycle("S-2", "activated", None, 0.0)
        await d._apply_books()
        await inbox.put(snap("S-1", 5))  # a jump: seq_gap -> reconnect
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert _subs(store) == [
        ("books-0", "connect", None),
        ("books-0", "subscribed", None),
        ("books-0", "update_add", None),
        ("books-0", "disconnect", "seq_gap"),
    ]
    beat = d.hb.beats[("kalshi", "books", "books-0")]
    assert beat.last_seq == 1 and beat.frames == 1  # the jumped frame was never stored
    assert beat.sid == 1  # shard 0 keeps the wire sid
    assert beat.up_since is None  # cancelled -> down


# -- the daemon: polymarket ----------------------------------------------------


def _poly(tmp_path, monkeypatch, inbox, sent, connects):
    d, store = _daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(d, "_poly_token_set", lambda: ["A", "B"])
    monkeypatch.setattr(streamd, "POLY_PING_SECS", 0.02)
    monkeypatch.setattr(streamd, "POLY_PROBE_QUIET_S", 0.0)
    monkeypatch.setattr(streamd, "POLY_PROBE_WAIT_S", 0.05)
    _install_connect(monkeypatch, inbox, sent, connects)
    return d, store


def _probes(sent):
    out = []
    for m in sent:
        with contextlib.suppress(ValueError):
            j = json.loads(m)
            if j.get("operation") == "subscribe":
                out.append(j)
    return out


def test_poly_probe_answer_proves_a_quiet_subscription_without_a_reconnect(tmp_path, monkeypatch):
    inbox, sent, connects = asyncio.Queue(), [], []
    d, store = _poly(tmp_path, monkeypatch, inbox, sent, connects)

    async def drive():
        task = asyncio.create_task(d.poly_books())
        await asyncio.sleep(0.05)  # a quiet stretch: the probe goes out
        await inbox.put(polymarket_ws.PROBE_HELD_REPLY)  # bare text, not JSON
        await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    beat = d.hb.beats[("polymarket", "market", "poly-books")]
    assert _probes(sent) and _probes(sent)[0]["assets_ids"] == ["A", "B"]
    assert beat.probes_ok == 1
    assert len(connects) == 1  # the text never reached json.loads
    assert ("poly-books", "connect", None) in _subs(store)
    assert store._events == [] and store._gaps == []  # an intact answer costs no row


def test_poly_probe_that_brings_back_books_records_a_dropped_subscription(tmp_path, monkeypatch):
    inbox, sent, connects = asyncio.Queue(), [], []
    d, store = _poly(tmp_path, monkeypatch, inbox, sent, connects)
    book = json.dumps(
        {
            "event_type": "book",
            "asset_id": "A",
            "market": "m",
            "timestamp": "1762203600000",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [],
        }
    )

    async def drive():
        task = asyncio.create_task(d.poly_books())
        await asyncio.sleep(0.03)  # probe out
        await inbox.put(book)  # the venue re-adds what it had dropped
        await asyncio.sleep(0.15)  # past the wait with no "NO NEW ASSETS"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert ("poly-books", "probe_dropped", "probe") in _subs(store)
    assert d.stats.get("poly_probe_dropped") == 1
    assert len(connects) == 1


def test_poly_probe_answered_with_an_empty_list_is_a_drop_not_silence(tmp_path, monkeypatch):
    """Staging 2026-11-03: assets of resolved markets have no book, so the
    venue re-adds them with `[]` -- still proof they were not held."""
    inbox, sent, connects = asyncio.Queue(), [], []
    d, store = _poly(tmp_path, monkeypatch, inbox, sent, connects)

    async def drive():
        task = asyncio.create_task(d.poly_books())
        await asyncio.sleep(0.03)
        await inbox.put("[]")
        await asyncio.sleep(0.15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert ("poly-books", "probe_dropped", "probe") in _subs(store)
    assert len(connects) == 1


def test_poly_probe_that_hears_nothing_is_recorded_unanswered(tmp_path, monkeypatch):
    inbox, sent, connects = asyncio.Queue(), [], []
    d, store = _poly(tmp_path, monkeypatch, inbox, sent, connects)

    async def drive():
        task = asyncio.create_task(d.poly_books())
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert ("poly-books", "probe_unanswered", "probe") in _subs(store)
    assert d.stats.get("poly_probe_unanswered", 0) >= 1


def test_heartbeat_off_sends_no_probe_and_writes_no_row(tmp_path, monkeypatch):
    inbox, sent, connects = asyncio.Queue(), [], []
    d, store = _poly(tmp_path, monkeypatch, inbox, sent, connects)
    d.hb.enabled = False

    async def drive():
        task = asyncio.create_task(d.poly_books())
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert _probes(sent) == []
    assert store._subevents == [] and store._heartbeats == []
