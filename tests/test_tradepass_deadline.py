"""Tradepass deadline + flush-open retry (EXP-964).

Two ways the 2026-08-03/04 runs broke the retro-pass's contract:

1. The worklist is unbounded. The sweep's first full crypto pass queued
   ~43k settled tapes overnight, turning a 5-minute unit into 15h06m —
   3.69h of it inside the 23:00Z fade window, spending Kalshi quota the
   live agent's hours assume is free. The pass is per-market resumable
   (trades_swept) and ordered oldest-close-first, so a wall-clock
   deadline costs only calendar days.

2. `_flush` opened the store bare. The flock excludes other WRITERS,
   but DuckDB refuses a read-write open while any read-only holder (QA,
   doctor, simui — none of which flock) is attached; the 08-04 run died
   at 26,000/42,978 markets on exactly that. `open_retry`'s docstring
   had already named this failure mode.
"""

import functools
import threading
from datetime import datetime

import duckdb
import pytest

import collector.qa as qa
import collector.trades_backfill as tb
from hyxlab.store import Store, open_retry


def _seed_settled_markets(db: str, n: int) -> list[str]:
    tickers = [f"KXT-{i}" for i in range(n)]
    store = Store(db)
    try:
        for i, t in enumerate(tickers):
            store.conn.execute(
                "INSERT INTO markets (venue, market_id, series, close_time, result)"
                " VALUES ('kalshi', ?, 'KXT', ?, 'yes')",
                [t, datetime(2026, 7, 1 + i)],
            )
    finally:
        store.close()
    return tickers


