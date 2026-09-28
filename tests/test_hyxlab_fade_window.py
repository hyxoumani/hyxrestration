"""EXP-960 — the fade-window capture detector.

The unit under test decides three things that are easy to get wrong and that
this file pins one at a time: an UNMEASURED window must not read as a clean
one, the alarm must be on lost CYCLES rather than on how long the poly sweep
ran, and a hole already reported must stop failing so the check does not decay
into noise.
"""

from datetime import UTC, datetime, timedelta

import pytest

import collector.qa as qa
from collector.qa import NightCapture

NOW = datetime(2026, 8, 3, 7, 0, tzinfo=UTC)


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


def _run(records, now=NOW):
    qa._failures.clear()
    qa._skipped.clear()
    qa.qa_fade_window_capture(records=records, now=now)
    return set(qa._failures), list(qa._skipped)


def _clean(date, sweep=False):
    return NightCapture(date, 60, 60, sweep)


def test_a_whole_night_of_capture_passes():
    failed, skipped = _run([_clean(f"2026-07-2{d}") for d in range(3, 9)])
    assert not failed and not skipped


def test_the_measured_breach_night_fails():
    """2026-07-29: 4 of 60 cycles lost while the sweep overran into the window."""
    recs = [_clean("2026-07-28"), NightCapture("2026-07-29", 60, 56, True), _clean("2026-07-30")]
    failed, _ = _run(recs)
    assert any("fade window" in f for f in failed), failed


def test_the_breach_detail_names_the_sweep_as_the_attribution(capsys):
    _run([NightCapture("2026-07-29", 60, 56, True)])
    out = capsys.readouterr().out
    assert "2026-07-29 lost 4/60" in out
    assert "poly sweep was still running" in out


def test_one_lost_cycle_is_within_budget_but_two_is_not():
    ok, _ = _run([NightCapture("2026-07-29", 60, 59, False)])
    assert not ok
    bad, _ = _run([NightCapture("2026-07-29", 60, 58, False)])
    assert bad


def test_an_unmeasured_window_is_unverified_not_a_pass(capsys):
    """The recurring defect (EXP-943/947/951/954): absence rendering as OK."""
    failed, skipped = _run([NightCapture("2026-07-29", None, None, None)])
    out = capsys.readouterr().out
    assert not failed
    assert skipped == ["fade-window"]
    assert "UNVERIFIED" in out and "PASS" not in out


def test_a_window_with_zero_activations_is_unmeasured_not_clean(capsys):
    """starts == 0 means the timer itself did not run — nothing was observed,
    so `completions == starts` must not be read as a complete tape."""
    failed, skipped = _run([NightCapture("2026-07-29", 0, 0, None)])
    assert not failed and skipped == ["fade-window"]
    assert "UNVERIFIED" in capsys.readouterr().out


def test_partly_unmeasured_nights_are_counted_out_loud(capsys):
    _run([_clean("2026-07-28"), NightCapture("2026-07-29", None, None, None)])
    assert "1 window(s) UNMEASURED" in capsys.readouterr().out


def test_an_overrun_that_costs_nothing_is_a_watch_not_a_failure(capsys):
    """The instrument choice, encoded. The sweep takes the lock in ~7-12s
    bursts at ~11% duty, so it can span the whole window and lose no cycle;
    alarming on the overrun itself would fire on a night that cost nothing."""
    failed, skipped = _run([_clean("2026-07-29", sweep=True)])
    out = capsys.readouterr().out
    assert not failed and not skipped
    assert "WATCH" in out and "cost no cycles" in out
    assert "PASS  fade window" in out


def test_a_reported_hole_stops_failing_but_keeps_saying_so(capsys):
    recs = [NightCapture("2026-07-29", 60, 56, True)]
    first, _ = _run(recs)
    assert first
    capsys.readouterr()
    second, skipped = _run(recs)
    out = capsys.readouterr().out
    assert not second and not skipped
    assert "WATCH" in out and "already reported" in out


