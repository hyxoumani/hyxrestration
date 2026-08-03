"""Exchange-wide breadth collector (EXP-928).

Covers the four things that can silently ruin an unattended tape:
schema/upsert idempotency, top-N selection ordering, pacing + 429
handling, and writer-burst behaviour (HTTP must never run under
`data/writer.lock` — a long-held lock dropped 11.4% of 5-min collect
cycles in the 14 days to 2026-08-02, which is unrecoverable loss).

No network: every HTTP call is a fixture.
"""

from __future__ import annotations

import fcntl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import collector.breadth as breadth
import collector.sweep as sweep
from collector.venues import kalshi
from hyxlab.store import Store

UNIT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "systemd"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _mkt(ticker: str, vol24: float, yes_bid="0.34", yes_ask="0.37") -> dict:
    return {
        "ticker": ticker,
        "event_ticker": f"{ticker.split('-')[0]}-26AUG02",
        "title": ticker,
        "status": "open",
        "open_time": "2026-08-02T00:00:00Z",
        "close_time": "2026-08-03T00:00:00Z",
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": "0.63",
        "no_ask_dollars": "0.66",
        "yes_bid_size_fp": "10.00",
        "yes_ask_size_fp": "7.00",
        "last_price_dollars": "0.35",
        "volume_fp": "500.00",
        "volume_24h_fp": f"{vol24:.2f}",
        "open_interest_fp": "1200.00",
    }


class _FakeResp:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class _PagedSession:
    """Serves `pages` in order, recording params and (optionally) whether
    the writer lock was held at each call."""

    def __init__(self, pages, lock_path=None):
        self.pages = list(pages)
        self.calls = []
        self.lock_path = lock_path
        self.held_during_http = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        if self.lock_path:
            with open(self.lock_path, "a") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe, fcntl.LOCK_UN)
                    self.held_during_http.append(False)
                except OSError:
                    self.held_during_http.append(True)
        return _FakeResp(self.pages[len(self.calls) - 1])


# ---------------------------------------------------------------------------
# 1. top-N selection ordering
# ---------------------------------------------------------------------------


def test_top_n_selects_by_24h_volume_descending():
    universe = [_mkt("A", 10), _mkt("B", 900), _mkt("C", 50), _mkt("D", 7000)]
    picked = breadth.top_n(universe, 3)
    assert [m["ticker"] for m, _, _ in picked] == ["D", "B", "C"]
    assert [v for _, v, _ in picked] == [7000.0, 900.0, 50.0]
    assert [r for _, _, r in picked] == [1, 2, 3]


def test_top_n_ranks_by_24h_volume_not_lifetime_volume():
    """`volume_fp` is cumulative and would rank old markets first; the
    breadth tape wants what is TRADING NOW. Both fields are present on
    every row, so a one-character slip picks the wrong universe."""
    hot = _mkt("HOT", 5000)
    hot["volume_fp"] = "1.00"
    stale = _mkt("STALE", 1)
    stale["volume_fp"] = "999999.00"
    assert [m["ticker"] for m, _, _ in breadth.top_n([stale, hot], 1)] == ["HOT"]


def test_top_n_ties_break_deterministically_on_ticker():
    """Two cycles seeing the same universe must pick the same markets —
    otherwise tape membership churns and coverage reads as flapping."""
    universe = [_mkt(t, 100) for t in ("ZZ", "AA", "MM")]
    assert [m["ticker"] for m, _, _ in breadth.top_n(universe, 2)] == ["AA", "MM"]
    assert [m["ticker"] for m, _, _ in breadth.top_n(list(reversed(universe)), 2)] == ["AA", "MM"]


def test_top_n_handles_missing_and_unparseable_volume():
    """Absent/blank volume reads as 0.0 — and therefore falls under the
    strict floor rather than sorting alongside real volume."""
    a, b = _mkt("A", 5), _mkt("B", 5)
    del a["volume_24h_fp"]
    b["volume_24h_fp"] = ""
    picked = breadth.top_n([a, b, _mkt("C", 1)], 3)
    assert [m["ticker"] for m, _, _ in picked] == ["C"]


