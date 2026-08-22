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
    assert abs(sweep[0].start - datetime(2026, 8, 3, 11, 10, 0, tzinfo=UTC)) < timedelta(
        seconds=1
    )


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
    failed, skipped = _run(
        {"hyxlab-sweep.timer": None, "hyxlab-tradepass.timer": None}
    )
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
