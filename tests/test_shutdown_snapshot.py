"""The drain's REFUSED branch no longer loses the buffer.

Measured 2026-10-14 03:06Z: the host was powered off while an outside
reader held `hyxstream.duckdb`, the periodic flush was stuck inside that
hold with `_flush_lock` taken, and after `DRAIN_LOCK_WAIT_S` the drain
printed `final drain SKIPPED ... 143520 row(s) left in memory`. That was
the #62 trade: a `spill_all` there lands in the sidecar the in-flight flush
unlinks on commit, so the drain declined, and the rows died with the
process. The fix is a snapshot to a FRESH file the in-flight flush cannot
name, untrimmed, adopted into the sidecar at the next boot before any flush
can race it. These arms re-run the #62 two-writer measurement against the
new file, both ways the in-flight flush can end.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from collector import streamd
from hyxlab.store import connect_retry
from hyxlab.streamstore import BookEvent, StreamStore, StreamTrade

RECV = datetime(2026, 10, 14, 3, 6, tzinfo=UTC)


def _fill(store: StreamStore, n: int, base: int = 0) -> None:
    nb = n // 2
    store.append_events(
        [
            BookEvent("kalshi", f"M{i}", RECV, RECV, 1, base + i, "delta", "yes", 0.4, 1.0)
            for i in range(nb)
        ]
    )
    store.append_trades(
        [
            StreamTrade("kalshi", f"M{i}", RECV, RECV, 0.4, 1.0, "yes", base + i)
            for i in range(n - nb)
        ]
    )


def _archived_ids(path: Path) -> list[tuple[str, int]]:
    with connect_retry(str(path), read_only=True) as conn:
        ev = [("e", int(r[0])) for r in conn.execute("SELECT seq FROM book_events").fetchall()]
        tr = [("t", int(r[0])) for r in conn.execute("SELECT seq FROM stream_trades").fetchall()]
    return ev + tr


def _snapshots(store: StreamStore) -> list[Path]:
    return sorted(store.path.parent.glob(store._snapshot_glob))


def _sidecar_rows(store: StreamStore) -> int:
    if not store._spill_path.exists():
        return 0
    return sum(1 for line in store._spill_path.read_text().splitlines() if line.strip())


def _hook_insert(store: StreamStore, inside: threading.Event, go: threading.Event, fail=False):
    real = store._insert

    def hooked(*a, **kw):
        inside.set()
        assert go.wait(5), "flush was never released"
        if fail:
            raise RuntimeError("archive refused (simulated outside hold)")
        return real(*a, **kw)

    store._insert = hooked


# --------------------------------------------------------------------------
# the #62 two-writer measurement, re-run against the snapshot file
# --------------------------------------------------------------------------


def test_a_snapshot_during_a_flush_survives_the_flush_and_is_archived_once(tmp_path):
    """The success arm: the in-flight flush commits its own batch and unlinks
    the sidecar; the snapshot is a different file, so it survives, and the
    next boot archives its rows exactly once -- 160 in, 160 out."""
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 100)
    assert store.spill_all() == 100  # a sidecar for the flush to unlink
    inside, go = threading.Event(), threading.Event()
    _hook_insert(store, inside, go)
    flusher = threading.Thread(target=store.flush, name="flusher")
    flusher.start()
    assert inside.wait(5)

    _fill(store, 60, base=10_000)  # arrived after the swap
    assert not store._flush_lock.acquire(timeout=0.2)  # the drain would be refused
    n, path = store.spill_snapshot()
    assert n == 60 and path is not None and path.exists()
    assert store.pending == 60, "a snapshot is a copy; it must not trim the buffer"

    go.set()
    flusher.join(10)
    assert not flusher.is_alive()
    assert not store._spill_path.exists(), "the flush unlinked its own sidecar"
    assert path.exists(), "the in-flight flush must not be able to unlink the snapshot"

    boot = StreamStore(tmp_path / "s.duckdb")
    assert boot.adopt_snapshots() == 60
    assert _snapshots(boot) == []
    boot.flush()
    ids = _archived_ids(boot.path)
    assert len(ids) == 160 and len(set(ids)) == 160


def test_a_snapshot_during_a_flush_that_fails_leaves_no_hole(tmp_path):
    """The failure arm: the in-flight flush restores its batch to the buffer
    front and overflow-spills; the process then exits, so what is in the
    buffer is gone. The snapshot caught the post-swap rows; the flush's
    batch is the pre-existing (bounded, #61) loss class and is restored to
    memory for a drain that is not coming. Every row the snapshot held
    must be archived: duplicates are allowed, holes are not."""
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 100)
    inside, go = threading.Event(), threading.Event()
    _hook_insert(store, inside, go, fail=True)

    def failing_flush() -> None:
        with pytest.raises(RuntimeError):
            store.flush()

    flusher = threading.Thread(target=failing_flush, name="flusher", daemon=True)
    flusher.start()
    assert inside.wait(5)

    _fill(store, 60, base=10_000)
    n, path = store.spill_snapshot()
    assert n == 60
    go.set()
    flusher.join(10)
    assert store.pending == 160, "the failed flush restored its batch"

    boot = StreamStore(tmp_path / "s.duckdb")
    assert boot.adopt_snapshots() == 60
    boot.flush()
    ids = set(_archived_ids(boot.path))
    assert {("e", 10_000 + i) for i in range(30)} | {("t", 10_000 + i) for i in range(30)} <= ids


# --------------------------------------------------------------------------
# adoption
# --------------------------------------------------------------------------


def test_adoption_appends_after_an_existing_sidecar_and_keeps_its_offset(tmp_path):
    """Snapshot rows are newer than anything the sidecar held at shutdown,
    so they go AFTER it, and a `.done` offset (a prefix) stays valid."""
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 40)
    store.spill_all()
    store._mark_spill_done(7)
    before = store._spill_path.read_bytes()
    _fill(store, 20, base=500)
    n, path = store.spill_snapshot()
    snap = path.read_bytes()

    boot = StreamStore(tmp_path / "s.duckdb")
    assert boot.adopt_snapshots() == 20
    assert boot._spill_path.read_bytes() == before + snap
    assert boot._spill_done() == 7
    assert not path.exists()


def test_adoption_with_no_sidecar_renames_and_drops_a_stale_offset(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    store._mark_spill_done(999)  # an orphan from a sidecar that was unlinked
    _fill(store, 20)
    n, path = store.spill_snapshot()

    boot = StreamStore(tmp_path / "s.duckdb")
    assert boot.adopt_snapshots() == 20
    assert boot._spill_done() == 0, "a stale offset would skip every adopted row"
    assert _sidecar_rows(boot) == 20 and not path.exists()
    assert boot.drain_rows_estimate() > 0, "adopted rows must count toward the drain budget"
    boot.flush()
    assert len(_archived_ids(boot.path)) == 20


def test_two_snapshots_adopt_oldest_first(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 2)
    _, first = store.spill_snapshot(datetime(2026, 10, 14, 3, 6, 0, tzinfo=UTC))
    first_bytes = first.read_bytes()
    _fill(store, 2, base=50)
    _, second = store.spill_snapshot(datetime(2026, 10, 14, 3, 6, 1, tzinfo=UTC))
    second_bytes = second.read_bytes()
    assert first.name < second.name
    boot = StreamStore(tmp_path / "s.duckdb")
    assert boot.adopt_snapshots() == 6  # 2 then 2+2: the second copy saw both
    assert boot._spill_path.read_bytes() == first_bytes + second_bytes


def test_an_empty_buffer_writes_no_snapshot(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    assert store.spill_snapshot() == (0, None)
    assert _snapshots(store) == []


def test_the_snapshot_never_waits_for_the_writer_lock(tmp_path):
    """The opposite of `test_every_store_mutation_the_drain_races_takes_the_lock`:
    this one exists for the moment the lock is held by a flush the process
    cannot wait for."""
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 10)
    done = threading.Event()

    def call():
        store.spill_snapshot()
        done.set()

    with store._flush_lock:
        threading.Thread(target=call, daemon=True).start()
        assert done.wait(2), "the snapshot blocked on the lock the flusher holds"


def test_adoption_takes_the_writer_lock(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    done = threading.Event()

    def call():
        store.adopt_snapshots()
        done.set()

    with store._flush_lock:
        threading.Thread(target=call, daemon=True).start()
        assert not done.wait(0.5), "adoption mutated the sidecar while the lock was held"
    assert done.wait(10)


def test_a_torn_snapshot_write_leaves_no_file(tmp_path, monkeypatch):
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 10)
    monkeypatch.setattr(
        "hyxlab.streamstore.os.fsync", lambda fd: (_ for _ in ()).throw(OSError(28, "ENOSPC"))
    )
    with pytest.raises(OSError):
        store.spill_snapshot()
    assert _snapshots(store) == []
    assert store.pending == 10


# --------------------------------------------------------------------------
# the daemon: the refused branch snapshots, and the ledger carries it
# --------------------------------------------------------------------------


def test_the_refused_drain_snapshots_and_the_ledger_records_the_handoff(tmp_path, capsys):
    import json

    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 30)
    d = streamd.Daemon.__new__(streamd.Daemon)
    d.store, d.stats, d._spill_corrupt_seen = store, {}, 0
    d.stalls = streamd.FlushStalls(str(tmp_path / "stalls.jsonl"))
    d.stalls.failed(30, 0, RuntimeError("held"), RECV)  # an open episode

    with store._flush_lock:
        t = threading.Thread(target=d._final_drain)
        t.start()
        t.join(streamd.DRAIN_LOCK_WAIT_S + 10)
        assert not t.is_alive()

    assert len(_snapshots(store)) == 1 and store.pending == 30
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "30 row(s) snapshotted" in out and "CRITICAL" not in out
    rec = json.loads((tmp_path / "stalls.jsonl").read_text().splitlines()[-1])
    assert rec["handoff"] == 30 and rec["spilled"] == 30 and rec["state"] == "open"


def test_the_declined_drain_records_spilled_beside_its_handoff(tmp_path):
    """The same ledger hole on the DECLINED branch, which `spill_all`s under
    the lock and never calls `observe`: `spilled - handoff` went negative."""
    import json

    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 30)
    d = streamd.Daemon.__new__(streamd.Daemon)
    d.store, d.stats, d._spill_corrupt_seen = store, {}, 0
    d.stalls = streamd.FlushStalls(str(tmp_path / "stalls.jsonl"))
    d.stalls.failed(30, 0, RuntimeError("held"), RECV)
    d._final_drain(elapsed=lambda: streamd.DRAIN_BUDGET_S)  # no budget left: declines
    rec = json.loads((tmp_path / "stalls.jsonl").read_text().splitlines()[-1])
    assert rec["handoff"] == 30 and rec["spilled"] >= rec["handoff"]


def test_boot_adopts_before_the_first_flush(tmp_path, monkeypatch):
    """`run()` folds snapshots in before the flusher exists; the order is the
    whole point, since a snapshot adopted mid-flush is #62 again."""
    import asyncio

    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 4)
    store.spill_snapshot()
    store2 = StreamStore(tmp_path / "s.duckdb")
    order: list[str] = []
    real_adopt = store2.adopt_snapshots
    monkeypatch.setattr(store2, "adopt_snapshots", lambda: order.append("adopt") or real_adopt())
    monkeypatch.setattr(store2, "mark_startup_gap", lambda now=None: order.append("gap"))
    d = streamd.Daemon.__new__(streamd.Daemon)
    d.store, d.stats, d._spill_corrupt_seen = store2, {}, 0
    d.stalls = streamd.FlushStalls(str(tmp_path / "stalls.jsonl"))

    async def noop():
        return None

    for name in ("kalshi_trades", "kalshi_books", "poly_books", "flusher"):
        monkeypatch.setattr(d, name, lambda: order.append("task") or noop())
    monkeypatch.setattr(d, "_final_drain", lambda elapsed=None: None)
    asyncio.run(d.run(duration=0.01))
    assert order[:2] == ["adopt", "gap"]
    assert _sidecar_rows(store2) == 4