def test_zero_volume_markets_are_never_captured():
    """MEASURED 2026-08-03: only 1,636 of 8,718 markets in the 24h close
    window have any volume, and the unwindowed universe is >200k dead
    parlay legs. Padding the tape with them records nothing and inflates
    every coverage number computed from it."""
    universe = [_mkt("DEAD1", 0), _mkt("DEAD2", 0), _mkt("LIVE", 3)]
    picked = breadth.top_n(universe, 1000)
    assert [m["ticker"] for m, _, _ in picked] == ["LIVE"]


def test_volume_floor_is_configurable_and_strict():
    universe = [_mkt("A", 100), _mkt("B", 10), _mkt("C", 1)]
    assert [m["ticker"] for m, _, _ in breadth.top_n(universe, 10, min_volume_24h=10)] == ["A"]


def test_top_n_is_not_gated_by_the_sweep_category_allowlist():
    """THE point of this collector: sports/politics/anything must be
    eligible. A future 'tidy-up' that reused sweep's allowlist would
    reinstate exactly the blind spot this exists to remove."""
    universe = [_mkt("KXNFLGAME-X", 9e6), _mkt("KXHIGHNY-B85", 10)]
    assert [m["ticker"] for m, _, _ in breadth.top_n(universe, 1)] == ["KXNFLGAME-X"]


def test_ties_at_the_cutoff_do_not_leak_extra_rows():
    universe = [_mkt(t, 100) for t in ("A", "B", "C", "D")]
    assert len(breadth.top_n(universe, 2)) == 2


def test_top_n_of_zero_or_empty_universe():
    assert breadth.top_n([_mkt("A", 1)], 0) == []
    assert breadth.top_n([], 10) == []


def test_top_n_larger_than_universe_returns_everything():
    assert len(breadth.top_n([_mkt("A", 1), _mkt("B", 2)], 1000)) == 2


# ---------------------------------------------------------------------------
# 2. schema + upsert idempotency
# ---------------------------------------------------------------------------


def _row(store, ticker):
    return store.conn.execute(
        "SELECT ts, yes_bid, no_ask, yes_bid_size, no_ask_size, volume,"
        " volume_24h, rank FROM breadth_snapshots WHERE market_id = ?",
        [ticker],
    ).fetchall()


def test_breadth_rows_round_trip_through_the_store(tmp_path):
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        ts = datetime(2026, 8, 2, 3, 5, tzinfo=UTC)
        snap = kalshi.to_snapshot(_mkt("KXA-1", 4200), ts)
        assert store.insert_breadth_snapshots([breadth.breadth_row(snap, 4200.0, 1)]) == 1
        (got,) = _row(store, "KXA-1")
        # tz-aware in must land as naive UTC, not box-local (mistakes #1/#10)
        assert got[0] == datetime(2026, 8, 2, 3, 5)
        assert (got[1], got[2]) == (0.34, 0.66)
        # NO sizes mirror the YES book when the API omits them
        assert (got[3], got[4]) == (10.0, 10.0)
        assert (got[5], got[6], got[7]) == (500.0, 4200.0, 1)
    finally:
        store.close()


def test_reinserting_the_same_cycle_is_a_no_op(tmp_path):
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        ts = datetime(2026, 8, 2, 3, 5, tzinfo=UTC)
        rows = [
            breadth.breadth_row(kalshi.to_snapshot(_mkt(t, 10), ts), 10.0, i + 1)
            for i, t in enumerate(("A", "B"))
        ]
        assert store.insert_breadth_snapshots(rows) == 2
        assert store.insert_breadth_snapshots(rows) == 0
        assert store.conn.execute("SELECT count(*) FROM breadth_snapshots").fetchone()[0] == 2
    finally:
        store.close()


def test_a_later_cycle_of_the_same_market_is_a_new_row(tmp_path):
    """Discrimination control for the test above: idempotency must key on
    (venue, market_id, ts), not collapse a market to one row — a tape
    that dedups by market captures nothing."""
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        t0 = datetime(2026, 8, 2, 3, 5, tzinfo=UTC)
        first = breadth.breadth_row(kalshi.to_snapshot(_mkt("A", 10), t0), 10.0, 1)
        second = breadth.breadth_row(
            kalshi.to_snapshot(_mkt("A", 10), t0 + timedelta(minutes=5)), 11.0, 1
        )
        assert store.insert_breadth_snapshots([first]) == 1
        assert store.insert_breadth_snapshots([second]) == 1
    finally:
        store.close()


