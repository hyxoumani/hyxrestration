"""Shadow outcome coverage: a fill is only observed if its market closes
while the run that opened it is still alive. Synthetic ledgers, no network."""

from datetime import datetime, timedelta

import duckdb

from simulator.shadow_coverage import LIVE_GRACE_S, build_coverage

T0 = datetime(2026, 8, 1, 0, 0)


def _ledger(runs, fills, equity):
    """runs: [(run_id, started_at)]; fills: [(run_id, market_id, qty, price)];
    equity: [(run_id, ts)]."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE shadow_runs (run_id VARCHAR, started_at TIMESTAMP)")
    conn.execute(
        "CREATE TABLE shadow_fills (run_id VARCHAR, market_id VARCHAR, qty DOUBLE, price DOUBLE)"
    )
    conn.execute("CREATE TABLE shadow_equity (run_id VARCHAR, ts TIMESTAMP)")
    conn.executemany("INSERT INTO shadow_runs VALUES (?,?)", runs)
    conn.executemany("INSERT INTO shadow_fills VALUES (?,?,?,?)", fills)
    conn.executemany("INSERT INTO shadow_equity VALUES (?,?)", equity)
    return conn


def _markets(rows):
    """rows: (market_id, close_time) or (market_id, close_time, result,
    updated_at). The short form defaults to `resolved at the instant of
    close`, which is the pre-2026-08-03 assumption this module now
    refutes — it keeps the close-time tests reading exactly as before
    while the settlement tests state their timing explicitly."""
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE markets (market_id VARCHAR, close_time TIMESTAMP,"
        " result VARCHAR, updated_at TIMESTAMP)"
    )
    full = [r if len(r) == 4 else (r[0], r[1], "yes", r[1]) for r in rows]
    if full:
        conn.executemany("INSERT INTO markets VALUES (?,?,?,?)", full)
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
    markets = _markets([("EARLY", T0 + timedelta(hours=1)), ("LATE", T0 + timedelta(hours=24))])

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

    # `now` is pinned well past every tick so all four runs are DEAD: this
    # test is about the recent WINDOW, and leaving it on the wall clock made
    # it depend on whether the fixture's dates had gone by yet.
    report = build_coverage(
        _ledger(runs, fills, equity),
        _markets(markets),
        recent_runs=3,
        now=T0 + timedelta(days=30),
    )
    assert all(not r["live"] for r in report["runs"])
    assert report["pooled"]["coverage_fills"] == 0.5
    assert report["recent"]["runs"] == 3
    assert report["recent"]["fills"] == 3
    assert report["recent"]["unobserved_fills"] == 3
    assert report["recent"]["coverage_fills"] == 0.0


# -- censoring: a live run's open position has not failed to be observed ----


def _censoring_ledger():
    """One observed fill (closes 1h in, well inside the run) and three whose
    markets close 30h in — long after the run's last equity tick at 6h."""
    return _ledger(
        runs=[("r1", T0)],
        fills=[
            ("r1", "EARLY", 10.0, 0.5),
            ("r1", "LATE-A", 10.0, 0.5),
            ("r1", "LATE-B", 10.0, 0.5),
            ("r1", "LATE-C", 10.0, 0.5),
        ],
        equity=[("r1", T0 + timedelta(hours=6))],
    )


_CENSORING_MARKETS = [
    ("EARLY", T0 + timedelta(hours=1)),
    ("LATE-A", T0 + timedelta(hours=30)),
    ("LATE-B", T0 + timedelta(hours=30)),
    ("LATE-C", T0 + timedelta(hours=30)),
]

END = T0 + timedelta(hours=6)


