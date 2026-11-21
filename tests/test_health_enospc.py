"""ENOSPC: full-disk write failures, and whether the journal was there to
record them (2026-11-21, mistakes #222).

The shared disk was 34 GB from full with a 7-43 GB nightly spill due, and ten
passes had answered "did a write fail" with `grep -c 'Errno 28'` over a
journal that lives on the same filesystem and stops growing under 4 GB free.
The message fixtures are the strings the writers print (streamd's
`flush FAILED`, DuckDB's commit refusal from the #174 rehearsal) and the one
real match in 30 days of journal: a pass's own prose in the autoloop unit.
"""

from __future__ import annotations

import json

import pytest

from collector import health

T0 = 1_795_240_000.0
STREAM = "hyxlab-stream.service"
COLLECT = "hyxlab-collect.service"

FLUSH = (
    "[streamd] CRITICAL flush FAILED (OSError: [Errno 28] No space left on device); retry in 15s"
)
DUCK = (
    "TransactionException: Failed to commit: Could not write file "
    '"data/hyxlab.duckdb.wal": No space left on device'
)
PROSE = (
    "- If the disk is not freed, the stream journal on 11-18 morning is where the first "
    "`flush FAILED (OSError: [Errno 28]` would appear."
)


def _jl(ts: float, msg: object = None, unit: object = None) -> str:
    rec: dict[str, object] = {"__REALTIME_TIMESTAMP": str(int(ts * 1e6))}
    if msg is not None:
        rec["MESSAGE"] = msg
    if unit is not None:
        rec["_SYSTEMD_USER_UNIT"] = unit
    return json.dumps(rec)


def _ticks(start: float, n: int, step: float = 300.0) -> tuple[float, ...]:
    return tuple(start + i * step for i in range(n))


def test_failures_are_read_by_unit_from_both_strings_and_prose_is_not_a_failure() -> None:
    out = "\n".join(
        [
            _jl(T0 + 20, DUCK, COLLECT),
            _jl(T0, FLUSH, STREAM),
            # The only real matches in 30 d of journal: a sentence ABOUT the string.
            _jl(T0 + 5, PROSE, "hyxlab-autoloop.service"),
            # Another project's unit is not this lab's writer.
            _jl(T0 + 6, FLUSH, "hylshi-lake-nightly.service"),
            # journalctl's sentinel goes to stdout (#99); a byte-array MESSAGE is not text.
            "-- No entries --",
            _jl(T0 + 7, [91, 115], STREAM),
            _jl(T0 + 8, "flush FAILED (IOException: lock held by pid 1)", STREAM),
        ]
    )
    units = {STREAM, COLLECT, "hyxlab-autoloop.service"}
    assert health.parse_enospc(out, units) == ((T0, STREAM), (T0 + 20, COLLECT))
    # Unrestricted, the sibling's line counts; the prose unit never does.
    assert [u for _, u in health.parse_enospc(out)] == [
        STREAM,
        "hylshi-lake-nightly.service",
        COLLECT,
    ]
    assert health.parse_enospc("-- No entries --\n") == ()


def test_lines_further_apart_than_the_gap_are_separate_episodes() -> None:
    fails = tuple((T0 + s, STREAM) for s in (0, 15, 108, 408, 409 + 300.5, 2000))
    assert health.enospc_episodes(fails) == [
        (T0, T0 + 408, 4),
        (T0 + 709.5, T0 + 709.5, 1),
        (T0 + 2000, T0 + 2000, 1),
    ]
    assert health.enospc_episodes(()) == []


def test_a_silence_is_a_hole_past_two_periods_and_the_open_tail_counts() -> None:
    times = (*_ticks(T0, 4), *_ticks(T0 + 900 + 1500, 3))
    # 318 s (the measured widest) and exactly two periods are the timer.
    assert health.witness_silences((T0, T0 + 318, T0 + 918), T0 + 918 + 600) == []
    assert health.witness_silences(times, times[-1] + 10) == [(T0 + 900, T0 + 2400)]
    # A journal that stopped and never resumed: the hole runs to the read.
    assert health.witness_silences(times, times[-1] + 3600)[-1] == (times[-1], times[-1] + 3600)


