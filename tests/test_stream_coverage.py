"""The 2026-10-20 stream coverage expansion, no network.

- `update_subscription` payloads match the venue's documented shape.
- The book set is sharded, follows the lifecycle channel, and is edited in
  place: a ticker-set change sends a command and NEVER reconnects.
- `ticker` and `market_lifecycle_v2` frames parse and round-trip through the
  stream store (NULLs included), with KXMVE combos excluded by default.
- The staged-CSV insert stores exactly what the old `executemany` stored.
- Raw frames rotate hourly into decodable compressed files.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from collector import streamd
from collector.bookcov import BookCoverage
from collector.rawframes import RawFrameWriter, _codec
from collector.venues import kalshi_ws
from hyxlab.streamstore import (
    _COLS,
    SHARD_SID_STRIDE,
    BookEvent,
    StreamLifecycle,
    StreamStore,
    StreamTicker,
    StreamTrade,
    _insert_frame,
)

RECV = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


# -- payloads ------------------------------------------------------------------


def test_update_subscription_add_and_delete_payloads_match_the_docs():
    add = json.loads(kalshi_ws.update_subscription(7, ["B-2", "A-1"], "add_markets", 12))
    assert add == {
        "id": 12,
        "cmd": "update_subscription",
        "params": {"sid": 7, "market_tickers": ["A-1", "B-2"], "action": "add_markets"},
    }
    rm = json.loads(kalshi_ws.update_subscription(7, ["A-1"], "delete_markets", 13))
    assert rm["params"] == {"sid": 7, "market_tickers": ["A-1"], "action": "delete_markets"}
    snap = json.loads(kalshi_ws.update_subscription(7, ["A-1"], "get_snapshot", 14))
    assert snap["params"]["action"] == "get_snapshot"
    with pytest.raises(ValueError):
        kalshi_ws.update_subscription(7, ["A-1"], "add", 15)


def test_exchange_wide_subscribes_name_no_market():
    for payload, channel in (
        (kalshi_ws.subscribe_ticker(), "ticker"),
        (kalshi_ws.subscribe_lifecycle(), "market_lifecycle_v2"),
    ):
        params = json.loads(payload)["params"]
        assert params == {"channels": [channel]}


def test_control_info_reads_sid_from_subscribed_ok_and_error_frames():
    assert kalshi_ws.control_info(
        {"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 4}}
    ) == ("subscribed", 1, 4, None)
    assert kalshi_ws.control_info({"type": "ok", "id": 9, "sid": 4, "seq": 3, "msg": {}}) == (
        "ok",
        9,
        4,
        None,
    )
    assert kalshi_ws.control_info(
        {"type": "error", "id": 9, "msg": {"code": 26, "msg": "limit"}}
    ) == ("error", 9, None, 26)


# -- coverage bookkeeping ------------------------------------------------------


def test_boot_shards_at_the_cap_and_adds_open_a_new_shard_only_when_full():
    cov = BookCoverage({"S"}, shard_max=2)
    shards = cov.boot({"S-1", "S-2", "S-3"})
    assert [sorted(s.assigned) for s in shards] == [["S-1", "S-2"], ["S-3"]]
    assert all(s.at_boot for s in shards)
    touched, created, emptied = cov.apply({"S-4"}, set())
    assert created == [] and touched == {1}  # room on shard 1
    touched, created, emptied = cov.apply({"S-5"}, set())
    assert len(created) == 1 and not created[0].at_boot
    assert created[0].assigned == {"S-5"}


def test_a_shard_emptied_by_removals_is_closed_not_kept_subscribed_to_nothing():
    cov = BookCoverage({"S"}, shard_max=1)
    a, b = cov.boot({"S-1", "S-2"})
    touched, created, emptied = cov.apply(set(), {"S-2"})
    assert emptied == [b] and cov.shards == [a]


def test_market_limit_rejection_learns_the_cap_and_rehomes_the_rejects():
    cov = BookCoverage({"S"}, shard_max=10)
    (sh,) = cov.boot({"S-1", "S-2", "S-3"})
    sh.assigned |= {"S-4", "S-5"}
    sh.subscribed = set(sh.assigned)
    cov.over_limit(sh, {"S-4", "S-5"})
    assert cov.shard_max == 3
    adds, removes = cov.take()
    _, created, _ = cov.apply(adds, removes)
    assert len(created) == 1 and created[0].assigned == {"S-4", "S-5"}


def test_lifecycle_adds_open_markets_defers_future_ones_and_closes_on_determination():
    cov = BookCoverage({"KXBTC15M"})
    future = datetime.now(UTC) + timedelta(minutes=10)
    assert cov.on_lifecycle("KXBTC15M-A", "created", future, 0.0)
    assert cov.on_lifecycle("KXBTC15M-B", "activated", None, 0.0)
    assert not cov.on_lifecycle("KXETH15M-C", "activated", None, 0.0)  # unwatched series
    assert cov.take() == ({"KXBTC15M-B"}, set())
    cov.due_opens(future + timedelta(seconds=1))
    assert cov.take() == ({"KXBTC15M-A"}, set())
    cov.on_lifecycle("KXBTC15M-A", "determined", None, 5.0)
    assert cov.take() == (set(), {"KXBTC15M-A"})


def test_reconcile_does_not_resurrect_a_market_the_lifecycle_closed():
    cov = BookCoverage({"S"})
    cov.boot({"S-1", "S-2"})
    cov.on_lifecycle("S-1", "settled", None, 100.0)
    cov.reconcile({"S-1", "S-2", "S-3"}, 101.0)  # REST still lists S-1 open
    adds, removes = cov.take()
    assert adds == {"S-3"} and removes == {"S-1"}


# -- the daemon: in-place edits, no reconnect -----------------------------------


class _WS:
    """A scripted connection: frames are pushed by the test, sends recorded."""

    def __init__(self, inbox: asyncio.Queue, sent: list) -> None:
        self.inbox, self.sent = inbox, sent

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item


def _daemon(tmp_path, monkeypatch, watchlist=None):
    store = StreamStore(tmp_path / "s.duckdb")
    d = streamd.Daemon(store, watchlist or {})
    d.key_id, d.pem = "k", b"p"
    monkeypatch.setattr(streamd.kalshi_ws, "auth_headers", lambda kid, pem: {})
    return d, store


def _install_connect(monkeypatch, inbox, sent, connects):
    class Connect:
        def __init__(self, *a, **kw):
            connects.append(1)

        async def __aenter__(self):
            return _WS(inbox, sent)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(streamd.websockets, "connect", Connect)


def _snap(ticker, sid=1, seq=1):
    return json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": sid,
            "seq": seq,
            "msg": {"market_ticker": ticker, "yes_dollars_fp": [["0.4000", "10.00"]]},
        }
    )


def test_a_ticker_set_change_is_an_update_command_never_a_reconnect(tmp_path, monkeypatch):
    d, store = _daemon(tmp_path, monkeypatch)
    inbox, sent, connects = asyncio.Queue(), [], []
    _install_connect(monkeypatch, inbox, sent, connects)
    cov = BookCoverage({"S"})
    d.books = cov
    (sh,) = cov.boot({"S-1"})

    async def drive():
        task = asyncio.create_task(
            d._kalshi_loop("books", lambda: kalshi_ws.subscribe_books(sorted(sh.assigned)), sh)
        )
        await inbox.put(json.dumps({"type": "subscribed", "id": 1, "msg": {"sid": 1}}))
        await inbox.put(_snap("S-1", seq=1))
        await asyncio.sleep(0.1)  # let the loop drain the inbox
        cov.on_lifecycle("S-2", "activated", None, 0.0)
        cov.on_lifecycle("S-1", "determined", None, 0.0)
        await d._apply_books()
        await inbox.put(json.dumps({"type": "ok", "id": 2, "sid": 1, "seq": 2, "msg": {}}))
        await inbox.put(json.dumps({"type": "ok", "id": 3, "sid": 1, "seq": 3, "msg": {}}))
        await inbox.put(_snap("S-2", seq=4))
        await asyncio.sleep(0.1)  # let the loop drain the inbox
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    store.flush()
    assert len(connects) == 1
    cmds = [m for m in sent if m.get("cmd") == "update_subscription"]
    assert [(c["params"]["action"], c["params"]["market_tickers"]) for c in cmds] == [
        ("delete_markets", ["S-1"]),
        ("add_markets", ["S-2"]),
    ]
    assert all(c["params"]["sid"] == 1 for c in cmds)
    with duckdb.connect(str(store.path), read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM stream_gaps").fetchone()[0] == 0
    assert d.stats.get("books_snap_auto") == 1  # S-2's snapshot arrived unasked
    assert "books_snap_requested" not in d.stats


def test_an_added_market_without_a_snapshot_gets_one_requested(tmp_path, monkeypatch):
    d, store = _daemon(tmp_path, monkeypatch)
    inbox, sent, connects = asyncio.Queue(), [], []
    _install_connect(monkeypatch, inbox, sent, connects)
    monkeypatch.setattr(streamd, "SNAPSHOT_WAIT_S", 0.0)
    cov = BookCoverage({"S"})
    d.books = cov
    (sh,) = cov.boot({"S-1"})

    async def drive():
        task = asyncio.create_task(
            d._kalshi_loop("books", lambda: kalshi_ws.subscribe_books(sorted(sh.assigned)), sh)
        )
        await inbox.put(json.dumps({"type": "subscribed", "id": 1, "msg": {"sid": 3}}))
        await asyncio.sleep(0.1)  # let the loop drain the inbox
        cov.on_lifecycle("S-2", "activated", None, 0.0)
        await d._apply_books()
        await inbox.put(json.dumps({"type": "ok", "id": 2, "sid": 3, "seq": 1, "msg": {}}))
        await asyncio.sleep(0.1)  # let the loop drain the inbox
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    actions = [m["params"]["action"] for m in sent if m.get("cmd") == "update_subscription"]
    assert actions == ["add_markets", "get_snapshot"]
    assert d.stats["books_snap_requested"] == 1


def test_a_market_limit_error_rehomes_the_markets_without_reconnecting(tmp_path, monkeypatch):
    d, store = _daemon(tmp_path, monkeypatch)
    inbox, sent, connects = asyncio.Queue(), [], []
    _install_connect(monkeypatch, inbox, sent, connects)
    cov = BookCoverage({"S"}, shard_max=10)
    d.books = cov
    d._book_wake = asyncio.Event()
    (sh,) = cov.boot({"S-1"})

    async def drive():
        task = asyncio.create_task(
            d._kalshi_loop("books", lambda: kalshi_ws.subscribe_books(sorted(sh.assigned)), sh)
        )
        await inbox.put(json.dumps({"type": "subscribed", "id": 1, "msg": {"sid": 1}}))
        await asyncio.sleep(0.1)  # let the loop drain the inbox
        cov.on_lifecycle("S-2", "activated", None, 0.0)
        await d._apply_books()
        await inbox.put(json.dumps({"type": "error", "id": 2, "msg": {"code": 26}}))
        await asyncio.sleep(0.1)  # let the loop drain the inbox
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert len(connects) == 1
    assert cov.shard_max == 1 and sh.assigned == {"S-1"}
    assert cov.adds == {"S-2"}  # queued for a new shard
    assert d._book_wake.is_set()


def test_an_update_ack_that_repeats_the_seq_is_not_a_gap(tmp_path, monkeypatch):
    d, store = _daemon(tmp_path, monkeypatch)
    inbox, sent, connects = asyncio.Queue(), [], []
    _install_connect(monkeypatch, inbox, sent, connects)
    cov = BookCoverage({"S"})
    d.books = cov
    (sh,) = cov.boot({"S-1"})

    async def drive():
        task = asyncio.create_task(
            d._kalshi_loop("books", lambda: kalshi_ws.subscribe_books(sorted(sh.assigned)), sh)
        )
        await inbox.put(json.dumps({"type": "subscribed", "id": 1, "msg": {"sid": 1}}))
        await inbox.put(_snap("S-1", seq=1))
        await inbox.put(json.dumps({"type": "ok", "id": 2, "sid": 1, "seq": 1, "msg": {}}))
        await inbox.put(_snap("S-1", seq=2))
        await asyncio.sleep(0.1)  # let the loop drain the inbox
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert len(connects) == 1
    assert d.stats["kalshi_books_ok_seq_repeat"] == 1


def test_a_later_shard_stores_its_sid_offset_so_shards_never_share_a_seq_stream(
    tmp_path, monkeypatch
):
    d, store = _daemon(tmp_path, monkeypatch)
    cov = BookCoverage({"S"}, shard_max=1)
    d.books = cov
    a, b = cov.boot({"S-1", "S-2"})
    for sh, t in ((a, "S-1"), (b, "S-2")):
        d._store_frame("books", sh, json.loads(_snap(t, sid=1, seq=1)), RECV, _snap(t))
    store.flush()
    with duckdb.connect(str(store.path), read_only=True) as conn:
        rows = conn.execute("SELECT market_id, sid FROM book_events ORDER BY market_id").fetchall()
    assert rows == [("S-1", 1), ("S-2", 1 + SHARD_SID_STRIDE)]


def test_lifecycle_frame_feeds_the_book_set_and_wakes_the_manager(tmp_path, monkeypatch):
    d, store = _daemon(tmp_path, monkeypatch)
    cov = BookCoverage({"KXBTC15M"})
    d.books, d._book_wake = cov, asyncio.Event()
    frame = _lifecycle("KXBTC15M-26OCT231230-30", "activated")
    d._store_frame("lifecycle", None, frame, RECV, json.dumps(frame))
    assert cov.adds == {"KXBTC15M-26OCT231230-30"}
    assert d._book_wake.is_set()


# -- ticker / lifecycle rows ---------------------------------------------------


def _ticker(market, **over):
    msg = {
        "market_ticker": market,
        "market_id": "uuid",
        "price_dollars": "0.4200",
        "yes_bid_dollars": "0.4100",
        "yes_ask_dollars": "0.4300",
        "volume_fp": "1234.00",
        "open_interest_fp": "500.00",
        "dollar_volume": 518,
        "dollar_open_interest": 210,
        "yes_bid_size_fp": "10.00",
        "yes_ask_size_fp": "20.00",
        "last_trade_size_fp": "3.00",
        "ts_ms": 1761220800000,
    }
    msg.update(over)
    return {"type": "ticker", "sid": 5, "msg": msg}


def _lifecycle(market, event_type, seq=1, **over):
    msg = {"market_ticker": market, "event_type": event_type}
    msg.update(over)
    return {"type": "market_lifecycle_v2", "sid": 9, "seq": seq, "msg": msg}


def test_ticker_frame_parses_to_dollars_and_counts():
    row = kalshi_ws.parse_ticker(_ticker("KXBTCD-X"), RECV)
    assert row.market_id == "KXBTCD-X" and row.seq is None and row.sid == 5
    assert (row.price, row.yes_bid, row.yes_ask) == (0.42, 0.41, 0.43)
    assert (row.volume, row.open_interest, row.dollar_volume) == (1234.0, 500.0, 518)
    assert row.src_ts == datetime.fromtimestamp(1761220800, tz=UTC)
    assert kalshi_ws.parse_ticker({"type": "trade", "msg": {}}, RECV) is None


def test_lifecycle_frame_parses_determination_fields_and_keeps_raw():
    frame = _lifecycle(
        "KXHIGHNY-26OCT23-B60.5",
        "determined",
        result="no",
        settlement_value="0.0000",
        determination_ts=1761260000,
    )
    raw = json.dumps(frame)
    row = kalshi_ws.parse_lifecycle(frame, RECV, raw)
    assert (row.msg_type, row.event_type, row.result) == ("market_lifecycle_v2", "determined", "no")
    assert row.settlement_value == 0.0 and row.series_ticker == "KXHIGHNY"
    assert row.determination_ts == datetime.fromtimestamp(1761260000, tz=UTC)
    assert row.raw == raw and row.seq == 1
    ev = kalshi_ws.parse_lifecycle(
        {"type": "event_lifecycle", "sid": 9, "seq": 2, "msg": {"event_ticker": "E1"}}, RECV, "{}"
    )
    assert ev.market_id == "E1" and ev.event_ticker == "E1"
    assert kalshi_ws.parse_lifecycle({"type": "subscribed", "msg": {}}, RECV, "{}") is None


def test_ticker_and_lifecycle_rows_round_trip_through_the_store_with_nulls(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_tickers(
        [
            kalshi_ws.parse_ticker(_ticker("A"), RECV),
            kalshi_ws.parse_ticker(_ticker("B", price_dollars=None, dollar_volume=None), RECV),
        ]
    )
    raw = json.dumps(_lifecycle("A", "created", open_ts=1761220000))
    store.append_lifecycles([kalshi_ws.parse_lifecycle(json.loads(raw), RECV, raw)])
    assert store.flush() == 3
    with duckdb.connect(str(store.path), read_only=True) as conn:
        t = conn.execute(
            "SELECT market_id, price, dollar_volume, recv_ts FROM stream_ticker ORDER BY 1"
        ).fetchall()
        lc = conn.execute(
            "SELECT event_type, open_ts, result, is_deactivated, raw FROM stream_lifecycle"
        ).fetchall()
    assert t[0][:3] == ("A", 0.42, 518) and t[1][:3] == ("B", None, None)
    assert t[0][3] == RECV.replace(tzinfo=None)  # naive UTC, as every table
    assert lc == [("created", datetime(2025, 10, 23, 11, 46, 40), None, None, raw)]


def test_mve_combos_are_excluded_from_ticker_and_lifecycle_unless_opted_in(tmp_path, monkeypatch):
    d, store = _daemon(tmp_path, monkeypatch)
    for ch, frame in (
        ("ticker", _ticker("KXMVECROSSCATEGORY-X")),
        ("ticker", _ticker("KXBTCD-X")),
        ("lifecycle", _lifecycle("KXMVESPORTS-Y", "created")),
    ):
        d._store_frame(ch, None, frame, RECV, json.dumps(frame))
    assert d.stats["kalshi_ticker_mve_dropped"] == 1
    assert d.stats["kalshi_lifecycle_mve_dropped"] == 1
    assert store.pending == 1

    (tmp_path / "b").mkdir()
    d2, store2 = _daemon(tmp_path / "b", monkeypatch, {"kalshi_ticker_include_mve": True})
    frame = _ticker("KXMVECROSSCATEGORY-X")
    d2._store_frame("ticker", None, frame, RECV, json.dumps(frame))
    assert store2.pending == 1


# -- the write path ------------------------------------------------------------


def test_staged_csv_insert_matches_executemany_exactly(tmp_path):
    """The oracle is the path it replaced. NULL vs empty string, quotes,
    commas, newlines, a literal backslash-N, NaN and microseconds."""
    ts = datetime(2026, 10, 23, 1, 2, 3, 456789)
    rows = {
        "book_events": [
            ("kalshi", "M,1", ts, None, 1, 2, "delta", "", 0.1, -3.0),
            ("kalshi", 'M"2', ts, ts, None, None, "void", "x", 0.0, 0.0),
        ],
        "stream_lifecycle": [
            ("kalshi", "M", ts, None, 9, 1, "market_lifecycle_v2", "settled", None, "S")
            + (None, ts, None, ts, "\\N", float("nan"), True, '{"a":\n"b,\\"c"}'),
        ],
    }
    for table, data in rows.items():
        a, b = tmp_path / f"a-{table}.duckdb", tmp_path / f"b-{table}.duckdb"
        for path in (a, b):
            StreamStore(path)  # schema
        with duckdb.connect(str(a)) as conn:
            _insert_frame(conn, table, data, tmp_path)
        with duckdb.connect(str(b)) as conn:
            ph = ",".join("?" * len(_COLS[table]))
            conn.executemany(f"INSERT INTO {table} VALUES ({ph})", data)
        got = [
            duckdb.connect(str(p), read_only=True).execute(f"FROM {table}").fetchall()
            for p in (a, b)
        ]
        assert repr(got[0]) == repr(got[1])  # repr: NaN != NaN
    assert not list(tmp_path.glob(".*.csv"))  # staging files removed


def test_sidecar_round_trips_the_new_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 0)
    store = StreamStore(tmp_path / "s.duckdb")
    raw = json.dumps(_lifecycle("A", "activated"))
    store.append_tickers([kalshi_ws.parse_ticker(_ticker("A"), RECV)])
    store.append_lifecycles([kalshi_ws.parse_lifecycle(json.loads(raw), RECV, raw)])
    store.append_events([BookEvent("kalshi", "A", RECV, None, 1, 1, "snap", "yes", 0.4, 1.0)])
    store.append_trades([StreamTrade("kalshi", "A", RECV, None, 0.4, 1.0, "yes", 1)])
    assert store.spill_all() == 4
    assert store.flush() == 4
    counts = store.counts()
    assert counts["stream_ticker"] == counts["stream_lifecycle"] == 1
    assert isinstance(StreamTicker, type) and isinstance(StreamLifecycle, type)


# -- raw frames ----------------------------------------------------------------


def _read(path):
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes()).decode()
    from compression import zstd

    return zstd.decompress(path.read_bytes()).decode()


@pytest.mark.parametrize("codec", ["default", "gzip"])
def test_raw_frames_rotate_hourly_per_channel_and_decode(tmp_path, codec):
    chosen = _codec() if codec == "default" else ("gz", lambda p: gzip.open(p, "ab"))  # noqa: SIM115
    w = RawFrameWriter(tmp_path, codec=chosen, flush_secs=0.01)
    w.start()
    t1 = datetime(2026, 10, 23, 13, 59, 59, tzinfo=UTC)
    t2 = t1 + timedelta(seconds=2)
    assert w.put("books", "books-1", t1, '{"a":1}')
    assert w.put("books", "books-1", t2, '{"a":\n2}')
    assert w.put("ticker", "ticker", t2, b'{"t":1}')
    assert w.close(5)
    ext = chosen[0]
    f13 = tmp_path / "2026-10-23" / f"13-books.jsonl.{ext}"
    f14 = tmp_path / "2026-10-23" / f"14-books.jsonl.{ext}"
    ft = tmp_path / "2026-10-23" / f"14-ticker.jsonl.{ext}"
    assert _read(f13) == f'{t1.isoformat()}\tbooks-1\t{{"a":1}}\n'
    assert _read(f14) == f'{t2.isoformat()}\tbooks-1\t{{"a": 2}}\n'  # one line per frame
    assert _read(ft).endswith('{"t":1}\n')
    assert w.stats()["raw_written"] == 3 and w.stats()["raw_dropped"] == 0


def test_raw_frames_drop_and_count_past_the_queue_budget(tmp_path):
    w = RawFrameWriter(tmp_path, queue_bytes=10)
    assert w.put("trades", "trades", RECV, "x" * 8)
    assert not w.put("trades", "trades", RECV, "y" * 8)  # never blocks, never raises
    assert w.stats()["raw_dropped"] == 1
    assert w.close()


def test_a_quiet_books_shard_is_probed_before_it_is_declared_dead_air(tmp_path, monkeypatch):
    """A shard of a few illiquid markets can be silent for minutes; a fixed
    dead-air timeout would reconnect it -- and write a gap row -- every few
    minutes all night. It asks for one snapshot first; only silence after
    that is dead air."""
    d, store = _daemon(tmp_path, monkeypatch)
    inbox, sent, connects = asyncio.Queue(), [], []
    _install_connect(monkeypatch, inbox, sent, connects)
    monkeypatch.setattr(streamd, "DEAD_AIR_SECS", 0.0)
    monkeypatch.setattr(streamd, "PROBE_WAIT_S", 0.0)
    cov = BookCoverage({"S"})
    d.books = cov
    (sh,) = cov.boot({"S-1"})
    sh.at_boot = False

    async def drive():
        task = asyncio.create_task(
            d._kalshi_loop("books", lambda: kalshi_ws.subscribe_books(sorted(sh.assigned)), sh)
        )
        await inbox.put(json.dumps({"type": "subscribed", "id": 1, "msg": {"sid": 2}}))
        await asyncio.sleep(1.2)  # one timeout: the probe goes out
        probes = [m for m in sent if m.get("params", {}).get("action") == "get_snapshot"]
        await inbox.put(_snap("S-1", sid=2, seq=1))  # ...and is answered
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return probes

    probes = asyncio.run(drive())
    assert len(probes) == 1 and probes[0]["params"]["market_tickers"] == ["S-1"]
    assert len(connects) == 1
    assert d.stats["books_probes"] == 1
    store.flush()
    with duckdb.connect(str(store.path), read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM stream_gaps").fetchone()[0] == 0


# -- follow-ups: enumeration, pacing, cache, retention ------------------------


def _paged_markets(monkeypatch, n_markets, page_seen=None):
    """Serve `n_markets` open markets for any series through the REAL
    get_markets pagination, honouring the requested page size."""
    from collector.venues import kalshi

    class Resp:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_get(sess, url, params, **kw):
        start = int(params.get("cursor") or 0)
        lim = params["limit"]
        if page_seen is not None:
            page_seen.append(lim)
        rows = [
            {"ticker": f"{params['series_ticker']}-{i}"}
            for i in range(start, min(start + lim, n_markets))
        ]
        nxt = start + lim
        return Resp({"markets": rows, "cursor": str(nxt) if nxt < n_markets else ""})

    monkeypatch.setattr(kalshi, "_get_with_429_retry", fake_get)


def test_open_ticker_walk_is_not_truncated_at_2000(monkeypatch):
    """Staging 2026-10-23: KXNCAAFSPREAD held exactly 2,000 books -- the walk
    took get_markets' default 10 pages x 200 and dropped the rest."""
    pages: list[int] = []
    _paged_markets(monkeypatch, 4_321, pages)
    monkeypatch.setattr(streamd._time, "sleep", lambda s: None)
    out, incomplete = streamd.open_tickers_detail(["KXNCAAFSPREAD"], pause_s=0)
    assert len(out) == 4_321 and incomplete == set()
    assert set(pages) == {streamd.OPEN_TICKERS_PAGE}


