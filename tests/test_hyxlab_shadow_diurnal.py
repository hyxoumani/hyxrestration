"""Shadow diurnal report: hour-END is the level series, the intra-hour
range sits beside it, and the entry-drag model refuses to guess.
Synthetic ledgers, no network."""

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from simulator.shadow_diurnal import (
    HALF_TICK,
    LEVEL_STATUSES,
    MIN_DAYS,
    PANEL_STATUSES,
    SETTLEMENT_ABSENCES,
    SETTLEMENT_STATUSES,
    VERDICT_POPULATION,
    build_diurnal,
)

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


# --- Bound 9: the level column is cumulative -------------------------------


def _clock_ledger(n_days, delta, run_id="R"):
    """A run of `n_days` full UTC days sampled twice an hour, where the
    equity CHANGE over hour-of-day `hod` on day `d` is `delta(d, hod)`.
    Equity is the running sum, so the level column is cumulative --
    which is the whole thing bound 9 is about. One padding hour on each
    end absorbs the partial-bucket rule (bound 2)."""
    eq, level, minute = [], 0.0, -60
    eq.append((run_id, minute, level))
    minute = 0
    for d in range(n_days):
        for hod in range(24):
            level += delta(d, hod)
            eq.append((run_id, minute + 30, level))
            eq.append((run_id, minute + 59, level))
            minute += 60
    eq.append((run_id, minute + 30, level))
    return _ledger(eq)


def _level(run):
    return run["diurnal_level"]


def test_a_pure_drift_run_reads_flat_not_a_late_clock_trough():
    """LOAD-BEARING, and the reason bound 9 exists. Eleven days that lose
    the SAME amount every hour of the clock. The raw level column falls
    monotonically from 00Z to 23Z by hundreds and invites "the book bleeds
    into the afternoon"; the truth is that no hour of the day is special
    at all. The jitter differs by day so the draws are not all ties."""
    run = _run(build_diurnal(_clock_ledger(11, lambda d, hod: -10.0 + (d - 5.0))))
    lv = _level(run)

    # The artefact, still published: the raw column really does fall.
    bal = [p["mean_equity_end_balanced"] for p in run["by_hour_of_day"]]
    assert bal == sorted(bal, reverse=True)
    assert lv["raw_level_span"] > 200

    # And the de-trended column says the fall is the drift, not the clock.
    assert lv["level_shape_status"] == "powered"
    assert lv["level_shape_verdict"].startswith("FLAT")
    assert lv["significant_hours"] == []
    assert abs(lv["detrended_span"]) < 5
    assert lv["clock_complete"] is True
    assert lv["drift_per_day"] == -240.0  # 24 x the mean hourly change


def test_a_real_hour_of_day_effect_survives_the_de_trending():
    """The control for the test above. Same steady drift, plus 12Z losing
    an extra 100 on every single day. The de-trended column must find it
    and the sign test must name 12Z alone."""
    run = _run(
        build_diurnal(
            _clock_ledger(
                11,
                lambda d, hod: -10.0 + (d - 5.0) - (100.0 if hod == 12 else 0.0),
            )
        )
    )
    lv = _level(run)
    assert lv["level_shape_status"] == "powered"
    assert lv["significant_hours"] == [12]
    assert lv["level_shape_verdict"].startswith("HOUR-OF-DAY LEVEL EFFECT")
    twelve = next(p for p in lv["by_hour_of_day"] if p["hour_of_day"] == 12)
    assert twelve["n_below_centre"] == twelve["n_effective"] == 11
    assert twelve["demeaned"] < -90


def test_a_nine_day_panel_cannot_clear_the_ceiling_even_if_every_day_agrees():
    """MEASURED on `20260810T081931`: 12Z is 9 of 9 days below the grand
    mean and its sign p is 0.0039, against a 24-hour ceiling of 0.00208.
    That is UNDERPOWERED, not a measured flat, and the verdict must say
    how many days would fix it rather than print the strongest possible
    reading as a null."""
    run = _run(
        build_diurnal(
            _clock_ledger(
                9,  # nine full days -> the panel the live ledger has
                lambda d, hod: -10.0 + (d - 4.0) - (100.0 if hod == 12 else 0.0),
            )
        )
    )
    lv = _level(run)
    assert lv["n_panel_days"] == 9
    twelve = next(p for p in lv["by_hour_of_day"] if p["hour_of_day"] == 12)
    assert twelve["n_below_centre"] == twelve["n_effective"] == 9  # every day agrees
    assert twelve["sign_p"] == 0.003906
    assert lv["level_shape_status"] == "underpowered"
    assert lv["significant_hours"] == []  # cannot be claimed at this power
    assert lv["panel_days_needed"] == 10
    assert lv["level_shape_verdict"].startswith("UNDERPOWERED")
    assert "10 untied panel days are needed" in lv["level_shape_verdict"]


