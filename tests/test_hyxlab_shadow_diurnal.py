"""Shadow diurnal report: hour-END is the level series, the intra-hour
range sits beside it, and the entry-drag model refuses to guess.
Synthetic ledgers, no network."""

from datetime import UTC, datetime, timedelta

import duckdb

from simulator.shadow_diurnal import HALF_TICK, MIN_DAYS, build_diurnal

T0 = datetime(2026, 8, 1, 0, 0)


def _ledger(equity, fills=(), settlements=()):
    """equity: [(run_id, minutes_from_T0, equity)];
    fills: [(run_id, strategy, minutes, qty, price, fee)];
    settlements: [(run_id, minutes)]."""
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE shadow_fills (run_id VARCHAR, strategy VARCHAR, venue VARCHAR,"
        " market_id VARCHAR, side VARCHAR, qty DOUBLE, price DOUBLE, fee DOUBLE,"
        " ts TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE shadow_settlements (run_id VARCHAR, market_id VARCHAR,"
        " side VARCHAR, payout DOUBLE, ts TIMESTAMP)"
    )
    conn.execute("CREATE TABLE shadow_equity (run_id VARCHAR, ts TIMESTAMP, equity DOUBLE)")
    conn.executemany(
        "INSERT INTO shadow_equity VALUES (?,?,?)",
        [(r, T0 + timedelta(minutes=m), e) for (r, m, e) in equity],
    )
    if fills:
        conn.executemany(
            "INSERT INTO shadow_fills VALUES (?,?,'kalshi','M','yes',?,?,?,?)",
            [(r, s, q, p, f, T0 + timedelta(minutes=m)) for (r, s, m, q, p, f) in fills],
        )
    if settlements:
        conn.executemany(
            "INSERT INTO shadow_settlements VALUES (?,'M','yes',0.0,?)",
            [(r, T0 + timedelta(minutes=m)) for (r, m) in settlements],
        )
    return conn


def _run(report, run_id="R"):
    return next(r for r in report["runs"] if r["run_id"] == run_id)


def _hour(run, label):
    return next(h for h in run["hours"] if h["hour"].endswith(label))


def _hod(run, hod):
    return next(p for p in run["by_hour_of_day"] if p["hour_of_day"] == hod)


def test_level_is_hour_end_and_min_gap_names_what_min_sampling_invents():
    """LOAD-BEARING, and the reason the module exists. Hour 01Z dives to
    -300 mid-hour and closes at +10; hour 02Z is flat at 0 throughout.
    Sampled at the minimum, 01Z reads 31x deeper than 02Z and would be
    narrated as a trough. Sampled at the close it is the HIGHER hour.
    `equity_end` must carry the close, and `min_gap` must report the 310
    of depth that min-sampling would have invented."""
    eq = [("R", 0, 0.0)]
    eq += [("R", 60 + i, v) for i, v in enumerate([0.0, -300.0, -50.0, 10.0])]
    eq += [("R", 120 + i, v) for i, v in enumerate([0.0, 0.0, 0.0, 0.0])]
    eq += [("R", 180, 10.0)]
    report = build_diurnal(_ledger(eq))
    run = _run(report)

    h1, h2 = _hour(run, " 01Z"), _hour(run, " 02Z")
    assert h1["equity_end"] == 10.0
    assert h1["equity_min"] == -300.0
    # The whole point: 01Z closes ABOVE 02Z while its minimum is 300 below.
    assert h1["equity_end"] > h2["equity_end"]
    assert h1["equity_min"] < h2["equity_min"]
    assert h1["min_gap"] == 310.0
    assert h2["min_gap"] == 0.0
    # And the range sits in the same row, so the confound cannot be
    # read without seeing the volatility that produced it.
    assert h1["range"] == 310.0 and h2["range"] == 0.0


def test_d_equity_splits_into_entry_drag_and_reval():
    """A taker pays the ask and is marked at the mid, so new fills cost
    (half tick + fee) before anything moves. Hour 01Z takes 100 contracts
    at 1.00 fee, so drag = 100*0.005 + 1.0 = 1.5; equity falls 2.5. The
    standing book therefore lost only 1.0, and `reval` must say so rather
    than blaming the whole 2.5 on the market."""
    eq = [("R", 0, 0.0), ("R", 59, 0.0), ("R", 119, -2.5), ("R", 179, -2.5)]
    fills = [("R", "probe", 70, 100.0, 0.20, 1.0)]
    run = _run(build_diurnal(_ledger(eq, fills)))
    h = _hour(run, " 01Z")
    assert h["entry_drag_modeled"] == round(100.0 * HALF_TICK + 1.0, 2) == 1.5
    assert h["d_equity"] == -2.5
    assert h["reval"] == -1.0


