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


def test_reader_marks_the_sweep_when_it_logged_inside_the_window(monkeypatch):
    def fake(unit, since, until):
        return "[poly] 900/16371 | ..." if unit == qa.SWEEP_UNIT else _CURRENT_ERA

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
