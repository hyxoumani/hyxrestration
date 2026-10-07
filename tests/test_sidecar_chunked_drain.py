"""The sidecar drain is bounded in memory by a chunk, not by the wedge.

Measured 2026-10-07 (mistakes #112): `hyxlab-qa` held `hyxstream.duckdb`
for 3750s, streamd spilled 1.69M rows to the sidecar, and the first good
flush parsed ALL of it into objects, concatenated it with the 400k buffer
and built one `executemany` list -- ~750 B/row resident (measured on the
box: 279 MB peak at 200k rows, 730 MB at 800k, 116 MB base), so ~1.6 GB
on top of a live daemon under a 2G cgroup. The unit was OOM-killed, and
because the sidecar survives a restart BY DESIGN, every boot re-ran the
same fatal drain: 189 kills in 1h53m, ~29s apart, while the stream
archive captured nothing. A recovery path whose cost grows with the
outage it recovers from turns every long wedge into a longer one.

So the sidecar drains in transactions of at most `DRAIN_CHUNK_ROWS`, and
the byte offset of the last committed chunk is checkpointed beside it --
a kill mid-drain resumes there instead of re-inserting what committed.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import duckdb

from hyxlab.streamstore import StreamStore, StreamTrade

RECV = datetime(2026, 10, 7, 11, 0, tzinfo=UTC)


def _trade(seq: int) -> StreamTrade:
    return StreamTrade("kalshi", "M1", RECV, RECV, 0.4, 1.0, "yes", seq)


def _wedge(monkeypatch):
    import hyxlab.store as ss

    def locked(*args, **kwargs):
        raise duckdb.IOException("Could not set lock on file")

    monkeypatch.setattr(ss.duckdb, "connect", locked)


def _spilled_store(tmp_path, monkeypatch, n: int) -> StreamStore:
    """A store whose `n` rows all sit in the sidecar, as after a wedge."""
    store = StreamStore(tmp_path / "s.duckdb")
    monkeypatch.setattr(StreamStore, "SPILL_CAP", 0)
    store.append_trades([_trade(i) for i in range(n)])
    _wedge(monkeypatch)
    with contextlib.suppress(duckdb.IOException):
        store.flush()
    monkeypatch.undo()
    assert store.pending == 0 and store._spill_path.exists()
    return store


def _seqs(tmp_path) -> list[int]:
    with duckdb.connect(str(tmp_path / "s.duckdb"), read_only=True) as conn:
        return [r[0] for r in conn.execute("SELECT seq FROM stream_trades ORDER BY rowid").fetchall()]


def test_drain_never_parses_or_inserts_more_than_one_chunk(tmp_path, monkeypatch):
    store = _spilled_store(tmp_path, monkeypatch, 10)
    monkeypatch.setattr(StreamStore, "DRAIN_CHUNK_ROWS", 3)
    store.append_trades([_trade(10), _trade(11)])

    parsed, inserted = [], []
    real_read, real_insert = StreamStore._read_spill, StreamStore._insert

    def spy_read(self, *a, **kw):
        out = real_read(self, *a, **kw)
        parsed.append(sum(len(x) for x in out[:3]))
        return out

    def spy_insert(self, conn, events, trades, gaps):
        inserted.append(len(events) + len(trades) + len(gaps))
        return real_insert(self, conn, events, trades, gaps)

    monkeypatch.setattr(StreamStore, "_read_spill", spy_read)
    monkeypatch.setattr(StreamStore, "_insert", spy_insert)

    assert store.flush() == 12
    assert max(parsed) <= 3  # the parse is bounded, not just the insert
    assert inserted[:-1] == [3, 3, 3, 1]  # sidecar chunks, oldest first
    assert inserted[-1] == 2  # then the live buffer
    assert _seqs(tmp_path) == list(range(12))  # recv order intact, zero loss
    assert not store._spill_path.exists()
    assert not store._spill_done_path.exists()


def test_a_drain_that_dies_mid_way_resumes_without_reinserting(tmp_path, monkeypatch):
    """Chunks that committed stay committed; the next flush starts after them."""
    store = _spilled_store(tmp_path, monkeypatch, 10)
    monkeypatch.setattr(StreamStore, "DRAIN_CHUNK_ROWS", 4)
    store.append_trades([_trade(10)])

    real_insert = StreamStore._insert
    calls = []

    def dies_on_second(self, conn, *rows):
        calls.append(1)
        if len(calls) == 2:
            raise duckdb.IOException("disk went away mid-drain")
        return real_insert(self, conn, *rows)

    monkeypatch.setattr(StreamStore, "_insert", dies_on_second)
    with contextlib.suppress(duckdb.IOException):
        store.flush()
    assert _seqs(tmp_path) == [0, 1, 2, 3]  # chunk one committed
    assert store.pending == 1  # live row restored, not dropped

    monkeypatch.setattr(StreamStore, "_insert", real_insert)
    fresh = StreamStore(tmp_path / "s.duckdb")  # also survives a restart
    fresh.append_trades(store._trades)
    assert fresh.flush() == 7  # 6 sidecar rows left + the live one
    assert _seqs(tmp_path) == list(range(11))  # no duplicates, no holes


def test_a_stale_checkpoint_cannot_skip_a_new_sidecar(tmp_path, monkeypatch):
    """A checkpoint left by an earlier sidecar must never be applied to a new
    one -- that would be a silent hole, the one outcome the store refuses."""
    store = StreamStore(tmp_path / "s.duckdb")
    store._spill_done_path.write_text("999999")  # orphaned: no sidecar exists
    store = _spilled_store(tmp_path, monkeypatch, 5)
    assert store.flush() == 5
    assert _seqs(tmp_path) == list(range(5))


def test_the_drain_estimate_counts_only_what_is_left(tmp_path, monkeypatch):
    """The shutdown drain budgets on this; a resumed sidecar is mostly done."""
    store = _spilled_store(tmp_path, monkeypatch, 10)
    size = store._spill_path.stat().st_size
    full = store.drain_rows_estimate()
    assert full == size // StreamStore.SPILL_BYTES_PER_ROW
    store._spill_done_path.write_text(str(size // 2))
    assert store.drain_rows_estimate() == (size - size // 2) // StreamStore.SPILL_BYTES_PER_ROW
