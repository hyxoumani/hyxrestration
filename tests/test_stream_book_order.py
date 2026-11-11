"""A flush stores its book events grouped by connection (mistakes #178).

`book_events` arrives interleaved across the books shards, and in that order
the `sid` column is run-length noise, `seq` is a sawtooth and `market_id`
alternates on every row. Grouped by `sid` -- arrival order kept inside each
group -- one DuckDB row group holds a few long runs instead: measured on a
real 10-minute slice (2,870,280 rows, 2026-11-11 13:00Z) 19.6-19.9 B/row
falls to 13.1-14.0, -29% to -34% across flush sizes of 72k-180k rows.

What must NOT change: the set of rows, the order of any one connection's
rows, and what an `ORDER BY recv_ts, seq` reader sees.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from hyxlab.streamstore import BookEvent, StreamStore, _book_order

T0 = datetime(2026, 11, 11, 13, 0, 0)


def _interleaved(n: int = 240) -> list[BookEvent]:
    """Three kalshi connections and polymarket (no sid), round-robin."""
    sids = (3_000_001, None, 1_000_001, 2_000_001)
    seqs: dict[int | None, int] = {}
    out = []
    for i in range(n):
        sid = sids[i % len(sids)]
        seqs[sid] = seqs.get(sid, 100) + 1
        out.append(
            BookEvent(
                venue="polymarket" if sid is None else "kalshi",
                market_id=f"M{i % 7}",
                recv_ts=T0 + timedelta(microseconds=i),
                src_ts=None,
                sid=sid,
                seq=None if sid is None else seqs[sid],
                kind="delta",
                side="yes",
                price=0.5,
                qty=float(i),
            )
        )
    return out


def _stored(path, order: str) -> list[tuple]:
    with duckdb.connect(str(path), read_only=True) as conn:
        return conn.execute(
            f"SELECT sid, seq, recv_ts, qty FROM book_events ORDER BY {order}"
        ).fetchall()


def test_a_flush_groups_book_events_by_connection_in_arrival_order(tmp_path):
    events = _interleaved()
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_events(list(events))
    assert store.flush() == len(events)

    physical = _stored(tmp_path / "s.duckdb", "rowid")
    sids = [r[0] for r in physical]
    # Vacuity guard: the input really was interleaved.
    assert [e.sid for e in events][:4] == [3_000_001, None, 1_000_001, 2_000_001]
    # Each connection is one contiguous run.
    runs = [s for i, s in enumerate(sids) if i == 0 or sids[i - 1] != s]
    assert len(runs) == len(set(sids)) == 4
    # Inside a run the rows are in ARRIVAL order (qty is the arrival index),
    # including the sid-less venue, which has no seq to sort by.
    for sid in set(sids):
        got = [r[3] for r in physical if r[0] == sid]
        assert got == sorted(got)
        assert got == [float(i) for i, e in enumerate(events) if e.sid == sid]


def test_an_ordered_reader_sees_exactly_the_arrival_sequence(tmp_path):
    events = _interleaved()
    store = StreamStore(tmp_path / "s.duckdb")
    store.append_events(list(events))
    store.flush()
    got = _stored(tmp_path / "s.duckdb", "recv_ts, seq")
    assert got == [(e.sid, e.seq, e.recv_ts, e.qty) for e in events]


def test_book_order_is_stable_and_leaves_the_callers_list_alone():
    events = _interleaved(40)
    before = list(events)
    out = _book_order(events)
    assert events == before  # the buffer is restored from this list on failure
    assert sorted(map(id, out)) == sorted(map(id, events))
    # Same-connection rows that share a seq (one snapshot frame's levels)
    # keep their arrival order: the key is the connection alone.
    a, b = events[0], events[4]
    assert a.sid == b.sid
    b.seq = a.seq
    out = _book_order(events)
    assert out.index(a) < out.index(b)