class _FakeClock:
    """Deterministic monotonic time; sleeps advance it instead of waiting."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run_main(tmp_path, monkeypatch, *, n_markets, deadline_min, fetch_cost_s):
    # `main` takes the instance lock under the RELATIVE `data/`, so without
    # this the test contends with the production `hyxlab-tradepass` run --
    # and since #107 made that run hold its lock for hours, the suite went
    # red (SystemExit 75) whenever the timer was mid-pass.
    monkeypatch.chdir(tmp_path)
    clock = _FakeClock()
    db = str(tmp_path / "t.duckdb")
    tickers = _seed_settled_markets(db, n_markets)
    monkeypatch.setattr(tb, "LOCK_FILE", str(tmp_path / "writer.lock"))
    monkeypatch.setattr(tb, "time", clock)

    def get_trades(ticker, session=None):
        clock.t += fetch_cost_s
        return [], False

    monkeypatch.setattr(tb.kalshi, "get_trades", get_trades)
    monkeypatch.setattr(
        "sys.argv",
        ["tradepass", "--db", db, "--deadline-min", str(deadline_min)],
    )
    tb.main()
    store = Store(db, read_only=True)
    try:
        swept = {r[0] for r in store.conn.execute("SELECT market_id FROM trades_swept").fetchall()}
    finally:
        store.close()
    return tickers, swept


def test_deadline_stops_the_pass_and_leaves_the_rest_pending(tmp_path, monkeypatch, capsys):
    """Each fetch costs 120 fake-seconds against a 3-minute deadline: the
    third market must not be fetched, and the unfetched tail must stay
    unmarked in trades_swept so the next run picks it up."""
    _, swept = _run_main(tmp_path, monkeypatch, n_markets=5, deadline_min=3, fetch_cost_s=120.0)

    assert len(swept) == 2, f"expected 2 markets before the deadline, got {swept}"
    out = capsys.readouterr().out
    assert "3 markets stay pending" in out
    assert "'remaining': 3" in out, "the done-line must record what was left"


def test_deadline_zero_disables_the_cutoff(tmp_path, monkeypatch, capsys):
    """Manual full drains (--deadline-min 0) must run to exhaustion."""
    tickers, swept = _run_main(
        tmp_path, monkeypatch, n_markets=5, deadline_min=0, fetch_cost_s=120.0
    )

    assert swept == set(tickers)
    assert "stay pending" not in capsys.readouterr().out


def test_default_deadline_fits_the_qa_run_budget():
    """The budget check reads the journal daily; the deadline is what makes
    its constant TRUE rather than merely written down. Slack absorbs
    startup, 429 backoffs and the final in-flight market + flush."""
    budget_h = qa.BATCH_RUN_BUDGET_H["hyxlab-tradepass.timer"]
    assert budget_h - 0.4 >= tb.DEADLINE_MIN / 60


def test_flush_waits_out_a_read_only_holder(tmp_path, monkeypatch):
    """Regression pin for the 08-04 crash: a read-only connection is
    attached when the flush starts and released while it retries. A bare
    Store() open dies immediately; the flush must wait and then land."""
    db = str(tmp_path / "t.duckdb")
    _seed_settled_markets(db, 1)
    monkeypatch.setattr(tb, "LOCK_FILE", str(tmp_path / "writer.lock"))
    monkeypatch.setattr(tb, "open_retry", functools.partial(open_retry, delay=0.05))

    reader = duckdb.connect(db, read_only=True)
    release = threading.Timer(0.4, reader.close)
    release.start()
    try:
        tb._flush(db, [("KXT-0", [], "empty")])
    finally:
        release.join()

    store = Store(db, read_only=True)
    try:
        status = store.conn.execute(
            "SELECT status FROM trades_swept WHERE market_id = 'KXT-0'"
        ).fetchone()
    finally:
        store.close()
    assert status == ("empty",), "the flush gave up instead of waiting out the reader"


def test_a_bare_store_open_would_have_died_here(tmp_path):
    """Fixture control: the scenario above must actually be fatal without
    retry — otherwise the test could pass while proving nothing."""
    db = str(tmp_path / "t.duckdb")
    Store(db).close()
    reader = duckdb.connect(db, read_only=True)
    try:
        with pytest.raises(duckdb.Error):
            Store(db)
    finally:
        reader.close()


def test_unit_does_not_override_the_deadline():
    """The systemd unit must run with the in-code default, not disable it."""
    from pathlib import Path

    unit = (
        Path(__file__).resolve().parent.parent / "scripts" / "systemd" / "hyxlab-tradepass.service"
    ).read_text()
    assert "--deadline-min" not in unit


def _http_error(code):
    import requests

    resp = requests.Response()
    resp.status_code = code
    return requests.HTTPError(f"{code}", response=resp)


def _run_with_fetch(tmp_path, monkeypatch, n_markets, fetch):
    monkeypatch.chdir(tmp_path)
    clock = _FakeClock()
    sleeps: list[float] = []

    def sleep(s):
        sleeps.append(s)
        clock.sleep(s)

    clock_mod = type("T", (), {"monotonic": clock.monotonic, "sleep": staticmethod(sleep)})
    db = str(tmp_path / "t.duckdb")
    _seed_settled_markets(db, n_markets)
    monkeypatch.setattr(tb, "LOCK_FILE", str(tmp_path / "writer.lock"))
    monkeypatch.setattr(tb, "time", clock_mod)
    monkeypatch.setattr(tb.kalshi, "get_trades", fetch)
    monkeypatch.setattr("sys.argv", ["tradepass", "--db", db, "--rps", "1000"])
    tb.main()
    store = Store(db, read_only=True)
    try:
        swept = {r[0] for r in store.conn.execute("SELECT market_id FROM trades_swept").fetchall()}
    finally:
        store.close()
    return swept, sleeps


def test_a_429_retries_the_same_market_after_seconds_not_30(tmp_path, monkeypatch):
    """2026-10-08: a flat 30s sleep per 429 that then SKIPPED the market spent
    ~167 of the run's 210 minutes asleep and left the market for tomorrow.
    Kalshi's bucket refills in seconds, so one 429 must cost 2s and the
    market must land in THIS run."""
    calls: dict[str, int] = {}

    def fetch(ticker, session=None):
        calls[ticker] = calls.get(ticker, 0) + 1
        if ticker == "KXT-1" and calls[ticker] == 1:
            raise _http_error(429)
        return [], False

    swept, sleeps = _run_with_fetch(tmp_path, monkeypatch, 3, fetch)
    assert swept == {"KXT-0", "KXT-1", "KXT-2"}, "the 429'd market was left pending"
    assert calls["KXT-1"] == 2
    assert max(sleeps) == tb.BACKOFF_429_S


def test_a_sustained_429_storm_escalates_to_the_old_cap_and_resets(tmp_path, monkeypatch):
    """A storm doubles the wait up to the 30s cap (never worse than the old
    pacing), gives the market up after ATTEMPTS_429 so it stays pending, and
    the first success resets the streak."""

    calls: dict[str, int] = {}

    def fetch(ticker, session=None):
        calls[ticker] = calls.get(ticker, 0) + 1
        if ticker == "KXT-0" or (ticker == "KXT-2" and calls[ticker] == 1):
            raise _http_error(429)
        return [], False

    swept, sleeps = _run_with_fetch(tmp_path, monkeypatch, 3, fetch)
    assert "KXT-0" not in swept and {"KXT-1", "KXT-2"} <= swept
    waits = [s for s in sleeps if s >= tb.BACKOFF_429_S]
    # 4 in-market retries (2,4,8,16) + the give-up backoff (30, capped),
    # then KXT-1's success resets the streak: KXT-2's lone 429 costs 2s.
    assert waits == [2.0, 4.0, 8.0, 16.0, 30.0, 2.0]
    assert max(waits) == tb.BACKOFF_429_CAP_S
