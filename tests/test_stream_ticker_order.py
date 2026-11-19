"""A flush stores its ticker rows grouped by market (2026-11-19).

`stream_ticker` is the archive's most expensive table per row (~49 B
against ~14 for a book event): each row repeats its market's last price,
quote and volume, and in arrival order ~90k markets interleave so no column
runs. Grouped by market inside the flush -- arrival order kept per market
-- two real 40-minute slices rebuilt in production-shaped 15 s flushes fell
49.9 -> 44.4 B/row at 14k rows a flush and 51.8 -> 41.1 at 33k.

What must NOT change: the set of rows, the order of any one market's rows,
and what an `ORDER BY recv_ts` reader sees -- on the direct path and on the
sidecar's.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from hyxlab.streamstore import StreamStore, StreamTicker, _ticker_order

T0 = datetime(2026, 11, 19, 2, 0, 0)
MARKETS = ("KXC-3", "KXA-1", "KXB-2", "KXA-0")


def _interleaved(n: int = 200) -> list[StreamTicker]:
    out = []
    for i in range(n):
        out.append(
            StreamTicker(
                venue="kalshi",
                market_id=MARKETS[i % len(MARKETS)],
                recv_ts=T0 + timedelta(microseconds=i),
                src_ts=None,
                sid=7,
                seq=i,
                price=0.5,
                yes_bid=0.49,
                yes_ask=0.51,
                yes_bid_size=1.0,
                yes_ask_size=1.0,
                last_trade_size=None,
                volume=float(i),
                open_interest=None,
                dollar_volume=None,
                dollar_open_interest=None,
            )
        )
    return out


def _stored(path, order: str) -> list[tuple]:
    with duckdb.connect(str(path), read_only=True) as conn:
        return conn.execute(
            f"SELECT market_id, seq, recv_ts, volume FROM stream_ticker ORDER BY {order}"
        ).fetchall()


def _assert_grouped(physical: list[tuple], rows: list[StreamTicker]) -> None:
    markets = [r[0] for r in physical]
    runs = [m for i, m in enumerate(markets) if i == 0 or markets[i - 1] != m]
    assert len(runs) == len(set(markets)) == len(MARKETS)
    for m in MARKETS:
        got = [r[3] for r in physical if r[0] == m]
        assert got == [float(i) for i, t in enumerate(rows) if t.market_id == m]


def test_a_flush_groups_ticker_rows_by_market_in_arrival_order(tmp_path):
    rows = _interleaved()
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_tickers(list(rows))
    assert store.flush() == len(rows)
    # Vacuity guard: the input really alternated markets row by row.
    assert [t.market_id for t in rows][:4] == list(MARKETS)
    _assert_grouped(_stored(tmp_path / "s.duckdb", "rowid"), rows)


def test_the_sidecar_drain_groups_them_too(tmp_path):
    rows = _interleaved()
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_tickers(list(rows))
    assert store.spill_all() == len(rows)
    assert store.pending == 0
    assert store.flush() == len(rows)
    _assert_grouped(_stored(tmp_path / "s.duckdb", "rowid"), rows)


def test_an_ordered_reader_sees_exactly_the_arrival_sequence(tmp_path):
    rows = _interleaved()
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_tickers(list(rows))
    store.flush()
    got = _stored(tmp_path / "s.duckdb", "recv_ts")
    assert got == [(t.market_id, t.seq, t.recv_ts, t.volume) for t in rows]


def test_ticker_order_is_stable_and_leaves_the_callers_list_alone():
    rows = _interleaved(40)
    before = list(rows)
    out = _ticker_order(rows)
    assert rows == before  # the buffer is restored from this list on failure
    assert sorted(map(id, out)) == sorted(map(id, rows))
    # Two rows of one market that share every sortable field keep their
    # arrival order: the key is the market alone.
    a, b = rows[0], rows[4]
    assert a.market_id == b.market_id
    b.seq, b.recv_ts = a.seq, a.recv_ts
    out = _ticker_order(rows)
    assert out.index(a) < out.index(b)