def test_the_ceiling_is_divided_by_the_hours_actually_tested():
    """Twenty-four hours are twenty-four chances. An unadjusted 0.05 per
    hour finds a trough on noise better than half the time, so the
    ceiling divides by the hours tested -- and by the hours TESTED, not
    by 24, so a run covering half a clock is not silently over-corrected."""
    run = _run(build_diurnal(_clock_ledger(11, lambda d, hod: -10.0 + (d - 5.0))))
    lv = _level(run)
    assert lv["hours_tested"] == 24
    assert lv["sign_p_ceiling"] == round(0.05 / 24, 6)
    # 11 draws an hour, one of which lands exactly ON the centre and is
    # dropped as a tie -- so the best test this panel can run is 10 draws.
    assert lv["best_achievable_sign_p"] == round(2.0 ** (1 - 10), 6)


def test_a_delta_across_an_outage_is_excluded_not_filed_under_one_hour():
    """The daemon goes down for four hours and comes back 5,000 lower.
    The buckets either side are adjacent in ROW order, so the naive
    close-to-close delta files a four-hour loss under a single
    hour-of-day -- exactly the value that manufactures a spike."""
    eq, level, minute = [("R", -60, 0.0)], 0.0, 0
    for d in range(11):
        for hod in range(24):
            if hod in (5, 6, 7, 8):
                minute += 60
                continue  # the nightly maintenance window: no rows at all
            level += -10.0 + (d - 5.0)
            if d == 1 and hod == 9:
                level -= 5000.0  # and one day it comes back 5,000 lower
            eq.append(("R", minute + 30, level))
            eq.append(("R", minute + 59, level))
            minute += 60
    eq.append(("R", minute + 30, level))
    lv = _level(_run(build_diurnal(_ledger(eq))))

    # Every 09Z delta spans the window, so 09Z has no honest draw at all
    # and is absent from the table rather than carrying a four-hour loss.
    assert lv["non_contiguous_deltas_excluded"] == 11
    assert [p["hour_of_day"] for p in lv["by_hour_of_day"]] == [
        h for h in range(24) if h not in (5, 6, 7, 8, 9)
    ]
    assert lv["hours_tested"] == 19
    assert lv["sign_p_ceiling"] == round(0.05 / 19, 6)
    # The 5,000 never reaches any hour-of-day mean.
    assert abs(lv["detrended_span"]) < 50
    assert lv["significant_hours"] == []


def test_a_run_with_no_panel_is_unscorable_not_flat():
    """Same partition rule as everywhere else: an absent measurement is
    not a measured zero (mistakes #32)."""
    run = _run(build_diurnal(_ledger([("R", 0, 0.0), ("R", 30, -5.0), ("R", 59, -7.0)])))
    lv = _level(run)
    assert lv["level_shape_status"] == "unscorable"
    assert lv["level_shape_verdict"].startswith("UNSCORABLE")
    assert lv["by_hour_of_day"] == []
    assert lv["detrended_span"] is None
    assert lv["drift_per_day"] is None


def test_drift_per_day_is_not_claimed_on_a_partial_clock():
    """A sum over 15 hours is not a daily rate, and nothing may read it
    as one."""
    eq, level, minute = [("R", -60, 0.0)], 0.0, 0
    for _d in range(4):
        for _hod in range(15):
            level -= 10.0
            eq.append(("R", minute + 30, level))
            minute += 60
        minute += 9 * 60  # the rest of the clock is simply absent
    eq.append(("R", minute + 30, level))
    lv = _level(_run(build_diurnal(_ledger(eq))))
    assert lv["clock_complete"] is False
    assert lv["hours_tested"] < 24
    assert lv["sign_p_ceiling"] == round(0.05 / lv["hours_tested"], 6)


def test_the_census_carries_no_level_term():
    """LEVEL is not poolable across runs -- each run seeds a different
    book. The census tallies shape only, and must never be extended."""
    census = build_diurnal(_clock_ledger(11, lambda d, hod: -10.0))["power_census"]
    assert "level" not in json.dumps(census)


def test_every_verdict_publisher_is_registered():
    """The registry that `atlas.py` and `queuescore.py` have and this
    module did not. An unregistered verdict is how a count gets plotted
    against readings that never tested it."""
    tree = ast.parse(Path("simulator/shadow_diurnal.py").read_text())
    published = {
        k.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for k in node.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value.endswith("_verdict")
    }
    assert published == set(VERDICT_POPULATION)
    assert VERDICT_POPULATION["level_shape_verdict"] == ("panel_day", LEVEL_STATUSES)
    # The unit is the point: only the `run`-unit verdicts may be pooled.
    assert {k for k, (unit, _) in VERDICT_POPULATION.items() if unit == "run"} == {
        "profile_verdict",
        "shape_verdict",
    }


