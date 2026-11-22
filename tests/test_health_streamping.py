"""The STREAMPING digest row: `keepalive ping timeout` per connection, by
distinct minute, with the whole-path minutes (a roam, an outage) counted apart.

The fixture lines are the daemon's own journal shape, copied from production
(2026-11-22 02:13Z); `test_the_fixture_is_the_line_the_daemon_prints` pins
the format string they mirror.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

from collector import health, streamd

NOW = datetime(2026, 11, 22, 2, 30, tzinfo=UTC)
TIMEOUT = (
    "ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout; "
    "no close frame received; retry in 1s"
)
CLOSED = "ConnectionClosedError: no close frame received or sent; retry in 1s"


def _line(at: datetime, label: str, what: str = TIMEOUT) -> str:
    return f"{at.isoformat()} hyz python[1386953]: [streamd] {at:%H:%M:%S} {label}: {what}"


def _ago(minutes: float, second: int = 1) -> datetime:
    return NOW - timedelta(minutes=minutes) + timedelta(seconds=second)


def test_a_starved_connection_is_named_and_a_roam_is_charged_to_nobody() -> None:
    roam = [_line(_ago(200), f"kalshi-books-{i}") for i in range(12)]
    roam += [_line(_ago(200, 30), "kalshi-ticker"), _line(_ago(200, 31), "kalshi-ticker")]
    ticker = [_line(_ago(m), "kalshi-ticker") for m in range(1, 90, 2)]  # 45 minutes, 30 recent
    ticker.append(_line(_ago(1, 40), "kalshi-ticker"))  # a second line in a counted minute
    shard = [_line(_ago(m), "kalshi-books-5") for m in (5, 100)]
    got = health.parse_stream_ping([*roam, *ticker, *shard, "-- No entries --"], NOW)
    assert got.lines == 12 + 2 + 46 + 2 and got.whole_path_minutes == 1 and len(got.conns) == 13
    top = got.conns[0]
    assert (top.label, top.lines, top.own_minutes, top.recent_minutes) == (
        "kalshi-ticker",
        48,
        45,
        30,
    )
    assert health.stream_ping_line(got) == (
        "24h: 62 keepalive ping timeout(s) on 13 connection(s) | 1 minute(s) with 10 or more "
        "connections at once (the whole path: a roam or an outage), charged to no connection | "
        "CONNECTION STARVED NOW: kalshi-ticker in 45 distinct minute(s) of its own 11-22 "
        "01:01Z..11-22 02:29Z, 30 of the last 60 min | most of the other 12: kalshi-books-5 in "
        "2 distinct minute(s) of its own 11-22 00:50Z..11-22 02:25Z, 1 of the last 60 min"
    )


def test_a_starvation_that_ended_stays_legible_and_is_not_called_now() -> None:
    old = [_line(_ago(m), "kalshi-ticker") for m in range(300, 400)]
    got = health.parse_stream_ping(old, NOW)
    assert got.conns[0].own_minutes == 100 and got.conns[0].recent_minutes == 0
    assert health.stream_ping_line(got) == (
        "24h: 100 keepalive ping timeout(s) on 1 connection(s) | 0 minute(s) with 10 or more "
        "connections at once (the whole path: a roam or an outage), charged to no connection | "
        "no connection timing out in 15 or more of the last 60 min | most: "
        "kalshi-ticker in 100 distinct minute(s) of its own 11-21 19:51Z..11-21 21:30Z, 0 of "
        "the last 60 min"
    )


def test_the_bars_sit_where_they_are_written() -> None:
    nine = [_line(_ago(30), f"kalshi-books-{i}") for i in range(9)]
    got = health.parse_stream_ping(nine, NOW)
    assert got.whole_path_minutes == 0 and all(c.own_minutes == 1 for c in got.conns)
    ten = health.parse_stream_ping([*nine, _line(_ago(30), "kalshi-trades")], NOW)
    assert ten.whole_path_minutes == 1 and all(c.own_minutes == 0 for c in ten.conns)
    assert "kalshi-books-0 only inside whole-path minutes" in health.stream_ping_line(ten)
    # 14 recent minutes is under the bar, 15 is at it; minute 60 back is inside, 61 is not.
    for n, named in ((14, False), (15, True)):
        lines = [_line(_ago(m), "kalshi-ticker") for m in range(1, n + 1)]
        assert ("STARVED NOW" in health.stream_ping_line(health.parse_stream_ping(lines, NOW))) is (
            named
        )
    edge = health.parse_stream_ping(
        [_line(_ago(60, 0), "kalshi-ticker"), _line(_ago(61, 0), "kalshi-ticker")], NOW
    )
    assert edge.conns[0].own_minutes == 2 and edge.conns[0].recent_minutes == 1


def test_only_the_keepalive_timeout_is_counted_and_empty_is_a_reading() -> None:
    other = [_line(_ago(m), "kalshi-ticker", CLOSED) for m in range(1, 30)]
    other.append(
        f"{_ago(3).isoformat()} hyz python[1]: [streamd] 02:27:01 kalshi/ticker GAP (reconnect)"
    )
    got = health.parse_stream_ping(other, NOW)
    assert got.lines == 0 and got.conns == ()
    assert health.stream_ping_line(got) == "24h: no keepalive ping timeout on any connection"
    assert health.parse_stream_ping("-- No entries --", NOW).lines == 0
    assert health.stream_ping_line("unreadable") == "24h: journal unreadable"


def test_read_stream_ping_distinguishes_a_failed_journalctl(monkeypatch) -> None:
    monkeypatch.setattr(health, "_journal_hourly", lambda args, hours: None)
    assert health.read_stream_ping(NOW) == "unreadable"
    seen = {}

    def fake(args, hours):
        seen["args"], seen["hours"] = args, hours
        return _line(_ago(2), "poly-books")

    monkeypatch.setattr(health, "_journal_hourly", fake)
    got = health.read_stream_ping(NOW)
    assert isinstance(got, health.StreamPing) and got.conns[0].label == "poly-books"
    assert "--utc" in seen["args"] and "--grep=keepalive ping timeout" in seen["args"]
    assert seen["hours"] == health.STREAMFLOW_WINDOW_H


def test_the_fixture_is_the_line_the_daemon_prints() -> None:
    src = inspect.getsource(streamd)
    assert '_log(f"kalshi-{label}: {type(exc).__name__}: {exc}{note}; retry in' in src
    assert '_log(f"poly-books: {type(exc).__name__}: {exc}; retry in' in src
    assert "stream_ping_line(read_stream_ping(now))" in inspect.getsource(health)
    # The starved clause's bar erodes under a long starvation; it must point here.
    assert "STREAMPING row" in inspect.getsource(health._starved_clause)
