"""The digest that gives the box's one egress channel something measured to carry.

Five passes in a row built a checker and found its verdict read by nothing.
`collector.health` answers the successor question those passes kept deferring --
does any signal LEAVE this box -- by measuring that the answer is "one thing,
and it carries only prose" (`git push origin main` from `scripts/autoloop.sh`,
with `/data/` and `/reports/` gitignored so no checker OUTPUT can ride it), and
then making the autoloop's cold start read a machine-written digest.

This suite exists because the digest's whole value is that it does not LIE. Four
properties of systemd's persisted state were measured on 2026-09-06 before they
were reported, and each is a way a naive digest would report a stale fact in the
present tense. They are asserted against `judge`, which is pure, so the arms hold
regardless of what this box happens to be doing while the suite runs.

The fifth is the live defect the digest found on its first run, and it is the
reason `qa_record_path` exists at all.
"""

from __future__ import annotations

import ast
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from collector import health
from collector.qa import _load_state, _prior_run

REPO = Path(__file__).resolve().parent.parent
NOW = 1_788_704_400.0  # 2026-09-06 09:20:00 CDT, the moment the fields below were read


def svc(**over: str) -> dict[str, str]:
    """A healthy exited oneshot, as `systemctl show --timestamp=unix` rendered it."""
    base = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "Result": "success",
        "NRestarts": "0",
        "ExecMainStatus": "0",
        "ExecMainStartTimestamp": "@1788704100",
        "ExecMainExitTimestamp": "@1788704132",
        "WorkingDirectory": "/home/devs/workspace/hyxrestration-stable",
    }
    return base | over


def timer(**over: str) -> dict[str, str]:
    base = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "Result": "success",
        "LastTriggerUSec": "@1788704100",
        "NextElapseUSecRealtime": "@1788704400",
    }
    return base | over


# --------------------------------------------------------------------------
# (1) `Result` is the PRIOR run's while a unit is in flight.
# --------------------------------------------------------------------------


def test_a_unit_in_flight_is_running_not_its_previous_result() -> None:
    """Measured: hyxlab-sweep, poly-sweep and autoloop all read
    ActiveState=activating with Result=success -- success belonging to the
    PREVIOUS run, with today's still executing. Reporting that as the current
    result would misreport exactly the long multi-hour units whose failures
    matter most."""
    h = health.judge(
        "hyxlab-sweep.service",
        svc(ActiveState="activating", Result="success", ExecMainExitTimestamp=""),
        timer(NextElapseUSecRealtime=""),
        NOW,
    )
    assert h.state == "RUNNING"
    assert "success" not in h.detail, "a run in flight must quote no result at all"


def test_a_running_unit_that_previously_failed_still_reads_running() -> None:
    """The same field, the other way round: a unit whose LAST run failed but
    which is running NOW is not currently failed. If RUNNING were decided after
    the result arm, every retry of a failing unit would read FAILED forever."""
    h = health.judge(
        "hyxlab-sweep.service",
        svc(ActiveState="activating", Result="exit-code", ExecMainStatus="1", ExecMainExitTimestamp=""),
        timer(NextElapseUSecRealtime=""),
        NOW,
    )
    assert h.state == "RUNNING"


# --------------------------------------------------------------------------
# (2) `ExecMainStatus=0` is a default, not a measurement.
# --------------------------------------------------------------------------


def test_exit_status_is_only_read_once_the_unit_has_exited() -> None:
    """Every unit that has never exited reports ExecMainStatus=0 -- the daemons
    up since 08-29 do, and so would a unit that has never run. The status is a
    measurement only when ExecMainExitTimestamp is populated."""
    never_exited = svc(ExecMainExitTimestamp="", ExecMainStatus="0", ActiveState="inactive")
    h = health.judge("hyxlab-qa.service", never_exited, timer(LastTriggerUSec=""), NOW)
    assert h.state == "NEVER-RAN", f"a unit with no trigger and no exit is not a pass: {h}"


def test_a_nonzero_status_on_a_unit_that_never_exited_is_not_a_failure() -> None:
    """The gate itself, exercised. Without it the reported failure would rest on
    a field that was never written by a run -- ExecMainStatus carries whatever
    the manager last put there, and "never exited" is precisely the case where
    that is not a measurement. The manager's own word for this unit is success."""
    h = health.judge(
        "hyxlab-qa.service",
        svc(ExecMainStatus="1", ExecMainExitTimestamp="", Result="success"),
        timer(),
        NOW,
    )
    assert h.state != "FAILED", f"a status no run wrote must not condemn the unit: {h}"