def test_panel_status_names_the_three_states():
    """`level_verdict` was a sentence with no machine-readable status
    beside it, so nothing could tally it."""
    balanced = _run(
        build_diurnal(_panel_ledger({0: (1, 2, 3), 1: (1, 2, 3)}, lambda d, hod: -100.0 * d))
    )
    ragged = _run(
        build_diurnal(_panel_ledger({0: (1, 2, 3), 1: (1, 2)}, lambda d, hod: -100.0 * d))
    )
    none = _run(build_diurnal(_ledger([("R", 0, 0.0), ("R", 30, -5.0), ("R", 59, -7.0)])))
    assert balanced["level_panel"]["panel_status"] == "balanced"
    assert ragged["level_panel"]["panel_status"] == "ragged"
    assert none["level_panel"]["panel_status"] == "no_panel"
    assert {r["level_panel"]["panel_status"] for r in (balanced, ragged, none)} <= set(
        PANEL_STATUSES
    )


def _clock_ledger_with_fills(n_days, delta, fills_at, run_id="R"):
    """`_clock_ledger` plus fills. `fills_at(hod)` returns (qty, fee) for
    an hour of the clock, or None for no fill. One fill is written ten
    minutes into every matching hour of every day."""
    eq, fills, level, minute = [], [], 0.0, -60
    eq.append((run_id, minute, level))
    minute = 0
    for d in range(n_days):
        for hod in range(24):
            level += delta(d, hod)
            spec = fills_at(hod)
            if spec is not None:
                qty, fee = spec
                fills.append((run_id, "probe", minute + 10, qty, 0.20, fee))
            eq.append((run_id, minute + 30, level))
            eq.append((run_id, minute + 59, level))
            minute += 60
    eq.append((run_id, minute + 30, level))
    return _ledger(eq, fills)


def test_the_split_says_whether_an_hour_is_a_marking_move_or_a_fill_cost():
    """LOAD-BEARING, and the reason bound 10 exists. Two ledgers whose
    de-trended LEVEL columns are indistinguishable -- 12Z down ~96 on
    every day of eleven -- built by opposite mechanisms. In the first,
    12Z's loss is entirely what its own new fills cost on the way in; in
    the second the fills are flat across the clock and the book was
    simply marked down. `demeaned` alone cannot tell them apart, and only
    one of them is a transaction-cost story."""
    # (a) the hour costs 100 in entry drag: 100 contracts x half a tick
    # plus 99.5 of fee. Its standing book does exactly what every other
    # hour's does.
    cost = _run(
        build_diurnal(
            _clock_ledger_with_fills(
                11,
                lambda d, hod: -10.0 + (d - 5.0) - (100.0 if hod == 12 else 0.0),
                lambda hod: (100.0, 99.5) if hod == 12 else None,
            )
        )
    )["diurnal_level"]
    # (b) the same level shape with no fills anywhere: pure revaluation.
    mark = _run(
        build_diurnal(
            _clock_ledger(11, lambda d, hod: -10.0 + (d - 5.0) - (100.0 if hod == 12 else 0.0))
        )
    )["diurnal_level"]

    c12 = next(p for p in cost["by_hour_of_day"] if p["hour_of_day"] == 12)
    m12 = next(p for p in mark["by_hour_of_day"] if p["hour_of_day"] == 12)
    # The column that cannot discriminate: both hours look identical.
    assert c12["demeaned"] == m12["demeaned"]
    assert c12["sign_p"] == m12["sign_p"] == 0.000977
    # The column that can.
    assert c12["carrier"] == "drag"
    assert abs(c12["demeaned_reval"]) < 1e-6
    assert c12["demeaned_drag"] > 90
    assert m12["carrier"] == "reval"
    assert m12["demeaned_reval"] < -90
    assert abs(m12["demeaned_drag"]) < 1e-6
    # And the ceiling on a cost story is published for each: on the
    # marking ledger no hour-of-day fill cost could account for ANY of it.
    assert mark["drag_deviation_span"] == 0.0
    assert mark["reval_deviation_span"] > 90
    assert cost["drag_deviation_span"] > 90
    assert "CEILING" in cost["level_split_verdict"]


