"""The cgroup's `sock_throttled` counter reaches the journal WITH A TIME.

`scripts/cgmem.sh` prints `memory.events` once, at stop, as a total over the
run: the 2026-10-10 .. 10-14 run of `hyxlab-stream` (3d18h) left
`sock_throttled=441`, the next (5h56m) left 0, and nothing could say when the
441 landed. The kernel throttles a cgroup's sockets when the cgroup is under
memory pressure, and this daemon lives at its cap against page cache
(ops.md), so a throttle is reclaim WITH A COST on the one unit whose job is
to receive. These arms pin: the parse (None is unknown, never 0), the delta
per stats period, the stats-line suffix, the WARNING line that lands only
in a period with an increment, and `?` outside a cgroup.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from collector import streamd
from hyxlab import memcap
from hyxlab.streamstore import StreamStore


def test_parse_reads_the_counter_and_a_missing_key_is_none_not_zero() -> None:
    assert memcap.sock_throttled_from_events("max 3\nsock_throttled 441\n") == 441
    assert memcap.sock_throttled_from_events("max 3\noom_kill 0\n") is None
    assert memcap.sock_throttled_from_events(None) is None


def test_read_from_the_leaf_under_a_root(tmp_path: Path) -> None:
    d = tmp_path / "user.slice" / "x.service"
    d.mkdir(parents=True)
    (d / "memory.events").write_text("low 0\nsock_throttled 7\n")
    assert memcap.cgroup_sock_throttled(root=tmp_path, rel="/user.slice/x.service") == 7
    assert memcap.cgroup_sock_throttled(root=tmp_path, rel="/user.slice/none.service") is None


def test_first_reading_has_no_increment_and_later_ones_difference() -> None:
    values = iter([441, 441, 450])
    w = streamd.SockThrottleWatch(reader=lambda: next(values))
    assert w.note() == ("sock_throttled=441 (+0)", 0)
    assert w.note() == ("sock_throttled=441 (+0)", 0)
    assert w.note() == ("sock_throttled=450 (+9)", 9)


def test_unreadable_counter_prints_a_question_mark_never_zero() -> None:
    w = streamd.SockThrottleWatch(reader=lambda: None)
    assert w.note() == ("sock_throttled=?", 0)


def _run_one_stats_round(tmp_path: Path, monkeypatch, values: list[int | None]) -> None:
    store = StreamStore(tmp_path / "s.duckdb")
    monkeypatch.setattr(streamd, "STATS_SECS", 0.0)
    rounds: list[int] = []

    async def fake_sleep(secs):
        if len(rounds) == len(values):
            raise asyncio.CancelledError
        rounds.append(1)

    monkeypatch.setattr(streamd.asyncio, "sleep", fake_sleep)
    d = streamd.Daemon(store, watchlist={})
    it = iter(values)
    d.sock_throttle = streamd.SockThrottleWatch(reader=lambda: next(it))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(d.flusher())


def test_stats_line_carries_the_counter_and_a_quiet_period_has_no_warning(
    tmp_path, monkeypatch, capsys
) -> None:
    _run_one_stats_round(tmp_path, monkeypatch, [441, 441])
    out = capsys.readouterr().out
    assert out.count("sock_throttled=441 (+0)") == 2
    assert "WARNING sock_throttled" not in out


def test_an_increment_gets_its_own_warning_line_in_that_period(tmp_path, monkeypatch, capsys):
    _run_one_stats_round(tmp_path, monkeypatch, [441, 450])
    lines = capsys.readouterr().out.splitlines()
    stats = [ln for ln in lines if " stats {" in ln]
    assert stats[0].endswith("sock_throttled=441 (+0)")
    assert stats[1].endswith("sock_throttled=450 (+9)")
    warn = [ln for ln in lines if "WARNING sock_throttled +9 this period" in ln]
    assert len(warn) == 1
    # The warning follows the stats line of ITS period, not the first one.
    assert lines.index(warn[0]) > lines.index(stats[1])


def test_outside_a_cgroup_the_line_says_unknown(tmp_path, monkeypatch, capsys) -> None:
    _run_one_stats_round(tmp_path, monkeypatch, [None])
    assert "sock_throttled=?" in capsys.readouterr().out
