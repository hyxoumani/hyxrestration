"""A truncated kalshi series must be catching up, not falling behind.

Measured 2026-11-10: KXHYPE/KXHYPED truncated on every run for 52 days, the
frontier receding 14 h/day to ~29 days behind, while the sweep printed
"resumes next run" and no check read the lag (`qa.qa_sweep_frontier`).
"""

from datetime import UTC, datetime, timedelta

import pytest

from collector import qa
from hyxlab.store import Store

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=UTC).replace(tzinfo=None)
NAME = "kalshi sweep frontiers keep up with their series"


@pytest.fixture(autouse=True)
def _clean_ledger():
    qa._failures.clear()
    qa._ran.clear()
    yield
    qa._failures.clear()
    qa._ran.clear()


def _run(tmp_path, rows):
    """rows: (series, days_ago, lag_h, status[, note])"""
    store = Store(tmp_path / "a.duckdb")
    for series, days_ago, lag_h, status, *note in rows:
        swept = NOW - timedelta(days=days_ago, hours=6)
        store.conn.execute(
            "INSERT INTO sweep_log VALUES (?,?,?,?,?,?,?,?)",
            [series, swept, None, swept - timedelta(hours=lag_h), 3000, 10, status, *(note or [""])],
        )
    qa.qa_sweep_frontier(store.conn, NOW)
    store.close()


def _receding(series="KXHYPE", start_lag=600.0, per_day=14.0, days=6):
    return [(series, d, start_lag + per_day * (days - 1 - d), "truncated") for d in range(days)]


def test_empty_log_is_green(tmp_path):
    _run(tmp_path, [])
    assert qa._failures == []
    assert NAME in qa._ran


def test_the_measured_shape_fails_per_series_with_rate_and_horizon(tmp_path, capsys):
    _run(tmp_path, _receding("KXHYPE", 620.0) + _receding("KXHYPED", 647.0))
    assert qa._failures == [NAME]
    out = capsys.readouterr().out
    assert "2 series falling behind" in out
    assert "KXHYPE 690h behind, +14.0 h/day over 6 truncated run(s)" in out
    assert "KXHYPED 717h behind" in out
    # (1440 - 690) / 14 = 53.6
    assert "reaches the 60 d purge horizon in 54 d" in out


def test_a_catch_up_day_does_not_fail_and_is_named(tmp_path, capsys):
    """11-10's KXETH: ok all week, one truncated run 14 h behind."""
    rows = [("KXETH", d, 6.0, "ok") for d in range(1, 6)] + [("KXETH", 0, 14.0, "truncated")]
    _run(tmp_path, rows)
    assert qa._failures == []
    assert "KXETH 14h behind after 1 truncated run(s)" in capsys.readouterr().out


def test_a_long_streak_that_is_closing_does_not_fail(tmp_path):
    """Truncated every run but the lag SHRINKS: the budget is above the
    density and the backlog is draining."""
    _run(tmp_path, _receding(per_day=-20.0))
    assert qa._failures == []


def test_a_growing_streak_under_the_floor_does_not_fail(tmp_path):
    _run(tmp_path, _receding(start_lag=10.0, per_day=5.0))
    assert qa._failures == []


def test_an_ok_run_ends_the_streak(tmp_path):
    rows = _receding(days=6)
    rows[0] = ("KXHYPE", 0, 700.0, "ok")
    _run(tmp_path, rows)
    assert qa._failures == []


def test_past_the_horizon_reads_pinned_not_a_negative_countdown(tmp_path, capsys):
    _run(tmp_path, _receding(start_lag=1500.0))
    assert qa._failures == [NAME]
    out = capsys.readouterr().out
    assert "PINNED at the 60 d purge horizon" in out and "in -" not in out


def test_repair_passes_are_not_frontier_readings(tmp_path):
    """A repair pass re-walks old range without moving the watermark: its
    max_close is old by construction."""
    rows = [("KXBTC", d, 6.0, "ok") for d in range(3, 6)]
    rows += [
        ("KXBTC", d, 900.0 + 30 * (2 - d), "truncated", "repair pass (watermark untouched)")
        for d in range(3)
    ]
    _run(tmp_path, rows)
    assert qa._failures == []


def test_rows_older_than_the_window_do_not_vote(tmp_path):
    rows = [("KXOLD", d, 100.0 + 14 * (20 - d), "truncated") for d in range(9, 15)]
    _run(tmp_path, rows)
    assert qa._failures == []