def test_breadth_tape_does_not_pollute_the_focus_snapshots_table(tmp_path):
    """`snapshots` is what the sim/QA read as 'the series we study'. If
    breadth wrote there, every coverage number in the lab would silently
    change meaning."""
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        snap = kalshi.to_snapshot(_mkt("KXA-1", 1), datetime(2026, 8, 2, tzinfo=UTC))
        store.insert_breadth_snapshots([breadth.breadth_row(snap, 1.0, 1)])
        assert store.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
    finally:
        store.close()


def test_insert_breadth_snapshots_of_nothing(tmp_path):
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        assert store.insert_breadth_snapshots([]) == 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. pacing + 429 handling
# ---------------------------------------------------------------------------


def test_pages_are_paced_and_the_first_page_is_not_delayed(monkeypatch):
    slept = []
    monkeypatch.setattr(kalshi, "_get_with_429_retry", lambda s, u, p, **k: _FakeResp(next(pages)))
    pages = iter(
        [
            {"markets": [_mkt("A", 1)], "cursor": "c1"},
            {"markets": [_mkt("B", 1)], "cursor": "c2"},
            {"markets": [_mkt("C", 1)], "cursor": ""},
        ]
    )
    import time as _t

    monkeypatch.setattr(_t, "sleep", lambda s: slept.append(s))

    out = kalshi.get_markets(status="open", max_pages=5, pause_s=0.2)

    assert len(out) == 3
    assert slept == [0.2, 0.2], "pacing must sit BETWEEN pages, not before the first"


def test_narrow_callers_are_unpaced_by_default(monkeypatch):
    """The per-series collector/sweep callers must stay byte-identical;
    a nonzero default would add ~0.2s x pages to every sweep series."""
    slept = []
    pages = iter([{"markets": [_mkt("A", 1)], "cursor": "c"}, {"markets": [], "cursor": ""}])
    monkeypatch.setattr(kalshi, "_get_with_429_retry", lambda s, u, p, **k: _FakeResp(next(pages)))
    import time as _t

    monkeypatch.setattr(_t, "sleep", lambda s: slept.append(s))

    kalshi.get_markets(series_ticker="KXHIGHNY", max_pages=5)
    assert slept == []


def test_breadth_enumeration_goes_through_the_429_retry_path(monkeypatch):
    """Reuse, do not reimplement: a private page loop in breadth.py would
    lose Retry-After honouring and 429 into a hard failure (the 08-02
    sweep defect, where 4,947 markets went unarchived)."""
    seen = []
    real = kalshi._get_with_429_retry
    monkeypatch.setattr(kalshi, "_get_with_429_retry", lambda *a, **k: seen.append(1) or real(*a, **k))
    sess = _PagedSession([{"markets": [_mkt("A", 1)], "cursor": ""}])

    breadth.fetch_universe(session=sess, pause_s=0.0)
    assert seen, "breadth bypassed kalshi._get_with_429_retry"


def test_429_is_retried_honouring_retry_after(monkeypatch):
    slept = []
    import time as _t

    monkeypatch.setattr(_t, "sleep", lambda s: slept.append(s))
    responses = [
        _FakeResp({}, status=429, headers={"Retry-After": "3"}),
        _FakeResp({"markets": [_mkt("A", 1)], "cursor": ""}),
    ]

    class S:
        def get(self, url, params=None, timeout=None):
            return responses.pop(0)

    out = kalshi.get_markets(status="open", session=S(), pause_s=0.0)
    assert [m["ticker"] for m in out] == ["A"]
    assert slept == [3.0], "Retry-After was not honoured"


def test_enumeration_is_bounded_by_a_close_time_window(monkeypatch):
    """THE cost guard. MEASURED 2026-08-03: without max_close_ts the walk
    hit 200 pages / >200,000 markets / 140s / 2x HTTP 429 with the cursor
    still live, and the resulting 'top 1,000' had a cutoff volume of 0
    because almost nothing in it had ever traded. With the 24h window:
    9 requests, 5.8s, 8,718 markets, 1,636 of them live.

    A regression that drops this param is a ~22x request-rate increase
    against a budget shared with a live trading loop.
    """
    sess = _PagedSession([{"markets": [_mkt("A", 1)], "cursor": ""}])
    now = datetime(2026, 8, 3, 2, 0, tzinfo=UTC)

    breadth.fetch_universe(session=sess, pause_s=0.0, close_window_h=24, now=now)

    (params,) = sess.calls
    assert params["status"] == "open"
    assert params["max_close_ts"] == int(datetime(2026, 8, 4, 2, 0, tzinfo=UTC).timestamp())
    assert "series_ticker" not in params, "breadth must stay exchange-wide"


