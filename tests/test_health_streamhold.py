"""STREAMHOLD: the digest reads what the stream daemon's own flushes hold
its archive for (mistakes #211). The fixture lines are the daemon's real
ones -- rendered by `streamd.stats_line` and `streamd.long_flush_line` and
stamped the way `journalctl -o short-iso --utc` stamps them -- so a change
to either renderer that the parser cannot read fails here, not in the
journal."""

from __future__ import annotations

from collector import health, streamd


def _stamp(clock: str, msg: str) -> str:
    return f"2026-11-18T{clock}+00:00 hyz python[350689]: [streamd] {clock} {msg}"


def _stats(clock: str, holds, monkeypatch, prefix: str = "") -> str:
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 271.1)
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: (55, 55.0))
    flushed = None if prefix else 117564
    sock = None if prefix else "sock_throttled=0 (+0)"
    return _stamp(clock, streamd.stats_line(prefix, {"kalshi_trades": 2}, flushed, sock, holds))


def test_the_window_sums_the_daemons_own_lines(monkeypatch):
    lines = [
        _stats("08:49:36", (19, 21.3, 2.5), monkeypatch),
        _stats("08:54:41", (20, 18.7, 1.4), monkeypatch),
        # A shutdown line closes a partial period: not in the duty.
        _stats("08:55:00", (1, 9.0, 9.0), monkeypatch, prefix="shutdown; "),
    ]
    hold = health.parse_stream_hold(lines)
    assert (hold.lines, hold.carrying, hold.flushes) == (2, 2, 39)
    assert round(hold.held_s, 1) == 40.0 and hold.worst_s == 2.5
    assert hold.worst_at.strftime("%H:%M") == "08:49"
    assert health.stream_hold_line(hold) == (
        "24h: own hold on hyxstream.duckdb: 1.03 s/flush over 39 flushes = 6.7% duty"
        " across 2 stats line(s) | worst flush 2.5 s (in the period ending 11-18 08:49Z)"
        " | no flush held a whole 15 s period"
    )
    # Perturbation: each field of the daemon's clause moves the row.
    base = health.stream_hold_line(hold)
    for holds in ((20, 21.3, 2.5), (19, 31.3, 2.5), (19, 21.3, 3.5)):
        moved = [_stats("08:49:36", holds, monkeypatch), lines[1]]
        assert health.stream_hold_line(health.parse_stream_hold(moved)) != base


def test_a_long_flush_is_named_with_its_own_clock_time(monkeypatch):
    assert health.STREAMHOLD_LONG_S == streamd.LONG_FLUSH_HOLD_S
    assert health.STREAMFLOW_PERIOD_S == streamd.STATS_SECS
    lines = [
        _stamp("03:55:20", streamd.long_flush_line(22.0, 9000)),
        _stamp("03:56:07", streamd.long_flush_line(41.26, 133483)),
        _stats("03:59:51", (6, 250.0, 41.3), monkeypatch),
    ]
    line = health.stream_hold_line(health.parse_stream_hold(lines))
    assert "41.67 s/flush over 6 flushes = 83.3% duty across 1 stats line(s)" in line
    assert line.endswith(
        "LONG: 2 flush(es) held a whole 15 s period or more, longest 41.3 s at 11-18 03:56:07Z"
    )


def test_an_unmeasured_window_is_never_a_zero(monkeypatch):
    old = _stats("08:42:19", None, monkeypatch)  # the daemon before #211
    assert " flush_hold=" not in old
    line = health.stream_hold_line(health.parse_stream_hold([old, old]))
    assert "UNMEASURED" in line and "none of 2 stats line(s)" in line and "0.0" not in line
    # Mixed: the reading covers the carrying lines and says how many.
    mixed = health.parse_stream_hold([old, _stats("08:49:36", (19, 21.3, 2.5), monkeypatch)])
    assert "= 7.1% duty across 1 stats line(s) (1 of 2 stats lines carry it)" in (
        health.stream_hold_line(mixed)
    )
    # Carried, but no flush opened the file: no per-flush figure to print.
    idle = health.parse_stream_hold([_stats("08:49:36", (0, 0.0, 0.0), monkeypatch)])
    assert "no flush opened the file" in health.stream_hold_line(idle)
    assert health.stream_hold_line(None).startswith("24h: no stats lines")
    assert health.stream_hold_line("unreadable") == "24h: journal unreadable"
    assert health.parse_stream_hold("-- No entries --") is None


def test_the_reader_tells_a_failed_journal_from_an_empty_one(monkeypatch):
    monkeypatch.setattr(health, "_journal_hourly", lambda args, hours: None)
    assert health.read_stream_hold() == "unreadable"
    seen = {}

    def fake(args, hours):
        seen["grep"] = next(a for a in args if a.startswith("--grep="))
        return ""

    monkeypatch.setattr(health, "_journal_hourly", fake)
    assert health.read_stream_hold() is None
    assert "long flush: held" in seen["grep"] and "last flush" in seen["grep"]
