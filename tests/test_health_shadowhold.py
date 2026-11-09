"""The SHADOWHOLD digest line: the shadow's hold on the stream archive and
its climb, and the stream's flush collisions by holder.

Fixture lines are the real `journalctl -o short-iso --utc` shapes of
2026-11-09: the shadow's 300 s line carries cumulative `held_n` /
`held_s_total`; streamd's `flush FAILED` line names the holder's PID.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from collector import health

NOW = datetime(2026, 11, 9, 20, 15, tzinfo=UTC)


def _hold(ts: datetime, pid: int, n: int, held_s: float) -> str:
    holds = (
        f'{{"n": {n + 10}, "db": "hyxstream.duckdb", "retained_n": 123, "held_n": {n}, '
        f'"held_unknown_n": 0, "held_s_max": 5.458, "held_s_total": {held_s:.3f}}}'
    )
    return (
        f"{ts:%Y-%m-%dT%H:%M:%S}+00:00 hyz python[{pid}]: [shadow] {{'snapshots': 1, "
        f"'events': 2, 'polls': {n}}} fills=40387 rss_anon_mb=179.8 stream_holds={holds}"
    )


def _fail(ts: datetime, pid: int) -> str:
    return (
        f"{ts:%Y-%m-%dT%H:%M:%S}+00:00 hyz python[840252]: [streamd] {ts:%H:%M:%S} flush FAILED "
        '(IOException: IO Error: Could not set lock on file "/x/hyxstream.duckdb": Conflicting '
        f"lock is held in /usr/bin/python3.14 (PID {pid}) by user devs. However, you would be "
        "able to open this database in read-only mode); 124898 rows held for retry in 2s"
    )


def _run(start: datetime, hours: float, per_poll: float, pid: int, n0: int = 0) -> list[str]:
    """A shadow run: one line per 300 s, 15 polls a line, a constant hold per poll."""
    out = []
    for i in range(int(hours * 12) + 1):
        n = n0 + 15 * i
        out.append(_hold(start + timedelta(seconds=300 * i), pid, n, n * per_poll))
    return out


def test_climb_is_recent_over_oldest_and_collisions_split_by_holder() -> None:
    old = _run(NOW - timedelta(hours=168), 6, 0.275, 700)
    recent = _run(NOW - timedelta(hours=6), 6, 0.458, 798)
    fails = [_fail(NOW - timedelta(minutes=m), 798) for m in (5, 55, 300)] + [
        _fail(NOW - timedelta(hours=3), 3586240),
        "-- No entries --",
    ]
    hold = health.parse_shadow_hold(old + recent + ["-- No entries --"], fails, NOW)
    assert hold is not None
    assert hold.recent.polls == 15 * 72 and hold.recent.per_poll_s == pytest.approx(0.458)
    assert hold.recent.duty == pytest.approx(0.458 * 15 / 300)
    assert hold.oldest is not None and hold.oldest.per_poll_s == pytest.approx(0.275)
    assert hold.climb == pytest.approx(0.458 / 0.275)
    assert hold.pids == {700, 798}
    assert (hold.collisions_shadow, hold.collisions_other) == (3, 1)
    line = health.shadow_hold_line(hold)
    assert line.startswith(
        "hold on hyxstream.duckdb: 0.458 s/poll over 1,080 polls in the last 6h (2.3% duty) | "
        "0.275 s/poll at 11-02 20:15Z, x1.67 = CLIMB (+26 ms/poll/day) | "
        "stream flush FAILED 24h: 4 (shadow 3, other 1)"
    )


def test_a_restart_inside_the_span_yields_no_poll_and_the_rest_still_reads() -> None:
    first = _run(NOW - timedelta(hours=6), 3, 0.4, 700, n0=20_000)
    second = _run(NOW - timedelta(hours=3), 3, 0.4, 798)  # counters fall to 0
    hold = health.parse_shadow_hold(first + second, [], NOW)
    assert hold is not None
    assert hold.recent.polls == 15 * 72  # the falling pair contributes nothing
    assert hold.recent.per_poll_s == pytest.approx(0.4)
    assert hold.oldest is None  # the whole window is one recent span
    assert "no span 6h earlier" in health.shadow_hold_line(hold)


def test_flat_hold_is_not_a_climb() -> None:
    old = _run(NOW - timedelta(hours=48), 6, 0.30, 798)
    recent = _run(NOW - timedelta(hours=6), 6, 0.33, 798, n0=100_000)
    hold = health.parse_shadow_hold(old + recent, [], NOW)
    assert hold is not None and hold.climb == pytest.approx(1.1)
    assert "x1.10 = flat" in health.shadow_hold_line(hold)


def test_empty_and_unreadable_render_as_absences_not_zero() -> None:
    assert health.parse_shadow_hold(["-- No entries --"], [], NOW) is None
    stale = _run(NOW - timedelta(hours=30), 6, 0.3, 798)
    assert health.parse_shadow_hold(stale, [], NOW) is None  # nothing in the recent 6h
    assert "no pair of stream_holds lines" in health.shadow_hold_line(None)
    assert health.shadow_hold_line("unreadable") == "168h: journal unreadable"


def test_rendered_line_moves_with_every_field() -> None:
    """#95: a field read into the block and dropped at the renderer is unrecoverable."""
    base = health.ShadowHold(
        health.HoldReading(NOW - timedelta(hours=6), NOW, 1000, 400.0),
        health.HoldReading(NOW - timedelta(hours=168), NOW - timedelta(hours=162), 1000, 300.0),
        frozenset({798}),
        10,
        2,
    )
    line = health.shadow_hold_line(base)
    from dataclasses import replace

    variants = [
        replace(base, recent=replace(base.recent, polls=1100)),
        replace(base, recent=replace(base.recent, held_s=450.0)),
        replace(base, recent=replace(base.recent, start=NOW - timedelta(hours=5))),
        replace(base, oldest=replace(base.oldest, held_s=200.0)),
        replace(base, oldest=replace(base.oldest, start=NOW - timedelta(hours=160))),
        replace(base, collisions_shadow=11),
        replace(base, collisions_other=3),
    ]
    for v in variants:
        assert health.shadow_hold_line(v) != line