def test_a_nonzero_exit_with_a_success_result_is_still_a_failure() -> None:
    h = health.judge("hyxlab-qa.service", svc(ExecMainStatus="1"), timer(), NOW)
    assert h.state == "FAILED"
    assert "exit=1" in h.detail


def test_a_failed_result_is_a_failure() -> None:
    h = health.judge("hyxlab-qa.service", svc(Result="exit-code", ExecMainStatus="1"), timer(), NOW)
    assert h.state == "FAILED"


# --------------------------------------------------------------------------
# (3) An empty NextElapseUSecRealtime is not a broken timer.
# --------------------------------------------------------------------------


def test_lateness_is_never_asked_of_a_running_service() -> None:
    """systemd clears the next elapse while the triggered service is active --
    that is why `list-timers` prints `-` for the sweeps. A staleness rule reading
    next-elapse alone would fire on every long sweep, daily, on a healthy box."""
    h = health.judge(
        "hyxlab-poly-sweep.service",
        svc(ActiveState="activating", ExecMainExitTimestamp=""),
        timer(NextElapseUSecRealtime="", LastTriggerUSec="@1788668109"),
        NOW,
    )
    assert h.state == "RUNNING"


def test_an_idle_timer_with_no_next_elapse_is_reported() -> None:
    """The same emptiness with the service IDLE is the anomaly: an active timer
    the manager has scheduled nothing for."""
    h = health.judge("hyxlab-qa.service", svc(), timer(NextElapseUSecRealtime=""), NOW)
    assert h.state == "NO-NEXT"


def test_an_idle_timer_past_its_own_next_elapse_is_late() -> None:
    overdue = NOW - health.LATE_SLACK_S - 60
    h = health.judge("hyxlab-qa.service", svc(), timer(NextElapseUSecRealtime=f"@{overdue}"), NOW)
    assert h.state == "LATE"


def test_the_late_slack_is_one_period_of_the_fastest_timer() -> None:
    """The slack is derived, not picked: one full period of the shortest cadence
    in the tree. Below one period, ordinary scheduling jitter could cross it;
    at one period a stopped timer still shows up within a single missed cycle."""
    cadences = [
        line.split("=", 1)[1].strip()
        for f in sorted((REPO / "scripts/systemd").glob("hyxlab-*.timer"))
        for line in f.read_text().splitlines()
        if line.startswith("OnCalendar=")
    ]
    assert "*:0/5" in cadences, f"the 5-minute cadence this slack is derived from is gone: {cadences}"
    assert health.LATE_SLACK_S == 300.0


def test_a_timer_that_is_off_is_not_a_late_service() -> None:
    h = health.judge("hyxlab-qa.service", svc(), timer(ActiveState="inactive"), NOW)
    assert h.state == "TIMER-OFF"


# --------------------------------------------------------------------------
# (4) An uninstalled unit answers quietly.
# --------------------------------------------------------------------------


def test_a_unit_the_manager_never_heard_of_reads_unloaded() -> None:
    """`systemctl show` on an unknown name exits 0 with LoadState=not-found.
    Since the set is discovered from the REPO, a unit committed but never
    promoted must read UNLOADED rather than silently drop out of the digest."""
    h = health.judge("hyxlab-new.service", {"LoadState": "not-found", "ActiveState": "inactive"}, timer(), NOW)
    assert h.state == "UNLOADED"


def test_show_on_an_unknown_unit_does_not_raise() -> None:
    if not health.discover_units():
        pytest.skip("no vendored units")
    props = health.show("hyxlab-definitely-not-a-unit.service")
    assert props.get("LoadState") in ("not-found", None) or props == {}


# --------------------------------------------------------------------------
# Daemons have no cadence to be late for.
# --------------------------------------------------------------------------


def test_a_daemon_is_judged_by_activestate_alone() -> None:
    up = health.judge("hyxlab-stream.service", svc(ActiveState="active", SubState="running", ExecMainExitTimestamp=""), None, NOW)
    assert up.state == "OK"
    assert "restarts" in up.detail
    down = health.judge("hyxlab-stream.service", svc(ActiveState="failed", Result="oom-kill"), None, NOW)
    assert down.state == "DOWN"
    assert "oom-kill" in down.detail


# --------------------------------------------------------------------------
# (5) THE LIVE DEFECT: qa's record is relative to the tree qa ran in.
# --------------------------------------------------------------------------


def test_the_qa_record_is_read_from_the_units_working_directory() -> None:
    """Measured 2026-09-06: the dev tree's record read 09-05T20:25Z (a hand run)
    while production's read 09-06T10:00Z, on the same morning. `qa.STATE` is a
    RELATIVE path, so a digest reading its own CWD reports a record the timer
    never wrote -- and the dev copy is the one a naive reader prints."""
    p = health.qa_record_path(svc(WorkingDirectory="/home/devs/workspace/hyxrestration-stable"))
    assert p == Path("/home/devs/workspace/hyxrestration-stable/reports/qa/sections.json")
    assert str(p).startswith("/home/devs/workspace/hyxrestration-stable"), "must not fall back to CWD"