def test_a_new_hole_after_an_accepted_one_escalates_again(capsys):
    _run([NightCapture("2026-07-29", 60, 56, True)])
    capsys.readouterr()
    failed, _ = _run(
        [NightCapture("2026-07-29", 60, 56, True), NightCapture("2026-07-31", 60, 55, False)]
    )
    out = capsys.readouterr().out
    assert failed
    assert "2026-07-31 lost 5/60" in out


def test_an_accepted_night_that_rolls_out_of_the_window_is_forgotten():
    """Otherwise the acceptance list grows forever and a hole on the SAME date
    a year later would be swallowed as 'already reported'."""
    _run([NightCapture("2026-07-29", 60, 56, True)])
    _run([_clean("2026-08-01")])  # 07-29 no longer in the measured set
    failed, _ = _run([NightCapture("2026-07-29", 60, 56, True)])
    assert failed


# --- the journal reader --------------------------------------------------

_FLOCK_ERA = """\
2026-07-29T18:00:00-05:00 hyz systemd[764]: Starting hyxlab 5-min collector (kalshi/poly books)...
2026-07-29T18:00:10-05:00 hyz flock[1020112]: [collect] 2026-07-29T23:00:10.094579+00:00 {'x': 1}
2026-07-29T18:05:00-05:00 hyz systemd[764]: Starting hyxlab 5-min collector (kalshi/poly books)...
2026-07-29T18:05:10-05:00 hyz flock[1022561]: [collect] 2026-07-29T23:05:10.093368+00:00 {'x': 1}
2026-07-29T18:10:00-05:00 hyz systemd[764]: Starting hyxlab 5-min collector (kalshi/poly books)...
2026-07-29T18:10:00-05:00 hyz systemd[764]: hyxlab-collect.service: Failed with result 'exit-code'.
"""

_CURRENT_ERA = """\
2026-08-02T18:00:00-05:00 hyz systemd[749]: Starting hyxlab 5-min collector (kalshi/poly books)...
2026-08-02T18:00:44-05:00 hyz python[2356540]: [collect] 2026-08-02T23:00:44.191120+00:00 {'x': 1}
2026-08-02T18:00:44-05:00 hyz systemd[749]: Finished hyxlab 5-min collector (kalshi/poly books).
"""


def test_reader_counts_both_journal_eras(monkeypatch):
    """The `flock -n` wrapper was removed on 2026-08-02, changing how a lost
    cycle looks. Counting the `[collect] <iso>` payload instead of an exit code
    is what keeps a window comparable across that change."""
    texts = {"2026-07-29": _FLOCK_ERA, "2026-08-02": _CURRENT_ERA}

    def fake(unit, since, until):
        if unit == qa.SWEEP_UNIT:
            return ""
        return texts.get(f"{since:%Y-%m-%d}", "")

    monkeypatch.setattr(qa, "_journal", fake)
    recs = {r.date: r for r in qa.read_fade_windows(7, now=datetime(2026, 8, 3, 7, tzinfo=UTC))}
    assert (recs["2026-07-29"].starts, recs["2026-07-29"].completions) == (3, 2)
    assert recs["2026-07-29"].holes == 1
    assert (recs["2026-08-02"].starts, recs["2026-08-02"].completions) == (1, 1)


def test_reader_reports_an_unreadable_journal_as_none_not_zero(monkeypatch):
    monkeypatch.setattr(qa, "_journal", lambda *a: None)
    recs = qa.read_fade_windows(3, now=datetime(2026, 8, 3, 7, tzinfo=UTC))
    assert recs and all(r.starts is None and r.completions is None for r in recs)
    assert not any(r.measured for r in recs)


_SWEEP_INSIDE = "2026-08-02T18:30:11-05:00 hyz python[1234]: [poly] 900/16371 | 5.5% | ...\n"