def test_the_split_is_an_exact_identity_at_every_hour():
    """`d_equity = reval - entry_drag` holds per ROW, so it survives
    averaging and de-meaning with no residual. That is what makes the
    split readable while the sign test is still underpowered -- it is
    arithmetic on the same rows, not a second test."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_fills(
                9,
                lambda d, hod: -10.0 + (d - 4.0) - (0.5 * hod),
                lambda hod: (20.0 * hod, 3.0) if hod % 2 else None,
            )
        )
    )["diurnal_level"]
    assert lv["level_shape_status"] == "underpowered"  # the test has no power
    assert lv["level_split_status"] == "scorable"  # the identity needs none
    # Exact before publication; the only residual is the printed
    # rounding (0.05 + 0.05 + 0.005 at worst).
    for p in lv["by_hour_of_day"]:
        assert abs(p["demeaned"] - (p["demeaned_reval"] - p["demeaned_drag"])) <= 0.105
    assert lv["reval_carried_hours"] + sum(
        1 for p in lv["by_hour_of_day"] if p["carrier"] == "drag"
    ) == len(lv["by_hour_of_day"])


def test_one_ungated_fill_makes_the_split_unscorable_not_a_partial_average():
    """`reval` is null wherever the entry-drag model does not apply
    (bound 1). Averaging only the rows that HAVE one would split a
    deviation computed over rows the terms were not taken from. Absent,
    not zero -- and the level test above must survive the refusal."""
    eq, fills, level, minute = [("R", -60, 0.0)], [], 0.0, 0
    for d in range(11):
        for hod in range(24):
            level += -10.0 + (d - 5.0)
            if d == 3 and hod == 7:
                fills.append(("R", "wide_maker", minute + 10, 50.0, 0.20, 1.0))
            eq.append(("R", minute + 30, level))
            eq.append(("R", minute + 59, level))
            minute += 60
    eq.append(("R", minute + 30, level))
    lv = _run(build_diurnal(_ledger(eq, fills)))["diurnal_level"]

    assert lv["level_split_status"] == "unscorable"
    assert lv["rows_without_reval"] == 1
    assert lv["level_split_verdict"].startswith("UNSCORABLE")
    assert lv["reval_carried_hours"] is None
    assert lv["drag_deviation_span"] is lv["reval_deviation_span"] is None
    assert all(p["demeaned_reval"] is None and p["carrier"] is None for p in lv["by_hour_of_day"])
    # The refusal is scoped to the split: the level test is unaffected.
    assert lv["level_shape_status"] == "powered"
    assert lv["level_shape_verdict"].startswith("FLAT")


def test_the_split_reads_the_same_rows_as_the_deviation_it_splits():
    """The profile's `mean_reval` column averages EVERY whole hour of the
    run; the deviation being split is over the contiguous deltas of the
    balanced panel. Differencing one against the other is bound 7's
    composition defect one axis over, so the split re-reads the terms off
    its own rows. Here 09Z's delta always spans a four-hour outage and is
    excluded -- its fills' 500 of drag must reach no split cell."""
    eq, fills, level, minute = [("R", -60, 0.0)], [], 0.0, 0
    for d in range(11):
        for hod in range(24):
            if hod in (5, 6, 7, 8):
                minute += 60
                continue
            level += -10.0 + (d - 5.0)
            if hod == 9:
                fills.append(("R", "probe", minute + 10, 100.0, 0.20, 499.5))
            eq.append(("R", minute + 30, level))
            eq.append(("R", minute + 59, level))
            minute += 60
    eq.append(("R", minute + 30, level))
    lv = _run(build_diurnal(_ledger(eq, fills)))["diurnal_level"]

    assert lv["non_contiguous_deltas_excluded"] == 11
    assert 9 not in [p["hour_of_day"] for p in lv["by_hour_of_day"]]
    # Every remaining row has no fill at all, so the excluded hour's 500
    # of drag is nowhere in the split.
    assert lv["level_split_status"] == "scorable"
    assert lv["grand_mean_drag"] == 0.0
    assert lv["drag_deviation_span"] == 0.0


# --- Bound 11: reval absorbs settlement, so a hole must be controlled ------


def _clock_ledger_with_settlements(n_days, delta, settled_at, run_id="R"):
    """`_clock_ledger` plus settlements. `settled_at(d, hod)` is True when
    a position expires during that hour of that day; one settlement row is
    written ten minutes into every matching hour."""
    eq, setts, level, minute = [], [], 0.0, -60
    eq.append((run_id, minute, level))
    minute = 0
    for d in range(n_days):
        for hod in range(24):
            level += delta(d, hod)
            if settled_at(d, hod):
                setts.append((run_id, minute + 10))
            eq.append((run_id, minute + 30, level))
            eq.append((run_id, minute + 59, level))
            minute += 60
    eq.append((run_id, minute + 30, level))
    return _ledger(eq, settlements=setts)


