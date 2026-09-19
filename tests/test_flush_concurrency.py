"""Two `flush()` calls on one `StreamStore` can run at the same time, and
the shutdown path is how it happens.

`streamd.flusher()` runs `store.flush()` through `asyncio.to_thread`, and
`run()`'s `finally` CANCELS that task before calling `_final_drain`.
Cancelling a task awaiting `to_thread` returns in 0.00s while the worker
thread keeps running -- it is joined only at interpreter exit (measured
2026-09-18). So the drain's write and a periodic flush's write overlap on
one store, one buffer and one sidecar.

Three holes, all silent, none marked by a gap row:

  (a) Both flushes parse the SAME sidecar before either unlinks it, so
      every sidecar row is inserted TWICE into tables with no key and no
      dedupe -- the EXP-1372 failure mode, from inside one process.
  (b) `spill_all` appends to the sidecar while a flush is mid-transaction;
      that flush commits and unlinks the file, deleting rows it never read.
      This is exactly the drain's DECLINE branch racing the flusher.
  (c) The buffer swap is three separate statements, so two flushers can
      split one recv-ordered batch between two transactions.

The fix is one lock the store holds across every buffer/sidecar mutation,
which the drain must acquire before it can decide anything -- and the wait
is charged against `DRAIN_BUDGET_S`, because a drain that waited 30s for
the flusher has 30s less to write in before SIGKILL.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from collector import streamd
from hyxlab.store import connect_retry
from hyxlab.streamstore import BookEvent, StreamStore, StreamTrade

RECV = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


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


def _archived(path: Path) -> int:
    with connect_retry(str(path), read_only=True) as conn:
        return (
            conn.execute("SELECT count(*) FROM book_events").fetchone()[0]
            + conn.execute("SELECT count(*) FROM stream_trades").fetchone()[0]
        )


def _sidecar_rows(store: StreamStore) -> int:
    p = store.path.parent / (store.path.name + ".spill.jsonl")
    if not p.exists():
        return 0
    return sum(1 for line in p.read_text().splitlines() if line.strip())


def _blocking_insert(store: StreamStore, inside: threading.Event, go: threading.Event):
    """Wrap `_insert` so the caller can stand inside the flush transaction:
    the sidecar has been parsed, the buffers are already in locals, and
    nothing has been committed or unlinked yet."""
    real = store._insert

    def hooked(*a, **kw):
        inside.set()
        assert go.wait(5), "flush was never released"
        return real(*a, **kw)

    store._insert = hooked


# --------------------------------------------------------------------------
# (a) two flushes, one sidecar
# --------------------------------------------------------------------------


def test_two_concurrent_flushes_do_not_double_write_the_sidecar(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 400)
    assert store.spill_all() == 400

    inside, go = threading.Event(), threading.Event()
    _blocking_insert(store, inside, go)
    first = threading.Thread(target=store.flush, name="flusher")
    first.start()
    assert inside.wait(5)

    second = threading.Thread(target=store.flush, name="drain")
    second.start()
    go.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()

    assert _archived(store.path) == 400, "sidecar rows archived more than once"


# --------------------------------------------------------------------------
# (b) a spill the in-flight flush then unlinks
# --------------------------------------------------------------------------


def test_a_spill_during_a_flush_is_not_unlinked_with_the_sidecar(tmp_path):
    """The drain's DECLINE branch: it hands the tape to the sidecar for the
    next boot to drain. If a flush is mid-transaction, that flush's
    post-commit `unlink` deletes the handoff -- the rows are in neither the
    archive nor the file, and the gap rows that would have marked the hole
    were in the same buffer."""
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 100)

    inside, go = threading.Event(), threading.Event()
    _blocking_insert(store, inside, go)
    flusher = threading.Thread(target=store.flush, name="flusher")
    flusher.start()
    assert inside.wait(5)

    _fill(store, 60, base=10_000)
    handoff = threading.Thread(target=store.spill_all, name="drain")
    handoff.start()
    go.set()
    flusher.join(10)
    handoff.join(10)
    assert not flusher.is_alive() and not handoff.is_alive()

    assert _archived(store.path) + _sidecar_rows(store) + store.pending == 160


# --------------------------------------------------------------------------
# the drain waits for the flusher, on the drain's own clock
# --------------------------------------------------------------------------


class _Store:
    """Real lock, stub writes: the drain's decision is what is under test."""

    PENDING_ALARM = StreamStore.PENDING_ALARM
    SPILL_CAP = StreamStore.SPILL_CAP
    FLUSH_ROWS_PER_S = StreamStore.FLUSH_ROWS_PER_S

    def __init__(self, pending: int, sidecar_rows: int = 0) -> None:
        self.pending = pending
        self.sidecar_rows = sidecar_rows
        self.spilled = 0
        self.spill_corrupt = 0
        self.flushes = 0
        self.spill_alls = 0
        self._flush_lock = threading.RLock()

    exclusive = StreamStore.exclusive

    def drain_rows_estimate(self) -> int:
        return self.pending + self.sidecar_rows

    def flush(self) -> int:
        self.flushes += 1
        n, self.pending = self.pending, 0
        return n

    def spill_all(self) -> int:
        self.spill_alls += 1
        moved, self.pending = self.pending, 0
        self.spilled += moved
        return moved


