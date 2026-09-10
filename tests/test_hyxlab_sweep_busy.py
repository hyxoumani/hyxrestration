"""A busy archive skips one series; it does not kill the sweep (2026-09-10).

The 09-10 06:10Z run died at series ~600 of 3,656 with a bare
`duckdb.IOException` out of `writer_burst`: a reader held
data/hyxlab.duckdb past the 300s open budget, `open_retry` re-raised,
and the traceback went straight through `run_sweep` and `main`. Nothing
was corrupt and nothing was lost -- `sweep_series` advances the
watermark only in its final burst -- but 3,000 untouched series waited a
day for a contention that cleared in minutes, and the unit went red.

The loop already had a graceful story for the FAR less recoverable
failure (venue degradation: count, log, break at ABORT_CONSEC_ERRORS,
exit 75, resume tomorrow) and none at all for the local one.
`hyxlab-collect` meets the identical condition and treats it as a skip.
These tests hold the two writers to the same rule, and hold the shorter
fuse: each skip has already spent the full 300s budget waiting, so an
unbroken run of them is a held file, not contention.
"""

from contextlib import contextmanager

import duckdb
import pytest
import requests

from collector import sweep as sweep_mod


class _FakeLock:
    """main() only ever close()s the instance lock."""

    def close(self):
        pass


class _StubStore:
    def __init__(self):
        self.logged = []

    def log_sweep(self, *args):
        self.logged.append(args)

    def counts(self):
        return {"markets": 0}

    def close(self):
        pass


def _wire(monkeypatch, sweep_series_fn, n=100):
    store = _StubStore()

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield store

    targets = [{"ticker": f"S{i:03d}", "category": "Test"} for i in range(n)]
    monkeypatch.setattr(sweep_mod.kalshi, "get_series_list", lambda sess: [])
    monkeypatch.setattr(sweep_mod, "refresh_series", lambda store, sess, series=None: targets)
    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod, "sweep_series", sweep_series_fn)
    return store


def _busy():
    return duckdb.IOException(
        'IO Error: Could not set lock on file "data/hyxlab.duckdb":'
        " Conflicting lock is held in /usr/bin/python3.14 (PID 2608548)"
    )


def test_one_busy_series_is_skipped_and_the_rest_still_run(monkeypatch):
    # The regression itself: series 7 loses the open, and the remaining
    # 92 must still be swept rather than dying with it.
    attempted = []

    def busy_once(db, ticker, days, sess, max_markets):
        attempted.append(ticker)
        if ticker == "S007":
            raise _busy()
        return 1, 1, False

    _wire(monkeypatch, busy_once)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])

    assert totals["aborted"] is False
    assert totals["lock_skips"] == 1
    assert len(attempted) == 100
    assert totals["markets"] == 99
    # A busy archive is not a venue error: the breaker must not see it.
    assert totals["errors"] == 0


def test_scattered_busy_opens_never_trip_the_short_fuse(monkeypatch):
    # Alternating busy/success is the densest still-organic contention
    # pattern: far more skips than the threshold, no unbroken run.
    calls = {"n": 0}

    def flaky(db, ticker, days, sess, max_markets):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            raise _busy()
        return 1, 1, False

    _wire(monkeypatch, flaky)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])

    assert totals["aborted"] is False
    assert totals["lock_skips"] == 50
    assert totals["lock_skips"] > sweep_mod.ABORT_CONSEC_LOCK_SKIPS
    assert calls["n"] == 100


def test_an_unbroken_busy_run_aborts_rather_than_waiting_out_the_list(monkeypatch):
    # A permanently held file is not contention. Each skip has already
    # burned the 300s budget, so walking all 3,656 series would spend ~12
    # days waiting; the fuse stops it and exit 75 makes systemd say so.
    attempted = []

    def always_busy(db, ticker, days, sess, max_markets):
        attempted.append(ticker)
        raise _busy()

    _wire(monkeypatch, always_busy)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])

    assert totals["aborted"] is True
    assert totals["lock_skips"] == sweep_mod.ABORT_CONSEC_LOCK_SKIPS
    assert len(attempted) == sweep_mod.ABORT_CONSEC_LOCK_SKIPS


def test_the_busy_fuse_is_shorter_than_the_venue_one_in_wall_clock():
    # Not a style preference: a venue error returns in HTTP time, a busy
    # open only after the full budget. Equal counts are not equal waits,
    # so the busy fuse must be strictly shorter, and short enough that an
    # abort is minutes rather than hours.
    budget_s = sweep_mod.BURST_OPEN_RETRIES * sweep_mod.BURST_OPEN_DELAY_S
    assert sweep_mod.ABORT_CONSEC_LOCK_SKIPS < sweep_mod.ABORT_CONSEC_ERRORS
    assert sweep_mod.ABORT_CONSEC_LOCK_SKIPS * budget_s <= 30 * 60