def test_a_hole_mostly_carried_by_settlement_rows_reads_confounded():
    """LOAD-BEARING, and the reason bound 11 exists. `reval` is a
    RESIDUAL, so it absorbs settlement as well as marking, and bound 5
    only counted settlements so a contaminated hour would be visible.
    Here 12Z is down on all eleven days -- it survives the sign test and
    is still the trough of the balanced control clock -- but it is down
    120 on the six days a position expires in it and only 40 on the five
    it does not. Calling the whole hole a marking move on the standing
    book overstates it by more than twice, and `demeaned` cannot see it."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 - (0.0 if hod != 12 else 110.0 if d < 6 else 30.0),
                lambda d, hod: hod == 12 and d < 6,
            )
        )
    )["diurnal_level"]
    chk = lv["settlement_check"]

    assert lv["settlement_status"] == "confounded"
    assert lv["settlement_verdict"].startswith("CONFOUNDED")
    assert lv["settlement_rows"] == 6
    assert lv["hours_with_settlements"] == 1
    assert chk["hour_of_day"] == 12
    # The hour is real in sign and still the extreme of the control clock
    # -- retention alone is what condemns it, and it must be able to.
    assert chk["control_rank"] == 1
    assert 0.40 < chk["retained_share"] < 0.45
    assert chk["n_settlement_rows"] == 6 and chk["n_free_rows"] == 5
    # Not a refutation, and it does not touch whether the hour exists.
    assert "not a refutation" in lv["settlement_verdict"]
    assert _hod_level(lv, 12)["sign_p"] == 0.000977


def _hod_level(lv, hod):
    return next(p for p in lv["by_hour_of_day"] if p["hour_of_day"] == hod)


def test_a_marking_hole_that_settlements_merely_coincide_with_survives():
    """The control must not condemn an hour just because settlements land
    in it. Here 12Z is down exactly 100 on every day, and a settlement
    lands in it on six of the eleven -- the rows that carried one are
    indistinguishable from the rows that did not, so nothing about the
    hole goes with the expiries."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 - (100.0 if hod == 12 else 0.0),
                lambda d, hod: hod == 12 and d < 6,
            )
        )
    )["diurnal_level"]
    chk = lv["settlement_check"]

    assert lv["settlement_status"] == "survives"
    assert lv["settlement_verdict"].startswith("SURVIVES")
    assert chk["hour_of_day"] == 12
    assert chk["control_rank"] == 1
    assert chk["retained_share"] > 1.0  # dropping them does not shrink it
    assert chk["control_days"] == [f"2026-08-{7 + d:02d}" for d in range(5)]


def test_the_settlement_gap_is_taken_within_the_hour_not_pooled():
    """LOAD-BEARING. Settlements cluster in the hours the shape is about
    -- on the live ledger every one lands between 11Z and 20Z -- so
    'settlement rows average worse than settlement-free rows' pooled over
    the panel is a RESTATEMENT of the hour-of-day effect, not a control on
    it. On the ledger above every settlement row sits in the one hour that
    is 100 down, so a pooled contrast reads about -96/hr while the honest
    within-hour gap is exactly zero."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 - (100.0 if hod == 12 else 0.0),
                lambda d, hod: hod == 12 and d < 6,
            )
        )
    )["diurnal_level"]
    rows = [
        (p["n_settlement_rows"], p["mean_d_settlement"], p["mean_d_free"])
        for p in lv["by_hour_of_day"]
    ]
    dirty = [(a, b) for n, a, b in rows if n]
    assert len(dirty) == 1
    assert abs(dirty[0][0] - dirty[0][1]) < 1e-6  # same hour, same number
    assert lv["stratified_settlement_gap"] == 0.0
    # What the pooled contrast would have said, computed here so the
    # difference is on the record rather than asserted in prose.
    pooled = _hod_level(lv, 12)["mean_d_settlement"] - (
        sum(p["mean_d_free"] * (p["n_days"] - p["n_settlement_rows"]) for p in lv["by_hour_of_day"])
        / sum(p["n_days"] - p["n_settlement_rows"] for p in lv["by_hour_of_day"])
    )
    assert pooled < -90


def test_an_hour_whose_every_draw_settled_is_unscorable_not_surviving():
    """Absent, not zero (mistakes #32). If a settlement lands in 12Z on
    every day of the panel there is no settlement-free reading of that
    hour to compare against, and a control that cannot run must say so
    rather than pass the hour through."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 + (d - 5.0) - (100.0 if hod == 12 else 0.0),
                lambda d, hod: hod == 12,
            )
        )
    )["diurnal_level"]

    assert lv["settlement_status"] == "unscorable"
    assert lv["settlement_verdict"].startswith("UNSCORABLE")
    assert lv["settlement_check"] is None
    assert lv["stratified_settlement_gap"] is None
    # Scoped to the control: the level test and the split are unaffected.
    assert lv["level_shape_status"] == "powered"
    assert lv["level_split_status"] == "scorable"
    assert _hod_level(lv, 12)["n_settlement_rows"] == 11


def test_no_settlements_at_all_reads_clean_which_is_not_a_passed_test():
    """A panel with no settlement row anywhere has nothing to control
    for. That is an absence of contamination, and the verdict says so in
    those words -- reading it as 'the marking story was tested and held'
    is exactly the mistake bound 11 exists to stop."""
    lv = _run(
        build_diurnal(_clock_ledger(11, lambda d, hod: -10.0 + (d - 5.0) - (100.0 * (hod == 12))))
    )["diurnal_level"]

    assert lv["settlement_status"] == "clean"
    assert lv["settlement_rows"] == 0
    assert lv["settlement_free_rows"] == 264
    assert lv["settlement_check"] is None
    assert "not a test the shape passed" in lv["settlement_verdict"]
    assert all(p["n_settlement_rows"] == 0 for p in lv["by_hour_of_day"])


