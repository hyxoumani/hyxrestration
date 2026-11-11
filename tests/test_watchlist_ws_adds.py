"""The checked-in watchlist as the daemon reads it (hylshi EXP-2070 phase 1).

Adds to `kalshi_book_series` are books only: they must reach the book walk and shard plan, must not reach
the REST collector's or the shadow's `kalshi_series`, and must not change the keys the daemon sizes itself by.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math

import duckdb

from collector import streamd
from collector.bookcov import BookCoverage
from collector.venues import kalshi_ws
from hyxlab.streamstore import StreamStore
from hyxlab.watchlist import load_watchlist

PHASE1 = {
    "KXBALLONDOR",
    "KXFEDDECISION",
    "KXUECLGAME",
    "KXUEFANLGAME",
    "KXUELGAME",
    "KXMLB",
    "KXNFL1H",
    "KXNFLTOTAL",
    "KXNFL1QSPREAD",
    "KXNFL1HSPREAD",
    "KXLALIGABTTS",
    "KXNFLTD",
    "KXNFLPASSTDS",
    "KXNFLPASSYDS",
}
#: Open markets per series at the 2026-11-04 census, the plan's input.
PHASE1_OPEN = {
    "KXBALLONDOR": 80,
    "KXFEDDECISION": 50,
    "KXUECLGAME": 54,
    "KXUEFANLGAME": 156,
    "KXUELGAME": 54,
    "KXMLB": 30,
    "KXNFL1H": 42,
    "KXNFLTOTAL": 285,
    "KXNFL1QSPREAD": 141,
    "KXNFL1HSPREAD": 225,
    "KXLALIGABTTS": 10,
    "KXNFLTD": 286,
    "KXNFLPASSTDS": 64,
    "KXNFLPASSYDS": 137,
}


def test_phase1_is_books_only_and_the_lists_stay_clean():
    w = load_watchlist()
    book, polled = w["kalshi_book_series"], w["kalshi_series"]
    assert set(book) >= PHASE1
    assert not PHASE1 & set(polled), (
        "a series in kalshi_series would start the REST collector polling it"
    )
    assert len(book) == len(set(book))
    assert not set(book) & set(polled)
    assert all(s.isalnum() and s == s.upper() for s in book)


def test_a_phase1_series_reaches_the_daemons_book_universe(tmp_path):
    w = load_watchlist()
    d = streamd.Daemon(StreamStore(tmp_path / "s.duckdb"), w)
    assert set(d._book_series()) >= PHASE1
    assert len(d._book_series()) == len(set(w["kalshi_series"]) | set(w["kalshi_book_series"]))


def test_the_shard_plan_holds_phase1_at_the_configured_cap():
    w = load_watchlist()
    cap = w["kalshi_book_shard_max"]
    cov = BookCoverage(set(w["kalshi_series"]) | set(w["kalshi_book_series"]), cap)
    base = 7349  # the live walk's open tickers, 2026-11-05T20:04Z
    tickers = {f"KXHIGHNY-X{i}" for i in range(base)}
    tickers |= {f"{s}-X{i}" for s, n in PHASE1_OPEN.items() for i in range(n)}
    shards = cov.boot(tickers)
    assert all(len(sh.assigned) <= cap for sh in shards)
    assert sum(len(sh.assigned) for sh in shards) == len(tickers)
    assert len(shards) == math.ceil(len(tickers) / cap)


class _WS:
    def __init__(self, inbox: asyncio.Queue, sent: list) -> None:
        self.inbox, self.sent = inbox, sent

    async def send(self, msg: str) -> None:
        self.sent.append(json.loads(msg))

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item


def test_a_phase1_market_is_subscribed_and_its_frames_reach_the_store(tmp_path, monkeypatch):
    """The real loop over a fake socket: nothing here touches the network or the live store."""
    store = StreamStore(tmp_path / "s.duckdb")
    d = streamd.Daemon(store, load_watchlist())
    d.key_id, d.pem = "k", b"p"
    monkeypatch.setattr(streamd.kalshi_ws, "auth_headers", lambda kid, pem: {})
    inbox: asyncio.Queue = asyncio.Queue()
    sent: list = []

    class Connect:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return _WS(inbox, sent)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(streamd.websockets, "connect", Connect)
    ticker = "KXNFLTOTAL-26NOV08BUFKC-47"
    cov = BookCoverage(set(d._book_series()))
    d.books = cov
    (sh,) = cov.boot({ticker})

    async def drive():
        task = asyncio.create_task(
            d._kalshi_loop("books", lambda: kalshi_ws.subscribe_books(sorted(sh.assigned)), sh)
        )
        await inbox.put(json.dumps({"type": "subscribed", "id": 1, "msg": {"sid": 1}}))
        await inbox.put(
            json.dumps(
                {
                    "type": "orderbook_snapshot",
                    "sid": 1,
                    "seq": 1,
                    "msg": {"market_ticker": ticker, "yes_dollars_fp": [["0.4000", "10.00"]]},
                }
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert any(ticker in json.dumps(m) for m in sent)
    store.flush()
    with duckdb.connect(str(store.path), read_only=True) as conn:
        rows = conn.execute("SELECT market_id, kind FROM book_events").fetchall()
    assert rows == [(ticker, "snap")]
    assert store.path.parent == tmp_path