def test_drag_model_nulls_reval_for_an_ungated_strategy():
    """The mid is taken as ask - half a tick, which is true BY
    CONSTRUCTION only for a one-tick-spread-gated strategy. A fill from
    an ungated strategy must null `reval` rather than report a number
    built on a spread nobody measured."""
    eq = [("R", 0, 0.0), ("R", 59, 0.0), ("R", 119, -2.5), ("R", 179, -2.5)]
    fills = [("R", "wide_maker", 70, 100.0, 0.20, 1.0)]
    run = _run(build_diurnal(_ledger(eq, fills)))
    h = _hour(run, " 01Z")
    assert h["drag_model_valid"] is False
    assert h["reval"] is None
    assert run["validity"]["drag_model_valid"] is False
    assert run["validity"]["strategies"] == ["wide_maker"]


def test_partial_first_and_last_hours_are_excluded_from_the_profile():
    """A run that starts at 00:50 has an 00Z bucket holding ten minutes.
    Its 'hour end' is not an hour end and its range is under-sampled, so
    it must be flagged partial and kept out of the hour-of-day means --
    otherwise a run's start time leaks into the diurnal shape."""
    eq = [("R", 50, -999.0), ("R", 59, -999.0)]
    eq += [("R", 60 + i * 10, 5.0) for i in range(6)]
    eq += [("R", 120 + i * 10, 7.0) for i in range(6)]
    eq += [("R", 180, 8.0)]
    run = _run(build_diurnal(_ledger(eq)))
    assert _hour(run, " 00Z")["partial"] is True
    assert _hour(run, " 03Z")["partial"] is True
    assert _hour(run, " 01Z")["partial"] is False
    assert [p["hour_of_day"] for p in run["by_hour_of_day"]] == [1, 2]
    assert run["validity"]["partial_hours_excluded"] == 2
    # The -999 start value must not reach any published mean.
    assert all(p["mean_equity_end"] > 0 for p in run["by_hour_of_day"])


def test_profile_reads_underpowered_below_the_day_floor():
    """Hour-of-day means need days to average over. Two days of data is
    two draws per hour, and the report must say so rather than print a
    'profile' that is a pair of readings."""
    eq = [("R", 0, 0.0)]
    for day in range(2):
        for h in range(24):
            for m in (0, 30, 59):
                eq.append(("R", day * 1440 + h * 60 + m, float(h)))
    eq.append(("R", 2 * 1440, 0.0))
    run = _run(build_diurnal(_ledger(eq)))
    assert run["min_draws_per_hour"] < MIN_DAYS
    assert run["validity"]["profile_verdict"].startswith("UNDERPOWERED")
    assert _hod(run, 12)["n_days"] == 2


def test_settlement_hours_are_counted_so_a_contaminated_reval_is_visible():
    """`reval` absorbs settlement as well as marking, so an hour that
    settled anything is not a clean marking reading. The count must be
    on the row -- a big reval beside zero settlements is the claim, and
    it is unverifiable if the settlements are in another table."""
    eq = [("R", 0, 0.0), ("R", 59, 0.0), ("R", 119, 50.0), ("R", 179, 60.0), ("R", 239, 60.0)]
    run = _run(build_diurnal(_ledger(eq, settlements=[("R", 70), ("R", 80)])))
    assert _hour(run, " 01Z")["n_settlements"] == 2
    assert _hour(run, " 02Z")["n_settlements"] == 0


