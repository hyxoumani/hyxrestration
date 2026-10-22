"""EXP-961 — the batch-run budget measured against the journal, not the spec.

The failure this closes: `test_kalshi_batch_units_finish_before_the_live_fade_
window` compares two constants (OnCalendar + BATCH_RUN_BUDGET_H) and is green
whenever they agree, however far the real runs have drifted from the budget.
On 2026-08-03 `hyxlab-sweep` ran 10h06m38s against a written 8.0h and nothing
in the repo noticed.

Pinned here, one at a time: the wall clock must be read from the right half of
systemd's `Consumed ... over ...` line, an unreadable journal must not read as
a clean one, a breach must FAIL, and a fade-window overlap already on the
record must decay to a non-failing WATCH.
"""

from datetime import UTC, datetime, timedelta

import pytest

import collector.qa as qa
from collector.qa import BatchRun

NOW = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)

# Verbatim from the journal of the run this experiment came from.
REAL_LINE = (
    "2026-08-03T21:16:38+00:00 hyz systemd[749]: hyxlab-sweep.service: "
    "Consumed 1h 12min 54.065s CPU time over 10h 6min 38.768s wall clock time, "
    "6.8G memory peak."
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Own section record per test — the real one gates production alarms."""
    monkeypatch.setattr(qa, "STATE", tmp_path / "sections.json")
    qa._failures.clear()
    qa._skipped.clear()
    qa._passes = 0
    yield
    qa._failures.clear()
    qa._skipped.clear()


def _run(runs, now=NOW):
    qa._failures.clear()
    qa._skipped.clear()
    qa.qa_batch_run_budget(runs=runs, now=now)
    return set(qa._failures), list(qa._skipped)


def _sweep(end, wall_h):
    return BatchRun("hyxlab-sweep.timer", end, wall_h)


# --- duration parsing -------------------------------------------------------


@pytest.mark.parametrize(
    "text,secs",
    [
        ("10h 6min 38.768s", 10 * 3600 + 6 * 60 + 38.768),
        ("25min 14.784s", 25 * 60 + 14.784),
        ("1h 12min 54.065s", 3600 + 12 * 60 + 54.065),
        ("1d 0h21min", 86400 + 21 * 60),
        ("704ms", 0.704),
        ("1.5s", 1.5),
    ],
)
def test_systemd_duration_forms_seen_in_this_journal(text, secs):
    assert qa.parse_systemd_duration(text) == pytest.approx(secs)


def test_a_duration_with_no_token_is_none_not_zero():
    assert qa.parse_systemd_duration("no duration here") is None


# --- journal reading --------------------------------------------------------


def test_wall_clock_is_read_after_over_not_the_cpu_time_before_it(monkeypatch):
    """The discrimination control: the same line carries both numbers.

    A parser that grabs the first duration on the line gets 1.21h of CPU time
    and would clear even the OLD 8.0h budget while the unit really ran 10.11h.
    """
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: REAL_LINE)
    runs = qa.read_batch_runs(now=NOW)
    sweep = runs["hyxlab-sweep.timer"]
    assert len(sweep) == 1
    assert sweep[0].wall_h == pytest.approx(10.1107, abs=1e-3)
    assert sweep[0].end == datetime(2026, 8, 3, 21, 16, 38, tzinfo=UTC)
    # ...and the start is derived, so the interval is the real one.
    assert abs(sweep[0].start - datetime(2026, 8, 3, 11, 10, 0, tzinfo=UTC)) < timedelta(seconds=1)


def test_another_units_line_in_the_same_journal_is_not_counted(monkeypatch):
    """`journalctl -u X` can carry lines about other cgroups; key on the name."""
    other = REAL_LINE.replace("hyxlab-sweep.service", "hyxlab-breadth.service")
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: other)
    assert qa.read_batch_runs(now=NOW)["hyxlab-sweep.timer"] == []


def test_an_unreadable_journal_is_none_and_a_silent_unit_is_empty(monkeypatch):
    """None and [] must stay distinct: only one of them is a problem."""
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: None)
    assert qa.read_batch_runs(now=NOW)["hyxlab-sweep.timer"] is None
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: "")
    assert qa.read_batch_runs(now=NOW)["hyxlab-sweep.timer"] == []


# --- the check --------------------------------------------------------------


def test_runs_inside_budget_pass():
    failed, skipped = _run(
        {
            "hyxlab-sweep.timer": [_sweep(datetime(2026, 8, 4, 16, 17, tzinfo=UTC), 10.11)],
            "hyxlab-tradepass.timer": [
                BatchRun("hyxlab-tradepass.timer", datetime(2026, 8, 4, 9, 26, tzinfo=UTC), 2.85)
            ],
        }
    )
    assert not failed and not skipped


def _poly(end, wall_h):
    return BatchRun("hyxlab-poly-sweep.timer", end, wall_h)


def test_a_poly_sweep_past_its_wall_budget_fails_as_a_stale_budget(capsys):
    """Added 2026-10-17: the 15.5-17.4h band lived in ops.md prose and no
    check read a done line against it. A run past the budget (8.0h since
    2026-10-21, from 17.5h: three skip-era done lines at 7.00-7.72h) must
    FAIL and say so with the budget, exactly as the Kalshi sweep's breach
    does."""
    runs = {"hyxlab-poly-sweep.timer": [_poly(datetime(2026, 8, 4, 22, 15, tzinfo=UTC), 9.0)]}
    failed, _ = _run(runs)
    assert failed == {"batch units within measured run budget"}
    assert "hyxlab-poly-sweep.timer ran 9.00h (budget 8h)" in capsys.readouterr().out


def test_a_poly_sweep_inside_the_fade_window_spent_no_kalshi_quota():
    """The 2026-08-21 exemption, kept: a Polymarket run spanning 23:00-04:00Z
    is not a fade overlap (it spends none of the quota the window protects),
    so being budgeted must not make it one. 04:15Z -> 01:00Z, 20.75h, is
    over the wall budget -- the only reason it fails -- and the overlap key
    must not be recorded for it."""
    runs = {"hyxlab-poly-sweep.timer": [_poly(datetime(2026, 8, 5, 1, 0, tzinfo=UTC), 20.75)]}
    failed, _ = _run(runs)
    assert failed == {"batch units within measured run budget"}
    reported = qa._load_state(qa.STATE).get("batch-run-budget", {}).get("reported", [])
    assert "hyxlab-poly-sweep.timer@2026-08-05T01:00" not in reported, reported
    # Inside budget and inside the window (18:30Z -> 02:00Z, a DIFFERENT end
    # so the record cannot excuse it as already reported): nothing at all.
    runs = {"hyxlab-poly-sweep.timer": [_poly(datetime(2026, 8, 5, 2, 0, tzinfo=UTC), 8.0 - 0.5)]}
    failed, _ = _run(runs)
    assert not failed


def test_the_2026_08_03_sweep_breaches_the_budget_it_was_written_against():
    """The regression proper: 10.11h measured against the 8.0h then on record."""
    runs = {"hyxlab-sweep.timer": [_sweep(datetime(2026, 8, 3, 21, 16, 38, tzinfo=UTC), 10.11)]}
    qa.BATCH_RUN_BUDGET_H["hyxlab-sweep.timer"] = 8.0
    try:
        failed, _ = _run(runs)
    finally:
        qa.BATCH_RUN_BUDGET_H["hyxlab-sweep.timer"] = 10.5
    assert failed == {"batch units within measured run budget"}


def test_no_completed_run_reads_unverified_never_pass(capsys):
    failed, skipped = _run({"hyxlab-sweep.timer": None, "hyxlab-tradepass.timer": None})
    assert not failed
    assert skipped == ["batch-run-budget"]
    assert "UNVERIFIED" in capsys.readouterr().out


def test_one_unmeasured_unit_does_not_hide_behind_a_measured_one(capsys):
    failed, skipped = _run(
        {
            "hyxlab-sweep.timer": [_sweep(datetime(2026, 8, 4, 16, 17, tzinfo=UTC), 10.11)],
            "hyxlab-tradepass.timer": None,
        }
    )
    assert not failed and not skipped
    assert "UNMEASURED: hyxlab-tradepass.timer" in capsys.readouterr().out


# --- fade-window overlap ----------------------------------------------------


def test_the_real_08_03_interval_did_not_touch_the_fade_window():
    """It ended 21:16:38Z. Close — 1h43m — but clear, and clear is a pass."""
    assert (
        qa._fade_overlap_h(
            datetime(2026, 8, 3, 11, 10, tzinfo=UTC),
            datetime(2026, 8, 3, 21, 16, 38, tzinfo=UTC),
        )
        == 0
    )


def test_a_run_ending_after_midnight_is_measured_against_the_prior_days_window():
    """The 07-29 poly-sweep shape: 05:00Z start, 05:22Z finish the NEXT day.

    Naive same-day arithmetic scores this 0 — the window it crossed opened on
    the day before it ended.
    """
    overlap = qa._fade_overlap_h(
        datetime(2026, 7, 29, 5, 0, tzinfo=UTC),
        datetime(2026, 7, 30, 5, 22, tzinfo=UTC),
    )
    assert overlap == pytest.approx(5.0)  # the whole 23:00-04:00Z window


def test_an_overlapping_run_fails_once_then_decays_to_a_watch(capsys):
    runs = {
        "hyxlab-sweep.timer": [
            _sweep(datetime(2026, 8, 4, 1, 0, tzinfo=UTC), 9.0)  # 16:00Z -> 01:00Z
        ]
    }
    failed, _ = _run(runs)
    assert failed == {"batch units within measured run budget"}

    failed, _ = _run(runs)
    assert not failed
    out = capsys.readouterr().out
    assert "WATCH" in out and "already reported" in out


def test_a_budget_breach_keeps_failing_even_once_reported():
    """Unlike an overlap, this one is repairable — so it must not decay."""
    runs = {"hyxlab-sweep.timer": [_sweep(datetime(2026, 8, 4, 16, 17, tzinfo=UTC), 12.0)]}
    assert _run(runs)[0] == {"batch units within measured run budget"}
    assert _run(runs)[0] == {"batch units within measured run budget"}


# --- EXP-1351: an aborted run is not a fast run ----------------------------
#
# Found 2026-08-22 from live data. `read_batch_runs` parsed only systemd's
# `Consumed ... over ...` line, which systemd emits for FAILED runs exactly as
# it does for successful ones. So the 08-20 sweep — which hit Kalshi's designed
# `ABORT after 25 consecutive series errors` and exited 75/TEMPFAIL 1h21m in,
# having swept 7,744 of ~46,000 markets — entered the measurement set as a
# 1.36h "completed run".
#
# That error only ever points one way. An abort TRUNCATES wall clock, so a
# unit that dies early always looks further inside its budget than a unit that
# does the work. A sweep that aborted after ten minutes every night for a week
# would report `worst 0.17h/12.5h` and pass, clean and green, while the archive
# rotted. The budget check cannot tell "fast because healthy" from "fast
# because it died" — and the second is the one worth an alarm.
#
# The same blindness misdiagnosed the breach it DID catch. 08-21 ran 14.64h
# against the 12.5h budget because it carried 08-20's unswept backlog; the
# check reported it as a stale constant and told the operator to re-measure,
# which is the one repair that would have made things worse.

FAILED_RUN_LINES = (
    "2026-08-20T02:31:42+00:00 hyz systemd[755]: hyxlab-sweep.service: "
    "Main process exited, code=exited, status=75/TEMPFAIL\n"
    "2026-08-20T02:31:42+00:00 hyz systemd[755]: hyxlab-sweep.service: "
    "Failed with result 'exit-code'.\n"
    "2026-08-20T02:31:42+00:00 hyz systemd[755]: "
    "Failed to start hyxlab daily incremental sweep.\n"
    "2026-08-20T02:31:42+00:00 hyz systemd[755]: hyxlab-sweep.service: "
    "Consumed 15min 47.891s CPU time over 1h 21min 41.815s wall clock time, "
    "7.3G memory peak."
)

GOOD_RUN_LINES = (
    "2026-08-19T15:39:23+00:00 hyz systemd[755]: "
    "Finished hyxlab daily incremental sweep.\n"
    "2026-08-19T15:39:23+00:00 hyz systemd[755]: hyxlab-sweep.service: "
    "Consumed 1h 18min 36.877s CPU time over 9h 29min 22.930s wall clock time, "
    "8.6G memory peak."
)


def _aborted(end, wall_h):
    return BatchRun("hyxlab-sweep.timer", end, wall_h, ok=False)


def test_the_08_20_abort_is_read_off_the_journal_as_a_failed_run(monkeypatch):
    """The `Consumed` line alone cannot tell the two apart; the exit does."""
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: FAILED_RUN_LINES)
    sweep = qa.read_batch_runs(now=datetime(2026, 8, 21, tzinfo=UTC))["hyxlab-sweep.timer"]
    assert len(sweep) == 1
    assert sweep[0].ok is False
    assert sweep[0].wall_h == pytest.approx(1.3616, abs=1e-3)


def test_a_run_that_finished_is_read_as_ok(monkeypatch):
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: GOOD_RUN_LINES)
    sweep = qa.read_batch_runs(now=datetime(2026, 8, 20, tzinfo=UTC))["hyxlab-sweep.timer"]
    assert len(sweep) == 1
    assert sweep[0].ok is True


def test_one_runs_failure_does_not_leak_onto_the_next_run(monkeypatch):
    """Both runs sit in one journal read; the flag must reset at each Consumed."""
    monkeypatch.setattr(
        qa, "_journal", lambda unit, since, until: FAILED_RUN_LINES + "\n" + GOOD_RUN_LINES
    )
    sweep = qa.read_batch_runs(now=datetime(2026, 8, 21, tzinfo=UTC))["hyxlab-sweep.timer"]
    assert [r.ok for r in sweep] == [False, True]


def test_an_abort_cannot_make_the_budget_look_safer(capsys):
    """The dangerous direction. A unit dying at 10 min must never read green.

    This is the whole point: every one of these runs is far inside 12.5h, and
    a check that measures wall clock without reading the exit passes all of
    them while the sweep does no work at all.
    """
    runs = {
        "hyxlab-sweep.timer": [
            _aborted(datetime(2026, 8, d, 6, 20, tzinfo=UTC), 0.17) for d in (18, 19, 20)
        ]
    }
    failed, _ = _run(runs, now=datetime(2026, 8, 21, tzinfo=UTC))
    assert failed == {"batch units within measured run budget"}
    assert "3 aborted" in capsys.readouterr().out


def test_a_breach_after_an_abort_is_attributed_to_catch_up_not_a_stale_budget(capsys):
    """The live 08-20/08-21 pair, verbatim. Still FAILS — but says why.

    The repair the bare breach implies (re-measure the constant upward) would
    bake a one-off backlog into the budget and blind it to real drift.
    """
    runs = {
        "hyxlab-sweep.timer": [
            _aborted(datetime(2026, 8, 20, 7, 31, tzinfo=UTC), 1.36),
            _sweep(datetime(2026, 8, 21, 20, 48, tzinfo=UTC), 14.64),
        ]
    }
    failed, _ = _run(runs, now=datetime(2026, 8, 22, tzinfo=UTC))
    assert failed == {"batch units within measured run budget"}
    out = capsys.readouterr().out
    assert "catch-up" in out and "08-20" in out


def test_a_catch_up_breach_fails_once_then_decays_to_a_watch(capsys):
    """The half of the attribution that was computed and then thrown away.

    `_catch_up_abort` certifies the constant is NOT stale, so neither repair a
    breach implies — re-measure, or make the unit faster — applies to this run.
    The work it was catching up on is already burned off, yet the run sits in
    the 7-day lookback for a week. Live case: the 09-11 sweep ran 16.66h after
    the 09-10 07:45Z abort and QA went red on it with nothing to do.
    """
    runs = {
        "hyxlab-sweep.timer": [
            _aborted(datetime(2026, 8, 20, 7, 31, tzinfo=UTC), 1.36),
            _sweep(datetime(2026, 8, 21, 20, 48, tzinfo=UTC), 14.64),
        ]
    }
    now = datetime(2026, 8, 22, tzinfo=UTC)
    assert _run(runs, now=now)[0] == {"batch units within measured run budget"}

    failed, _ = _run(runs, now=now)
    assert not failed
    out = capsys.readouterr().out
    assert "WATCH" in out and "catch-up" in out and "already reported" in out
    # ...and the breach itself is still SAID, not silently dropped.
    assert "14.64h" in out


def test_a_catch_up_breach_is_loud_on_the_day_it_first_appears(capsys):
    """The sequence production actually runs, where nothing else is fresh.

    The abort is reported the day it happens; the catch-up run that follows it
    lands a day later. So on the day the breach first exists, its abort is
    already on the record and cannot raise the alarm for it — only the breach's
    own key can. The previous test cannot see this: it shows both on the same
    day, where the abort's own FAIL would mask a catch-up that never fired.
    """
    abort = _aborted(datetime(2026, 8, 20, 7, 31, tzinfo=UTC), 1.36)
    day1 = {"hyxlab-sweep.timer": [abort]}
    assert _run(day1, now=datetime(2026, 8, 21, tzinfo=UTC))[0]
    capsys.readouterr()

    day2 = {"hyxlab-sweep.timer": [abort, _sweep(datetime(2026, 8, 21, 20, 48, tzinfo=UTC), 14.64)]}
    failed, _ = _run(day2, now=datetime(2026, 8, 22, tzinfo=UTC))
    assert failed == {"batch units within measured run budget"}
    out = capsys.readouterr().out
    assert "FAIL" in out and "14.64h" in out and "catch-up" in out


def test_a_stale_budget_breach_never_decays(capsys):
    """The discrimination control: only the certified-not-stale half decays.

    A constant that is wrong today is still wrong tomorrow, and the repair is
    available — so this one must stay red until someone makes it.
    """
    runs = {"hyxlab-sweep.timer": [_sweep(datetime(2026, 8, 21, 20, 48, tzinfo=UTC), 14.64)]}
    now = datetime(2026, 8, 22, tzinfo=UTC)
    assert _run(runs, now=now)[0] == {"batch units within measured run budget"}
    assert _run(runs, now=now)[0] == {"batch units within measured run budget"}
    assert "catch-up" not in capsys.readouterr().out


def test_a_second_catch_up_breach_on_a_new_date_fails_again(capsys):
    """Decay is per-run: a unit that aborts and overruns again is fresh news."""
    first_abort = _aborted(datetime(2026, 8, 20, 7, 31, tzinfo=UTC), 1.36)
    first = _sweep(datetime(2026, 8, 21, 20, 48, tzinfo=UTC), 14.64)
    now = datetime(2026, 8, 24, tzinfo=UTC)
    runs = [first_abort, first]
    assert _run({"hyxlab-sweep.timer": list(runs)}, now=now)[0]
    assert not _run({"hyxlab-sweep.timer": list(runs)}, now=now)[0]

    runs += [
        _aborted(datetime(2026, 8, 22, 7, 40, tzinfo=UTC), 1.5),
        _sweep(datetime(2026, 8, 23, 21, 0, tzinfo=UTC), 14.9),
    ]
    failed, _ = _run({"hyxlab-sweep.timer": list(runs)}, now=now)
    assert failed == {"batch units within measured run budget"}
    out = capsys.readouterr().out
    assert "14.90h" in out and "08-22 07:40Z abort" in out


def test_a_decayed_catch_up_does_not_mask_a_new_stale_budget_breach(capsys):
    """A WATCH-ed catch-up must not take an unrelated live breach down with it."""
    runs = {
        "hyxlab-sweep.timer": [
            _aborted(datetime(2026, 8, 20, 7, 31, tzinfo=UTC), 1.36),
            _sweep(datetime(2026, 8, 21, 20, 48, tzinfo=UTC), 14.64),
        ]
    }
    now = datetime(2026, 8, 24, tzinfo=UTC)
    _run(runs, now=now)
    runs["hyxlab-tradepass.timer"] = [
        BatchRun("hyxlab-tradepass.timer", datetime(2026, 8, 23, 12, 0, tzinfo=UTC), 5.0)
    ]
    failed, _ = _run(runs, now=now)
    assert failed == {"batch units within measured run budget"}
    out = capsys.readouterr().out
    assert "hyxlab-tradepass.timer ran 5.00h" in out


def test_a_breach_with_no_abort_before_it_still_reads_as_a_stale_budget(capsys):
    """The discrimination control — attribution must not fire on every breach."""
    runs = {
        "hyxlab-sweep.timer": [
            _sweep(datetime(2026, 8, 20, 16, 0, tzinfo=UTC), 9.0),
            _sweep(datetime(2026, 8, 21, 20, 48, tzinfo=UTC), 14.64),
        ]
    }
    failed, _ = _run(runs, now=datetime(2026, 8, 22, tzinfo=UTC))
    assert failed == {"batch units within measured run budget"}
    assert "catch-up" not in capsys.readouterr().out


def test_an_aborted_run_still_counts_against_the_fade_window(capsys):
    """It died at 01:00Z — the quota it spent in the window was still spent."""
    runs = {"hyxlab-sweep.timer": [_aborted(datetime(2026, 8, 4, 1, 0, tzinfo=UTC), 9.0)]}
    failed, _ = _run(runs)
    assert failed == {"batch units within measured run budget"}
    assert "fade window" in capsys.readouterr().out


def test_a_unit_whose_only_runs_aborted_is_not_reported_as_within_budget(capsys):
    """No healthy run means no measurement — that is UNVERIFIED, not a pass."""
    failed, skipped = _run(
        {
            "hyxlab-sweep.timer": [_aborted(datetime(2026, 8, 4, 8, 0, tzinfo=UTC), 1.0)],
            "hyxlab-tradepass.timer": None,
        }
    )
    assert failed == {"batch units within measured run budget"}
    out = capsys.readouterr().out
    assert "hyxlab-sweep.timer" in out and "aborted" in out


def test_an_abort_fails_once_then_decays_to_a_watch(capsys):
    """Same policy as the fade overlap, for the same reason.

    An abort is a past event: the sweep already did not run, and no repair
    un-runs it. It sits in the 7-day lookback for a week, so a permanent FAIL
    here is precisely the noise this check's docstring refuses to create. It
    must be loud the first time and quiet — but still SAID — afterwards.
    """
    runs = {"hyxlab-sweep.timer": [_aborted(datetime(2026, 8, 20, 7, 31, tzinfo=UTC), 1.36)]}
    now = datetime(2026, 8, 21, tzinfo=UTC)
    assert _run(runs, now=now)[0] == {"batch units within measured run budget"}

    failed, _ = _run(runs, now=now)
    assert not failed
    out = capsys.readouterr().out
    assert "WATCH" in out and "aborted" in out and "already reported" in out


def test_a_second_abort_on_a_new_date_fails_again(capsys):
    """Decay is per-run, not per-check: a fresh abort is fresh news."""
    now = datetime(2026, 8, 22, tzinfo=UTC)
    first = _aborted(datetime(2026, 8, 20, 7, 31, tzinfo=UTC), 1.36)
    assert _run({"hyxlab-sweep.timer": [first]}, now=now)[0]
    assert not _run({"hyxlab-sweep.timer": [first]}, now=now)[0]

    second = _aborted(datetime(2026, 8, 21, 7, 40, tzinfo=UTC), 1.5)
    failed, _ = _run({"hyxlab-sweep.timer": [first, second]}, now=now)
    assert failed == {"batch units within measured run budget"}
    assert "08-21 07:40Z" in capsys.readouterr().out


def test_a_reported_catch_up_stays_one_after_its_abort_leaves_the_lookback(capsys):
    """An abort always ENDS before the catch-up it certifies, so it ages out first.

    Live case: the 09-10 07:45Z abort left the 7-day read at 09-17 10:00Z while
    the 09-11 22:49Z catch-up it certified was still in it. With no sibling
    left to certify it, the run re-classified as a stale budget and QA went red
    for a constant nobody could repair. The certification was made while the
    abort was visible and is on the record; losing the witness does not revoke it.
    """
    abort = _aborted(datetime(2026, 9, 10, 7, 45, tzinfo=UTC), 1.58)
    catch_up = _sweep(datetime(2026, 9, 11, 22, 49, tzinfo=UTC), 16.66)
    assert _run({"hyxlab-sweep.timer": [abort, catch_up]}, now=datetime(2026, 9, 12, tzinfo=UTC))[0]
    capsys.readouterr()

    failed, _ = _run({"hyxlab-sweep.timer": [catch_up]}, now=datetime(2026, 9, 17, 10, tzinfo=UTC))
    assert not failed
    out = capsys.readouterr().out
    assert "WATCH" in out and "catch-up" in out and "16.66h" in out and "already reported" in out


def test_an_unrecorded_breach_with_no_abort_in_view_still_fails(capsys):
    """The control: only a RECORDED catch-up survives its abort ageing out."""
    runs = {"hyxlab-sweep.timer": [_sweep(datetime(2026, 9, 11, 22, 49, tzinfo=UTC), 16.66)]}
    failed, _ = _run(runs, now=datetime(2026, 9, 17, 10, tzinfo=UTC))
    assert failed == {"batch units within measured run budget"}
    assert "catch-up" not in capsys.readouterr().out


# --- #118: a failure with no `Consumed` line ---------------------------------
#
# Live case, verbatim. The 10-05 sweep exited 1 at boot before its cgroup did
# any work, so systemd wrote no `Consumed` line, and the failure flag rode on
# to the 10-06 run: a CLEAN 18.94h finish (`'aborted': False` in its own done
# line) read as an abort for five QA runs. On 10-12 the 10-05 line left the
# 7-day read, the flag stopped leaking, and the same run flipped to a stale-
# budget FAIL -- for a run that was in truth catching up the missed day.

BOOT_FAIL_THEN_CLEAN_RUN = (
    "2026-10-05T04:01:39-05:00 hyz systemd[765]: Starting hyxlab daily incremental sweep...\n"
    "2026-10-05T04:01:39-05:00 hyz systemd[765]: hyxlab-sweep.service: "
    "Main process exited, code=exited, status=1/FAILURE\n"
    "2026-10-05T04:01:39-05:00 hyz systemd[765]: hyxlab-sweep.service: "
    "Failed with result 'exit-code'.\n"
    "-- Boot b626d443e4b54894aa5a8b952c609d9b --\n"
    "2026-10-06T01:10:00-05:00 hyz systemd[759]: Starting hyxlab daily incremental sweep...\n"
    "2026-10-06T20:06:38-05:00 hyz systemd[759]: Finished hyxlab daily incremental sweep.\n"
    "2026-10-06T20:06:38-05:00 hyz systemd[759]: hyxlab-sweep.service: Consumed 2h 53min "
    "43.660s CPU time over 18h 56min 37.744s wall clock time, 14.8G memory peak."
)

QA_10_12 = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


def _sweep_journal(text):
    return lambda unit, since, until: text if unit == "hyxlab-sweep.service" else ""


def test_a_failure_with_no_consumed_line_does_not_leak_onto_the_next_run(monkeypatch):
    monkeypatch.setattr(qa, "_journal", _sweep_journal(BOOT_FAIL_THEN_CLEAN_RUN))
    sweep = qa.read_batch_runs(now=QA_10_12)["hyxlab-sweep.timer"]
    assert [(r.ok, round(r.wall_h, 2)) for r in sweep] == [(False, 0.0), (True, 18.94)]
    assert sweep[0].end == datetime(2026, 10, 5, 9, 1, 39, tzinfo=UTC)


@pytest.mark.parametrize("boundary", ["-- Boot", "Starting"])
def test_each_run_boundary_closes_a_cgroupless_failure(monkeypatch, boundary):
    """Either boundary alone must close it -- a reboot need not precede a start."""
    lines = [
        ln
        for ln in BOOT_FAIL_THEN_CLEAN_RUN.splitlines()
        if not (ln.startswith("-- Boot") if boundary == "Starting" else "T01:10:00" in ln)
    ]
    monkeypatch.setattr(qa, "_journal", _sweep_journal("\n".join(lines)))
    sweep = qa.read_batch_runs(now=QA_10_12)["hyxlab-sweep.timer"]
    assert [r.ok for r in sweep] == [False, True]


def test_a_cgroupless_failure_at_the_end_of_the_read_is_still_an_abort(monkeypatch):
    head = "\n".join(BOOT_FAIL_THEN_CLEAN_RUN.splitlines()[:3])
    monkeypatch.setattr(qa, "_journal", _sweep_journal(head))
    sweep = qa.read_batch_runs(now=QA_10_12)["hyxlab-sweep.timer"]
    assert [(r.ok, r.wall_h) for r in sweep] == [(False, 0.0)]


def test_the_read_reaches_behind_the_lookback_for_the_catch_up_witness(monkeypatch):
    seen = []
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: seen.append(since) or "")
    qa.read_batch_runs(now=QA_10_12)
    days = qa.BATCH_RUN_LOOKBACK_DAYS + qa.BATCH_RUN_CONTEXT_DAYS
    assert set(seen) == {QA_10_12 - timedelta(days=days)}
    assert qa.BATCH_RUN_CONTEXT_DAYS >= 1


def test_the_10_12_reading_is_a_catch_up_not_a_stale_budget(monkeypatch, capsys):
    """The 10-05 abort sits behind the lookback yet still certifies 10-06."""
    monkeypatch.setattr(qa, "_journal", _sweep_journal(BOOT_FAIL_THEN_CLEAN_RUN))
    _run(None, now=QA_10_12)
    out = capsys.readouterr().out
    assert "catch-up after the 10-05 09:01Z abort" in out
    # The context abort is a witness only: not counted, not reported.
    assert "aborted (" not in out and "ABORTED" not in out


def test_a_context_run_is_never_a_verdict(capsys):
    """A breach that ended before the lookback is out of scope, as before."""
    old = _sweep(QA_10_12 - timedelta(days=8), 18.0)
    failed, _ = _run({"hyxlab-sweep.timer": [old]}, now=QA_10_12)
    assert "18.00h" not in capsys.readouterr().out
    assert failed == set()


# --- the done line's segments ride the breach line ---------------------------
# The 2026-10-22 poly run breached 8.0h by 10 min, and the whole excess lived
# in `closed_fetched` (1,604 markets vs 1,056-1,225 on the three prior lines;
# 322 of them the one-day repair re-fetch of mistakes #131). Read bare, the
# breach line called the budget stale on a day it was right; the decomposition
# is what tells universe growth (open), backlog or repair (closed_fetched) and
# archive contention (flush) apart, and it has to be ON the line.

# Verbatim from the journal, the done + attach_wait + Finished + Consumed quartet.
POLY_10_22_LINES = (
    '2026-10-22T12:25:38+00:00 hyz python[1692960]: [poly] done: {"markets": 18789, '
    '"errors": 9, "skipped_resolved": 10896, "n_prices": 806685, "n_trades": 1285866, '
    '"segments": {"open": {"markets": 6289, "prices": 475984, "trades": 4626211, '
    '"wall_min": 236.4}, "closed_fetched": {"markets": 1604, "prices": 330701, '
    '"trades": 1159628, "wall_min": 134.0}, "closed_skipped": {"markets": 10896, '
    '"prices": 0, "trades": 0, "wall_min": 0.0}}, "flush_min": 115.8, "n_tail_stops": 16, '
    '"elapsed_min": 486.3, "error_classes": {"ReadTimeout": 9}, "http_retries": '
    '{"keyset_page": 0, "keyset_restart": 0, "tail_truncated": 16, "tail_truncated_empty": 7, '
    '"total": 0}, "keyset_budget_frac": 0.0}\n'
    "2026-10-22T12:25:38+00:00 hyz python[1692960]: [poly] attach_wait: on hyxlab.duckdb "
    "754 attaches, 15 contended, worst attach 2.0s of 42.0s elapsed\n"
    "2026-10-22T12:25:38+00:00 hyz systemd[758]: Finished hyxlab polymarket daily archival sweep.\n"
    "2026-10-22T12:25:38+00:00 hyz systemd[758]: hyxlab-poly-sweep.service: Consumed 2h 53min "
    "56.058s CPU time over 8h 10min 25.218s wall clock time, 6G memory peak.\n"
)
POLY_10_22_CLAUSE = (
    " [open 6,289 mkts 236.4 min; closed_fetched 1,604 mkts 134.0 min; "
    "closed_skipped 10,896 mkts 0.0 min; flush 115.8 min]"
)
QA_10_23 = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)


def _poly_journal(text):
    return lambda unit, since, until: text if unit == "hyxlab-poly-sweep.service" else ""


def test_the_10_22_poly_done_line_is_read_onto_its_run(monkeypatch):
    monkeypatch.setattr(qa, "_journal", _poly_journal(POLY_10_22_LINES))
    (run,) = qa.read_batch_runs(now=QA_10_23)["hyxlab-poly-sweep.timer"]
    assert run.ok and round(run.wall_h, 2) == 8.17
    assert run.segments == POLY_10_22_CLAUSE


def test_the_10_22_breach_line_carries_the_segments(monkeypatch, capsys):
    monkeypatch.setattr(qa, "_journal", _poly_journal(POLY_10_22_LINES))
    failed, _ = _run(None, now=QA_10_23)
    assert failed == {"batch units within measured run budget"}
    out = capsys.readouterr().out
    assert "hyxlab-poly-sweep.timer ran 8.17h (budget 8h), 10-22 04:15Z -> 10-22 12:25Z" in out
    assert POLY_10_22_CLAUSE in out


def test_a_done_line_describes_only_the_consumed_line_that_follows_it(monkeypatch):
    """Two runs in one read: the first's done line must not ride on to the
    second (which here printed none -- a boundary or a kill between them)."""
    second = (
        "2026-10-23T04:15:00+00:00 hyz systemd[758]: Starting hyxlab polymarket daily "
        "archival sweep...\n"
        "2026-10-23T11:00:00+00:00 hyz systemd[758]: hyxlab-poly-sweep.service: Consumed 2h "
        "CPU time over 6h 45min wall clock time, 6G memory peak.\n"
    )
    monkeypatch.setattr(qa, "_journal", _poly_journal(POLY_10_22_LINES + second))
    first, nxt = qa.read_batch_runs(now=QA_10_23 + timedelta(days=1))["hyxlab-poly-sweep.timer"]
    assert first.segments == POLY_10_22_CLAUSE
    assert nxt.segments == ""


def test_a_unit_with_no_done_line_renders_no_brackets(monkeypatch, capsys):
    """The Kalshi sweep prints no `done:` JSON; its breach line is unchanged."""
    monkeypatch.setattr(qa, "_journal", lambda unit, since, until: REAL_LINE)
    (run,) = qa.read_batch_runs(now=NOW)["hyxlab-sweep.timer"]
    assert run.segments == ""
    _run(None, now=NOW)
    assert "[" not in capsys.readouterr().out.split("10-03 21:16Z")[0].split("->")[-1]


@pytest.mark.parametrize(
    "done,clause",
    [
        ({}, ""),
        ({"segments": {}}, ""),
        ({"flush_min": 12.34}, " [flush 12.3 min]"),
        ({"segments": {"open": {"markets": "x", "wall_min": 1.0}}}, ""),
        (
            {"segments": {"open": {"markets": 10, "wall_min": 2.0}}, "flush_min": None},
            " [open 10 mkts 2.0 min]",
        ),
    ],
)
def test_segments_clause_leaves_out_what_it_cannot_render(done, clause):
    assert qa._segments_clause(done) == clause


def test_an_unparseable_done_line_reads_as_no_segments(monkeypatch):
    bad = POLY_10_22_LINES.replace('"flush_min": 115.8', '"flush_min": 115.8 oops')
    monkeypatch.setattr(qa, "_journal", _poly_journal(bad))
    (run,) = qa.read_batch_runs(now=QA_10_23)["hyxlab-poly-sweep.timer"]
    assert run.ok and run.segments == ""