def test_a_success_rearms_the_busy_fuse(monkeypatch):
    # One good series must reset the count, or a long run accumulates
    # scattered skips into a false abort.
    seq = iter([True] * (sweep_mod.ABORT_CONSEC_LOCK_SKIPS - 1) + [False] * 96)

    def scripted(db, ticker, days, sess, max_markets):
        if next(seq):
            raise _busy()
        return 1, 1, False

    _wire(monkeypatch, scripted)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])

    assert totals["aborted"] is False


def test_a_busy_audit_write_does_not_kill_the_run_either(monkeypatch):
    # The venue-error branch opens its OWN burst to log the failure. That
    # open can lose the same race, and used to escape from inside the
    # handler -- a venue outage plus a reader was still a fatal sweep.
    class _BusyLogStore(_StubStore):
        def log_sweep(self, *args):
            raise _busy()

    store = _BusyLogStore()

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield store

    targets = [{"ticker": f"S{i:03d}", "category": "Test"} for i in range(100)]
    monkeypatch.setattr(sweep_mod.kalshi, "get_series_list", lambda sess: [])
    monkeypatch.setattr(sweep_mod, "refresh_series", lambda store, sess, series=None: targets)
    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)

    calls = {"n": 0}

    def flaky(db, ticker, days, sess, max_markets):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            raise requests.RequestException("503")
        return 1, 1, False

    monkeypatch.setattr(sweep_mod, "sweep_series", flaky)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"])

    assert calls["n"] == 100
    assert totals["errors"] == 50


def test_a_busy_closing_census_does_not_retitle_a_finished_run(monkeypatch, capsys):
    # Every series is persisted by the time main takes its census burst.
    # Losing THAT open used to exit 1 on a run that did all of its work.
    monkeypatch.setattr(sweep_mod, "run_sweep", lambda *a, **k: {"series": 3, "aborted": False})

    @contextmanager
    def busy_burst(db, lock_file=None):
        raise _busy()
        yield  # pragma: no cover

    monkeypatch.setattr(sweep_mod, "writer_burst", busy_burst)
    monkeypatch.setattr(sweep_mod, "instance_lock_or_reason", lambda name: (_FakeLock(), None))
    monkeypatch.setattr("sys.argv", ["sweep"])

    sweep_mod.main()  # must not raise and must not SystemExit

    out = capsys.readouterr().out
    assert "db=busy" in out


def test_an_aborted_run_still_exits_temp_fail(monkeypatch):
    # The exit contract the new abort path rides on.
    monkeypatch.setattr(sweep_mod, "run_sweep", lambda *a, **k: {"series": 3, "aborted": True})

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield _StubStore()

    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod, "instance_lock_or_reason", lambda name: (_FakeLock(), None))
    monkeypatch.setattr("sys.argv", ["sweep"])

    with pytest.raises(SystemExit) as e:
        sweep_mod.main()
    assert e.value.code == 75


def test_the_skips_land_in_sweep_log_so_an_instrument_can_see_them(monkeypatch):
    # The #46 rule: a partial sweep that reports green must leave a trace
    # something other than a human reading the journal can find. The skip
    # itself cannot write one -- the burst it needs is the burst that
    # failed -- so main replays them into the closing burst, where
    # `doctor`'s "sweep_log (48h)" by-status census picks them up.
    store = _StubStore()

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield store

    def busy_two(db, ticker, days, sess, max_markets):
        if ticker in ("S003", "S011"):
            raise _busy()
        return 1, 1, False

    targets = [{"ticker": f"S{i:03d}", "category": "Test"} for i in range(20)]
    monkeypatch.setattr(sweep_mod.kalshi, "get_series_list", lambda sess: [])
    monkeypatch.setattr(sweep_mod, "refresh_series", lambda store, sess, series=None: targets)
    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod, "sweep_series", busy_two)
    monkeypatch.setattr(sweep_mod, "instance_lock_or_reason", lambda name: (_FakeLock(), None))
    monkeypatch.setattr("sys.argv", ["sweep", "--categories", "Test"])

    sweep_mod.main()

    busy_rows = [r for r in store.logged if r[5] == "busy"]
    assert [r[0] for r in busy_rows] == ["S003", "S011"]
    assert all("Conflicting lock" in r[6] for r in busy_rows)


def test_the_ticker_list_never_reaches_the_journal_line(monkeypatch, capsys):
    # 3,000 skipped tickers inline would bury the totals line that the
    # digest and every past pass read. The list is popped before the print.
    monkeypatch.setattr(
        sweep_mod,
        "run_sweep",
        lambda *a, **k: {
            "series": 3,
            "lock_skips": 2,
            "lock_skipped": [("S001", "x")],
            "aborted": False,
        },
    )

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield _StubStore()

    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod, "instance_lock_or_reason", lambda name: (_FakeLock(), None))
    monkeypatch.setattr("sys.argv", ["sweep"])

    sweep_mod.main()

    done = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[sweep] done:")]
    assert len(done) == 1
    assert "lock_skips" in done[0]  # the COUNT stays
    assert "S001" not in done[0]  # the LIST does not
