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
