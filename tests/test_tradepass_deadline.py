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
        swept = {
            r[0]
            for r in store.conn.execute("SELECT market_id FROM trades_swept").fetchall()
        }
    finally:
        store.close()
    return tickers, swept


def test_deadline_stops_the_pass_and_leaves_the_rest_pending(
    tmp_path, monkeypatch, capsys
):
    """Each fetch costs 120 fake-seconds against a 3-minute deadline: the
    third market must not be fetched, and the unfetched tail must stay
    unmarked in trades_swept so the next run picks it up."""
    _, swept = _run_main(
        tmp_path, monkeypatch, n_markets=5, deadline_min=3, fetch_cost_s=120.0
    )

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
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "systemd"
        / "hyxlab-tradepass.service"
    ).read_text()
    assert "--deadline-min" not in unit