def test_live_and_dead_runs_read_differently_on_identical_fills():
    """LOAD-BEARING. Same ledger, same markets, same run end — only the
    wall clock differs. Still live: the three late fills are PENDING, so
    coverage is 1/(1+0) = 1.0. Already dead: they are MISSED, so coverage
    is 1/(1+3) = 0.25. An implementation that counts censored fills as
    failures fails here on the NUMBERS, not on a missing key, and the
    0.25 side proves the partition is not merely always-pending."""
    live = _run(
        build_coverage(
            _censoring_ledger(), _markets(_CENSORING_MARKETS), now=END + timedelta(seconds=60)
        ),
        "r1",
    )
    assert live["live"] is True
    assert (live["observed_fills"], live["pending_fills"], live["missed_fills"]) == (1, 3, 0)
    assert live["coverage_fills"] == 1.0
    assert live["coverage_notional"] == 1.0

    dead = _run(
        build_coverage(
            _censoring_ledger(), _markets(_CENSORING_MARKETS), now=END + timedelta(hours=9)
        ),
        "r1",
    )
    assert dead["live"] is False
    assert (dead["observed_fills"], dead["pending_fills"], dead["missed_fills"]) == (1, 0, 3)
    assert dead["coverage_fills"] == 0.25
    assert dead["coverage_notional"] == 0.25

    # The pre-partition field keeps its old meaning either way, so archived
    # reports stay comparable.
    assert live["unobserved_fills"] == dead["unobserved_fills"] == 3


def test_live_run_with_nothing_yet_due_reads_none_not_zero():
    """The exact production case on 2026-08-01: a live run 8.7h short of
    its first market close. Every fill is pending, so the run has had NO
    opportunity to observe an outcome and coverage is undefined. Reading
    0.0 here is what made the 08:20 report call 1,059 censored fills a
    100% failure."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[("r1", "LATE-A", 10.0, 0.5), ("r1", "LATE-B", 10.0, 0.5)],
        equity=[("r1", END)],
    )
    markets = [("LATE-A", T0 + timedelta(hours=30)), ("LATE-B", T0 + timedelta(hours=30))]
    r = _run(build_coverage(ledger, _markets(markets), now=END + timedelta(seconds=60)), "r1")
    assert r["live"] is True
    assert r["pending_fills"] == 2
    assert r["missed_fills"] == 0
    assert r["coverage_fills"] is None
    assert r["coverage_notional"] is None


def test_liveness_grace_boundary_discriminates():
    """DISCRIMINATION CONTROL. Liveness must key on the grace window, not
    on being the newest run. One second inside LIVE_GRACE_S is live; one
    second outside is dead, and the same fill flips from pending to
    missed. An always-live or newest-run-is-live implementation fails."""
    inside = _run(
        build_coverage(
            _censoring_ledger(),
            _markets(_CENSORING_MARKETS),
            now=END + timedelta(seconds=LIVE_GRACE_S - 1),
        ),
        "r1",
    )
    outside = _run(
        build_coverage(
            _censoring_ledger(),
            _markets(_CENSORING_MARKETS),
            now=END + timedelta(seconds=LIVE_GRACE_S + 1),
        ),
        "r1",
    )
    assert (inside["live"], inside["pending_fills"], inside["missed_fills"]) == (True, 3, 0)
    assert (outside["live"], outside["pending_fills"], outside["missed_fills"]) == (False, 0, 3)


def test_hours_to_first_outcome_is_the_shortfall():
    """The number that decides whether restarting the daemon destroys an
    observation. A run ending at 6h whose earliest market closes at 30h
    needed 24 more hours. None once something HAS been observed — there is
    no first outcome still owed."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[("r1", "LATE-A", 10.0, 0.5), ("r1", "LATE-B", 10.0, 0.5)],
        equity=[("r1", END)],
    )
    markets = [("LATE-A", T0 + timedelta(hours=30)), ("LATE-B", T0 + timedelta(hours=40))]
    r = _run(build_coverage(ledger, _markets(markets), now=END + timedelta(seconds=60)), "r1")
    # the EARLIEST close, not the latest and not the mean
    assert r["first_close"] == (T0 + timedelta(hours=30)).isoformat()
    assert r["hours_to_first_outcome"] == 24.0

    already = _run(
        build_coverage(
            _censoring_ledger(), _markets(_CENSORING_MARKETS), now=END + timedelta(seconds=60)
        ),
        "r1",
    )
    assert already["hours_to_first_outcome"] is None


