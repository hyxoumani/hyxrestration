"""Nothing consumed QA's own verdict, so a failure that healed overnight and a
run that never happened were both invisible.

WHY THIS EXISTS (2026-09-05, EXP-1383). The four coverage derivations
(test_qa_table_coverage, test_qa_staleness_coverage, test_qa_silent_guards,
test_qa_early_returns) all read collector/qa.py as TEXT: is the table read, is
its age asked, is the check still printed, does the section leave silently.
None of them asks the question one level out — is what QA prints ever READ?

The answer, measured across the repo rather than assumed:

  * `scripts/systemd/hyxlab-qa.service` is `Type=oneshot`, with no
    `OnFailure=`, no `ExecStopPost=`, no `Restart=` and no notifier.
  * No `systemctl is-failed`, no `--failed`, and no other reader of any unit's
    state exists anywhere in the tree (`test_nothing_outside_qa_reads_qa`
    keeps that true).
  * `read_batch_runs` is the project's only consumer of a unit's journal, and
    `BATCH_RUN_BUDGET_H` covers `hyxlab-sweep` and `hyxlab-tradepass` only.
  * `scripts/autoloop.sh` runs the agent loop; it never invokes qa.

So `sys.exit(1)` set the unit to `failed` where nothing queried it, and the
`NOT a full pass` line exits 0 — for that one, even systemd's own state read
clean. The only consumer available to QA is the next QA run, which is what
`qa_prior_run` now is.

THE TWO STATES IT MAKES REPORTABLE. A FAIL that HEALS: the freshness checks
are instantaneous (EXP-1359), so a defect that repairs itself between two
10:00Z runs leaves yesterday's FAIL in a journal nobody reads and today's run
green. And a MISSED run: a disabled timer, a box that stayed down, or a qa.py
that dies before its first line are indistinguishable from inside any single
run, and are named by the record's AGE.

WHAT THIS DOES NOT CLAIM. That anyone reads the NEW line either — that is a
strictly smaller claim than before (one unread line instead of a whole unread
run), and it is bounded by the same journal. Nor that QA can report its own
non-execution: it cannot, and the gap arm exists because it cannot.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import collector.qa as qa

REPO = Path(__file__).resolve().parent.parent
QA = REPO / "collector" / "qa.py"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "STATE", tmp_path / "sections.json")
    qa._failures.clear()
    qa._skipped.clear()
    qa._passes = 0
    yield
    qa._failures.clear()
    qa._skipped.clear()
    qa._passes = 0


def _write_record(at: datetime, failures=(), skipped=()) -> None:
    qa.STATE.parent.mkdir(parents=True, exist_ok=True)
    qa.STATE.write_text(
        json.dumps(
            {
                qa.QA_RUN_SECTION: {
                    "last_run": at.isoformat(),
                    "failures": list(failures),
                    "skipped": list(skipped),
                }
            }
        )
    )


def _record() -> dict:
    return json.loads(qa.STATE.read_text())[qa.QA_RUN_SECTION]


def _run_main(monkeypatch, capsys, *, failures=(), skipped=()):
    """main() with every section but qa_prior_run stubbed out. Module-level
    accumulators are cleared first: production gets a fresh process per run,
    so a test that simulates consecutive runs must too."""
    qa._failures.clear()
    qa._skipped.clear()
    qa._passes = 0

    def _sections(*_a, **_k):
        for name in failures:
            qa.check(name, False)
        for name in skipped:
            qa._skipped.append(name)
        return None

    for fn in (
        "qa_stream",
        "qa_archive",
        "qa_signals_fetch",
        "qa_collect_skips",
        "qa_fade_window_capture",
        "qa_batch_run_budget",
    ):
        monkeypatch.setattr(qa, fn, lambda *a, _f=fn, **k: None)
    monkeypatch.setattr(qa, "qa_stream", _sections)
    monkeypatch.setattr("sys.argv", ["qa"])
    code = 0
    try:
        qa.main()
    except SystemExit as exc:
        code = exc.code
    return code, capsys.readouterr().out


# --- the record is written on every exit path -------------------------------


def test_a_passing_run_records_nothing_open(monkeypatch, capsys):
    code, out = _run_main(monkeypatch, capsys)
    assert code == 0
    assert "all checks pass" in out
    assert _record()["failures"] == [] and _record()["skipped"] == []


def test_a_failing_run_records_the_names_before_exiting_nonzero(monkeypatch, capsys):
    code, _ = _run_main(monkeypatch, capsys, failures=["some check"])
    assert code == 1
    assert _record()["failures"] == ["some check"]


def test_a_partial_run_is_recorded_even_though_it_exits_zero(monkeypatch, capsys):
    """The exact state systemd cannot see: skipped sections, exit code 0."""
    code, out = _run_main(monkeypatch, capsys, skipped=["archive"])
    assert code == 0 and "NOT a full pass" in out
    assert _record()["skipped"] == ["archive"]


def test_no_exit_in_main_precedes_the_record(monkeypatch):
    """Path-sensitive, not a call-count: a `return` or `sys.exit` placed above
    `_record_run` would drop the verdict of exactly the runs worth recording."""
    tree = ast.parse(QA.read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [
        n.lineno
        for n in ast.walk(main)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_record_run"
    ]
    assert len(calls) == 1, "one record per run, or the verdicts disagree"
    exits = [
        n.lineno
        for n in ast.walk(main)
        if isinstance(n, ast.Return)
        or (isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "exit")
    ]
    assert exits, "guard is vacuous if main has no exit statements"
    assert min(exits) > calls[0]
    # and the reader runs against the OLD record, never the one just written
    reads = [
        n.lineno
        for n in ast.walk(main)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "qa_prior_run"
    ]
    assert len(reads) == 1 and reads[0] < calls[0]


# --- the two arms -----------------------------------------------------------


def test_first_run_ever_is_quiet_but_not_silent(capsys):
    qa.qa_prior_run(datetime.now(UTC), [], [])
    out = capsys.readouterr().out
    assert out.startswith("PASS") and "no prior run" in out
    assert not qa._failures


def test_a_healed_failure_is_named_by_the_next_run(capsys):
    now = datetime.now(UTC)
    _write_record(now - timedelta(hours=24), failures=["stream fresh"])
    qa.qa_prior_run(now, [], [])
    out = capsys.readouterr().out
    assert out.startswith("FAIL") and "stream fresh" in out and "green today" in out
    assert qa._failures == [qa.QA_RUN_CHECK]


def test_a_failure_that_is_still_failing_today_is_not_reported_twice(capsys):
    """Today's own line reports it. A second opinion about a live state is the
    noise that trains an operator to stop reading QA."""
    now = datetime.now(UTC)
    _write_record(now - timedelta(hours=24), failures=["stream fresh"])
    qa.qa_prior_run(now, ["stream fresh"], [])
    assert not qa._failures
    assert "still open today" in capsys.readouterr().out


def test_a_section_that_stopped_being_skipped_is_named(capsys):
    now = datetime.now(UTC)
    _write_record(now - timedelta(hours=24), skipped=["archive"])
    qa.qa_prior_run(now, [], [])
    assert qa._failures == [qa.QA_RUN_CHECK]
    assert "SKIPPED ['archive']" in capsys.readouterr().out


def test_the_production_steady_state_of_a_chronic_skip_stays_quiet(capsys):
    """`collect-skips` reports UNVERIFIED on every run of a healthy box, so
    "the prior run was partial" is the NORMAL verdict here. An arm that fired
    on it would fail every night forever, which is why only the healed set is
    reported. Guarding the exact live shape, not a hypothetical one."""
    now = datetime.now(UTC)
    _write_record(now - timedelta(hours=24), skipped=["collect-skips"])
    qa.qa_prior_run(now, [], ["collect-skips"])
    assert not qa._failures
    assert "collect-skips" in capsys.readouterr().out


def test_a_missed_run_is_named_even_when_the_last_run_was_clean(capsys):
    now = datetime.now(UTC)
    _write_record(now - timedelta(hours=qa.QA_RUN_GAP_BUDGET_H + 1))
    qa.qa_prior_run(now, [], [])
    assert qa._failures == [qa.QA_RUN_CHECK]
    assert "DID NOT RUN" in capsys.readouterr().out


def test_one_missed_daily_slot_is_inside_the_budget_and_two_are_not():
    """48h is one missed 10:00Z run; the budget must sit strictly between the
    24h normal cadence and it, or the check cannot see a single miss."""
    assert 24.0 < qa.QA_RUN_GAP_BUDGET_H < 48.0


def test_a_gap_that_ends_on_a_healed_failure_reports_both(capsys):
    now = datetime.now(UTC)
    _write_record(now - timedelta(hours=qa.QA_RUN_GAP_BUDGET_H + 1), failures=["stream fresh"])
    qa.qa_prior_run(now, [], [])
    out = capsys.readouterr().out
    assert "DID NOT RUN" in out and "stream fresh" in out


def test_a_normal_daily_cadence_passes(capsys):
    now = datetime.now(UTC)
    _write_record(now - timedelta(hours=24))
    qa.qa_prior_run(now, [], [])
    assert not qa._failures
    assert capsys.readouterr().out.startswith("PASS")


@pytest.mark.parametrize(
    "blob",
    ['{"run": {"last_run": "not-a-date"}}', '{"run": 3}', "{}", "junk"],
)
def test_an_unreadable_record_reads_as_no_prior_run(blob, capsys):
    qa.STATE.parent.mkdir(parents=True, exist_ok=True)
    qa.STATE.write_text(blob)
    qa.qa_prior_run(datetime.now(UTC), [], [])
    assert not qa._failures
    assert "no prior run" in capsys.readouterr().out


def test_a_record_with_a_date_but_no_name_lists_still_reads_its_age(capsys):
    """A truncated record must not become an excuse to skip the gap arm."""
    qa.STATE.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(UTC) - timedelta(hours=qa.QA_RUN_GAP_BUDGET_H + 1)).isoformat()
    qa.STATE.write_text(json.dumps({"run": {"last_run": old}}))
    qa.qa_prior_run(datetime.now(UTC), [], [])
    assert qa._failures == [qa.QA_RUN_CHECK]
    assert "DID NOT RUN" in capsys.readouterr().out


def test_a_naive_timestamp_is_read_as_utc_not_crashed_on(capsys):
    now = datetime.now(UTC)
    _write_record(now.replace(tzinfo=None) - timedelta(hours=24))
    qa.qa_prior_run(now, [], [])
    assert not qa._failures


# --- the report does not re-arm itself --------------------------------------


def test_reporting_an_unread_failure_does_not_make_it_report_forever(monkeypatch, capsys):
    """Day 1 fails; day 2 is clean and names it; day 3 must be clean. If the
    prior-run FAIL counted toward the run's own verdict, every failure in the
    archive's history would be re-reported every day until the end of time."""
    _run_main(monkeypatch, capsys, failures=["some check"])
    assert _record()["failures"] == ["some check"]

    code2, out2 = _run_main(monkeypatch, capsys)
    assert code2 == 1 and "some check" in out2 and "green today" in out2
    assert _record()["failures"] == [], "the prior-run FAIL is not the run's own finding"

    code3, out3 = _run_main(monkeypatch, capsys)
    assert code3 == 0 and "all checks pass" in out3