def test_loudest_hour_is_reported_and_need_not_be_the_lowest():
    """The headline of the report: the hour with the largest mean range
    is the one that min-sampling distorts most, and on the real ledger it
    is NOT the hour with the lowest level. 01Z swings +-100 and closes
    high; 02Z is quiet and closes low."""
    # Densely sampled: the range/min_gap columns are only published for
    # hours holding >= MIN_PTS_PER_HOUR points (validity bound 3).
    loud = [0.0, -100.0, 100.0, 0.0, 50.0, 80.0]
    quiet = [-9.0, -10.0, -10.0, -9.0, -10.0, -10.0]
    eq = [("R", 0, 0.0)]
    eq += [("R", 60 + i, loud[i % len(loud)]) for i in range(59)]
    eq += [("R", 120 + i, quiet[i % len(quiet)]) for i in range(59)]
    eq += [("R", 180, -10.0)]
    run = _run(build_diurnal(_ledger(eq)))
    r = run["range_extremes"]
    assert r["loudest_hour_of_day"] == 1
    assert r["quietest_hour_of_day"] == 2
    # Loudest is the HIGHEST-closing hour, not the lowest.
    assert _hod(run, 1)["mean_equity_end"] > _hod(run, 2)["mean_equity_end"]


def _multiday(curves):
    """curves: {day_offset: {hour_of_day: hour_end_level}}.

    Emits enough points per hour to clear MIN_PTS_PER_HOUR, with the
    LAST point of each hour at the requested level, plus a leading and
    trailing partial hour so no published hour is a partial one.
    """
    eq = [("R", -30, 0.0)]  # partial opening hour, excluded
    for day, hours in sorted(curves.items()):
        for hod, level in sorted(hours.items()):
            base = day * 1440 + hod * 60
            for k in range(25):
                eq.append(("R", base + k * 2, level - 1.0))
            eq.append(("R", base + 59, level))
    last = max(d * 1440 + max(h) * 60 for d, h in curves.items())
    eq.append(("R", last + 61, 0.0))  # partial closing hour, excluded
    return _ledger(eq)


def _shape(curves):
    return _run(build_diurnal(_multiday(curves)))


#: 14 hours, enough to clear MIN_SHARED_HOURS on every pair.
_HODS = list(range(4, 18))


def test_by_day_publishes_each_days_hour_end_series_unaveraged():
    """The mean profile cannot answer "does it repeat", so the per-day
    curves are published beside it rather than reconstructed later."""
    curves = {d: {h: float(h * 10 + d * 100) for h in _HODS} for d in range(3)}
    run = _shape(curves)
    assert [d["n_whole_hours"] for d in run["by_day"]] == [14, 14, 14]
    first = run["by_day"][0]
    assert first["hour_end"]["04"] == 40.0
    assert first["hour_end"]["17"] == 170.0
    # A day's own extremes, so a reader can see whether the trough MOVES.
    assert (first["trough_hour"], first["peak_hour"]) == (4, 17)
    assert first["amplitude"] == 130.0


def test_identical_shapes_at_different_levels_read_as_repeating():
    """Rank correlation is the statistic precisely so that a day's
    equity OFFSET and amplitude are nuisance parameters: these three
    days trace the same curve 500 apart and twice as tall."""
    base = {h: float((h - 10) ** 2) for h in _HODS}
    curves = {
        0: base,
        1: {h: v + 500 for h, v in base.items()},
        2: {h: v * 2 - 300 for h, v in base.items()},
    }
    agree = _shape(curves)["shape_agreement"]
    assert agree["shape_verdict"].startswith("REPEATS")
    assert all(p["rho"] == 1.0 for p in agree["pairs"])


def test_unlike_days_that_average_into_a_clean_cycle_read_as_not_repeating():
    """LOAD-BEARING (bound 6, mistakes #24 family). Day 0 rises all day
    and day 1 falls all day; the four-day MEAN is flat, and a mean-only report
    would publish that flat line with no hint the days disagree."""
    rising = {h: float(h * 20) for h in _HODS}
    falling = {h: float(-h * 20) for h in _HODS}
    run = _shape({0: rising, 1: falling, 2: rising, 3: falling})
    # The mean profile is indeed featureless -- every hour averages to 0.
    assert {p["mean_equity_end"] for p in run["by_hour_of_day"]} == {0.0}
    agree = run["shape_agreement"]
    assert agree["shape_verdict"].startswith("DOES NOT REPEAT")
    assert min(p["rho"] for p in agree["pairs"]) == -1.0


def test_a_pair_with_too_little_overlap_is_unscored_not_disagreeing():
    """An UNSCORED pair must not be read as a pair that disagreed, and
    it cannot be the evidence for a REPEATS verdict either."""
    full = {h: float((h - 10) ** 2) for h in _HODS}
    run = _shape({0: full, 1: full, 2: {h: full[h] for h in _HODS[:4]}})
    pairs = {tuple(p["days"]): p for p in run["shape_agreement"]["pairs"]}
    thin = [p for p in pairs.values() if p["n_shared_hours"] < 12]
    assert thin and all(p["rho"] is None for p in thin)