def test_the_settlement_control_is_registered_with_the_other_verdicts():
    """An unregistered verdict is how a count gets plotted against
    readings that never tested it (mistakes #32/#33/#35). LEVEL-side
    verdicts are per PANEL DAY and may never be pooled by the census."""
    assert VERDICT_POPULATION["settlement_verdict"] == ("panel_day", SETTLEMENT_STATUSES)
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 - (100.0 if hod == 12 else 0.0),
                lambda d, hod: hod == 12 and d < 6,
            )
        )
    )["diurnal_level"]
    assert lv["settlement_status"] in SETTLEMENT_STATUSES


def test_an_hour_that_keeps_its_size_but_loses_its_rank_reads_confounded():
    """Retention is not enough on its own. Dropping settlement rows makes
    the surviving panel RAGGED -- 12Z's five remaining draws are a
    different set of DAYS from 00Z's eleven -- which is bound 7's
    composition defect on the delta axis. Here 12Z is down 100 on every
    day and so keeps all of itself, but on the balanced control panel (the
    five days it is settlement-free, every hour averaging those same days)
    18Z is three times deeper. The ragged subset could not have seen
    that."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: (
                    -10.0 - (100.0 if hod == 12 else 0.0) - (300.0 if hod == 18 and d >= 6 else 0.0)
                ),
                lambda d, hod: hod == 12 and d < 6,
            )
        )
    )["diurnal_level"]
    chk = lv["settlement_check"]

    assert _hod_level(lv, 12)["sign_p"] == 0.000977  # still the strongest hour
    assert chk["retained_share"] > 1.0  # and it keeps every bit of its size
    assert chk["control_rank"] == 2
    assert chk["control_deepest_hour"] == 18
    assert lv["settlement_status"] == "confounded"


def test_the_balanced_control_can_clear_an_hour_the_ragged_subset_condemns():
    """The control panel must be BALANCED, and this is the case that
    proves it has to be. 12Z is down 100 on every day; 18Z is down 300,
    but only on the six days a position expires in 12Z. Over the whole
    panel 18Z is therefore the deeper hour and 12Z would read second --
    yet on the five days 12Z is actually settlement-free 18Z is an
    ordinary hour, and the comparison the control is asked to make is
    between hours averaging THE SAME days."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: (
                    -10.0 - (100.0 if hod == 12 else 0.0) - (300.0 if hod == 18 and d < 6 else 0.0)
                ),
                lambda d, hod: hod == 12 and d < 6,
            )
        )
    )["diurnal_level"]
    chk = lv["settlement_check"]

    # Over the whole panel 18Z is the deeper hour by a distance...
    assert _hod_level(lv, 18)["demeaned"] < _hod_level(lv, 12)["demeaned"]
    # ...and on the days the control actually compares, it is not.
    assert chk["control_rank"] == 1
    assert chk["control_deepest_hour"] == 12
    assert lv["settlement_status"] == "survives"


def test_no_hour_holds_both_kinds_of_row_and_the_gap_is_absent_not_zero():
    """The within-hour gap needs an hour holding BOTH kinds of row. Here
    04Z settles on every one of its eleven days and no other hour settles
    at all, so there is no hour to take a contrast in -- while the hour
    under test, 12Z, is clean and perfectly scorable. Found by the
    all-runs path, which is the same way bound 8's crash was found: the
    live run is never the awkward one."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 - (100.0 if hod == 12 else 0.0),
                lambda d, hod: hod == 4,
            )
        )
    )["diurnal_level"]

    assert lv["settlement_rows"] == 11
    assert lv["stratified_settlement_gap"] is None
    assert "UNAVAILABLE" in lv["settlement_verdict"]
    assert "pooled contrast is the confound" in lv["settlement_verdict"]
    # 12Z carried no settlement, so the control still runs and clears it.
    assert lv["settlement_check"]["hour_of_day"] == 12
    assert lv["settlement_check"]["n_settlement_rows"] == 0
    assert lv["settlement_status"] == "survives"


# --- Bound 11b: one hour is scored, every contaminated hour is swept -------


def _sweep_hour(lv, hod):
    return next(q for q in lv["settlement_sweep"] if q["hour_of_day"] == hod)


def test_the_sweep_re_reads_every_contaminated_hour_not_only_the_scored_one():
    """The control scores the STRONGEST hour, which leaves the rest of a
    contaminated clock unread. Here 05Z is contaminated too and is
    nowhere near the strongest hour, yet dropping its settlement rows is
    exactly as cheap. It must appear in the sweep."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 - (100.0 if hod == 12 else 0.0),
                lambda d, hod: hod in (5, 12) and d < 6,
            )
        )
    )["diurnal_level"]

    assert lv["settlement_check"]["hour_of_day"] == 12  # scored hour unchanged
    assert [q["hour_of_day"] for q in lv["settlement_sweep"]] == [5, 12]
    assert _sweep_hour(lv, 5)["n_settlement_rows"] == 6
    assert _sweep_hour(lv, 5)["n_free_rows"] == 5
    assert _sweep_hour(lv, 12)["retained_share"] == lv["settlement_check"]["retained_share"]