def test_an_optional_working_directory_prefix_is_stripped() -> None:
    """systemd prefixes WorkingDirectory with `-` when the directory is allowed
    to be missing. Left in place it makes every path relative and wrong."""
    assert health.qa_record_path(svc(WorkingDirectory="-/tmp/x")) == Path("/tmp/x/reports/qa/sections.json")


def test_a_unit_without_a_working_directory_falls_back_to_this_repo() -> None:
    assert health.qa_record_path(svc(WorkingDirectory="")).is_relative_to(health.REPO)


def test_the_record_path_tracks_qas_own_constant() -> None:
    """Spelled once. If qa moves its record, the digest follows it."""
    from collector.qa import STATE

    assert str(health.qa_record_path(svc(WorkingDirectory="/x"))) == str(Path("/x") / STATE)


# --------------------------------------------------------------------------
# The arm qa cannot have: the run STARTED but its record did not land here.
# --------------------------------------------------------------------------


def _record(at: str, failures: list[str] | None = None, skipped: list[str] | None = None) -> dict:
    return {"run": {"last_run": at, "failures": failures or [], "skipped": skipped or []}}


def test_a_record_older_than_the_units_own_start_is_called_stale() -> None:
    """qa reads its record from inside the process that writes it, so it cannot
    notice that the manager started a run whose record never landed here. The
    unit's start time and the record's timestamp are two independent facts about
    the same run; this is the only place both are in hand."""
    now = datetime(2026, 9, 6, 14, 20, tzinfo=UTC)
    line = health.judge_qa(
        _record("2026-09-05T20:25:25+00:00"),
        svc(ExecMainStartTimestamp=f"@{datetime(2026, 9, 6, 10, 0, tzinfo=UTC).timestamp()}"),
        now,
    )
    assert "RECORD-STALE" in line
    assert "13.6h AFTER" in line


def test_a_record_written_by_the_units_own_run_is_not_stale() -> None:
    now = datetime(2026, 9, 6, 14, 20, tzinfo=UTC)
    start = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    line = health.judge_qa(
        _record("2026-09-06T10:00:00.318298+00:00"),
        svc(ExecMainStartTimestamp=f"@{start.timestamp()}"),
        now,
    )
    assert "RECORD-STALE" not in line
    assert "09-06 10:00Z" in line


def test_the_record_lag_slack_is_wider_than_a_runs_own_startup() -> None:
    """`_record_run` stores main()'s `now`, taken before any check runs, so the
    record is the unit's start plus interpreter startup -- sub-second in
    production. The slack must be far above that and far below the daily cadence
    it must not confuse."""
    assert 60 < health.QA_RECORD_LAG_S < 24 * 3600 / 2


def test_the_qa_verdict_reports_skips_as_not_a_full_pass() -> None:
    now = datetime(2026, 9, 6, 14, 20, tzinfo=UTC)
    start = f"@{datetime(2026, 9, 6, 10, 0, tzinfo=UTC).timestamp()}"
    skipped = health.judge_qa(
        _record("2026-09-06T10:00:00+00:00", skipped=["collect-skips"]), svc(ExecMainStartTimestamp=start), now
    )
    assert "NOT a full pass" in skipped and "collect-skips" in skipped
    failed = health.judge_qa(
        _record("2026-09-06T10:00:00+00:00", failures=["stream"], skipped=["collect-skips"]),
        svc(ExecMainStartTimestamp=start),
        now,
    )
    assert "FAILURES" in failed, "a failure must not be hidden behind a skip"


def test_an_absent_record_is_reported_not_treated_as_clean() -> None:
    assert "no run on record" in health.judge_qa({}, svc(), datetime.now(UTC))


# --------------------------------------------------------------------------
# The premise this whole module rests on, so it fails the day it stops holding.
# --------------------------------------------------------------------------


def test_the_unit_set_is_discovered_not_enumerated() -> None:
    """The defect `test_lint_scope.py` closed for Python and `test_shell_lint.py`
    for shell: an enumerated list cannot fail on the day something is added to it."""
    src = (REPO / "collector/health.py").read_text()
    assert 'glob("hyxlab-*.service")' in src
    on_disk = {p.name for p in (REPO / "scripts/systemd").glob("hyxlab-*.service")}
    assert {u for u, _ in health.discover_units()} == on_disk