def test_shape_verdict_reads_underpowered_below_two_scorable_pairs():
    """One agreeing pair is not evidence of a daily cycle, and the
    report says so instead of publishing REPEATS off a single rho."""
    full = {h: float((h - 10) ** 2) for h in _HODS}
    agree = _shape({0: full, 1: full})["shape_agreement"]
    assert agree["shape_verdict"].startswith("UNDERPOWERED")
    assert len(agree["pairs"]) == 1


def test_shape_agreement_survives_a_day_with_a_flat_hour_end_curve():
    """A constant series has no ranks to correlate; rho is None (a
    refusal), never 0.0 (a measured disagreement)."""
    full = {h: float((h - 10) ** 2) for h in _HODS}
    run = _shape({0: full, 1: full, 2: dict.fromkeys(_HODS, 7.0)})
    flat_pairs = [p for p in run["shape_agreement"]["pairs"] if p["days"][1].endswith("03")]
    assert flat_pairs and all(p["rho"] is None for p in flat_pairs)
    assert run["shape_agreement"]["shape_verdict"].startswith("UNDERPOWERED")


# ---------------------------------------------------------------------------
# Bound 7: an hour-of-day mean averages whatever DAYS that hour had
# ---------------------------------------------------------------------------

# Every fixture below pads with a throwaway 00Z and a throwaway 04Z so
# that bound 2's partial-hour trim lands on the padding and not on an
# hour under test — otherwise the trim, not the fixture, decides the
# day sets.
PAD_LO, PAD_HI = 0, 4


def _panel_ledger(spec, level):
    """spec: {day_index: (hours_of_day, ...)}. level(day, hod) -> equity."""
    eq = [("R", PAD_LO * 60, level(0, PAD_LO))]
    for d in sorted(spec):
        for hod in spec[d]:
            for m in (0, 30, 59):
                eq.append(("R", d * 1440 + hod * 60 + m, level(d, hod)))
    last = max(spec)
    eq.append(("R", last * 1440 + PAD_HI * 60, level(last, PAD_HI)))
    return _ledger(sorted(eq, key=lambda r: r[1]))


def test_a_step_between_hours_can_be_a_change_of_days_and_the_panel_says_so():
    """LOAD-BEARING (falsified the 21-00Z peak, 2026-08-26).

    Three days, equity FLAT within each day and sliding hard between
    them: 0, -1000, -2000. 03Z is missing from the last day, so its
    unbalanced mean averages the two better days and reads as a +500
    recovery that no day ever traded. On the two days both hours share,
    the step is zero — which is what the data says.
    """
    run = _run(
        build_diurnal(
            _panel_ledger(
                {0: (1, 2, 3), 1: (1, 2, 3), 2: (1, 2)},
                lambda d, hod: -1000.0 * d,
            )
        )
    )

    h2, h3 = _hod(run, 2), _hod(run, 3)
    assert h2["n_days"] == 3 and h3["n_days"] == 2
    assert h2["mean_equity_end"] == -1000.0
    assert h3["mean_equity_end"] == -500.0  # the artefact, still published
    # Same two days, no step at all: the +500 was entirely composition.
    assert h2["mean_equity_end_balanced"] == h3["mean_equity_end_balanced"] == -500.0

    panel = run["level_panel"]
    assert panel["days"] == ["2026-08-01", "2026-08-02"]
    assert h2["balanced"] is False and h3["balanced"] is True
    assert 2 in panel["ragged_hours"] and 3 not in panel["ragged_hours"]
    assert panel["level_verdict"].startswith("RAGGED")
    assert "mean_end_bal" in panel["level_verdict"]


def test_an_even_run_reads_balanced_and_the_two_level_columns_agree():
    """The control. Without it, RAGGED could be what this report always
    says, and the flag would carry no information."""
    run = _run(
        build_diurnal(
            _panel_ledger(
                {0: (1, 2, 3), 1: (1, 2, 3), 2: (1, 2, 3)},
                lambda d, hod: -100.0 * d - hod,
            )
        )
    )
    panel = run["level_panel"]
    assert panel["ragged_hours"] == []
    assert panel["level_verdict"].startswith("BALANCED")
    assert [p["hour_of_day"] for p in run["by_hour_of_day"]] == [1, 2, 3]
    for p in run["by_hour_of_day"]:
        assert p["balanced"] is True
        assert p["mean_equity_end_balanced"] == p["mean_equity_end"]