def test_an_hour_that_changes_sign_without_its_settlement_rows_is_flagged():
    """The finding this rung exists for. 07Z is -200 on the six days a
    position expires in it and +200 on the five it does not, so it reads
    as a hole over the whole panel and as a PEAK on its settlement-free
    rows. The sweep must say the sign changed -- that is a statement
    about how soft the rest of the clock is, and it is invisible to a
    control that only ever looks at 12Z."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: (
                    -10.0
                    - (100.0 if hod == 12 else 0.0)
                    + ((-200.0 if d < 6 else 200.0) if hod == 7 else 0.0)
                ),
                lambda d, hod: hod in (7, 12) and d < 6,
            )
        )
    )["diurnal_level"]

    seven = _sweep_hour(lv, 7)
    assert seven["demeaned"] < 0 < seven["demeaned_free"]
    assert seven["flipped"] is True
    assert _sweep_hour(lv, 12)["flipped"] is False
    assert lv["sweep_hours_scorable"] == 2
    assert lv["sweep_hours_flipped"] == 1
    assert "change SIGN" in lv["settlement_verdict"]


def test_the_sweep_is_a_diagnostic_and_never_moves_the_verdict():
    """A flip at another hour is a warning about the panel, not evidence
    about the hour under test. The two ledgers below are identical at 12Z
    and differ only in whether 07Z flips: the scored verdict and its
    control rank must not move. (The de-meaned SIZES legitimately do:
    both the ragged and the balanced reading are deviations from a grand
    mean that every hour's rows help set, so adding a loud hour anywhere
    shifts the centre. Which is why the thing this test pins is the
    ORDERING, and why the verdict is a rank and a retention rather than
    a number of dollars.)"""

    def lv_for(seven):
        return _run(
            build_diurnal(
                _clock_ledger_with_settlements(
                    11,
                    lambda d, hod: (
                        -10.0
                        - (100.0 if hod == 12 else 0.0)
                        + ((-200.0 if d < 6 else 200.0) if hod == 7 and seven else 0.0)
                    ),
                    lambda d, hod: hod in (7, 12) and d < 6,
                )
            )
        )["diurnal_level"]

    flat, flipping = lv_for(False), lv_for(True)
    assert flipping["sweep_hours_flipped"] == 1 and flat["sweep_hours_flipped"] == 0
    assert flat["settlement_status"] == flipping["settlement_status"]
    assert flat["settlement_check"]["control_rank"] == flipping["settlement_check"]["control_rank"]
    assert (
        flat["settlement_check"]["control_deepest_hour"]
        == flipping["settlement_check"]["control_deepest_hour"]
        == 12
    )


def test_a_fully_contaminated_hour_is_listed_with_its_re_reading_absent():
    """Absent, not zero (mistakes #32). 04Z settles on every one of its
    days, so it has no settlement-free reading at all -- it must still be
    LISTED (it is contaminated, which is the sweep's subject) with
    `demeaned_free` and `flipped` None, and it must not be counted among
    the hours the sweep actually scored."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 - (100.0 if hod == 12 else 0.0),
                lambda d, hod: hod == 4 or (hod == 12 and d < 6),
            )
        )
    )["diurnal_level"]

    four = _sweep_hour(lv, 4)
    assert four["n_free_rows"] == 0
    assert four["demeaned_free"] is None and four["flipped"] is None
    assert four["retained_share"] is None
    assert lv["sweep_hours_scorable"] == 1  # 12Z only
    assert lv["sweep_hours_flipped"] == 0


