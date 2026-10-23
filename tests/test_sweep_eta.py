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
    monkeypatch.setattr(sweep_mod, "prior_series_wall", lambda store, now=None, run=None: prior)
    monkeypatch.setattr(sweep_mod, "PROGRESS_EVERY", 2)
    sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "min left" in ln]
    assert len(lines) == 3  # at 2, 4 and 6 of 7
    # after A+B (3s today vs 6s prior -> x0.50) the rest's prior is 1384s,
    # so ~692s = 11.5 min; the series-rate line would have said 5 x 1.5s
    assert lines[0] == (
        "[sweep] 2/7 series | 2 markets, 0 candles, 0 errors, 0 truncated"
        " | ~12 min left (prior-run weighted x0.50, 0 unpriced)"
        " | done: ladders 0/0 mkts 0.000 s/mkt, small 0 min, dense 0 min"
    )


def test_the_progress_line_carries_the_class_terms_of_the_series_done_so_far():
    """Read against yesterday's line at the same index, the tag separates
    LISTINGS (more ladder markets at the same s/mkt) from PACE (the same
    markets at a higher s/mkt) -- the ETA's scale cannot (mistakes #130).
    Each term perturbs the tag, so none can be read into a local and dropped."""
    base = {
        "ladder": {"n": 2, "wall_s": 6000.0, "markets": 12000},
        "dense": {"n": 1, "wall_s": 2700.0, "markets": 96, "tickers": ["KXBTC15M"]},
        "small": {"n": 500, "wall_s": 1200.0, "markets": 600},
    }
    tag = sweep_mod.class_terms_tag(base)
    assert tag == "done: ladders 2/12000 mkts 0.500 s/mkt, small 20 min, dense 45 min"
    for cls, key, bump in (
        ("ladder", "markets", 3000),  # listings grew
        ("ladder", "wall_s", 600.0),  # pace fell
        ("ladder", "n", 1),
        ("small", "wall_s", 600.0),
        ("dense", "wall_s", 600.0),
    ):
        mutant = {k: dict(v) for k, v in base.items()}
        mutant[cls][key] += bump
        assert sweep_mod.class_terms_tag(mutant) != tag, (cls, key)


def test_the_progress_line_names_the_series_rate_when_there_is_no_prior(monkeypatch, capsys):
    from tests.test_sweep_wall_attribution import _wire

    _wire(monkeypatch)  # its fake store returns no sweep_log rows
    monkeypatch.setattr(sweep_mod, "PROGRESS_EVERY", 2)
    sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "min left" in ln]
    assert lines and all("min left (series-rate) | done: ladders" in ln for ln in lines)


def test_the_listings_delta_is_against_the_prior_runs_series_set_not_its_wall_dict(tmp_path):
    """A run's first series has no wall, so a delta taken from the wall dict
    would name it a new listing every day; the set has it. Measured
    2026-10-21: 84 series the 10-20 run never swept, legible only as
    `68 unpriced` until this landed."""
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        for s, off in (("A", 0), ("B", 60), ("C", 2760)):
            _log(store, s, T0 + timedelta(seconds=off))
        run = sweep_mod._prior_run(store, now=T0 + timedelta(hours=24))
        assert sweep_mod.prior_series_wall(store, run=run) == {"B": 60.0, "C": 2700.0}
        delta = sweep_mod.listings_delta(["A", "B", "KXNEW", "KXNEW2"], run)
    finally:
        store.close()
    assert delta == {"targets": 4, "prior": 3, "new": ["KXNEW", "KXNEW2"], "dropped": ["C"]}


def test_no_prior_run_is_no_delta_never_everything_new(tmp_path):
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        assert sweep_mod._prior_run(store) == []
        assert sweep_mod.listings_delta(["A", "B"], []) is None
    finally:
        store.close()
    assert sweep_mod.listings_line(None) == "[sweep] listings: no prior run in the lookback"


def test_the_listings_line_names_the_delta_and_every_field_perturbs_it():
    base = {"targets": 4031, "prior": 3963, "new": ["KXFTSED", "KXHSID"], "dropped": []}
    line = sweep_mod.listings_line(base)
    assert line == (
        "[sweep] listings: 4031 targets vs 3963 swept last run"
        " | +2 new (KXFTSED, KXHSID) | -0 dropped"
    )
    for key, val in (
        ("targets", 4032),
        ("prior", 3962),
        ("new", ["KXFTSED"]),
        ("dropped", ["KXGONE"]),
    ):
        mutant = dict(base)
        mutant[key] = val
        assert sweep_mod.listings_line(mutant) != line, key
    many = dict(base, new=[f"KX{i:02d}" for i in range(sweep_mod.LISTINGS_NAMED + 3)])
    assert ", ... +3)" in sweep_mod.listings_line(many)


def test_the_run_prints_the_listings_line_first_and_the_done_dict_carries_the_counts(
    monkeypatch, capsys
):
    from tests.test_sweep_wall_attribution import COST, _wire

    _wire(monkeypatch)
    run = [(t, T0 + timedelta(seconds=i)) for i, t in enumerate(["ZZOLD", *sorted(COST)][:-1])]
    monkeypatch.setattr(sweep_mod, "_prior_run", lambda store, now=None: run)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        "[sweep] listings: 7 targets vs 7 swept last run | +1 new (LADDER) | -1 dropped (ZZOLD)"
    )
    assert (totals["new_series"], totals["dropped_series"]) == (1, 1)


def test_a_first_run_reports_none_for_the_listings_counts(monkeypatch, capsys):
    from tests.test_sweep_wall_attribution import _wire

    _wire(monkeypatch)  # its fake store has no archive: no prior run
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])
    assert (totals["new_series"], totals["dropped_series"]) == (None, None)
    assert (
        capsys.readouterr().out.splitlines()[0] == "[sweep] listings: no prior run in the lookback"
    )
