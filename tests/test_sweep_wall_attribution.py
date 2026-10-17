"""The sweep names where its wall clock went, per series (2026-10-16).

`qa.BATCH_RUN_BUDGET_H` fails a sweep past 12.5h and the 10-15 chase ruled
that the response to a breach is a per-market cost or a category decision,
never a budget bump -- a decision that needs the per-series cost, which no
instrument recorded. Hand-differencing `sweep_log.swept_at` over the 10-15
run reproduced its done line (655.4 vs 655.7 min) and found KXBTC15M at
46.9 min for 91 markets (30.9 s/market) beside ladders at a flat 0.45.
These tests pin the line that makes that read automatic.
"""

from __future__ import annotations

from contextlib import contextmanager

from collector import sweep as sweep_mod

COST = {"BIG": 600.0, "LADDER": 80.0, "A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0, "E": 5.0}
MARKETS = {"BIG": 20, "LADDER": 200, "A": 1, "B": 1, "C": 1, "D": 1, "E": 1}


class _Store:
    def log_sweep(self, *args):
        pass

    def counts(self):
        return {}

    def close(self):
        pass


def _wire(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(sweep_mod.time, "monotonic", lambda: clock[0])

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield _Store()

    def fake_series(db, ticker, days, sess, budget, **kw):
        clock[0] += COST[ticker]
        return MARKETS[ticker], 0, False

    monkeypatch.setattr(sweep_mod.kalshi, "get_series_list", lambda sess: [])
    targets = [{"ticker": t, "category": "Test"} for t in COST]
    monkeypatch.setattr(sweep_mod, "refresh_series", lambda store, sess, series=None: targets)
    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod, "sweep_series", fake_series)
    return clock


def test_the_done_dict_and_the_line_name_the_slowest_series_with_their_rate(monkeypatch, capsys):
    _wire(monkeypatch)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])
    assert totals["slowest"][0] == ("BIG", 600.0, 20)
    assert [t for t, _, _ in totals["slowest"]] == ["BIG", "LADDER", "E", "D", "C"]
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "wall attribution" in ln)
    # the expensive-per-market series and the many-markets series read apart
    assert "BIG 10.0min/20 mkts (30.0 s/mkt)" in line
    assert "LADDER 1.3min/200 mkts (0.4 s/mkt)" in line
    # the share is of the RUN, and the run here is exactly the series sum
    assert "top 5 of 7 series = 11.5 of 11.6 min (100%)" in line


def test_the_share_is_of_the_whole_run_not_of_the_series_sum():
    wall = {"X": (60.0, 10), "Y": (30.0, 10)}
    line = sweep_mod.wall_attribution_line(wall, elapsed_s=300.0)
    assert "= 1.5 of 5.0 min (30%)" in line
    assert sweep_mod.wall_attribution_line({}, 1.0).endswith("no series ran")


def test_a_skipped_or_failed_series_is_still_charged_its_wall(monkeypatch):
    import duckdb

    clock = _wire(monkeypatch)

    def busy(db, ticker, days, sess, budget, **kw):
        clock[0] += 120.0
        raise duckdb.IOException("busy")

    monkeypatch.setattr(sweep_mod, "sweep_series", busy)
    only = [{"ticker": "ONLY", "category": "Test"}]
    monkeypatch.setattr(sweep_mod, "refresh_series", lambda store, sess, series=None: only)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])
    assert totals["slowest"] == [("ONLY", 120.0, 0)]


def test_the_done_dict_and_the_line_split_the_wall_into_the_budget_model_terms(monkeypatch, capsys):
    """The budget headroom is `budget - small - dense` over the ladder rate
    (data-pipeline.md); until 10-17 those terms were a hand read of sweep_log."""
    _wire(monkeypatch)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])
    bc = totals["by_class"]
    assert bc["ladder"] == {"n": 1, "wall_s": 80.0, "markets": 200}
    assert bc["dense"] == {"n": 1, "wall_s": 600.0, "markets": 20, "tickers": ["BIG"]}
    assert bc["small"] == {"n": 5, "wall_s": 15.0, "markets": 5}
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "wall by class" in ln)
    assert "ladders (>=200 mkts) 1 series 1.3 min/200 mkts (0.400 s/mkt)" in line
    assert "dense (>=10 s/mkt) 1 series 10.0 min/20 mkts (30.0 s/mkt) [BIG]" in line
    assert "small 5 series 0.2 min/5 mkts (3.00 s/series)" in line
    assert line.endswith("loop 11.6 of 11.6 min")


def test_a_class_is_decided_by_count_then_rate_and_a_slow_ladder_is_still_a_ladder():
    wall = {
        "LAD": (7000.0, 250),  # 28 s/mkt on many markets: the count wins, a ladder
        "TAPE": (950.0, 95),  # 10.0 s/mkt on 95 markets: dense, at the threshold
        "CATCHUP": (606.0, 192),  # KXSOL15M 10-17: a long wall at 3.2 s/mkt is small
        "FEW": (300.0, 9),  # 33 s/mkt on 9 markets: too few to be a rate, small
        "NEW": (0.5, 0),  # a series with no markets is a 0-market small series
    }
    bc = sweep_mod.wall_by_class(wall)
    assert bc["ladder"] == {"n": 1, "wall_s": 7000.0, "markets": 250}
    assert bc["dense"] == {"n": 1, "wall_s": 950.0, "markets": 95, "tickers": ["TAPE"]}
    assert bc["small"] == {"n": 3, "wall_s": 906.5, "markets": 201}
    empty = sweep_mod.wall_by_class({})
    assert empty["dense"] == {"n": 0, "wall_s": 0.0, "markets": 0, "tickers": []}
    line = sweep_mod.wall_class_line(empty, elapsed_s=120.0)
    assert "dense (>=10 s/mkt) 0 series 0.0 min/0 mkts (0.0 s/mkt);" in line  # no names
    assert line.endswith("loop 0.0 of 2.0 min")
