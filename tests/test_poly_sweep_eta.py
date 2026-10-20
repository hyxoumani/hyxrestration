"""The poly sweep's progress line names which ETA it is (2026-10-17).

The mean rate since the start carried the run's slow opening to its last
line: `~404 min left` at 11,600/18,497 for 328 actual, `~60` at 17,400 for
48, and three passes pre-registered a budget FAIL off it. The line now
prints the mean-rate ETA AND the trailing-window one, named.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from collector import poly_sweep as ps
from hyxlab.store import Store


def test_the_two_estimates_price_the_remainder_at_the_mean_and_the_window_rate():
    # 1,000 done in 2,000 s (2 s/market mean); the last 200 took 200 s (1 s/market)
    mean, window = ps.eta_minutes(1000, 1600, elapsed_s=2000.0, window_s=200.0)
    assert mean == 600 * 2 / 60
    assert window == 600 * 1 / 60
    # a zero-length window (first line, or a stubbed clock) falls back to the mean
    assert ps.eta_minutes(1000, 1600, 2000.0, 0.0) == (20.0, 20.0)
    # nothing done yet, or nothing left: no division, zero minutes
    assert ps.eta_minutes(0, 10, 0.0, 0.0) == (0.0, 0.0)
    assert ps.eta_minutes(10, 10, 100.0, 10.0) == (0.0, 0.0)


def test_the_line_names_both_estimates(monkeypatch, capsys, tmp_path):
    """Drives `sweep` end to end (no network, no archive) with a clock that
    makes the opening 200 markets cost 2 s each and the rest 1 s each."""
    clock = [0.0]
    monkeypatch.setattr(ps.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    n = 3 * ps.PROGRESS_EVERY
    markets = [{"conditionId": f"0x{i:04d}"} for i in range(n)]
    seen = [0]

    def fake_tail(cond, session=None):
        clock[0] += 2.0 if seen[0] < ps.PROGRESS_EVERY else 1.0
        seen[0] += 1
        return []

    class _Store:
        def poly_price_watermarks(self):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(
        ps.poly, "iter_markets_by_volume", lambda *a, **k: [] if k.get("closed") else list(markets)
    )
    monkeypatch.setattr(ps.poly, "token_pair", lambda m: ("yes-token", "no-token"))
    monkeypatch.setattr(ps.poly, "gamma_market_info", lambda m: (m["conditionId"],))
    monkeypatch.setattr(ps.poly, "prices_history_range", lambda *a, **k: [])
    monkeypatch.setattr(ps.poly, "price_rows", lambda *a, **k: [])
    monkeypatch.setattr(ps.poly, "trades_tail", fake_tail)
    monkeypatch.setattr(ps.poly, "take_tail_stops", lambda: [])
    monkeypatch.setattr(ps, "_flush", lambda db, batch: None)
    monkeypatch.setattr(ps, "_open", lambda db: _Store())
    monkeypatch.setattr(ps, "note_holder", lambda path: None)
    monkeypatch.setattr(ps, "LOCK_FILE", str(tmp_path / "writer.lock"))
    totals = ps.sweep("unused.duckdb", min_volume=0.0)
    assert totals["markets"] == n
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "min left" in ln]
    # 200 done in 400 s, 400 left: mean 2 s -> 13.3 min; the window IS the mean here
    assert lines[0].endswith("~13 min left (mean rate; ~13 at the last 200's rate)")
    # 400 done in 600 s (1.5 s mean), 200 left: mean 5.0 min; the last 200 ran 1 s -> 3.3
    assert lines[1].endswith("~5 min left (mean rate; ~3 at the last 200's rate)")
    assert len(lines) == 3  # the final line has nothing left and says so
    assert lines[2].endswith("~0 min left (mean rate; ~0 at the last 200's rate)")


# -- the prior-run weighted estimate (2026-10-18) ------------------------------
#
# The two rates above were both wrong by ~100 min in OPPOSITE directions on the
# 10-18 run: ~100 low at 4,800 (trade-heavy markets sit mid-rank, 18-24 min per
# 200 through 6,000-7,200) and ~85 high at 10,000 (the mean carried them past
# the point they were done). The kalshi sweep's answer -- weight the remainder
# by the previous run's per-item wall -- needed a record the poly loop never
# wrote; `poly_sweep_log` is that record.


def test_prior_is_each_markets_latest_wall_within_the_lookback(tmp_path):
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        now = datetime(2026, 10, 18, 4, 15)
        yesterday = now - timedelta(hours=24)
        store.insert_poly_sweep_log(
            [
                ("m1", yesterday, 2.0),
                ("m2", yesterday + timedelta(minutes=1), 30.0),
                # a hand `--limit` repair later the same day: the LATEST wall wins
                ("m1", yesterday + timedelta(hours=20), 5.0),
                # the run before that is outside the lookback and ignored
                ("m3", now - timedelta(hours=ps.ETA_PRIOR_LOOKBACK_H + 1), 99.0),
            ]
        )
        assert ps.prior_market_wall(store, now=now) == {"m1": 5.0, "m2": 30.0}
    finally:
        store.close()


def test_prior_is_empty_without_an_archive_or_a_readable_table(tmp_path):
    class _NoArchive:
        pass

    assert ps.prior_market_wall(_NoArchive()) == {}
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        assert ps.prior_market_wall(store) == {}
        store.conn.execute("DROP TABLE poly_sweep_log")
        assert ps.prior_market_wall(store) == {}
    finally:
        store.close()


def test_the_sweep_log_insert_dedups_a_reflushed_batch(tmp_path):
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        rows = [("m1", datetime(2026, 10, 18, 5, 0, tzinfo=UTC), 1.5)]
        assert store.insert_poly_sweep_log(rows) == 1
        assert store.insert_poly_sweep_log(rows) == 0
        # stored naive UTC, like every other hyxlab timestamp
        assert store.conn.execute("SELECT swept_at, wall_s FROM poly_sweep_log").fetchone() == (
            datetime(2026, 10, 18, 5, 0),
            1.5,
        )
    finally:
        store.close()


def test_eta_weights_the_remainder_by_prior_wall_scaled_by_todays_pace():
    prior = {"a": 10.0, "b": 10.0, "c": 100.0, "d": 20.0}
    # a and b cost 20 s on the prior run and 30 s today: pace x1.50
    eta, tag = ps.eta_weighted(["a", "b"], ["c", "d"], elapsed_s=30.0, prior=prior)
    assert eta == 120.0 * 1.5 / 60
    assert tag == "prior-run weighted x1.50, 0 unpriced"


def test_a_market_without_a_prior_costs_the_median_and_is_counted():
    prior = {"a": 10.0, "b": 20.0, "c": 30.0}  # median 20
    eta, tag = ps.eta_weighted(["a"], ["b", "new"], elapsed_s=10.0, prior=prior)
    assert eta == (20.0 + 20.0) * 1.0 / 60
    assert tag == "prior-run weighted x1.00, 1 unpriced"


def test_an_unpriced_market_costs_the_median_of_the_markets_the_run_fetches():
    """A market with no prior is new to the enumeration, so the run fetches
    it; priced at the median over ALL priors it cost ~0 on 2026-10-20 --
    12,475 of 18,458 priors were skips at microseconds -- and 1,151 new
    markets dropped ~40 min out of the first line. The skip set is excluded
    from the fallback population."""
    prior = {"s1": 0.0, "s2": 0.0, "s3": 0.0, "f1": 10.0, "f2": 20.0, "f3": 30.0}
    skip = {"s1", "s2", "s3"}
    eta, tag = ps.eta_weighted(["f1"], ["new", "s1"], elapsed_s=10.0, prior=prior, skip=skip)
    assert eta == (20.0 + 0.0) * 1.0 / 60  # new at the fetched median, s1 at its own
    assert tag == "prior-run weighted x1.00, 1 unpriced"


def test_without_a_prior_the_estimate_is_the_mean_rate_and_says_so():
    assert ps.eta_weighted(["a", "b"], ["c"], elapsed_s=120.0, prior={}) == (1.0, "mean rate")
    assert ps.eta_weighted([], ["c"], elapsed_s=0.0, prior={"c": 1.0}) == (0.0, "mean rate")


def test_the_line_carries_the_weighted_tag_and_the_batch_carries_each_markets_wall(
    monkeypatch, capsys, tmp_path
):
    """Same drive as above with a prior: the opening 200 cost 2 s each on the
    prior run too (so the pace scale is x1.00 there) and the rest 1 s."""
    clock = [0.0]
    monkeypatch.setattr(ps.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    n = 3 * ps.PROGRESS_EVERY
    markets = [{"conditionId": f"0x{i:04d}"} for i in range(n)]
    prior = {
        m["conditionId"]: (2.0 if i < ps.PROGRESS_EVERY else 1.0) for i, m in enumerate(markets)
    }
    seen = [0]
    flushed: list[tuple] = []

    def fake_tail(cond, session=None):
        clock[0] += 2.0 if seen[0] < ps.PROGRESS_EVERY else 1.0
        seen[0] += 1
        return []

    class _Store:
        def poly_price_watermarks(self):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(
        ps.poly, "iter_markets_by_volume", lambda *a, **k: [] if k.get("closed") else list(markets)
    )
    monkeypatch.setattr(ps.poly, "token_pair", lambda m: ("yes-token", "no-token"))
    monkeypatch.setattr(ps.poly, "gamma_market_info", lambda m: (m["conditionId"],))
    monkeypatch.setattr(ps.poly, "prices_history_range", lambda *a, **k: [])
    monkeypatch.setattr(ps.poly, "price_rows", lambda *a, **k: [])
    monkeypatch.setattr(ps.poly, "trades_tail", fake_tail)
    monkeypatch.setattr(ps.poly, "take_tail_stops", lambda: [])
    monkeypatch.setattr(ps, "prior_market_wall", lambda store: dict(prior))

    def fake_flush(db, batch):  # drains like the real one: cleared on success
        flushed.extend(batch["swept"])
        batch["swept"] = []

    monkeypatch.setattr(ps, "_flush", fake_flush)
    monkeypatch.setattr(ps, "_open", lambda db: _Store())
    monkeypatch.setattr(ps, "note_holder", lambda path: None)
    monkeypatch.setattr(ps, "LOCK_FILE", str(tmp_path / "writer.lock"))
    ps.sweep("unused.duckdb", min_volume=0.0)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "min left" in ln]
    # 200 done in 400 s = the prior's 400 s (x1.00); 400 left at 1 s each = 6.7 min --
    # where the mean rate said ~13 and the window ~13
    assert lines[0].endswith(
        "~7 min left (prior-run weighted x1.00, 0 unpriced; ~13 at the last 200's rate)"
    )
    assert lines[1].endswith(
        "~3 min left (prior-run weighted x1.00, 0 unpriced; ~3 at the last 200's rate)"
    )
    # one (market_id, swept_at, wall_s) per market, the wall being the market's own cost
    assert len(flushed) == n
    assert [r[0] for r in flushed[:2]] == ["0x0000", "0x0001"]
    assert all(r[2] == 2.0 for r in flushed[: ps.PROGRESS_EVERY])
    assert all(r[2] == 1.0 for r in flushed[ps.PROGRESS_EVERY :])


def test_a_market_the_run_skips_costs_nothing_whatever_its_prior_says():
    """A market resolved between the two runs was FETCHED by the prior run
    (its prior is a 1-30 s fetch wall) and is SKIPPED by this one (it costs
    the microseconds of the skip branch). Priced at its prior it charges a
    fetch that never happens: on 2026-10-20, 1,671 such markets (median
    5.3 s, 132 min unscaled), 1,055 of them past index 11,600, and the
    line read `~125 min left` for 75 actual; on the done side the same
    priors inflate the pace denominator and read the scale LOW
    (x1.39 -> x1.24 across the closed segment). A skipped market costs
    0.0 on both sides; the skip legs' flush time rides in the scale."""
    prior = {"f1": 10.0, "f2": 10.0, "s_done": 10.0, "s_left": 100.0, "f3": 20.0}
    skip = {"s_done", "s_left"}
    # f1, f2 and s_done are done in 30 s: the scale is 30 / (10 + 10 + 0) = x1.50,
    # not 30 / 30 = x1.00 -- s_done's 10 s was a fetch yesterday and a skip today
    eta, tag = ps.eta_weighted(
        ["f1", "f2", "s_done"], ["s_left", "f3"], elapsed_s=30.0, prior=prior, skip=skip
    )
    # s_left's 100 s prior is a fetch that will not happen; only f3 is owed
    assert eta == 20.0 * 1.5 / 60
    assert tag == "prior-run weighted x1.50, 0 unpriced"