def test_a_panel_where_every_row_settled_sweeps_hours_rather_than_nothing():
    """An empty sweep means "no hour was contaminated", which is the
    OPPOSITE finding to "every row settled". The control itself is
    unscorable here -- there is no free reading of the strongest hour --
    and the sweep must still come through it naming the hours."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 - (100.0 if hod == 12 else 0.0),
                lambda d, hod: True,
            )
        )
    )["diurnal_level"]

    assert lv["settlement_status"] == "unscorable"
    assert lv["settlement_check"] is None
    assert len(lv["settlement_sweep"]) == 24
    assert all(q["flipped"] is None for q in lv["settlement_sweep"])
    assert lv["sweep_hours_scorable"] == 0 and lv["sweep_hours_flipped"] == 0


def test_a_clean_panel_sweeps_zero_hours_which_is_a_measurement():
    """The mirror of the case above: nothing settled anywhere, so zero
    contaminated hours IS the reading and the sweep is an empty list
    rather than None."""
    lv = _run(
        build_diurnal(_clock_ledger(11, lambda d, hod: -10.0 - (100.0 * (hod == 12))))
    )["diurnal_level"]

    assert lv["settlement_status"] == "clean"
    assert lv["settlement_sweep"] == []
    assert lv["sweep_hours_scorable"] == 0 and lv["sweep_hours_flipped"] == 0


def test_the_verdict_says_why_the_control_is_taken_at_one_hour():
    """The choice of unit is the finding of this rung, so it has to be in
    the report and not only in a docstring: 24 balanced controls are 24
    different day-panels, and 24 verdicts are 24 more chances at the
    multiplicity the sign-test ceiling exists to control."""
    lv = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 - (100.0 if hod == 12 else 0.0),
                lambda d, hod: hod == 12 and d < 6,
            )
        )
    )["diurnal_level"]

    v = lv["settlement_verdict"]
    assert "at ONE hour deliberately" in v
    assert "24 different day-panels" in v and "multiplicity" in v
    assert "never as a verdict" in v


def test_a_control_that_never_ran_is_not_a_control_that_found_nothing():
    """Bound 11c. `unscorable` pools two OPPOSITE ledgers: a run that
    produced no contiguous hour-to-hour change never reached the control
    and carries no evidence about contamination either way, while a run
    whose every draw at the tested hour settled reached it and lost to
    total contamination. One count of 29 cannot be both."""
    no_delta = _run(build_diurnal(_ledger([("R", 0, 0.0), ("R", 30, -5.0), ("R", 59, -7.0)])))[
        "diurnal_level"
    ]
    saturated = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 + (d - 5.0) - (100.0 if hod == 12 else 0.0),
                lambda d, hod: hod == 12,
            )
        )
    )["diurnal_level"]

    assert no_delta["settlement_status"] == saturated["settlement_status"] == "unscorable"
    # ...and the status is where the likeness stops.
    assert no_delta["settlement_absence"] == "no_delta"
    assert saturated["settlement_absence"] == "all_settled_at_hour"
    assert no_delta["settlement_absence"] in SETTLEMENT_ABSENCES
    assert saturated["settlement_absence"] in SETTLEMENT_ABSENCES
    # The unreached control says so in prose too, not only in the code.
    assert "never REACHED" in no_delta["settlement_verdict"]


def test_a_scored_settlement_control_has_no_absence_to_explain():
    """The field is the reason an absent reading is absent, so a control
    that RAN must carry None -- a code there would make a scored panel
    tally as a missing one."""
    clean = _run(
        build_diurnal(_clock_ledger(11, lambda d, hod: -10.0 - (100.0 * (hod == 12))))
    )["diurnal_level"]
    scored = _run(
        build_diurnal(
            _clock_ledger_with_settlements(
                11,
                lambda d, hod: -10.0 + (d - 5.0) - (100.0 if hod == 12 else 0.0),
                lambda d, hod: hod == 12 and d < 6,
            )
        )
    )["diurnal_level"]

    assert clean["settlement_status"] == "clean"
    assert scored["settlement_status"] in ("survives", "confounded")
    assert clean["settlement_absence"] is None
    assert scored["settlement_absence"] is None


def test_the_census_splits_settlement_absences_and_counts_them_in_runs():
    """The census must name WHY each control could not run, and must read
    the absences against the runs that could have had one (mistakes #35).
    On this ledger every run is `no_delta`-free or not, and the split is
    what distinguishes a young ledger from a contaminated one."""
    report = build_diurnal(_census_ledger())
    a = report["power_census"]["settlement_absence"]

    assert a["runs_published"] == 4
    assert a["counts"]["no_delta"] == 1  # r1_none: two points inside one hour
    assert a["run_ids"]["no_delta"] == ["r1_none"]
    assert a["counts"]["all_settled_at_hour"] == 0
    assert a["unscorable"] == 1
    assert a["reached"] == 3
    assert a["reached"] + a["counts"]["no_delta"] == a["runs_published"]


def test_the_census_does_not_pool_the_scored_settlement_statuses():
    """`settlement_verdict` is a PANEL DAY verdict: "17 runs read clean"
    pools readings taken on unlike panels, which is exactly what the
    census rule forbids for LEVEL. Only absences -- a property of the run
    -- may be counted here."""
    census = build_diurnal(_census_ledger())["power_census"]
    blob = json.dumps(census)

    for scored in ("clean", "survives", "confounded"):
        assert scored not in blob
    assert set(census["settlement_absence"]["counts"]) == set(SETTLEMENT_ABSENCES)
    assert "panel day" in census["settlement_absence"]["rule"]
