"""The tail-stop ledger's STATUS column, read.

`trades_tail` records a stop's HTTP status "because the classes have different
remedies", and until this check nothing read it: on 2026-11-03 every one of
7,871 stops carried 410 (Data API v1 retired, mistakes #154), the run archived
0 trades and filed `errors: 0`. `qa_poly_tail_absorbed` reads depth after a
grace and an endpoint-wide failure re-visits every market at the same zero
depth, so it could not see it either.
"""

from datetime import UTC, datetime, timedelta

import pytest

from collector import qa
from hyxlab.store import Store

NOW = datetime(2026, 11, 4, 10, 0, tzinfo=UTC).replace(tzinfo=None)
NAME = "polymarket tail stops in the absorbed classes and band"


@pytest.fixture(autouse=True)
def _clean_ledger():
    qa._failures.clear()
    qa._ran.clear()
    yield
    qa._failures.clear()
    qa._ran.clear()


def _run(tmp_path, stops):
    store = Store(tmp_path / "a.duckdb")
    if stops:
        store.insert_poly_tail_stops(stops)
    qa.qa_poly_tail_stop_classes(store.conn, NOW)
    store.close()


def test_empty_window_is_green(tmp_path):
    _run(tmp_path, [])
    assert qa._failures == []
    assert NAME in qa._ran


def test_absorbed_classes_inside_band_pass(tmp_path, capsys):
    recent = NOW - timedelta(hours=2)
    stops = [(f"0x{i:04x}", recent, 500, 429) for i in range(40)]
    stops += [(f"0xb{i:03x}", recent, 0, 408) for i in range(5)]
    _run(tmp_path, stops)
    assert qa._failures == []
    assert "429 x40, 408 x5" in capsys.readouterr().out


def test_a_foreign_class_fails_and_is_named(tmp_path, capsys):
    """The 11-03 shape: one stop carrying 410 is a new failure, whatever
    the count."""
    recent = NOW - timedelta(hours=5)
    _run(tmp_path, [("0xaa", recent, 0, 410), ("0xbb", recent, 500, 429)])
    assert qa._failures == [NAME]
    out = capsys.readouterr().out
    assert "1 of 2 stop(s)" in out and "410 x1" in out


def test_a_null_status_is_foreign(tmp_path, capsys):
    """Rows from before the column existed, or a stop the recorder could not
    classify: not an absorbed class, and the detail says so."""
    _run(tmp_path, [("0xaa", NOW - timedelta(hours=1), 100, None)])
    assert qa._failures == [NAME]
    assert "none x1" in capsys.readouterr().out


def test_absorbed_class_past_the_band_fails(tmp_path, capsys):
    recent = NOW - timedelta(hours=3)
    stops = [(f"0x{i:05x}", recent, 0, 429) for i in range(qa.POLY_TAIL_STOP_BUDGET + 1)]
    _run(tmp_path, stops)
    assert qa._failures == [NAME]
    assert "unmeasured at this rate" in capsys.readouterr().out


def test_the_prior_days_run_is_outside_the_window(tmp_path):
    """At the 10:00Z QA the prior day's 04:19Z stops are ~30h old; the 11-03
    410 wall must not keep the check red once the next run is clean."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_STOP_WINDOW_H + 1)
    _run(tmp_path, [(f"0x{i:04x}", old, 0, 410) for i in range(50)])
    assert qa._failures == []


def test_budget_sits_above_the_measured_worst_clean_day():
    """446 stops on 2026-10-01 was a clean day; the band edge must not
    re-read it as a failure."""
    assert qa.POLY_TAIL_STOP_BUDGET > 446
