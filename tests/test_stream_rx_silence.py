"""The stream daemon measures whole-universe receive silence (mistakes #226).

From 2026-11-20 16:34Z the box's own WiFi background scan took the path away
for ~10 s every ~308 s. No socket dropped, TCP delivered the backlog, and so
no counter the daemon kept moved: 221 holes in 22 h were visible only as
`recv_ts - src_ts` inside the archive.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from collector import health, streamd


def test_no_tick_and_no_frame_ever_are_unmeasured_not_a_clean_zero():
    w = streamd.RxSilenceWatch()
    assert w.take(10.0) is None
    w.note(1.0)  # ticks ran, but no frame was ever received
    assert w.take(10.0) is None


def test_steady_frames_read_one_tick_and_a_hole_reads_its_length():
    w = streamd.RxSilenceWatch()
    t = 0.0
    for _ in range(8):  # a frame before every tick
        w.frames += 1
        t += 0.25
        assert (w.note(t) or 0.0) <= 0.25
    for _ in range(40):  # 10 s with nothing on any connection
        t += 0.25
        assert w.note(t) is None
    w.frames += 500  # the backlog lands
    t += 0.25
    assert w.note(t) == pytest.approx(10.25)
    assert w.take(t) == (pytest.approx(10.25), 1)
    # Handed over once: the next period starts empty, not at the old maximum.
    w.frames += 1
    assert w.note(t + 0.25) == pytest.approx(0.25)
    assert w.take(t + 0.25) == (pytest.approx(0.25), 0)


def test_a_silence_below_the_threshold_is_in_the_maximum_and_not_the_count():
    w = streamd.RxSilenceWatch()
    w.frames += 1
    w.note(0.0)
    w.frames += 1
    w.note(streamd.RX_SILENT_S - 0.01)
    assert w.take(streamd.RX_SILENT_S)[1] == 0
    w.frames += 1
    w.note(2 * streamd.RX_SILENT_S)
    assert w.take(2 * streamd.RX_SILENT_S)[1] == 1


def test_a_silence_still_open_at_the_stats_line_is_in_that_lines_maximum():
    """A dead path must not print as its last good period."""
    w = streamd.RxSilenceWatch()
    w.frames += 1
    w.note(0.0)
    for i in range(1, 400):
        w.note(i * 0.25)
    worst, n = w.take(100.0)
    assert worst == pytest.approx(100.0) and n == 0
    # ... and the next line carries the whole silence, and counts it when it ends.
    w.note(100.25)
    w.frames += 1
    assert w.note(130.0) == pytest.approx(130.0)
    assert w.take(130.0) == (pytest.approx(130.0), 1)


def test_only_a_silence_past_the_threshold_gets_its_own_journal_line():
    assert streamd.rx_silence_line(None) is None
    assert streamd.rx_silence_line(streamd.RX_SILENCE_LOG_S - 0.01) is None
    line = streamd.rx_silence_line(9.84)
    assert line is not None
    assert line.startswith("rx silence: no frame on any connection for 9.8s")


def test_stats_line_carries_the_silence_between_loop_lag_and_the_holds(monkeypatch):
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 1.0)
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: None)
    args = ("", {"a": 1}, 5, "sock_throttled=0 (+0)", (19, 17.34, 2.06), (0.1, 0.2, 0.3, 0.4))
    line = streamd.stats_line(*args, (0.03, 0), (9.837, 1))
    assert " loop_lag=0.03/0 rx_gap=9.84/1 flush_hold=19/17.3/2.1 flush_steps=" in line
    # Perturbation, one field at a time: each reaches the line (#95).
    assert streamd.stats_line(*args, (0.03, 0), (8.837, 1)) != line
    assert streamd.stats_line(*args, (0.03, 0), (9.837, 2)) != line
    # No reading is no clause.
    assert "rx_gap" not in streamd.stats_line(*args, (0.03, 0))
    # The digest's STREAMHOLD pattern is anchored at the END of the line.
    with_rx = health._STREAMHOLD_RE.search(line)
    without = health._STREAMHOLD_RE.search(streamd.stats_line(*args, (0.03, 0)))
    assert with_rx is not None and without is not None
    assert with_rx.groups() == without.groups()


def test_both_publishers_and_both_receive_loops_reach_the_watch():
    src = inspect.getsource(streamd)
    assert src.count("rx = self.rx_gap.take(") == 2
    assert src.count("steps, lag, rx))") == 2
    # One bump per receive loop (Kalshi's shared loop, Polymarket's).
    assert src.count("self.rx_gap.frames += 1") == 2
    assert src.count("rx_silence_line(self.rx_gap.note(loop.time()))") == 1


def test_the_watch_task_sees_a_hole_in_the_frames_on_a_real_loop(monkeypatch, capsys):
    tick, hole = 0.02, 0.30
    monkeypatch.setattr(streamd, "LOOP_TICK_S", tick)
    monkeypatch.setattr(streamd, "RX_SILENT_S", 0.1)
    monkeypatch.setattr(streamd, "RX_SILENCE_LOG_S", 0.1)
    d = streamd.Daemon.__new__(streamd.Daemon)
    d.loop_lag = streamd.LoopLagWatch()

    async def feed(seconds: float):
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            d.rx_gap.frames += 1
            await asyncio.sleep(tick / 4)

    async def main():
        task = asyncio.create_task(d.loop_watch())
        await feed(0.15)
        await asyncio.sleep(hole)  # the loop runs; nothing arrives
        await feed(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return asyncio.get_running_loop().time()

    now = asyncio.run(main())
    worst, n = d.rx_gap.take(now)
    assert hole - 0.01 <= worst <= hole + 2 * tick + 0.15
    assert n == 1
    # The loop itself never stalled: this is the path's reading, not the loop's.
    assert d.loop_lag.take()[0] < 0.1
    assert capsys.readouterr().out.count("rx silence: no frame on any connection for 0.") == 1
