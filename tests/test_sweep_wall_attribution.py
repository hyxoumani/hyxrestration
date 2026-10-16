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