def test_an_unpriced_closed_market_costs_a_first_seen_closed_fetch():
    """New to the enumeration AND closed means the run fetches prices to the
    close and the whole tail from offset 0: 1,056 such markets ran 5.0 s
    each on 2026-10-20 against the 1.56 s fetched median. The price is the
    median prior of today's skip members whose prior is a fetch wall --
    yesterday's first-seen-closed set -- told from the skip branch's
    microseconds by `SKIP_WALL_MAX_S`."""
    prior = {
        "o1": 1.0,
        "o2": 2.0,
        "o3": 3.0,  # open, fetched both days: median 2.0
        "s_skip": 1e-5,  # skipped both days
        "c1": 4.0,
        "c2": 6.0,
        "c3": 8.0,  # fetched closed yesterday, skipped today: median 6.0
    }
    skip = {"s_skip", "c1", "c2", "c3"}
    closed = skip | {"new_closed"}
    eta, tag = ps.eta_weighted(
        ["o1", "o2", "o3"], ["new_closed", "new_open", "c1"], 6.0, prior, skip, closed
    )
    assert eta == (6.0 + 2.0 + 0.0) * 1.0 / 60
    assert tag == "prior-run weighted x1.00, 2 unpriced"
    # no closed-fetched pool (the first run that skips): a new closed market
    # costs the fetched median like any other
    eta, _ = ps.eta_weighted(["o1"], ["new_closed"], 1.0, {"o1": 1.0, "o2": 3.0}, set(), closed)
    assert eta == 3.0 / 60
