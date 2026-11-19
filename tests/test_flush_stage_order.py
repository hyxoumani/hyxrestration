"""The flush stages its CSV files BEFORE it opens the archive, and yields the
GIL while it does.

Until 2026-11-19 the ~1.1 s of pure Python that turns 300k buffered events
into CSV ran INSIDE the hold on `hyxstream.duckdb` -- every reader's wait --
and as one unbroken GIL hold: the receive loop ran at 5-10% of its rate for
that second, which was the whole `recv_ts - src_ts` lag tail (99.3% of the
02Z hour's lagged prints in one-second bursts 17-18 s apart).

Three things are pinned here:

  * the archive is NOT held while a healthy flush stages (proved from a
    second process, with the arm that shows the probe can be refused);
  * a flush that FAILED puts the next one back on the old order -- open
    first -- so an outside hold costs one wasted staging, not one per tick
    of a buffer that grows for as long as the hold lasts;
  * no staged file outlives the flush that wrote it, on any path.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from hyxlab import streamstore
from hyxlab.store import connect_retry
from hyxlab.streamstore import BookEvent, StreamStore, StreamTrade

REPO = Path(__file__).resolve().parents[1]
RECV = datetime(2026, 11, 19, 2, 0, tzinfo=UTC)

#: Exits 0 when it could open the archive read-write, 3 when DuckDB refused.
PROBE = (
    "import sys, duckdb\n"
    "try:\n"
    "    duckdb.connect(sys.argv[1]).close()\n"
    "except duckdb.IOException:\n"
    "    sys.exit(3)\n"
)


def _outside_can_write(db: Path) -> bool:
    r = subprocess.run([sys.executable, "-c", PROBE, str(db)], capture_output=True, text=True)
    assert r.returncode in (0, 3), r.stderr[-400:]
    return r.returncode == 0


def _fill(store: StreamStore, n: int, base: int = 0) -> None:
    store.append_events(
        [
            BookEvent(
                "kalshi",
                f"M{i % 7}",
                RECV + timedelta(microseconds=base + i),
                RECV,
                i % 3,
                base + i,
                "delta",
                "yes",
                0.4,
                1.0,
            )
            for i in range(n)
        ]
    )
    store.append_trades(
        [StreamTrade("kalshi", "M0", RECV, RECV, 0.4, 1.0, "yes", base + i) for i in range(n)]
    )


def _seqs(db: Path, table: str) -> list[int]:
    with connect_retry(str(db), read_only=True) as conn:
        return [r[0] for r in conn.execute(f"SELECT seq FROM {table} ORDER BY seq").fetchall()]


def _staged_files(db: Path) -> list[str]:
    return sorted(p.name for p in db.parent.glob(".*.csv"))


def _order_spies(monkeypatch) -> list[str]:
    """Record every staging and every archive open the store makes."""
    calls: list[str] = []
    real_stage, real_open = streamstore._stage_frame, streamstore.duck_connect

    def stage(table, items, *a, **kw):
        if len(items):
            calls.append("stage")
        return real_stage(table, items, *a, **kw)

    def opened(*a, **kw):
        calls.append("open")
        return real_open(*a, **kw)

    monkeypatch.setattr(streamstore, "_stage_frame", stage)
    monkeypatch.setattr(streamstore, "duck_connect", opened)
    return calls


def test_the_archive_is_not_held_while_a_healthy_flush_stages(tmp_path, monkeypatch):
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    _fill(store, 50)
    inside, go = threading.Event(), threading.Event()
    real_stage = streamstore._stage_frame

    def blocking_stage(table, items, *a, **kw):
        if len(items):
            inside.set()
            assert go.wait(30), "staging was never released"
        return real_stage(table, items, *a, **kw)

    monkeypatch.setattr(streamstore, "_stage_frame", blocking_stage)
    t = threading.Thread(target=store.flush)
    t.start()
    try:
        assert inside.wait(10)
        assert _outside_can_write(db)
    finally:
        go.set()
        t.join(30)
    assert _seqs(db, "book_events") == list(range(50))


def test_the_outside_probe_is_refused_while_the_flush_is_inside_its_hold(tmp_path):
    """Vacuity guard for the test above: the same probe, the same store, the
    flush parked inside the transaction instead of inside the staging."""
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    _fill(store, 50)
    inside, go = threading.Event(), threading.Event()
    real = store._insert

    def hooked(*a, **kw):
        inside.set()
        assert go.wait(30), "flush was never released"
        return real(*a, **kw)

    store._insert = hooked
    t = threading.Thread(target=store.flush)
    t.start()
    try:
        assert inside.wait(10)
        assert not _outside_can_write(db)
    finally:
        go.set()
        t.join(30)


def test_a_healthy_flush_stages_every_table_before_it_opens(tmp_path, monkeypatch):
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 20)
    calls = _order_spies(monkeypatch)
    store.flush()
    assert calls == ["stage", "stage", "open"]


def test_after_a_refused_flush_the_next_ones_open_first_until_one_lands(tmp_path, monkeypatch):
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    _fill(store, 20)
    calls = _order_spies(monkeypatch)
    spied_open = streamstore.duck_connect
    refuse = [True]

    def maybe_refused(*a, **kw):
        if refuse[0]:
            calls.append("refused")
            raise duckdb.IOException("Could not set lock on file (simulated outside hold)")
        return spied_open(*a, **kw)

    monkeypatch.setattr(streamstore, "duck_connect", maybe_refused)

    with pytest.raises(duckdb.IOException):
        store.flush()
    assert calls == ["stage", "stage", "refused"]  # the one wasted staging
    assert store.pending == 40 and _staged_files(db) == []

    calls.clear()
    _fill(store, 20, base=20)  # the buffer grows under the hold
    with pytest.raises(duckdb.IOException):
        store.flush()
    assert calls == ["refused"]  # ...and is NOT staged again
    assert store.pending == 80

    calls.clear()
    refuse[0] = False
    assert store.flush() == 80
    assert calls == ["open", "stage", "stage"]  # the old order, inside the hold
    assert _seqs(db, "book_events") == list(range(40))
    assert _seqs(db, "stream_trades") == list(range(40))
    assert _staged_files(db) == []

    calls.clear()
    _fill(store, 20, base=40)
    store.flush()
    assert calls == ["stage", "stage", "open"]  # one landed: staging is outside again


def test_staging_outside_the_hold_yields_once_a_chunk_and_inside_it_never(tmp_path, monkeypatch):
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    monkeypatch.setattr(streamstore, "STAGE_CHUNK_ROWS", 8)
    sleeps: list[float] = []
    monkeypatch.setattr(streamstore.time, "sleep", sleeps.append)

    _fill(store, 20)  # 20 events + 20 trades at 8 rows a chunk: 3 + 3 yields
    store.flush()
    assert sleeps == [streamstore.STAGE_YIELD_S] * 6
    assert _seqs(db, "book_events") == list(range(20))  # no row lost at a chunk edge
    assert _seqs(db, "stream_trades") == list(range(20))

    sleeps.clear()
    store._stage_in_hold = True
    _fill(store, 20, base=20)
    store.flush()
    assert sleeps == []


def test_no_staged_file_survives_a_flush_whose_insert_failed(tmp_path, monkeypatch):
    db = tmp_path / "s.duckdb"
    store = StreamStore(db)
    _fill(store, 20)
    seen: list[list[str]] = []

    def failing_load(conn, staged):
        seen.append(_staged_files(db))
        raise duckdb.IOException("disk went away mid-insert")

    monkeypatch.setattr(streamstore, "_load_staged", failing_load)
    with pytest.raises(duckdb.IOException):
        store.flush()
    assert len(seen[0]) == 2  # vacuity: the files did exist when the load ran
    assert _staged_files(db) == []
    assert store.pending == 40
