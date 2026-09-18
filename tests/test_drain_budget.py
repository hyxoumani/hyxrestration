"""The shutdown drain is a race against systemd's SIGKILL, and until
2026-09-18 nobody had timed either side of it.

`collector.streamd.Daemon._final_drain` promises that no buffered row dies
with the process. Its only tool is `StreamStore.flush()`, which writes the
in-memory buffer AND the whole sidecar in one transaction at a MEASURED
7,100 rows/s -- while systemd stops the unit with SIGTERM and SIGKILLs it
`TimeoutStopSec` later. The unit set no `TimeoutStopSec`, so the deadline
was systemd's 90s default: ~639,000 rows. The sidecar alone is unbounded
(the 7h poly-sweep wedge its own comment prices is 6.3M rows, ~890s).

Losing the race is strictly worse than declining it. SIGKILL lands after
`flush()` has moved the buffers into locals, so neither the
`except BaseException` that restores them nor the `spill_all` that would
have saved them ever runs: up to SPILL_CAP rows die with no gap row marking
the hole. `spill_all` moves the same 400,000 rows in 1.11s against
`flush()`'s 56.46s and is equally lossless, because the next boot drains
the sidecar ahead of the buffer.

These tests pin the refusal, and the two ratios between the constants that
make the refusal fire only when it must.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from collector import streamd
from hyxlab.streamstore import BookEvent, StreamStore, StreamTrade

UNIT = Path(__file__).resolve().parents[1] / "scripts/systemd/hyxlab-stream.service"
RECV = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# the constants, and the two ratios that keep them honest
# --------------------------------------------------------------------------


def test_a_full_buffer_always_fits_the_drain_budget():
    """The refusal must be reachable ONLY through the sidecar. A buffer
    pinned at SPILL_CAP is the worst case the daemon can reach without a
    disk handoff, and flushing it is what the drain is for -- if the budget
    were under that, an ordinary restart during a stall would decline a
    flush it could have finished and leave the archive short for no
    reason."""
    worst = StreamStore.SPILL_CAP / StreamStore.FLUSH_ROWS_PER_S
    assert worst == pytest.approx(56.3, abs=0.5)
    assert 1.5 * worst <= streamd.DRAIN_BUDGET_S, (
        f"a full buffer takes {worst:.1f}s to flush; a "
        f"{streamd.DRAIN_BUDGET_S:.0f}s budget leaves it no margin"
    )


def test_the_unit_pins_a_stop_timeout_that_covers_the_drain_budget():
    """Unset, this was systemd's 90s default -- a deadline the drain was
    sized against by nobody, and one any host-wide config change could move
    underneath it. The excess over the budget is the rest of the stop path:
    websocket close, task cancellation, and a periodic flush still in flight
    on its worker thread."""
    m = re.search(r"^TimeoutStopSec=(\d+)$", UNIT.read_text(), re.M)
    assert m, "hyxlab-stream.service must pin TimeoutStopSec, not inherit the default"
    assert int(m.group(1)) >= 2 * streamd.DRAIN_BUDGET_S


def test_the_flush_rate_is_slower_than_the_arrival_rate():
    """The two rates are not interchangeable and the drain needs the slow
    one. Reading `BUFFER_ROWS_PER_S` here would price the drain at 28x its
    real speed -- which is how a 90s deadline looked infinite."""
    assert StreamStore.FLUSH_ROWS_PER_S > StreamStore.BUFFER_ROWS_PER_S


# --------------------------------------------------------------------------
# the estimate
# --------------------------------------------------------------------------


def _fill(store: StreamStore, n: int) -> None:
    nb = n // 2
    store.append_events(
        [
            BookEvent("kalshi", f"M{i % 50}", RECV, RECV, 1, i, "delta", "yes", 0.4, 1.0)
            for i in range(nb)
        ]
    )
    store.append_trades(
        [StreamTrade("kalshi", f"M{i % 50}", RECV, RECV, 0.4, 1.0, "yes", i) for i in range(n - nb)]
    )


def test_the_estimate_sizes_a_real_sidecar_without_parsing_it(tmp_path):
    """Sized from `stat()` over SPILL_BYTES_PER_ROW. The one caller is the
    drain deciding whether it can afford the parse, so an estimate that pays
    for the parse first answers nothing."""
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 4_000)
    assert store.spill_all() == 4_000
    assert store.pending == 0

    est = store.drain_rows_estimate()
    assert est == pytest.approx(4_000, rel=0.25), f"sidecar of 4,000 rows estimated at {est}"

    _fill(store, 100)  # buffer rows count too: the flush writes both
    assert store.drain_rows_estimate() == est + 100


def test_a_missing_sidecar_estimates_to_the_buffer_alone(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    _fill(store, 10)
    assert store.drain_rows_estimate() == 10


# --------------------------------------------------------------------------
# the refusal, through the real drain
# --------------------------------------------------------------------------


class _Store:
    """StreamStore stand-in whose flush SUCCEEDS -- the archive is
    reachable, which is the whole point of path (c): the old drain would
    happily start a flush it could not finish."""

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
        self.sidecar_broken = False

    def drain_rows_estimate(self) -> int:
        return self.pending + self.sidecar_rows

    def flush(self) -> int:
        self.flushes += 1
        n, self.pending = self.pending, 0
        return n

    def spill_all(self) -> int:
        if self.sidecar_broken:
            raise OSError("No space left on device")
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


def _budget_rows() -> int:
    return int(streamd.DRAIN_BUDGET_S * StreamStore.FLUSH_ROWS_PER_S)


def test_a_drain_that_cannot_finish_spills_instead_of_starting_it(tmp_path, capsys):
    """The 7h-wedge case. The archive is reachable, so the old drain called
    `flush()` -- ~890s of work against a 90s deadline -- and the SIGKILL that
    ended it took the in-memory buffer with it, unmarked."""
    store = _Store(pending=300_000, sidecar_rows=6_000_000)
    d = _daemon(tmp_path, store)

    d._final_drain()

    assert store.flushes == 0, "started a flush that cannot finish before SIGKILL"
    assert store.spill_alls == 1
    assert store.pending == 0 and store.spilled == 300_000
    out = capsys.readouterr().out
    assert "DECLINED" in out and "6300000" in out.replace(",", "")


def test_a_drain_that_fits_still_flushes(tmp_path):
    """The ordinary restart -- a few thousand buffered rows, well under a
    second -- must not be pushed onto the sidecar, which would leave the
    archive short until the next boot for no reason."""
    store = _Store(pending=5_000)
    d = _daemon(tmp_path, store)

    d._final_drain()

    assert store.flushes == 1 and store.spill_alls == 0


def test_the_budget_boundary_is_the_estimate_not_the_buffer(tmp_path):
    """Exactly at the budget it flushes; one row over, it declines. The
    sidecar is what carries it over, which is why the estimate has to count
    rows that are already on disk."""
    fits = _Store(pending=1, sidecar_rows=_budget_rows() - 1)
    _daemon(tmp_path, fits)._final_drain()
    assert fits.flushes == 1 and fits.spill_alls == 0

    over = _Store(pending=1, sidecar_rows=_budget_rows())
    _daemon(tmp_path, over)._final_drain()
    assert over.flushes == 0 and over.spill_alls == 1


def test_a_declined_drain_records_the_handoff_against_an_open_episode(tmp_path):
    """A sidecar that large is only ever built by a stall, so the episode is
    open and this restart is its interruption. The handoff count is the
    slice that left memory at SHUTDOWN -- not the sidecar rows that were
    already down, which the reader must not see as memory pressure."""
    store = _Store(pending=300_000, sidecar_rows=6_000_000)
    d = _daemon(tmp_path, store)
    d.stalls.failed(300_000, 0, OSError("IO Error: Could not set lock on file"), RECV)

    d._final_drain()

    lines = (tmp_path / "stalls.jsonl").read_text().splitlines()
    rec = json.loads(lines[-1])
    assert rec["state"] == "open" and rec["handoff"] == 300_000


def test_a_declined_drain_does_not_close_the_episode(tmp_path):
    """No flush succeeded, so `ok()` must stay unreached: closing here would
    claim a stall ended when in fact the tape was handed to the sidecar and
    the wedge is still there for the next boot to find."""
    store = _Store(pending=300_000, sidecar_rows=6_000_000)
    d = _daemon(tmp_path, store)
    d.stalls.failed(300_000, 0, OSError("wedged"), RECV)

    d._final_drain()

    states = {
        json.loads(ln)["state"] for ln in (tmp_path / "stalls.jsonl").read_text().splitlines()
    }
    assert "closed" not in states


def test_a_declined_drain_whose_sidecar_refuses_says_what_was_lost(tmp_path, capsys):
    """ENOSPC during a multi-hour wedge is a live possibility -- the sidecar
    runs ~1.0 GB for a 7h one. Nothing can be saved at that point, so the
    only job left is to say so rather than exit quietly."""
    store = _Store(pending=300_000, sidecar_rows=6_000_000)
    store.sidecar_broken = True
    d = _daemon(tmp_path, store)

    d._final_drain()  # must not raise

    out = capsys.readouterr().out
    assert "CRITICAL" in out and "300000" in out.replace(",", "")