def test_a_clean_window_says_zero_only_beside_a_continuous_witness() -> None:
    w = _ticks(T0, 10, 318.0)
    win = health.EnospcWindow(fails=(), witness=w, ends_at=w[-1] + 100)
    assert health.enospc_line(win, free_b=34 * 10**9) == (
        "24h: 0 full-disk write failure lines in any unit's journal | witness: "
        "hyxlab-stream.service journal continuous (widest silence 318 s against a 300 s "
        "stats line)"
    )


def test_zero_failures_over_a_silent_journal_is_not_printed_as_clean() -> None:
    w = (*_ticks(T0, 3), *_ticks(T0 + 600 + 4200, 3))
    line = health.enospc_line(health.EnospcWindow(fails=(), witness=w, ends_at=w[-1] + 60))
    assert "0 full-disk write failure lines" in line
    assert "JOURNAL SILENT 1 time(s), 70.0 min" in line
    assert "(70.0 min)" in line and "failures inside a silence are uncounted" in line
    assert "continuous" not in line


def test_failures_print_units_episodes_and_the_span_as_a_span() -> None:
    fails = (
        *((T0 + 15 * i, STREAM) for i in range(9)),  # 120 s of failed flushes
        (T0 + 100, COLLECT),
        (T0 + 5000, STREAM),
    )
    win = health.EnospcWindow(fails=tuple(sorted(fails)), witness=_ticks(T0, 20), ends_at=T0 + 5800)
    line = health.enospc_line(win)
    assert "FULL-DISK WRITE FAILURES 11 line(s)" in line
    assert "(hyxlab-stream.service 10, hyxlab-collect.service 1)" in line
    assert "in 2 episode(s)" in line and "2.0 min between first and last line" in line
    assert "not a measured outage" in line
    assert "(2.0 min, 10 line(s))" in line
    assert "journal continuous" in line


def test_unread_halves_and_a_journal_under_keep_free_say_so() -> None:
    line = health.enospc_line(health.EnospcWindow(None, None, T0), free_b=3 * 10**9)
    assert line.startswith("24h: journal unreadable | witness (hyxlab-stream.service journal)")
    assert "NOW 3 GB free, under journald's 4 GB keep-free: the journal is not recording" in line
    empty = health.enospc_line(health.EnospcWindow((), (), T0))
    assert "NO hyxlab-stream.service line in the window" in empty and "proves nothing" in empty
    assert "keep-free" not in empty


def test_the_digest_prints_the_enospc_line_with_the_live_free_figure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def fake_read(units, now=None):
        seen["units"] = units
        return health.EnospcWindow(((now - 60, STREAM),), (now - 30,), now)

    monkeypatch.setattr(health, "read_enospc", fake_read)
    monkeypatch.setattr(health, "read_hostfs", lambda: health.HostFs(2 * 10**9, 2 * 10**12))
    monkeypatch.setattr(health, "discover_units", lambda: [(STREAM, False)])
    monkeypatch.setattr(health, "read_unit_failures", lambda units: [])
    monkeypatch.setattr(health, "read_unit_jobs", lambda units: {})
    monkeypatch.setattr(health, "live_cgmem", lambda units: [])
    monkeypatch.setattr(health, "journal_floor", lambda: None)
    monkeypatch.setattr(health, "drift_report", lambda: [])
    monkeypatch.setattr(health, "show", lambda unit: {})
    monkeypatch.setattr(health, "judge_qa", lambda rec, svc, now: "QA (stubbed)")
    health.main()
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("ENOSPC"))
    assert "FULL-DISK WRITE FAILURES 1 line(s) (hyxlab-stream.service 1)" in line
    assert "NOW 2 GB free" in line
    assert seen["units"] == {STREAM}
