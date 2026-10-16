"""The sweep's `~N min left` is weighted by the previous run's per-series wall
(2026-10-16).

The run's cost is per MARKET and the old ETA was per SERIES; with the ~7k-market
crypto ladders past index 3000 the line read `~152 min left` at 3100/3958 on
10-16 against 190 actual, and `~330` at 400 against 703. Replaying that run
with the 10-15 `sweep_log` differences as the prior landed within 5-14% from
index 400 on. These tests pin the prior read and the estimate.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from collector import sweep as sweep_mod
from collector.sweep import eta_minutes, prior_series_wall
from hyxlab.store import Store

T0 = datetime(2026, 10, 15, 6, 11)


def _log(store, series, at):
    store.conn.execute(
        "INSERT INTO sweep_log VALUES (?,?,?,?,?,?,?,?)",
        [series, at, None, None, 1, 0, "ok", ""],
    )


def test_prior_is_the_consecutive_difference_within_the_largest_recent_run(tmp_path):
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        # yesterday's daily run: A (start, no wall), B 60s, C 2700s, D 1s
        for s, off in (("A", 0), ("B", 60), ("C", 2760), ("D", 2761)):
            _log(store, s, T0 + timedelta(seconds=off))
        # a hand `--series` repair 5h later: a run of two rows, not the day
        _log(store, "C", T0 + timedelta(hours=5))
        _log(store, "D", T0 + timedelta(hours=5, seconds=9))
        # the run before yesterday, outside the lookback
        _log(store, "B", T0 - timedelta(hours=24))
        _log(store, "C", T0 - timedelta(hours=24) + timedelta(seconds=5))
        now = T0 + timedelta(hours=24)
        prior = prior_series_wall(store, now=now)
    finally:
        store.close()
    assert prior == {"B": 60.0, "C": 2700.0, "D": 1.0}


def test_prior_is_empty_when_sweep_log_has_no_recent_rows_or_cannot_be_read(tmp_path):
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        assert prior_series_wall(store) == {}
        store.conn.execute("DROP TABLE sweep_log")
        assert prior_series_wall(store) == {}  # advisory: a failed read is no prior
    finally:
        store.close()


def test_eta_weights_the_remainder_by_prior_wall_scaled_by_todays_pace():
    prior = {"A": 10.0, "B": 10.0, "BIG": 6000.0, "C": 10.0}
    # two 10s series took 24s today -> pace x1.20; BIG (6000s) and C (10s) remain
    eta, tag = eta_minutes(["A", "B"], ["BIG", "C"], 24.0, prior)
    assert eta == pytest.approx((6000.0 + 10.0) * 1.2 / 60)
    assert tag == "prior-run weighted x1.20, 0 unpriced"
    # the series-rate estimate would have said 2 more series = 24s
    assert eta > 100


def test_a_series_without_a_prior_costs_the_median_and_is_counted():
    prior = {"A": 10.0, "B": 20.0, "C": 30.0}
    eta, tag = eta_minutes(["A"], ["NEW", "C"], 10.0, prior)
    assert eta == pytest.approx((20.0 + 30.0) / 60)  # NEW at the median 20s, pace x1.00
    assert tag.endswith("1 unpriced")


def test_without_a_prior_the_estimate_is_the_series_rate_and_says_so():
    eta, tag = eta_minutes(["A", "B"], ["C", "D", "E"], 60.0, {})
    assert eta == pytest.approx(3 / (2 / 60.0) / 60)
    assert tag == "series-rate"
    assert eta_minutes([], ["C"], 0.0, {"C": 5.0}) == (0.0, "series-rate")


def test_the_progress_line_carries_the_weighted_eta_and_its_tag(monkeypatch, capsys):
    from tests.test_sweep_wall_attribution import COST, _wire

    _wire(monkeypatch)
    names = sorted(COST)  # the loop sorts: A, B, BIG, C, D, E, LADDER
    prior = {t: COST[t] * 2 for t in names}  # yesterday was twice as slow
    monkeypatch.setattr(sweep_mod, "prior_series_wall", lambda store, now=None: prior)
    monkeypatch.setattr(sweep_mod, "PROGRESS_EVERY", 2)
    sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "min left" in ln]
    assert len(lines) == 3  # at 2, 4 and 6 of 7
    # after A+B (3s today vs 6s prior -> x0.50) the rest's prior is 1384s,
    # so ~692s = 11.5 min; the series-rate line would have said 5 x 1.5s
    assert lines[0] == (
        "[sweep] 2/7 series | 2 markets, 0 candles, 0 errors, 0 truncated"
        " | ~12 min left (prior-run weighted x0.50, 0 unpriced)"
    )


def test_the_progress_line_names_the_series_rate_when_there_is_no_prior(monkeypatch, capsys):
    from tests.test_sweep_wall_attribution import _wire

    _wire(monkeypatch)  # its fake store returns no sweep_log rows
    monkeypatch.setattr(sweep_mod, "PROGRESS_EVERY", 2)
    sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "min left" in ln]
    assert lines and all(ln.endswith("min left (series-rate)") for ln in lines)