def test_the_panel_is_an_intersection_not_the_days_spanned():
    """A day that contributes one corner of the clock does not join the
    panel — three days are SPANNED and two are comparable. The hour that
    day does cover is the ragged one, because it has a day the others
    lack."""
    run = _run(
        build_diurnal(
            _panel_ledger({0: (3,), 1: (1, 2, 3), 2: (1, 2, 3)}, lambda d, hod: -1.0 * hod)
        )
    )
    assert run["n_days"] == 3
    panel = run["level_panel"]
    assert panel["days"] == ["2026-08-02", "2026-08-03"]
    assert panel["ragged_hours"] == [3]
    assert _hod(run, 3)["n_days"] == 3 and _hod(run, 3)["balanced"] is False
    assert _hod(run, 1)["balanced"] is True


# ---------------------------------------------------------------------------
# The census: which RUNS in a ledger are entitled to a shape claim
#
# The defect this section exists for (2026-08-29) is not across readings the
# way the atlas's and the queue-score's were -- it is across RUNS inside one
# reading. Every archived reading was taken with `--run` on whatever run was
# live that day, which is always the shortest, so all of them printed
# UNDERPOWERED; the all-runs default path raised TypeError on the first run
# with no whole hour and had never once completed. Four fully powered runs
# (up to 11 whole days, 55 day-pairs) sat unread for nine days.
# ---------------------------------------------------------------------------

_CENSUS_HODS = list(range(4, 18))


def _run_rows(rid, curves, day0=0):
    """`_multiday`'s row emitter for one run of a multi-run ledger."""
    rows = [(rid, day0 * 1440 - 30, 0.0)]  # partial opening hour, excluded
    for day, hours in sorted(curves.items()):
        for hod, level in sorted(hours.items()):
            base = (day0 + day) * 1440 + hod * 60
            for k in range(25):
                rows.append((rid, base + k * 2, level - 1.0))
            rows.append((rid, base + 59, level))
    last = max((day0 + d) * 1440 + max(h) * 60 for d, h in curves.items())
    rows.append((rid, last + 61, 0.0))  # partial closing hour, excluded
    return rows


_RISING = {h: float(h * 20) for h in _CENSUS_HODS}
_FALLING = {h: float(-h * 20) for h in _CENSUS_HODS}
_BOWL = {h: float((h - 10) ** 2) for h in _CENSUS_HODS}


def _census_ledger():
    """Four runs, one of each thing a run can be. Ordered by run_id, so
    the LAST one is deliberately the weakest -- that is the run every
    archived reading was taken on."""
    rows = []
    # r1: two points inside a single hour. No whole hour at all.
    rows += [("r1_none", 0, 1.0), ("r1_none", 20, 2.0)]
    # r2: MIN_DAYS+1 days of the same curve -> powered, and it repeats.
    rows += _run_rows("r2_pow_rep", dict.fromkeys(range(MIN_DAYS + 1), _BOWL))
    # r3: the same span, days that disagree -> powered, does not repeat.
    rows += _run_rows(
        "r3_pow_not", {d: (_RISING if d % 2 == 0 else _FALLING) for d in range(MIN_DAYS + 1)}
    )
    # r4: MIN_DAYS days, but 17Z is missing from the last one, so the
    # weakest hour has MIN_DAYS-1 draws. Underpowered PROFILE while its
    # day-pairs are still scorable -- which is what makes it the mutant
    # detector for tallying shape over every run instead of the powered
    # ones.
    r4 = {d: dict(_RISING) for d in range(MIN_DAYS)}
    del r4[MIN_DAYS - 1][17]
    rows += _run_rows("r4_under", r4)
    return _ledger(sorted(rows, key=lambda r: (r[0], r[1])))


