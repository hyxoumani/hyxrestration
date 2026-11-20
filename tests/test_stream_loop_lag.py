"""The stream daemon measures its own event loop (mistakes #220).

Since the 2026-10-23 coverage expansion ~95% of the daemon's abrupt
connection drops land in 08-15Z, on every connection of both venues alike.
The journal could clear the path and the daemon's own rate-limit retries; it
could say nothing about whether the event loop -- which runs every socket
read and every keepalive pong -- was running, because nothing measured it.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from collector import health, streamd


def test_an_unwatched_period_is_unmeasured_not_a_clean_zero():
    assert streamd.LoopLagWatch().take() is None


def test_a_period_reports_its_worst_wake_and_its_late_wakes_then_resets():
    w = streamd.LoopLagWatch()
    for lag in (0.0, 0.4, 3.2, streamd.LOOP_LATE_S, 0.99):
        w.note(lag)
    assert w.take() == (3.2, 2)
    # Handed over once: the next period starts empty, not at the old maximum.
    assert w.take() is None
    w.note(0.0)
    assert w.take() == (0.0, 0)


def test_only_a_wake_past_the_threshold_gets_its_own_journal_line():
    assert streamd.loop_stall_line(streamd.LOOP_STALL_LOG_S - 0.01) is None
    line = streamd.loop_stall_line(7.34)
    assert line is not None and line.startswith("loop stall: the event loop ran 7.3s late")


def test_stats_line_carries_the_loop_lag_ahead_of_the_hold_clauses(monkeypatch):
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 1.0)
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: None)
    args = ("", {"a": 1}, 5, "sock_throttled=0 (+0)", (19, 17.34, 2.06), (0.1, 0.2, 0.3, 0.4))
    line = streamd.stats_line(*args, (3.217, 2))
    assert " sock_throttled=0 (+0) loop_lag=3.22/2 flush_hold=19/17.3/2.1 flush_steps=" in line
    # Perturbation, one field at a time: each reaches the line (#95).
    assert streamd.stats_line(*args, (4.217, 2)) != line
    assert streamd.stats_line(*args, (3.217, 3)) != line
    # No reading is no clause.
    assert "loop_lag" not in streamd.stats_line(*args)
    # The digest's STREAMHOLD pattern is anchored at the END of the line: the
    # new clause must leave it reading the same hold it read before.
    with_lag = health._STREAMHOLD_RE.search(line)
    without = health._STREAMHOLD_RE.search(streamd.stats_line(*args))
    assert with_lag is not None and without is not None
    assert with_lag.groups() == without.groups()


def test_both_publishers_hand_the_watch_to_the_one_renderer():
    src = inspect.getsource(streamd)
    assert src.count("lag = self.loop_lag.take()") == 2
    assert src.count('asyncio.create_task(self.loop_watch(), name="loop-watch")') == 1


def _watch_while(monkeypatch, body, *, tick: float = 0.02) -> streamd.Daemon:
    """Run `Daemon.loop_watch` on a real loop beside `body`, then stop it."""
    monkeypatch.setattr(streamd, "LOOP_TICK_S", tick)
    d = streamd.Daemon.__new__(streamd.Daemon)  # the watch needs no store
    d.loop_lag = streamd.LoopLagWatch()

    async def main():
        task = asyncio.create_task(d.loop_watch())
        await asyncio.sleep(tick * 3)  # the watch is ticking before the body
        await body()
        await asyncio.sleep(tick * 3)  # and gets a wake after it
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    return d


def test_a_blocked_loop_is_measured_to_within_one_tick(monkeypatch, capsys):
    block_s, tick = 0.30, 0.02
    monkeypatch.setattr(streamd, "LOOP_STALL_LOG_S", 0.1)
    monkeypatch.setattr(streamd, "LOOP_LATE_S", 0.1)

    async def body():
        time.sleep(block_s)  # the defect under measurement: a blocking call ON the loop

    d = _watch_while(monkeypatch, body, tick=tick)
    worst, late = d.loop_lag.take()
    # A lower bound, at most one tick short of the stall (and never more than
    # the stall plus scheduling noise).
    assert block_s - tick - 0.01 <= worst <= block_s + 0.15
    assert late == 1
    assert capsys.readouterr().out.count("loop stall: the event loop ran 0.") == 1


def test_a_loop_that_only_awaits_reads_no_stall(monkeypatch, capsys):
    """Vacuity guard for the test above: the same duration spent AWAITING is
    not a stall, so the reading there is the block and not the wall time."""
    monkeypatch.setattr(streamd, "LOOP_STALL_LOG_S", 0.1)
    monkeypatch.setattr(streamd, "LOOP_LATE_S", 0.1)

    async def body():
        await asyncio.sleep(0.30)

    d = _watch_while(monkeypatch, body)
    worst, late = d.loop_lag.take()
    assert worst < 0.1 and late == 0
    assert "loop stall" not in capsys.readouterr().out
