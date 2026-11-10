"""Sweep consecutive-failure circuit breaker (2026-08-06 outage).

Kalshi's /markets endpoint degraded mid-run (503s, an hour-long 429
storm): every series from ~550 onward errored, the sweep fail-fasted
through 2,569 consecutive failures — ~10k useless requests against a
venue already refusing service — and the unit still reported success.
The breaker aborts after ABORT_CONSEC_ERRORS unbroken failures, leaving
watermarks intact so the next timer firing resumes where this one
stopped. One success resets the count: scattered organic errors must
never trip it.
"""

from contextlib import contextmanager

import requests

from collector import sweep as sweep_mod


class _StubStore:
    def __init__(self):
        self.logged = []

    def log_sweep(self, *args):
        self.logged.append(args)


def _wire(monkeypatch, sweep_series_fn):
    """Point run_sweep at 100 fake series and a no-lock, no-DB store."""
    store = _StubStore()

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield store

    targets = [{"ticker": f"S{i:03d}", "category": "Test"} for i in range(100)]
    monkeypatch.setattr(sweep_mod.kalshi, "get_series_list", lambda sess: [])
    monkeypatch.setattr(sweep_mod, "refresh_series", lambda store, sess, series=None: targets)
    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod, "sweep_series", sweep_series_fn)
    return store


def test_unbroken_error_run_aborts_at_threshold(monkeypatch):
    attempted = []

    def always_down(db, ticker, days, sess, max_markets):
        attempted.append(ticker)
        raise requests.RequestException("429 Too Many Requests")

    store = _wire(monkeypatch, always_down)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])

    assert totals["aborted"] is True
    assert totals["errors"] == sweep_mod.ABORT_CONSEC_ERRORS
    # The remaining 75 series were never requested — that is the point:
    # stop hammering a venue that is refusing service.
    assert len(attempted) == sweep_mod.ABORT_CONSEC_ERRORS
    # Every failure still landed in sweep_log before the abort.
    assert len(store.logged) == sweep_mod.ABORT_CONSEC_ERRORS
    assert all(row[5] == "error" for row in store.logged)


def test_scattered_errors_never_trip_the_breaker(monkeypatch):
    # Alternating error/success is the densest error pattern that is
    # still "organic": total errors far exceed the threshold, but no
    # unbroken run forms, so the sweep must finish all 100 series.
    calls = {"n": 0}

    def flaky(db, ticker, days, sess, max_markets):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            raise requests.RequestException("transient")
        return 1, 1, False

    _wire(monkeypatch, flaky)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])

    assert totals["aborted"] is False
    assert totals["errors"] == 50
    assert totals["errors"] > sweep_mod.ABORT_CONSEC_ERRORS
    assert calls["n"] == 100


def test_an_outage_mid_run_costs_one_wait_not_every_series_behind_it(monkeypatch):
    """2026-11-09: a 13-min drop burned 21 series in 5 min, each failing fast
    into the same dead network. The loop now waits the outage out and re-runs
    the series it interrupted (mistakes #172)."""
    from collector import netwait

    down = {"until": 3}  # probes that fail before the venue is back
    calls = []

    def probe(host, timeout_s=5.0):
        down["until"] -= 1
        return down["until"] < 0

    def drops_once(db, ticker, days, sess, max_markets):
        calls.append(ticker)
        if calls == ["S000", "S001", "S002"]:
            raise requests.ConnectionError("NameResolutionError")
        return 1, 0, False

    monkeypatch.setattr(netwait, "_reachable", probe)
    monkeypatch.setattr(netwait.time, "sleep", lambda s: None)
    store = _wire(monkeypatch, drops_once)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])

    assert calls.count("S002") == 2 and len(calls) == 101
    assert totals["errors"] == 0 and totals["markets"] == 100
    assert store.logged == []
    assert totals["net_pauses"]["n"] == 1
    assert totals["net_pauses"]["healed"] == 1
    assert totals["net_pauses"]["recovered"] == 1


def test_a_run_that_met_no_outage_says_so_on_its_done_line(monkeypatch):
    _wire(monkeypatch, lambda db, ticker, days, sess, max_markets: (1, 0, False))
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])
    assert totals["net_pauses"] == {"n": 0, "healed": 0, "recovered": 0, "waited_s": 0.0}