def test_live_run_does_not_drag_pooled_coverage_down():
    """The bug in the 08:20 headline, at the pooled level. A dead run that
    observed everything plus a live run with three pending fills must pool
    to 1.0 — the live run contributes to neither side. Folding pending in
    reads 0.25 and prints as a collapse that did not happen."""
    runs = [("dead", T0), ("live", T0 + timedelta(days=1))]
    fills = [("dead", "EARLY", 10.0, 0.5)]
    fills += [("live", f"L{i}", 10.0, 0.5) for i in range(3)]
    equity = [("dead", END), ("live", T0 + timedelta(days=1, hours=6))]
    markets = [("EARLY", T0 + timedelta(hours=1))]
    markets += [(f"L{i}", T0 + timedelta(days=2, hours=6)) for i in range(3)]

    report = build_coverage(
        _ledger(runs, fills, equity),
        _markets(markets),
        now=T0 + timedelta(days=1, hours=6, seconds=60),
    )
    assert report["pooled"]["live_runs"] == 1
    assert report["pooled"]["pending_fills"] == 3
    assert report["pooled"]["missed_fills"] == 0
    assert report["pooled"]["coverage_fills"] == 1.0


# --- settlement partition (2026-08-03) ---------------------------------
#
# A close is not a resolution. `_settle` gates on `markets.result`, which
# the daily kalshi sweep writes hours after the market closes, so a run
# can observe every close it holds and settle nothing.


def test_closed_but_unresolved_reads_covered_and_unsettleable():
    """LOAD-BEARING. The 08-03 finding, at fixture scale. An IDENTICAL
    ledger and IDENTICAL run lifetime: every market closes well inside the
    run's life, so `coverage_fills` reads a perfect 1.0 — and none of them
    has a result, so BOTH settlement bounds read 0.0. An implementation
    that reuses the close-time predicate for settlement fails on the
    contrast between the two numbers, not on a missing key."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[("r1", "A", 10.0, 0.5), ("r1", "B", 10.0, 0.5)],
        equity=[("r1", T0 + timedelta(hours=6))],
    )
    # Closed at +1h/+2h, run died at +6h, still unresolved at report time.
    markets = _markets(
        [
            ("A", T0 + timedelta(hours=1), "", T0 + timedelta(hours=1)),
            ("B", T0 + timedelta(hours=2), "", T0 + timedelta(hours=2)),
        ]
    )

    r = _run(build_coverage(ledger, markets), "r1")
    assert r["coverage_fills"] == 1.0, "both markets closed inside the run"
    assert r["settle_coverage_fills_floor"] == 0.0
    assert r["settle_coverage_fills_ceiling"] == 0.0
    assert r["settle_missed_fills_floor"] == 2
    assert r["unresolved_fills"] == 2


def test_result_written_after_run_end_is_missed_not_observed():
    """The real mechanism: the market closes in-life but the sweep writes
    `result` after the run is dead. The close bar is cleared, the
    settlement bar is not, and the shortfall is the sweep lag."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[("r1", "A", 10.0, 0.5)],
        equity=[("r1", T0 + timedelta(hours=6))],
    )
    # Closes at +1h, run dies at +6h, sweep writes the result at +11h.
    markets = _markets([("A", T0 + timedelta(hours=1), "yes", T0 + timedelta(hours=11))])

    r = _run(build_coverage(ledger, markets), "r1")
    assert r["coverage_fills"] == 1.0
    assert r["settle_coverage_fills_floor"] == 0.0
    assert r["hours_to_first_outcome"] is None, "the close was already observed"
    assert r["hours_to_first_settleable"] == 5.0, "5h short of the sweep"