def test_every_timer_backed_service_is_paired_with_its_timer() -> None:
    paired = dict(health.discover_units())
    for t in (REPO / "scripts/systemd").glob("hyxlab-*.timer"):
        assert paired.get(f"{t.stem}.service") is True, f"{t.name} has no service, or it was not paired"


def test_the_digest_opens_no_database() -> None:
    """Ops rule / mistakes #20: an ad-hoc reader that takes a writer lock can
    kill the daemon it is reporting on. A health report must not be able to hurt
    the thing it watches, so it reads the manager and one JSON file."""
    tree = ast.parse((REPO / "collector/health.py").read_text())
    names = {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    } | {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "duckdb" not in names
    assert "connect" not in (REPO / "collector/health.py").read_text().replace("connect_retry", "")


def test_nothing_in_this_tree_notifies_off_box() -> None:
    """The measurement the digest's design rests on: no smtp, no webhook, no
    mail, no push client anywhere. The day one appears, this pass's conclusion
    ("the git push is the whole egress") needs re-deciding rather than inheriting."""
    files = subprocess.run(
        ["git", "ls-files", "*.py", "*.sh", "*.service", "*.timer"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()
    hits = []
    for f in files:
        if f.startswith(("phase0/", "tests/")):
            continue
        low = (REPO / f).read_text(errors="ignore").lower()
        for token in ("smtplib", "sendmail", "webhook", "ntfy.sh", "pushover", "api.telegram", "hooks.slack"):
            if token in low:
                hits.append((f, token))
    assert not hits, f"something notifies off-box now; the digest's premise has changed: {hits}"


def test_checker_output_cannot_ride_the_push() -> None:
    """`/data/` and `/reports/` are gitignored, rooted, on purpose. That is why
    the digest is an INPUT the autoloop reads rather than a file that leaves."""
    ignore = (REPO / ".gitignore").read_text().splitlines()
    assert "/data/" in ignore and "/reports/" in ignore
    from collector.qa import STATE

    assert subprocess.run(
        ["git", "check-ignore", "-q", str(STATE)], cwd=REPO
    ).returncode == 0, "qa's record is committed now — the digest's premise has changed"


def test_the_autoloop_prompt_reads_the_digest() -> None:
    """The digest's ONLY consumer. Without this line it is the fifth unread
    verdict in a row, which is the exact defect this pass exists to answer."""
    prompt = (REPO / "scripts/autoloop-prompt.md").read_text()
    assert "collector.health" in prompt, "the autoloop no longer reads the digest"


def test_the_autoloop_is_the_only_thing_that_pushes() -> None:
    loop = (REPO / "scripts/autoloop.sh").read_text()
    assert "origin main" in loop


# --------------------------------------------------------------------------
# qa's own parser is reused, not reimplemented -- and reusing it changed nothing.
# --------------------------------------------------------------------------


def test_qa_parser_is_unchanged_when_called_the_old_way(tmp_path: Path) -> None:
    """`_load_state(path)` and `_prior_run(state)` gained parameters so a reader
    outside the process can name another tree's record. Defaulted, they must
    behave exactly as before."""
    assert _load_state(tmp_path / "nope.json") == {}
    assert _prior_run({}) is None
    assert _prior_run({"run": {"last_run": "not-a-date"}}) is None
    rec = _record("2026-09-06T10:00:00+00:00", skipped=["collect-skips"])
    parsed = _prior_run(rec)
    assert parsed is not None and parsed.skipped == ("collect-skips",)


def test_a_naive_timestamp_in_the_record_still_reads_as_utc() -> None:
    """sections.json has carried both forms; the digest must not shift by the
    box's offset when it meets the older one."""
    parsed = _prior_run(_record("2026-09-06T10:00:00"))
    assert parsed is not None and parsed.at.tzinfo is not None
    assert parsed.at == datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


def test_load_state_reads_the_path_it_is_given(tmp_path: Path) -> None:
    p = tmp_path / "sections.json"
    p.write_text(json.dumps(_record("2026-09-06T10:00:00+00:00")))
    assert _load_state(p)["run"]["last_run"].startswith("2026-09-06")


# --------------------------------------------------------------------------
# End to end, against this box.
# --------------------------------------------------------------------------


def test_the_digest_runs_and_reports_every_vendored_unit() -> None:
    rows = health.report()
    assert {r.unit for r in rows} == {u for u, _ in health.discover_units()}
    assert all(r.state for r in rows)


def test_the_digest_exits_zero_even_with_a_failing_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is a report, not a gate. A gate whose failure nothing reads is the
    defect this pass exists to answer."""
    monkeypatch.setattr(
        health, "report", lambda *a, **k: [health.UnitHealth("hyxlab-x.service", "FAILED", "exit=1")]
    )
    health.main()  # must not raise SystemExit