def test_close_window_default_is_24h_and_is_configurable(monkeypatch):
    assert breadth.CLOSE_WINDOW_H == 24
    sess = _PagedSession([{"markets": [], "cursor": ""}])
    now = datetime(2026, 8, 3, 2, 0, tzinfo=UTC)
    breadth.fetch_universe(session=sess, pause_s=0.0, close_window_h=6, now=now)
    assert sess.calls[0]["max_close_ts"] == int(
        datetime(2026, 8, 3, 8, 0, tzinfo=UTC).timestamp()
    )


def test_max_pages_stays_a_loud_truncation_guard():
    """60 pages ~= 7x the measured 24h universe (so a normal cycle never
    truncates) but ~1/3 of the unwindowed one (so a dropped window trips
    get_markets' TRUNCATED warning instead of quietly costing 200+
    requests every 5 minutes)."""
    assert breadth.MAX_PAGES * breadth.PAGE_LIMIT >= 8_718 * 5
    assert breadth.MAX_PAGES * breadth.PAGE_LIMIT < 200_000


def test_breadth_pacing_default_is_the_repo_safe_constant():
    """This collector shares Kalshi's rate budget with a live trading
    loop; the pacing constant must be the measured-safe one, not a fresh
    guess. 1,000/page x 0.2s => ~5 req/s ceiling."""
    assert breadth.MARKETS_PAUSE_S is sweep.MARKETS_PAUSE_S
    assert sweep.MARKETS_PAUSE_S >= 0.2
    assert breadth.PAGE_LIMIT == 1000


# ---------------------------------------------------------------------------
# 4. writer-burst behaviour
# ---------------------------------------------------------------------------


def test_no_http_happens_while_the_writer_lock_is_held(tmp_path):
    """THE load-bearing test. The lock must be free during every request:
    holding it across HTTP is what dropped 421 of 3,706 collect cycles."""
    lock_path = str(tmp_path / "writer.lock")
    db = str(tmp_path / "t.duckdb")
    sess = _PagedSession(
        [
            {"markets": [_mkt("A", 9), _mkt("B", 5)], "cursor": "c1"},
            {"markets": [_mkt("C", 7)], "cursor": ""},
        ],
        lock_path=lock_path,
    )

    counts = breadth.collect_breadth_once(db, n=2, session=sess, lock_file=lock_path, pause_s=0.0)

    assert sess.held_during_http, "fixture made no HTTP calls"
    assert not any(sess.held_during_http), (
        "the writer lock was held across a REST call — that is the hold that "
        "starves hyxlab-collect"
    )
    assert counts["universe"] == 3 and counts["picked"] == 2


def test_a_cycle_actually_persists_what_it_selected(tmp_path):
    """Discrimination control: a collector that simply never wrote would
    pass the lock test above."""
    lock_path = str(tmp_path / "writer.lock")
    db = str(tmp_path / "t.duckdb")
    sess = _PagedSession([{"markets": [_mkt("A", 9), _mkt("B", 5), _mkt("C", 7)], "cursor": ""}])

    counts = breadth.collect_breadth_once(db, n=2, session=sess, lock_file=lock_path, pause_s=0.0)

    assert counts["inserted"] == 2
    assert counts["cutoff_volume_24h"] == 7.0
    store = Store(db)
    try:
        got = store.conn.execute(
            "SELECT market_id, rank FROM breadth_snapshots ORDER BY rank"
        ).fetchall()
        assert got == [("A", 1), ("C", 2)]
        # market metadata is upserted too, so the tape is joinable to
        # close_time/strike without a second pass
        ids = {r[0] for r in store.conn.execute("SELECT market_id FROM markets").fetchall()}
        assert ids == {"A", "C"}
    finally:
        store.close()