def test_a_walk_that_still_truncates_is_reported_incomplete_and_never_removes(monkeypatch):
    monkeypatch.setattr(streamd, "OPEN_TICKERS_MAX_PAGES", 2)
    _paged_markets(monkeypatch, 5_000)
    out, incomplete = streamd.open_tickers_detail(["S"], pause_s=0)
    assert len(out) == 2_000 and incomplete == {"S"}
    cov = BookCoverage({"S", "T"})
    cov.boot({"S-4999", "T-1"})
    cov.reconcile(out, 0.0, incomplete)
    adds, removes = cov.take()
    assert "S-4999" not in removes  # absent from a truncated walk is not closed
    assert removes == {"T-1"}  # complete series: absence IS a close


def test_the_walk_paces_pages_as_well_as_series(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(streamd._time, "sleep", lambda s: sleeps.append(s))
    import time as real_time

    monkeypatch.setattr(real_time, "sleep", lambda s: sleeps.append(s))
    _paged_markets(monkeypatch, 2_500)  # 3 pages per series
    streamd.open_tickers_detail(["A", "B"], pause_s=1.0)
    assert sleeps == [1.0] * 5  # 2 page gaps + 1 series gap + 2 page gaps


def test_a_restart_boots_from_a_fresh_cache_without_a_rest_walk(tmp_path, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(streamd, "open_tickers", lambda s: calls.append(1) or {"S-1"})
    started: list[set] = []

    async def fake_loop(self, channel, make_subscribe, shard=None):
        started.append(set(shard.assigned))

    monkeypatch.setattr(streamd.Daemon, "_kalshi_loop", fake_loop)
    store = StreamStore(tmp_path / "s.duckdb")
    d = streamd.Daemon(store, {"kalshi_book_series": ["S"]})
    d._write_ticker_cache(["S"], {"S-7", "S-8"})
    asyncio.run(d.kalshi_books())
    assert calls == [] and started == [{"S-7", "S-8"}]

    # a cache for another series list, or a stale one, is ignored
    d._write_ticker_cache(["OTHER"], {"X-1"})
    assert d._read_ticker_cache(["S"]) == (set(), 0.0)
    path = d._ticker_cache_path()
    rec = json.loads(path.read_text())
    rec["series"] = ["S"]
    rec["at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    path.write_text(json.dumps(rec))
    assert d._read_ticker_cache(["S"]) == (set(), 0.0)


def test_raw_frame_retention_prunes_only_old_day_directories(tmp_path):
    for name in ("2026-10-01", "2026-10-08", "2026-10-09", "2026-10-23", "notes"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "00-books.jsonl.zst").write_bytes(b"x")
    w = RawFrameWriter(tmp_path, retain_days=14)
    gone = w.prune(datetime(2026, 10, 23, 5, 0, tzinfo=UTC))
    assert gone == ["2026-10-01", "2026-10-08"]
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["2026-10-09", "2026-10-23", "notes"]
    assert w.stats()["raw_pruned_days"] == 2
    assert RawFrameWriter(tmp_path, retain_days=None).prune() == []


def test_the_writer_thread_prunes_at_start(tmp_path):
    (tmp_path / "2020-01-01").mkdir()
    w = RawFrameWriter(tmp_path, retain_days=14)
    w.start()
    assert w.close(5)
    assert not (tmp_path / "2020-01-01").exists()


# -- V2-378: the periodic walk is phase-locked ---------------------------------


def _epoch(hms: str) -> float:
    return datetime.fromisoformat(f"2026-10-23T{hms}+00:00").timestamp()


def test_reconcile_slots_sit_offset_seconds_after_each_quarter_hour():
    off = streamd.RECONCILE_OFFSET_S
    assert streamd.secs_to_next_reconcile(_epoch("12:00:00"), period=900, offset=180) == 180
    assert streamd.secs_to_next_reconcile(_epoch("12:03:00"), period=900, offset=180) == 0
    assert streamd.secs_to_next_reconcile(_epoch("12:03:01"), period=900, offset=180) == 899
    # min_s skips a slot that is too close: 12:02:00 + 120 s is past 12:03:00
    assert (
        streamd.secs_to_next_reconcile(_epoch("12:02:00"), min_s=120, period=900, offset=180) == 960
    )
    # whatever the clock, the slot lands `off` seconds into a quarter-hour,
    # i.e. `off` seconds after one of the :X0/:X5 collector starts
    for k in range(0, 3600, 37):
        now = _epoch("17:08:55") + k
        slot = now + streamd.secs_to_next_reconcile(now, min_s=streamd.BOOT_RECONCILE_MIN_S)
        assert slot - now >= streamd.BOOT_RECONCILE_MIN_S
        assert round(slot) % 900 == off and round(slot) % 300 == off % 300


def test_reconcile_period_is_commensurate_with_the_five_minute_passes():
    # The phase lock only means something if every walk lands at the same
    # offset into the :X0/:X5 cycle -- i.e. the period is a whole number of
    # 5-minute passes and divides the hour.
    assert streamd.RECONCILE_SECS % 300 == 0 and 3600 % streamd.RECONCILE_SECS == 0
    # ...and the default offset leaves walk room before the next pass. The
    # room is NOT the ~90 s the V2-378 note assumed: the walk's floor is one
    # SERIES_PAUSE_S per series (97 series = 97 s), so measured 10-22..10-29
    # over 459 walks it ran p50 105 s / p90 115 s / max 275 s, and 39 (8.5%)
    # ended 0-54 s PAST the :X5 collector tick (one 128 s). Read by the tick
    # (#125) that overrun costs the collector nothing -- 0.41/0.45/0.44
    # 429s per sweep-idle cycle at marks :X0/:X5/:X10, overlapped cycles
    # 0.25 -- so the 120 s room stands on the listing-delay half of the
    # design, not on the collision half (status 2026-10-29 02:15Z).
    assert 150 <= streamd.RECONCILE_OFFSET_S <= 300 - 100


def test_first_periodic_walk_after_a_rest_boot_is_phase_locked(tmp_path, monkeypatch):
    now = _epoch("17:10:50")  # 10-23: the boot walk ended here; old code walked at 17:25:50
    monkeypatch.setattr(streamd._time, "time", lambda: now)
    monkeypatch.setattr(streamd, "open_tickers", lambda s: {"S-1"})

    async def fake_loop(self, channel, make_subscribe, shard=None):
        await asyncio.Event().wait()  # a live shard: never returns

    monkeypatch.setattr(streamd.Daemon, "_kalshi_loop", fake_loop)
    timeouts: list[float] = []

    class Stop(Exception):
        pass

    async def fake_wait(aws, timeout=None, return_when=None):
        timeouts.append(timeout)
        for a in aws:
            if isinstance(a, asyncio.Future) and not a.done():
                a.cancel()
        raise Stop

    monkeypatch.setattr(streamd.asyncio, "wait", fake_wait)
    store = StreamStore(tmp_path / "s.duckdb")
    d = streamd.Daemon(store, {"kalshi_book_series": ["S"]})
    with pytest.raises(Stop):
        asyncio.run(d.kalshi_books())
    # next slot >= 120 s away is 17:18:00 (= quarter-hour 17:15 + 180 s)
    assert timeouts and abs(timeouts[0] - 430.0) < 1.0


def test_reconcile_walk_times_the_walk_and_reads_only_its_own_429_delta(monkeypatch):
    """Status 2026-10-24 03:55Z: a walk's duration was inferred from its 429
    lines, so a clean walk had none. The line carries the elapsed seconds,
    both wall-clock ends and the kalshi ledger's delta across the walk --
    the delta, not the counter, so retries spent before the walk (the boot
    walk's, say) are not charged to it."""
    from collector.venues import kalshi

    kalshi.reset_retry_counts()
    kalshi._RATE_LIMIT_RETRIES = 7  # spent before this walk
    kalshi._RATE_LIMIT_UNRETRIED = 2
    clock = [100.0]
    monkeypatch.setattr(streamd._time, "monotonic", lambda: clock[0])

    def fake_walk(series_list, pause_s):
        kalshi._RATE_LIMIT_RETRIES += 3
        kalshi._RATE_LIMIT_UNRETRIED += 1
        clock[0] += 47.25
        return {"A-1", "A-2", "B-1"}, {"C"}

    monkeypatch.setattr(streamd, "open_tickers_detail", fake_walk)
    try:
        w = streamd.reconcile_walk(["A", "B", "C"])
    finally:
        kalshi.reset_retry_counts()
    assert w.tickers == {"A-1", "A-2", "B-1"} and w.incomplete == {"C"}
    assert w.series_n == 3
    assert abs(w.elapsed_s - 47.25) < 1e-9
    assert (w.rate_limit_d, w.unretried_d) == (3, 1)
    assert w.started.tzinfo is UTC and w.ended >= w.started
    line = streamd.walk_line(w)
    assert "reconcile walk 47.2s" in line or "reconcile walk 47.3s" in line
    assert "3 tickers across 3 series, 429 retried +3 unretried +1, incomplete 1" in line


def test_walk_line_renders_every_field_of_the_walk():
    """Mistakes #95: a renderer is complete only by perturbation -- bump each
    field of a real result and the line must change."""
    import dataclasses

    base = streamd.WalkResult(
        tickers={"A-1", "A-2"},
        incomplete=set(),
        series_n=2,
        started=datetime(2026, 10, 24, 3, 48, 0, tzinfo=UTC),
        ended=datetime(2026, 10, 24, 3, 49, 47, tzinfo=UTC),
        elapsed_s=107.0,
        rate_limit_d=4,
        unretried_d=0,
    )
    line = streamd.walk_line(base)
    assert line == (
        "kalshi-books: reconcile walk 107.0s (03:48:00 -> 03:49:47),"
        " 2 tickers across 2 series, 429 retried +4 unretried +0, incomplete 0"
    )
    bumps = {
        "tickers": {"A-1"},
        "incomplete": {"B"},
        "series_n": 3,
        "started": base.started + timedelta(seconds=1),
        "ended": base.ended + timedelta(seconds=1),
        "elapsed_s": 108.0,
        "rate_limit_d": 5,
        "unretried_d": 1,
    }
    assert set(bumps) == {f.name for f in dataclasses.fields(base)}
    for name, value in bumps.items():
        assert streamd.walk_line(dataclasses.replace(base, **{name: value})) != line, name


# -- the lifecycle-announced queue survives a restart (2026-10-29) ------------


def _created(store, market, open_at: datetime, recv: datetime, seq=1, event="created", **over):
    raw = json.dumps(_lifecycle(market, event, seq=seq, open_ts=int(open_at.timestamp()), **over))
    store.append_lifecycles([kalshi_ws.parse_lifecycle(json.loads(raw), recv, raw)])


def test_announced_opens_reads_created_frames_not_yet_closed(tmp_path):
    """`created` frames for the 15-minute series arrive 24-30h ahead in one
    daily batch; the archive keeps them, the daemon's queue did not. The
    read returns every announced market the lookback holds that no later
    frame closed -- opened or not -- as aware UTC."""
    store = StreamStore(tmp_path / "s.duckdb")
    assert StreamStore(tmp_path / "absent.duckdb").announced_opens() == {}
    now = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)
    recv = now - timedelta(hours=30)
    _created(store, "KXBTC15M-FUTURE", now + timedelta(hours=1), recv)
    _created(store, "KXBTC15M-OPEN", now - timedelta(minutes=3), recv, seq=2)
    _created(store, "KXBTC15M-SETTLED", now + timedelta(hours=2), recv, seq=3)
    raw = json.dumps(_lifecycle("KXBTC15M-SETTLED", "settled", seq=4))
    store.append_lifecycles(
        [kalshi_ws.parse_lifecycle(json.loads(raw), recv + timedelta(hours=1), raw)]
    )
    _created(store, "KXBTC15M-OLD", now + timedelta(hours=3), now - timedelta(days=8), seq=5)
    assert store.flush() == 5
    got = store.announced_opens(now)
    assert got == {
        "KXBTC15M-FUTURE": now + timedelta(hours=1),
        "KXBTC15M-OPEN": now - timedelta(minutes=3),
    }
    assert all(ts.tzinfo is UTC for ts in got.values())


def test_seed_announced_restores_the_pending_queue_and_queues_overdue_opens():
    cov = BookCoverage({"KXBTC15M"})
    for sh in cov.boot({"KXBTC15M-HELD"}):
        pass
    now = datetime.now(UTC)  # on_lifecycle reads the real clock for `created`
    live_ts = now + timedelta(minutes=5)
    assert cov.on_lifecycle("KXBTC15M-LIVE", "created", live_ts, 0.0)  # the channel got there first
    cov.on_lifecycle("KXBTC15M-CLOSED", "determined", None, 0.0)
    cov.take()
    opens = {
        "KXBTC15M-FUTURE": now + timedelta(hours=1),
        "KXBTC15M-LIVE": now + timedelta(hours=9),  # archive says later; the live frame wins
        "KXBTC15M-OVERDUE": now - timedelta(minutes=3),
        "KXBTC15M-HELD": now - timedelta(minutes=10),
        "KXBTC15M-CLOSED": now + timedelta(hours=1),
        "KXETH15M-OTHER": now + timedelta(hours=1),
    }
    assert cov.seed_announced(opens, now) == (2, 1)
    assert cov.pending_open == {
        "KXBTC15M-FUTURE": now + timedelta(hours=1),
        "KXBTC15M-LIVE": live_ts,
    }
    assert cov.take() == ({"KXBTC15M-OVERDUE"}, set())
    cov.due_opens(now + timedelta(hours=1, seconds=1))  # both waits have come due
    assert cov.take() == ({"KXBTC15M-FUTURE", "KXBTC15M-LIVE"}, set())


def test_a_restart_restores_announced_opens_from_the_archive(tmp_path, monkeypatch, capsys):
    """End to end through the daemon's book-set boot: the queue the previous
    process built from `created` frames comes back from the archive, and a
    market that opened while the daemon was down is queued at once instead
    of at the next walk (~4m45s after the quarter-hour, measured 10-29)."""
    monkeypatch.setattr(streamd, "open_tickers", lambda s: {"S-1"})

    async def fake_loop(self, channel, make_subscribe, shard=None):
        return None

    monkeypatch.setattr(streamd.Daemon, "_kalshi_loop", fake_loop)
    store = StreamStore(tmp_path / "s.duckdb")
    now = datetime.now(UTC)
    _created(store, "S-FUTURE", now + timedelta(hours=2), now - timedelta(hours=20))
    _created(store, "S-DOWNTIME", now - timedelta(minutes=2), now - timedelta(hours=20), seq=2)
    _created(store, "S-7", now - timedelta(hours=5), now - timedelta(hours=20), seq=3)  # held
    store.flush()
    d = streamd.Daemon(store, {"kalshi_book_series": ["S"]})
    d._write_ticker_cache(["S"], {"S-7", "S-8"})
    asyncio.run(d.kalshi_books())
    cov = d.books
    assert set(cov.pending_open) == {"S-FUTURE"}
    assert cov.adds == {"S-DOWNTIME"}  # queued at boot, not left to the walk
    out = capsys.readouterr().out
    assert "1 lifecycle-announced opens restored from the archive (3 announced" in out
    assert "1 already open and queued" in out


def test_announced_opens_reads_while_the_owner_holds_a_writer_connection(tmp_path):
    """The daemon's flusher may be mid-write when the book set boots, and
    DuckDB refuses a same-process attach whose configuration differs from
    the open one. The first production boot asked read-only and got
    `different configuration than existing connections` (2026-10-29
    14:59:39Z) -- the walk backstop ran and the restore did nothing."""
    from hyxlab.store import duck_connect

    store = StreamStore(tmp_path / "s.duckdb")
    now = datetime.now(UTC)
    opens_at = now + timedelta(hours=2)
    _created(store, "S-FUTURE", opens_at, now - timedelta(hours=20))
    store.flush()
    with duck_connect(str(store.path)) as writer:  # the flusher's configuration
        writer.execute("SELECT 1")
        got = store.announced_opens(now)
    assert set(got) == {"S-FUTURE"} and abs((got["S-FUTURE"] - opens_at).total_seconds()) < 1