def test_reader_marks_the_sweep_when_it_logged_inside_the_window(monkeypatch):
    def fake(unit, since, until):
        return _SWEEP_INSIDE if unit == qa.SWEEP_UNIT else _CURRENT_ERA

    monkeypatch.setattr(qa, "_journal", fake)
    recs = qa.read_fade_windows(1, now=datetime(2026, 8, 3, 7, tzinfo=UTC))
    assert recs and recs[0].sweep_in_window is True


def test_reader_never_reports_a_window_that_has_not_closed(monkeypatch):
    """A window still in progress would look holed simply for being young."""
    monkeypatch.setattr(qa, "_journal", lambda *a: _CURRENT_ERA)
    now = datetime(2026, 8, 3, 2, tzinfo=UTC)  # mid-window
    for r in qa.read_fade_windows(7, now=now):
        end = datetime.fromisoformat(r.date).replace(tzinfo=UTC) + timedelta(
            days=1, hours=qa.FADE_WINDOW_END_H
        )
        assert end <= now


def test_the_journal_reader_is_read_only(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        raise OSError("blocked")

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    assert qa._journal("u.service", NOW, NOW) is None
    assert seen["cmd"][0] == "journalctl"
    assert not any(a.startswith("--") and "vacuum" in a for a in seen["cmd"])


# --- EXP-1385: the expected count comes from the CADENCE ------------------
#
# `starts - completions` can only see a cycle that started. A slot the timer
# never fired in is absent from BOTH terms, so it cancels and the night reads
# clean -- which is what happened to the largest capture hole in this archive.


def test_the_2026_08_20_shape_read_as_a_clean_night(capsys):
    """MEASURED (read-only query, 2026-09-28): the 08-20 outage left one
    265.0-minute hole in `snapshots`, 21:30Z -> 01:55Z, covering 35 of that
    night's 60 fade-window slots. The 25 cycles that did run all completed, so
    the old term is 0 -- and that is the whole defect."""
    r = NightCapture("2026-08-20", 25, 25, False, 60)
    assert r.measured and r.holes == 0  # the old verdict, preserved as evidence
    assert (r.missed, r.lost, r.expected) == (35, 35, 60)
    failed, _ = _run([r])
    assert failed
    out = capsys.readouterr().out
    assert "2026-08-20 lost 35/60" in out and "none of them started" in out


def test_holes_and_missed_slots_are_reported_as_different_failures(capsys):
    """A cycle that ran and lost its data is not a cycle that never ran; one
    number cannot be read as either, so both are named."""
    _run([NightCapture("2026-08-20", 58, 56, False, 60)])
    out = capsys.readouterr().out
    assert "lost 4/60" in out and "(2 after starting, 2 never started)" in out


def test_a_window_without_a_cadence_count_claims_nothing_about_activation(capsys):
    """`slots is None` means journald no longer retains the window's opening,
    where a timer that did not fire and a record nobody kept look identical."""
    r = NightCapture("2026-08-20", 25, 25, False, None)
    assert (r.missed, r.lost, r.expected) == (0, 0, 25)
    failed, _ = _run([r])
    assert not failed
    assert "non-activation is unresolved there" in capsys.readouterr().out


# --- EXP-1385: cycles are PAIRED across the window edges ------------------

_FAR_STRADDLE = """\
2026-08-02T23:00:09+00:00 hyz systemd[749]: Starting hyxlab 5-min collector (x)...
2026-08-02T23:00:29+00:00 hyz python[1]: [collect] 2026-08-02T23:00:29.1+00:00 {'x': 1}
2026-08-03T03:55:09+00:00 hyz systemd[749]: Starting hyxlab 5-min collector (x)...
2026-08-03T04:00:20+00:00 hyz python[2]: [collect] 2026-08-03T04:00:20.1+00:00 {'x': 1}
"""

# A oneshot cannot run twice, so the 23:00 activation is QUEUED behind the
# 22:55 cycle and the journal stays strictly S C S C -- which is why pairing in
# order is sound.
_NEAR_STRADDLE = """\
2026-08-02T22:55:09+00:00 hyz systemd[749]: Starting hyxlab 5-min collector (x)...
2026-08-02T23:00:20+00:00 hyz python[1]: [collect] 2026-08-02T23:00:20.1+00:00 {'x': 1}
2026-08-02T23:00:25+00:00 hyz systemd[749]: Starting hyxlab 5-min collector (x)...
2026-08-02T23:05:09+00:00 hyz systemd[749]: Starting hyxlab 5-min collector (x)...
2026-08-02T23:05:30+00:00 hyz python[3]: [collect] 2026-08-02T23:05:30.1+00:00 {'x': 1}
"""

_ONE_NIGHT = datetime(2026, 8, 3, 5, tzinfo=UTC)


def _read_one(monkeypatch, text):
    """Stand in for journalctl, HONOURING `--since`/`--until`.

    A fake that returns its whole fixture whatever interval was asked for is
    blind to the interval, so it cannot witness the read widening past the
    window edges -- reverting the grace leaves such a test green (checked).
    """

    def fake(unit, since, until):
        if unit == qa.SWEEP_UNIT:
            return ""
        lines = [
            ln
            for ln in text.splitlines(keepends=True)
            if since <= datetime.fromisoformat(ln.split(" ", 1)[0]) < until
        ]
        return "".join(lines) or "-- No entries --\n"

    monkeypatch.setattr(qa, "_journal", fake)
    recs = qa.read_fade_windows(1, now=_ONE_NIGHT)
    assert len(recs) == 1 and recs[0].date == "2026-08-02"
    return recs[0]


def test_a_cycle_finishing_past_the_far_edge_is_captured_not_holed(monkeypatch):
    """Its payload lands after 04:00Z. Counted inside the window only, that is
    a hole that never happened -- and the budget is one hole."""
    r = _read_one(monkeypatch, _FAR_STRADDLE)
    assert (r.starts, r.completions, r.holes) == (2, 2, 0)


def test_a_cycle_finishing_past_the_near_edge_cannot_cancel_a_real_hole(monkeypatch):
    """The worse arm. The 22:55 cycle's payload lands after 23:00Z, so counting
    the two terms separately credits this window a completion it did not earn,
    and `max(0, starts - completions)` clamps the 23:00:25 hole to zero."""
    r = _read_one(monkeypatch, _NEAR_STRADDLE)
    assert (r.starts, r.completions, r.holes) == (2, 1, 1)


def test_the_cadence_sets_the_slot_count(monkeypatch):
    r = _read_one(monkeypatch, _FAR_STRADDLE)
    assert r.slots == 5 * 60 // qa.FADE_WINDOW_CADENCE_MIN == 60


# --- EXP-1386: journalctl's empty-read sentinel is not content ------------


def test_journalctl_s_no_entries_sentinel_is_not_a_record():
    assert qa._journal_records("-- No entries --\n") == []
    assert qa._journal_records(_CURRENT_ERA)


def test_an_empty_sweep_journal_does_not_attribute_the_hole_to_the_sweep(monkeypatch):
    """LIVE DEFECT, measured 2026-09-28: `journalctl -o short-iso` prints
    `-- No entries --` on stdout, so `bool(text.strip())` was True for every
    window ever examined. Seven QA runs 09-21..09-27 each announced the sweep
    inside 7 of 7 windows while `-o cat` over the same spans returns nothing."""
    monkeypatch.setattr(
        qa,
        "_journal",
        lambda unit, a, b: "-- No entries --\n" if unit == qa.SWEEP_UNIT else _CURRENT_ERA,
    )
    recs = qa.read_fade_windows(1, now=_ONE_NIGHT)
    assert recs[0].sweep_in_window is False


def test_an_unretained_window_front_does_not_invent_missed_slots(monkeypatch):
    """The sentinel again, one question over: reading it as content would make
    the retention probe always True and turn a rotated-away window into 60
    fabricated losses."""

    def fake(unit, a, b):
        return "-- No entries --\n" if unit is None else _FAR_STRADDLE

    monkeypatch.setattr(qa, "_journal", fake)
    assert qa.read_fade_windows(1, now=_ONE_NIGHT)[0].slots is None