def test_the_lock_is_released_after_the_burst(tmp_path):
    lock_path = str(tmp_path / "writer.lock")
    sess = _PagedSession([{"markets": [_mkt("A", 1)], "cursor": ""}])
    breadth.collect_breadth_once(
        str(tmp_path / "t.duckdb"), n=1, session=sess, lock_file=lock_path, pause_s=0.0
    )
    with open(lock_path, "a") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        fcntl.flock(probe, fcntl.LOCK_UN)


def test_every_row_in_a_cycle_shares_one_timestamp(tmp_path):
    """A cycle is one instant of the book. Per-row now() would make the
    tape unjoinable across markets (and every re-run would duplicate)."""
    lock_path = str(tmp_path / "writer.lock")
    db = str(tmp_path / "t.duckdb")
    sess = _PagedSession([{"markets": [_mkt(t, 10 - i) for i, t in enumerate("ABCDE")], "cursor": ""}])
    breadth.collect_breadth_once(db, n=5, session=sess, lock_file=lock_path, pause_s=0.0)
    store = Store(db)
    try:
        assert store.conn.execute("SELECT count(DISTINCT ts) FROM breadth_snapshots").fetchone()[0] == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 5. enablement (default OFF) + config
# ---------------------------------------------------------------------------


def test_disabled_by_default():
    assert breadth.enabled({}) is False
    assert breadth.enabled({"HYXLAB_BREADTH_ENABLED": "0"}) is False
    assert breadth.enabled({"HYXLAB_BREADTH_ENABLED": ""}) is False


@pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "on"])
def test_enable_flag_accepts_the_usual_truths(v):
    assert breadth.enabled({"HYXLAB_BREADTH_ENABLED": v}) is True


def test_main_does_nothing_when_disabled(monkeypatch, capsys):
    monkeypatch.delenv(breadth.ENABLE_ENV, raising=False)
    monkeypatch.setattr("sys.argv", ["breadth", "--once", "--db", "/nonexistent/x.duckdb"])

    def boom(*a, **k):  # a disabled run must not touch the network or the DB
        raise AssertionError("collect ran while disabled")

    monkeypatch.setattr(breadth, "collect_breadth_once", boom)
    with pytest.raises(SystemExit) as exc:
        breadth.main()
    # exit 0: "off" is a normal state; nonzero would make the timer look broken
    assert exc.value.code == 0
    assert "disabled" in capsys.readouterr().out


def test_env_overrides_are_read_and_bad_values_fall_back(monkeypatch):
    assert breadth._env_int(breadth.TOP_N_ENV, 1000, {}) == 1000
    assert breadth._env_int(breadth.TOP_N_ENV, 1000, {breadth.TOP_N_ENV: "250"}) == 250
    assert breadth._env_int(breadth.TOP_N_ENV, 1000, {breadth.TOP_N_ENV: "junk"}) == 1000
    assert breadth._env_int(breadth.TOP_N_ENV, 1000, {breadth.TOP_N_ENV: "-5"}) == 1000
    assert breadth.DEFAULT_TOP_N == 1000
    assert breadth.DEFAULT_INTERVAL_S == 300
    assert breadth._env_int(breadth.WINDOW_ENV, 24, {breadth.WINDOW_ENV: "6"}) == 6


# ---------------------------------------------------------------------------
# 6. the shipped unit files
# ---------------------------------------------------------------------------


def test_breadth_unit_exists_and_follows_repo_convention():
    svc = (UNIT_DIR / "hyxlab-breadth.service").read_text()
    timer = (UNIT_DIR / "hyxlab-breadth.timer").read_text()
    assert "Type=oneshot" in svc and "OOMScoreAdjust=500" in svc
    assert "hyxrestration-stable" in svc
    assert "writer.lock" not in svc, "no unit-level flock; bursts are taken in python"
    assert f"Environment={breadth.ENABLE_ENV}=1" in svc, (
        "installing the timer must be what enables it, since the code is off by default"
    )
    assert f"Environment={breadth.WINDOW_ENV}=24" in svc, (
        "the unit must pin the close window: unwindowed is a ~22x request rate"
    )
    assert "OnCalendar=*:2/5" in timer, "must be offset from hyxlab-collect's *:0/5"