def _daemon(tmp_path, store):
    d = streamd.Daemon.__new__(streamd.Daemon)
    d.store = store
    d.stats = {}
    d._spill_corrupt_seen = 0
    d.stalls = streamd.FlushStalls(str(tmp_path / "stalls.jsonl"))
    return d


def test_the_drain_will_not_write_while_a_flush_holds_the_store(tmp_path, capsys):
    """Nothing the drain could do here is safe: a flush would double-write
    the sidecar, a spill would be unlinked by the commit in flight. So it
    declines, loudly and with the row count, rather than racing."""
    store = _Store(pending=5_000)
    d = _daemon(tmp_path, store)

    with store._flush_lock:
        held = threading.Thread(target=d._final_drain, name="drain")
        held.start()
        held.join(streamd.DRAIN_LOCK_WAIT_S + 10)
        assert not held.is_alive(), "the drain blocked past its own lock budget"

    assert store.flushes == 0 and store.spill_alls == 0
    out = capsys.readouterr().out
    assert "CRITICAL" in out and "5000" in out.replace(",", "")


def test_a_drain_that_gets_the_store_flushes_as_before(tmp_path):
    store = _Store(pending=5_000)
    _daemon(tmp_path, store)._final_drain()
    assert store.flushes == 1 and store.spill_alls == 0


def test_waiting_for_the_flusher_is_charged_against_the_drain_budget(tmp_path, capsys):
    """A drain that spent part of its budget waiting has that much less
    left to write in -- SIGKILL lands `TimeoutStopSec` after SIGTERM, not
    after the lock is free. A row count that fits the FULL budget but not
    the remainder must be declined."""
    full = int(streamd.DRAIN_BUDGET_S * StreamStore.FLUSH_ROWS_PER_S)
    store = _Store(pending=full - 1_000)
    d = _daemon(tmp_path, store)
    waited = streamd.DRAIN_BUDGET_S / 2

    # A real holder, released from its OWN thread (an RLock cannot be
    # released by another one), with the elapsed clock stubbed to the wait
    # the drain would have paid for it at production scale.
    held, done = threading.Event(), threading.Event()

    def holder() -> None:
        with store._flush_lock:
            held.set()
            done.wait(2)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert held.wait(5)
    threading.Timer(0.05, done.set).start()
    d._final_drain(elapsed=lambda: waited)
    t.join(5)

    assert store.flushes == 0, "flushed a batch that only fit the budget it had already spent"
    assert store.spill_alls == 1
    assert "DECLINED" in capsys.readouterr().out


def test_the_lock_wait_leaves_room_for_a_full_buffer(tmp_path):
    """The two constants have to compose: after the longest wait the drain
    will sit through, what is left of the budget must still cover the worst
    case it is REQUIRED to flush (a buffer pinned at SPILL_CAP). Otherwise
    any wait at all turns an ordinary restart into a sidecar handoff."""
    left = streamd.DRAIN_BUDGET_S - streamd.DRAIN_LOCK_WAIT_S
    worst = StreamStore.SPILL_CAP / StreamStore.FLUSH_ROWS_PER_S
    assert left >= worst, (
        f"a {streamd.DRAIN_LOCK_WAIT_S:.0f}s wait leaves {left:.0f}s of a "
        f"{streamd.DRAIN_BUDGET_S:.0f}s budget, under the {worst:.1f}s a full buffer needs"
    )


def test_the_stop_timeout_covers_the_wait_and_the_drain(tmp_path):
    """`TimeoutStopSec` bounds wait + write, not write alone."""
    import re

    unit = Path(__file__).resolve().parents[1] / "scripts/systemd/hyxlab-stream.service"
    m = re.search(r"^TimeoutStopSec=(\d+)$", unit.read_text(), re.M)
    assert m
    assert int(m.group(1)) >= streamd.DRAIN_BUDGET_S + streamd.DRAIN_LOCK_WAIT_S


@pytest.mark.parametrize("name", ["flush", "spill_all", "drain_rows_estimate"])
def test_every_store_mutation_the_drain_races_takes_the_lock(tmp_path, name):
    """Held from outside, each of these must block rather than proceed --
    the property the two repros above turn into archive holes."""
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 10)
    done = threading.Event()

    def call():
        getattr(store, name)()
        done.set()

    with store._flush_lock:
        threading.Thread(target=call, daemon=True).start()
        assert not done.wait(0.5), f"{name} mutated the store while the lock was held"
    assert done.wait(10)
