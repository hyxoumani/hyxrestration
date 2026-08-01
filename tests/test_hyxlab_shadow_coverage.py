"""Shadow outcome coverage: a fill is only observed if its market closes
while the run that opened it is still alive. Synthetic ledgers, no network."""

from datetime import datetime, timedelta

import duckdb

from simulator.shadow_coverage import build_coverage

T0 = datetime(2026, 8, 1, 0, 0)


def _ledger(runs, fills, equity):
    """runs: [(run_id, started_at)]; fills: [(run_id, market_id, qty, price)];
    equity: [(run_id, ts)]."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE shadow_runs (run_id VARCHAR, started_at TIMESTAMP)")
    conn.execute(
        "CREATE TABLE shadow_fills (run_id VARCHAR, market_id VARCHAR,"
        " qty DOUBLE, price DOUBLE)"
    )
    conn.execute("CREATE TABLE shadow_equity (run_id VARCHAR, ts TIMESTAMP)")
    conn.executemany("INSERT INTO shadow_runs VALUES (?,?)", runs)
    conn.executemany("INSERT INTO shadow_fills VALUES (?,?,?,?)", fills)
    conn.executemany("INSERT INTO shadow_equity VALUES (?,?)", equity)
    return conn


def _markets(rows):
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE markets (market_id VARCHAR, close_time TIMESTAMP)")
    if rows:
        conn.executemany("INSERT INTO markets VALUES (?,?)", rows)
    return conn


def _run(report, run_id):
    return next(r for r in report["runs"] if r["run_id"] == run_id)


def test_count_and_notional_coverage_disagree():
    """LOAD-BEARING. A run whose three observed fills are tiny and whose one
    unobserved fill is large reads 0.75 by COUNT and 0.0625 by NOTIONAL. An
    implementation that reports only one unit, or that weights both the same
    way, fails on the numbers rather than on a missing key."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[
            ("r1", "EARLY-A", 5.0, 0.20),  # 1.0 notional, closes in-life
            ("r1", "EARLY-B", 5.0, 0.20),  # 1.0
            ("r1", "EARLY-C", 5.0, 0.20),  # 1.0
            ("r1", "LATE", 100.0, 0.45),  # 45.0 notional, closes after death
        ],
        equity=[("r1", T0 + timedelta(hours=6))],
    )
    markets = _markets(
        [
            ("EARLY-A", T0 + timedelta(hours=1)),
            ("EARLY-B", T0 + timedelta(hours=2)),
            ("EARLY-C", T0 + timedelta(hours=3)),
            ("LATE", T0 + timedelta(hours=24)),
        ]
    )

    r = _run(build_coverage(ledger, markets), "r1")
    assert r["observed_fills"] == 3
    assert r["unobserved_fills"] == 1
    assert r["coverage_fills"] == 0.75
    assert r["observed_notional"] == 3.0
    assert r["unobserved_notional"] == 45.0
    assert r["coverage_notional"] == 0.0625
    assert r["life_hours"] == 6.0


def test_run_end_is_last_equity_tick_not_last_fill():
    """DISCRIMINATION CONTROL for the obvious wrong implementation. The run
    stops trading at +1h but keeps marking to +6h, and the market closes at
    +3h. Taking the last FILL as the run end would call this unobserved;
    the run was alive and did see the close."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[("r1", "M", 10.0, 0.5)],
        equity=[("r1", T0 + timedelta(hours=1)), ("r1", T0 + timedelta(hours=6))],
    )
    markets = _markets([("M", T0 + timedelta(hours=3))])

    r = _run(build_coverage(ledger, markets), "r1")
    assert r["observed_fills"] == 1
    assert r["unobserved_fills"] == 0
    assert r["coverage_fills"] == 1.0


def test_undated_market_is_neither_observed_nor_unobserved():
    """An unknown expiry is not evidence of coverage. The undated fill is
    counted and reported, but must not move either side of the ratio —
    coverage stays exactly the 0.5 the two dated fills give."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[
            ("r1", "EARLY", 10.0, 0.5),
            ("r1", "LATE", 10.0, 0.5),
            ("r1", "UNDATED", 10.0, 0.5),
        ],
        equity=[("r1", T0 + timedelta(hours=6))],
    )
    markets = _markets(
        [("EARLY", T0 + timedelta(hours=1)), ("LATE", T0 + timedelta(hours=24))]
    )

    r = _run(build_coverage(ledger, markets), "r1")
    assert r["fills"] == 3
    assert r["undated_fills"] == 1
    assert r["undated_notional"] == 5.0
    assert r["observed_fills"] == 1
    assert r["unobserved_fills"] == 1
    assert r["coverage_fills"] == 0.5
    assert r["coverage_notional"] == 0.5


def test_no_dated_fills_reads_none_not_zero_or_one():
    """A run with nothing datable has NO coverage. Defaulting to 0.0 would
    print as total blindness and 1.0 as full observation; both are findings
    the data does not support."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[("r1", "UNDATED", 10.0, 0.5)],
        equity=[("r1", T0 + timedelta(hours=6))],
    )
    r = _run(build_coverage(ledger, _markets([])), "r1")
    assert r["coverage_fills"] is None
    assert r["coverage_notional"] is None


def test_recent_window_isolates_the_current_regime():
    """The production shape: one long historical run that observed
    everything, then three short runs that observed nothing. Pooling all
    four dilutes the collapse to 0.5; the `recent` window (here 3) must
    read exactly 0.0 so the current regime is visible rather than averaged
    away by history."""
    runs = [("old", T0)] + [(f"new{i}", T0 + timedelta(days=1 + i)) for i in range(3)]
    fills = [("old", f"OLD{i}", 10.0, 0.5) for i in range(3)]
    fills += [(f"new{i}", f"NEW{i}", 10.0, 0.5) for i in range(3)]
    equity = [("old", T0 + timedelta(days=1))]
    equity += [(f"new{i}", T0 + timedelta(days=1 + i, hours=6)) for i in range(3)]

    markets = [(f"OLD{i}", T0 + timedelta(hours=1 + i)) for i in range(3)]
    # each new run's market closes ~24h after that run opened, well past its
    # 6h life — the real weather-ladder shape.
    markets += [(f"NEW{i}", T0 + timedelta(days=2 + i)) for i in range(3)]

    report = build_coverage(_ledger(runs, fills, equity), _markets(markets), recent_runs=3)
    assert report["pooled"]["coverage_fills"] == 0.5
    assert report["recent"]["runs"] == 3
    assert report["recent"]["fills"] == 3
    assert report["recent"]["unobserved_fills"] == 3
    assert report["recent"]["coverage_fills"] == 0.0