def test_a_run_with_no_whole_hour_is_unscorable_not_a_zero_draw_run():
    """LOAD-BEARING (mistakes #32, and the crash that hid the ledger).

    `min(..., default=0)` made a run that published NO hour-of-day mean
    report "weakest hour has 0 < 3 draws" -- an absent measurement
    printed as a measured zero -- and the panel intersection over the
    same empty family raised TypeError, which is why the all-runs path
    had never completed and the powered runs were never read.
    """
    report = build_diurnal(_census_ledger())  # must not raise
    run = _run(report, "r1_none")
    assert run["n_whole_hours"] == 0
    assert run["validity"]["profile_status"] == "unscorable"
    assert run["validity"]["profile_verdict"].startswith("UNSCORABLE")
    assert "0 <" not in run["validity"]["profile_verdict"]
    # And no panel is not a balanced panel over zero days.
    assert run["level_panel"]["level_verdict"].startswith("NO PANEL")
    assert run["level_panel"]["days"] == []

    census = report["power_census"]
    assert census["unscorable"]["run_ids"] == ["r1_none"]
    # Held OUT of the partition rather than counted as underpowered.
    assert census["profile"]["scorable"] == 3
    assert sum(census["profile"]["counts"].values()) == 3
    assert census["runs_published"] == 4


def test_shape_is_tallied_over_the_powered_runs_only():
    """LOAD-BEARING. `r4_under` has scorable day-pairs, an underpowered
    profile, and a REPEATS status: a census that tallies every run's
    `shape_status` counts it and prints repeats 2 -- a false positive
    manufactured out of a run whose weakest hour has fewer than MIN_DAYS
    draws."""
    census = build_diurnal(_census_ledger())["power_census"]
    assert census["profile"]["counts"] == {"powered": 2, "underpowered": 1}
    pw = census["shape_among_powered"]
    assert pw["n"] == 2
    assert pw["counts"] == {"repeats": 1, "does_not_repeat": 1, "underpowered": 0}
    assert [u["run_id"] for u in census["profile"]["powered_runs"]] == [
        "r2_pow_rep",
        "r3_pow_not",
    ]
    # r4 IS scorable and DOES say something -- it is excluded on power,
    # not because it had nothing to say.
    r4 = _run(build_diurnal(_census_ledger()), "r4_under")
    assert r4["validity"]["profile_status"] == "underpowered"
    assert r4["shape_agreement"]["shape_status"] == "repeats"
    assert r4["shape_agreement"]["n_scored_pairs"] >= 2


def test_every_census_count_carries_the_span_that_produced_it():
    """mistakes #35: a class count moves both because statuses changed
    and because runs entered the ledger under them, and nothing in the
    count says which. The spans are published beside it."""
    census = build_diurnal(_census_ledger())["power_census"]
    pw = census["shape_among_powered"]
    assert pw["days_span"] == [MIN_DAYS + 1, MIN_DAYS + 1]
    assert pw["total_scored_pairs"] == sum(
        u["n_scored_pairs"] for u in census["profile"]["powered_runs"]
    )
    assert pw["total_scored_pairs"] > 0
    for u in census["profile"]["powered_runs"]:
        assert u["n_days"] == MIN_DAYS + 1
        assert u["min_draws_per_hour"] >= MIN_DAYS


def test_the_latest_run_is_named_and_is_not_the_powered_one():
    """The whole failure mode was reading the newest run's verdict. The
    census names it explicitly and says whether it is entitled to the
    claim, rather than letting it be the last line on the screen."""
    census = build_diurnal(_census_ledger())["power_census"]
    latest = census["latest_run"]
    assert latest["run_id"] == "r4_under"
    assert latest["profile_status"] == "underpowered"
    assert latest["is_powered"] is False
    # ... while the ledger it sits in has answered the question twice.
    assert census["shape_among_powered"]["n"] == 2


def test_an_open_run_is_flagged_off_the_ledger_not_off_run_id_order():
    """A run still writing has a span that will grow, so its status is a
    snapshot. Newest run_id is not the test -- the last equity point is:
    here the alphabetically LAST run is the stale one."""
    # The ledger stores naive UTC, so the clock this is built against is
    # UTC too -- a naive LOCAL now would read as hours stale on any host
    # west of Greenwich, and pass this test for the wrong reason.
    now = datetime.now(UTC).replace(tzinfo=None)
    live = [("a_live", int((now - T0).total_seconds() // 60) - m, 1.0) for m in (5, 3, 1)]
    stale = [("z_stale", 0, 1.0), ("z_stale", 20, 2.0)]
    report = build_diurnal(_ledger(sorted(live + stale, key=lambda r: r[1])))
    assert _run(report, "a_live")["open"] is True
    assert _run(report, "z_stale")["open"] is False
    assert report["power_census"]["open_runs"] == ["a_live"]
