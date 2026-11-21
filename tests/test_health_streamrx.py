"""The STREAMRX digest row: whole-path silences from the stats line's `rx_gap`.

The fixture lines are rendered by the daemon's own `streamd.stats_line`, so a
change to the clause's shape there turns this file red rather than leaving the
row reading `UNMEASURED` in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from collector import health, streamd

P = "hyz python[998515]: [streamd] "
T0 = datetime(2026, 11, 21, 15, 24, 9, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fixed_process_readings(monkeypatch):
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 1.0)
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: None)


def _line(i: int, rx, loop=(0.1, 0)) -> str:
    ts = (T0 + timedelta(seconds=305 * i)).isoformat()
    body = streamd.stats_line(
        "", {"kalshi_trades": i}, 5, "sock_throttled=0 (+0)", (18, 26.7, 1.8), None, loop, rx
    )
    return f"{ts} {P}{ts[11:19]} {body}"


def test_stalled_periods_are_counted_with_their_span_hour_and_the_loops_alibi() -> None:
    rx = [(5.26, 1), (4.51, 2), (0.25, 0), (7.02, 1), (3.99, 1), (9.02, 1), (4.0, 1)]
    got = health.parse_stream_rx([_line(i, g) for i, g in enumerate(rx)] + ["-- No entries --"])
    assert got is not None and got.lines == 7 and got.carrying == 7
    assert got.stalled == 5  # 3.99 is under the bar, 4.00 is at it
    assert got.worst_s == pytest.approx(9.02) and got.late_wakes_in_stalled == 0
    assert got.by_hour == ((datetime(2026, 11, 21, 15, tzinfo=UTC), 5),) and got.first_at == T0
    assert health.stream_rx_line(got) == (
        "24h: STALLS: 5 of 7 period(s) held a frame-less stretch of 4 s or more on all "
        "connections (71%), 11-21 15:24Z..11-21 15:54Z, busiest hour 11-21 15Z with 5 | "
        "longest 9.0 s (in the period ending 11-21 15:49Z) | 0 late loop wakes in those "
        "periods (the path, not the loop)"
    )


def test_late_wakes_are_charged_only_from_the_stalled_periods() -> None:
    lines = [
        _line(0, (0.5, 0), loop=(3.0, 4)),  # a late loop in a CLEAN period is not an alibi lost
        _line(1, (6.0, 1), loop=(2.5, 2)),
        _line(2, (6.0, 1)),
    ]
    got = health.parse_stream_rx(lines)
    assert got is not None and got.stalled == 2 and got.late_wakes_in_stalled == 2
    assert health.stream_rx_line(got).endswith(
        "| 2 late loop wake(s) in those periods -- read loop_lag there before blaming the path"
    )


def test_a_clean_window_says_what_it_looked_for_and_keeps_the_longest() -> None:
    got = health.parse_stream_rx([_line(i, (0.25 + i, 0)) for i in range(3)])
    assert got is not None and got.stalled == 0
    assert health.stream_rx_line(got) == (
        "24h: no frame-less stretch of 4 s or more on all connections in 3 period(s) | "
        "longest 2.2 s (in the period ending 11-21 15:34Z)"
    )


def test_lines_from_before_the_clause_are_no_reading_never_clean_periods() -> None:
    old = [_line(i, None) for i in range(4)]
    got = health.parse_stream_rx(old)
    assert got is not None and got.carrying == 0
    assert health.stream_rx_line(got) == (
        "24h: whole-path silences UNMEASURED -- none of 4 stats line(s) carries rx_gap "
        "(daemon on pre-#226 code)"
    )
    mixed = health.parse_stream_rx([*old, _line(4, (5.0, 1)), _line(5, (0.3, 0))])
    assert mixed is not None and (mixed.lines, mixed.carrying, mixed.stalled) == (6, 2, 1)
    assert "STALLS: 1 of 2 period(s) of 6 stats lines held" in health.stream_rx_line(mixed)


def test_no_lines_unreadable_and_the_shutdown_line_is_not_a_period() -> None:
    assert health.parse_stream_rx(["-- No entries --"]) is None
    assert health.stream_rx_line(None).startswith("24h: no stats lines")
    assert health.stream_rx_line("unreadable") == "24h: journal unreadable"
    ts = T0.isoformat()
    body = streamd.stats_line("shutdown; ", {"a": 1}, None, None, None, None, (0.1, 0), (8.0, 1))
    assert health.parse_stream_rx([f"{ts} {P}{ts[11:19]} {body}"]) is None  # a partial period


def test_the_digest_prints_the_row_and_the_bar_sits_under_the_daemons_line_threshold() -> None:
    import inspect

    assert health.STREAMRX_STALL_S == 4.0 < streamd.RX_SILENCE_LOG_S
    assert "stream_rx_line(read_stream_rx())" in inspect.getsource(health)