# --- the premise: nothing outside qa reads qa -------------------------------


def test_nothing_outside_qa_reads_qa(monkeypatch):
    """The finding's premise, kept honest. If a real consumer of the unit's
    state is ever added — an `OnFailure=`, a `systemctl is-failed`, a mailer —
    this fails and the docstring above it must be rewritten, not the test."""
    out = subprocess.run(
        [
            "git",
            "grep",
            "-lE",
            r"OnFailure=|ExecStopPost=|is-failed|systemctl .*--failed",
            "--",
            ".",
            ":(exclude)phase0",
            ":(exclude)collector/qa.py",  # the reader this pass adds, and its comment
            ":(exclude)tests",
            ":(exclude)docs",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert out == [], f"a consumer of unit state now exists: {out}"

    service = (REPO / "scripts/systemd/hyxlab-qa.service").read_text()
    assert "Type=oneshot" in service
    assert not re.search(r"^(OnFailure|ExecStopPost|Restart)=", service, re.M)
    # the project's only journal reader, and it does not cover this unit
    assert "hyxlab-qa.timer" not in qa.BATCH_RUN_BUDGET_H


def test_recording_the_run_keeps_the_other_sections_clocks():
    """The record shares sections.json with every bounded-skip clock in the
    file. A writer that replaced the document instead of adding a key would
    reset `first_seen` for all of them every night — and a clock that resets
    daily can never go stale, which is the exact defect STATE exists to stop."""
    qa.STATE.parent.mkdir(parents=True, exist_ok=True)
    qa.STATE.write_text(json.dumps({"archive": {"first_seen": "2026-08-02T08:20:31"}}))
    qa._record_run([], [], datetime.now(UTC))
    state = json.loads(qa.STATE.read_text())
    assert state["archive"]["first_seen"] == "2026-08-02T08:20:31"
    assert state[qa.QA_RUN_SECTION]["failures"] == []