def test_the_two_bounds_bracket_the_unrecorded_write_instant():
    """The bounds must actually differ where the write instant is unknown.
    Same market, same run: the ceiling assumes the result was there at
    close and counts it observed; the floor sees a row last written after
    the run died and counts it missed. A single-bound implementation
    cannot produce both."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[("r1", "A", 10.0, 0.5)],
        equity=[("r1", T0 + timedelta(hours=6))],
    )
    markets = _markets([("A", T0 + timedelta(hours=1), "yes", T0 + timedelta(hours=11))])

    r = _run(build_coverage(ledger, markets), "r1")
    assert r["settle_coverage_fills_ceiling"] == 1.0
    assert r["settle_coverage_fills_floor"] == 0.0


def test_resolved_before_run_end_settles_under_both_bounds():
    """DISCRIMINATION CONTROL. A genuinely settleable fill must not read as
    unsettleable just because the partition got stricter — without this the
    whole feature passes by always returning 0."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[("r1", "A", 10.0, 0.5)],
        equity=[("r1", T0 + timedelta(hours=6))],
    )
    # Closes +1h, result written +2h, run lives to +6h.
    markets = _markets([("A", T0 + timedelta(hours=1), "yes", T0 + timedelta(hours=2))])

    r = _run(build_coverage(ledger, markets), "r1")
    assert r["settle_coverage_fills_floor"] == 1.0
    assert r["settle_coverage_fills_ceiling"] == 1.0
    assert r["hours_to_first_settleable"] is None
    assert r["unresolved_fills"] == 0


def test_live_run_pending_on_settlement_is_censoring_not_failure():
    """The 08-01 lesson carried onto the new partition. A LIVE run holding
    an unresolved position has not failed to settle it — the sweep has not
    run yet. It must read None, never 0.0, or every live run manufactures
    a collapse."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[("r1", "A", 10.0, 0.5)],
        equity=[("r1", T0 + timedelta(hours=6))],
    )
    markets = _markets([("A", T0 + timedelta(hours=1), "", T0 + timedelta(hours=1))])

    r = _run(
        build_coverage(ledger, markets, now=T0 + timedelta(hours=6, seconds=LIVE_GRACE_S - 10)),
        "r1",
    )
    assert r["live"] is True
    assert r["settle_pending_fills_floor"] == 1
    assert r["settle_missed_fills_floor"] == 0
    assert r["settle_coverage_fills_floor"] is None


def test_pooled_settlement_recomputes_from_counts_not_run_ratios():
    """Unit of counting. One run with a single settled fill and one with
    999 unsettled fills pool to ~0.001, not to the 0.5 an average of the
    two per-run ratios would give."""
    runs = [("small", T0), ("big", T0)]
    fills = [("small", "S", 10.0, 0.5)]
    fills += [("big", f"B{i}", 10.0, 0.5) for i in range(999)]
    equity = [("small", T0 + timedelta(hours=6)), ("big", T0 + timedelta(hours=6))]
    markets = [("S", T0 + timedelta(hours=1), "yes", T0 + timedelta(hours=2))]
    markets += [(f"B{i}", T0 + timedelta(hours=1), "", T0 + timedelta(hours=1)) for i in range(999)]

    pooled = build_coverage(_ledger(runs, fills, equity), _markets(markets))["pooled"]
    assert pooled["settle_observed_fills_floor"] == 1
    assert pooled["settle_missed_fills_floor"] == 999
    assert pooled["settle_coverage_fills_floor"] == 0.001


def test_settlement_notional_tracks_size_not_count():
    """Same split, second unit. One large settled fill against three tiny
    unsettled ones reads 0.25 by count and 0.9375 by notional."""
    ledger = _ledger(
        runs=[("r1", T0)],
        fills=[
            ("r1", "BIG", 100.0, 0.45),  # 45.0 notional, settled
            ("r1", "T1", 5.0, 0.20),  # 1.0, unresolved
            ("r1", "T2", 5.0, 0.20),
            ("r1", "T3", 5.0, 0.20),
        ],
        equity=[("r1", T0 + timedelta(hours=6))],
    )
    markets = _markets(
        [("BIG", T0 + timedelta(hours=1), "yes", T0 + timedelta(hours=2))]
        + [(f"T{i}", T0 + timedelta(hours=1), "", T0 + timedelta(hours=1)) for i in (1, 2, 3)]
    )

    r = _run(build_coverage(ledger, markets), "r1")
    assert r["settle_coverage_fills_floor"] == 0.25
    assert r["settle_coverage_notional_floor"] == 0.9375
