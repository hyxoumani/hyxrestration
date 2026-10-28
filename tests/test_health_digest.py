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
import io
import json
import re
import subprocess
import tokenize
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from collector import health, qa
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
        svc(
            ActiveState="activating",
            Result="exit-code",
            ExecMainStatus="1",
            ExecMainExitTimestamp="",
        ),
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


def test_a_scheduled_first_run_is_not_a_fault() -> None:
    """A timer with no trigger and a next elapse still AHEAD is a new unit
    waiting for its first slot -- `hyxlab-atlas.timer` on 2026-10-17, enabled
    38h before its Monday 02:48Z slot, and `hyxlab-divergence` across three
    digests in 2026-09. Printing those as ATTENTION outranks the ladder for
    a unit whose schedule is on the same line; the manager has it covered,
    and the LATE arm catches the slot going by. The verdict says WHEN."""
    never = svc(ExecMainExitTimestamp="", ExecMainStatus="0", ActiveState="inactive")
    h = health.judge(
        "hyxlab-atlas.service",
        never,
        timer(LastTriggerUSec="", NextElapseUSecRealtime=f"@{int(NOW) + 38 * 3600}"),
        NOW,
    )
    assert h.state == "FIRST-DUE", h
    assert h.ok, "a scheduled first run must not be counted against the box"
    assert "38.0h" in h.detail, h.detail
    # The slot already passed without a trigger: that is the fault the arm
    # exists for, and a next elapse in the past must not excuse it.
    h = health.judge(
        "hyxlab-atlas.service",
        never,
        timer(LastTriggerUSec="", NextElapseUSecRealtime=f"@{int(NOW) - 3600}"),
        NOW,
    )
    assert h.state == "NEVER-RAN", h
    assert not h.ok


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
    assert "*:0/5" in cadences, (
        f"the 5-minute cadence this slack is derived from is gone: {cadences}"
    )
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
    h = health.judge(
        "hyxlab-new.service", {"LoadState": "not-found", "ActiveState": "inactive"}, timer(), NOW
    )
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
    up = health.judge(
        "hyxlab-stream.service",
        svc(ActiveState="active", SubState="running", ExecMainExitTimestamp=""),
        None,
        NOW,
    )
    assert up.state == "OK"
    assert "restarts" in up.detail
    down = health.judge(
        "hyxlab-stream.service", svc(ActiveState="failed", Result="oom-kill"), None, NOW
    )
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
    assert str(p).startswith("/home/devs/workspace/hyxrestration-stable"), (
        "must not fall back to CWD"
    )


def test_an_optional_working_directory_prefix_is_stripped() -> None:
    """systemd prefixes WorkingDirectory with `-` when the directory is allowed
    to be missing. Left in place it makes every path relative and wrong."""
    assert health.qa_record_path(svc(WorkingDirectory="-/tmp/x")) == Path(
        "/tmp/x/reports/qa/sections.json"
    )


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


def test_the_qa_verdict_reports_an_unexpected_skip_as_not_a_full_pass() -> None:
    now = datetime(2026, 9, 6, 14, 20, tzinfo=UTC)
    start = f"@{datetime(2026, 9, 6, 10, 0, tzinfo=UTC).timestamp()}"
    skipped = health.judge_qa(
        _record("2026-09-06T10:00:00+00:00", skipped=["fade-window"]),
        svc(ExecMainStartTimestamp=start),
        now,
    )
    assert "NOT a full pass" in skipped and "fade-window" in skipped
    failed = health.judge_qa(
        _record("2026-09-06T10:00:00+00:00", failures=["stream"], skipped=["fade-window"]),
        svc(ExecMainStartTimestamp=start),
        now,
    )
    assert "FAILURES" in failed, "a failure must not be hidden behind a skip"


def test_the_standing_skip_alone_does_not_make_the_run_partial() -> None:
    """`collect-skips` is SKIPPED on every run of a healthy box, so a verdict
    that called it partial read `NOT a full pass` every night forever -- the
    alarm fatigue qa refuses to manufacture, reintroduced by its reader."""
    now = datetime(2026, 9, 6, 14, 20, tzinfo=UTC)
    line = health.judge_qa(
        _record("2026-09-06T10:00:00+00:00", skipped=[qa.COLLECT_SKIP_SECTION]),
        svc(ExecMainStartTimestamp=f"@{datetime(2026, 9, 6, 10, 0, tzinfo=UTC).timestamp()}"),
        now,
    )
    assert "NOT a full pass" not in line
    assert "clean" in line
    assert qa.COLLECT_SKIP_SECTION in line, "quiet is not the same as unreported"


def test_a_real_skip_beside_the_standing_one_still_reads_partial() -> None:
    """The masking half: before this, adding a genuinely new skip changed only
    the contents of a list inside a phrase that was already there every night."""
    now = datetime(2026, 9, 6, 14, 20, tzinfo=UTC)
    line = health.judge_qa(
        _record("2026-09-06T10:00:00+00:00", skipped=[qa.COLLECT_SKIP_SECTION, "batch-run-budget"]),
        svc(ExecMainStartTimestamp=f"@{datetime(2026, 9, 6, 10, 0, tzinfo=UTC).timestamp()}"),
        now,
    )
    assert "NOT a full pass" in line
    assert "batch-run-budget" in line
    assert "skipped ['batch-run-budget']" in line, "the standing name must not pad the partial list"


def test_the_standing_set_is_exactly_the_skip_qa_never_ages() -> None:
    """The premise the digest's quiet arm rests on. Every OTHER `_skipped.append`
    name means something could not be MEASURED, so its presence is news; this one
    is the only site deliberately exempt from `_skip_age_h`. Read from the source
    so a fifth skip added tomorrow fails here instead of being silently quieted."""
    src = Path(qa.__file__).read_text()
    appended = set(
        re.findall(r"_skipped\.append\(\s*(?:\"([^\"]+)\"|([A-Z_][A-Z_0-9]*))\s*\)", src)
    )
    names = {a or b for a, b in appended}
    assert "COLLECT_SKIP_SECTION" in names, (
        "the standing skip must be appended by its constant, not a literal"
    )
    assert frozenset({qa.COLLECT_SKIP_SECTION}) == qa.STANDING_SKIPS
    assert len(names) > 1, "the scan must actually be finding the other skip sites"


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
        assert paired.get(f"{t.stem}.service") is True, (
            f"{t.name} has no service, or it was not paired"
        )


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


def _code_only(path: Path) -> str:
    """A file's CODE, with comments and string literals removed.

    The naive version of this scan read raw text and matched the word "webhook"
    in `health.py`'s own docstring -- the file that DOCUMENTS the measurement
    became the counter-example to it, the moment it was committed. That is the
    self-match trap the ops rules already name for `pkill -f`. Stripping
    comments and literals asks the question of the code instead, which is what
    "does anything notify" actually means; a notifier needs an import and a
    call, neither of which survives as a comment.
    """
    src = path.read_text(errors="ignore")
    if path.suffix != ".py":
        return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            out.append(tok.string)
    return " ".join(out)


def test_nothing_in_this_tree_notifies_off_box() -> None:
    """The measurement the digest's design rests on: no smtp, no webhook, no
    mail, no push client anywhere. The day one appears, this pass's conclusion
    ("the git push is the whole egress") needs re-deciding rather than
    inherited. Asserted over CODE -- see `_code_only` for why that matters."""
    files = subprocess.run(
        ["git", "ls-files", "*.py", "*.sh", "*.service", "*.timer"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    hits = []
    for f in files:
        if f.startswith("phase0/"):
            continue
        low = _code_only(REPO / f).lower()
        for token in (
            "smtplib",
            "sendmail",
            "webhook",
            "ntfy.sh",
            "pushover",
            "api.telegram",
            "hooks.slack",
        ):
            if token in low:
                hits.append((f, token))
    assert not hits, f"something notifies off-box now; the digest's premise has changed: {hits}"


def test_the_premise_scan_still_sees_a_real_notifier(tmp_path: Path) -> None:
    """The stripper must not have turned the scan into something that passes by
    matching nothing. A notifier in CODE is still caught; the same word in a
    docstring is not."""
    real = tmp_path / "real.py"
    real.write_text("import smtplib\nsmtplib.SMTP('x')\n")
    assert "smtplib" in _code_only(real)
    prose = tmp_path / "prose.py"
    prose.write_text('"""No smtplib here, only talk of it."""\n# and a webhook comment\nx = 1\n')
    assert "smtplib" not in _code_only(prose) and "webhook" not in _code_only(prose)


def test_checker_output_cannot_ride_the_push() -> None:
    """`/data/` and `/reports/` are gitignored, rooted, on purpose. That is why
    the digest is an INPUT the autoloop reads rather than a file that leaves."""
    ignore = (REPO / ".gitignore").read_text().splitlines()
    assert "/data/" in ignore and "/reports/" in ignore
    from collector.qa import STATE

    assert subprocess.run(["git", "check-ignore", "-q", str(STATE)], cwd=REPO).returncode == 0, (
        "qa's record is committed now — the digest's premise has changed"
    )


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
        health,
        "report",
        lambda *a, **k: [health.UnitHealth("hyxlab-x.service", "FAILED", "exit=1")],
    )
    health.main()  # must not raise SystemExit


# --- OOM attribution -------------------------------------------------------
# `Result=oom-kill` names the verb, never the culprit. These arms are the real
# kernel lines from 2026-09-07 12:50:38-39, when a global OOM killed three
# hyxlab batch units and then the 24 GiB stranger that had actually exhausted
# the box. The digest reported "FAILED result=oom-kill, exit=9" and nothing
# else, and reading that as a poly-sweep fault cost a whole pass.

OOM_LINES = """\
2026-09-07T12:50:38-05:00 hyz kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=user.slice,mems_allowed=0,global_oom,task_memcg=/user.slice/user-1000.slice/user@1000.service/app.slice/hyxlab-poly-sweep.service,task=python,pid=3235032,uid=1000
2026-09-07T12:50:38-05:00 hyz kernel: Out of memory: Killed process 3235032 (python) total-vm:8630308kB, anon-rss:341160kB, file-rss:8kB, shmem-rss:0kB, UID:1000 pgtables:2520kB oom_score_adj:500
2026-09-07T12:50:39-05:00 hyz kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=user.slice,mems_allowed=0,global_oom,task_memcg=/user.slice/user-1000.slice/user@1000.service/app.slice/hyxlab-breadth.service,task=python,pid=3991282,uid=1000
2026-09-07T12:50:39-05:00 hyz kernel: Out of memory: Killed process 3991282 (python) total-vm:559068kB, anon-rss:272276kB, file-rss:8kB, shmem-rss:0kB, UID:1000 pgtables:848kB oom_score_adj:500
2026-09-07T12:50:39-05:00 hyz kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=user.slice,mems_allowed=0,global_oom,task_memcg=/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.chromium.Chromium-305403.scope,task=python,pid=3990817,uid=1000
2026-09-07T12:50:39-05:00 hyz kernel: Out of memory: Killed process 3990817 (python) total-vm:26683232kB, anon-rss:25213472kB, file-rss:8kB, shmem-rss:0kB, UID:1000 pgtables:51152kB oom_score_adj:0
"""


def test_parse_kernel_oom_joins_cgroup_and_footprint_on_pid():
    victims = health.parse_kernel_oom(OOM_LINES)
    assert [v.owner for v in victims] == [
        "hyxlab-poly-sweep.service",
        "hyxlab-breadth.service",
        "app-org.chromium.Chromium-305403.scope",
    ]
    assert [v.anon_kb for v in victims] == [341160, 272276, 25213472]
    assert [v.adj for v in victims] == [500, 500, 0]


def test_bystander_is_named_as_one():
    """The whole point: the failing unit's footprint next to the episode's real hog."""
    note = health.oom_attribution(health.parse_kernel_oom(OOM_LINES), "hyxlab-poly-sweep.service")
    assert note.startswith("OOM BYSTANDER: held 333 MiB anon (adj=500)")
    assert "24.0 GiB" in note and "app-org.chromium.Chromium-305403.scope" in note


def test_a_unit_that_was_the_memory_is_not_excused():
    """Symmetry arm: the attribution must be able to convict, or it is a whitewash."""
    note = health.oom_attribution(
        health.parse_kernel_oom(OOM_LINES), "app-org.chromium.Chromium-305403.scope"
    )
    assert "this unit WAS the memory" in note and "24.0 GiB" in note


def test_a_separate_episode_is_not_borrowed_as_an_excuse():
    """A stranger's kill hours later must not be quoted as this unit's alibi."""
    later = OOM_LINES.replace("2026-09-07T12:50:39", "2026-09-07T18:50:39")
    note = health.oom_attribution(health.parse_kernel_oom(later), "hyxlab-poly-sweep.service")
    assert "this unit WAS the memory" in note, note


def test_attribution_is_silent_rather_than_wrong_when_the_journal_is_gone():
    """Journald rotates. No record must mean no claim -- not an invented one."""
    assert health.parse_kernel_oom("") == []
    assert health.oom_attribution([], "hyxlab-poly-sweep.service") == ""
    assert health.oom_attribution(health.parse_kernel_oom(OOM_LINES), "hyxlab-qa.service") == ""


def test_judge_appends_attribution_only_to_the_failure_it_explains():
    victims = health.parse_kernel_oom(OOM_LINES)
    note = health.oom_attribution(victims, "hyxlab-poly-sweep.service")
    h = health.judge(
        "hyxlab-poly-sweep.service", svc(Result="oom-kill", ExecMainStatus="9"), timer(), NOW, note
    )
    assert h.state == "FAILED" and "OOM BYSTANDER" in h.detail
    # A healthy unit carries no note even if the kernel log is full of them.
    assert health.judge("hyxlab-poly-sweep.service", svc(), timer(), NOW).state == "OK"


# --------------------------------------------------------------------------
# (7) The faults that left no trace: a cadenced unit's failure is erased by its
#     own next success, so the digest's six-hour reader finds nothing to read.
#     Fixtures are VERBATIM records from this box's journal, 2026-09-08.
# --------------------------------------------------------------------------

FAIL_ID = "d9b373ed55a64feb8242e02dbe79a49c"
START_FAIL_ID = "be02cf6855d2428ba40df7e9d022f03d"
# 2026-09-08 14:19Z, the moment the section below was measured.
FAIL_NOW = datetime(2026, 9, 8, 14, 19, tzinfo=UTC)


def jrec(unit: str, us: int, result: str = "exit-code", mid: str = FAIL_ID) -> str:
    return json.dumps(
        {
            "MESSAGE_ID": mid,
            "USER_UNIT": unit,
            "MESSAGE": f"{unit}: Failed with result '{result}'.",
            "__REALTIME_TIMESTAMP": str(us),
        }
    )


# The real 24h window: five in-scope failures, two strangers, one duplicate id.
JOURNAL = "\n".join(
    [
        jrec("hyxlab-poly-sweep.service", 1788803438908394, "oom-kill"),
        jrec("hyxlab-breadth.service", 1788803438966480, "oom-kill"),
        jrec("hyxlab-collect.service", 1788803438979683, "oom-kill"),
        jrec("app-org.chromium.Chromium-305403.scope", 1788803440647878, "oom-kill"),
        jrec("hylshi-cli-products.service", 1788858086720797),
        jrec("hyxlab-qa.service", 1788861607714922),
        jrec("hyxlab-breadth.service", 1788871650593392),
    ]
)
HYXLAB_UNITS = {u for u, _ in health.discover_units()}


def test_a_failure_a_later_success_erased_is_still_reported():
    """THE DEFECT, end to end. `hyxlab-breadth` timed out against Kalshi at
    09-08 12:47Z and succeeded five minutes later, so `Result` reads success and
    the unit row says OK -- correctly. Measured on this box: five in-scope
    failures in 24h, of which the digest showed ONE. Four were unrecoverable."""
    failures = health.parse_unit_failures(JOURNAL, HYXLAB_UNITS)
    assert len(failures) == 5
    # The box as it actually read: breadth back to OK, qa still in ATTENTION.
    rows = [
        health.UnitHealth("hyxlab-breadth.service", "OK", ""),
        health.UnitHealth("hyxlab-qa.service", "FAILED", "result=exit-code"),
    ]
    lines = health.failure_history_lines(failures, rows, FAIL_NOW)
    breadth = next(ln for ln in lines if "hyxlab-breadth" in ln)
    assert "2x in 24h" in breadth and "CLEARED" in breadth
    assert "exit-code 09-08 12:47Z" in breadth and "oom-kill 09-07 17:50Z" in breadth
    assert "4 on units now healthy (invisible above)" in lines[-1]


def test_each_failure_is_counted_once_though_systemd_logs_it_twice():
    """(1) Every failure emits BOTH `Failed with result` and `Failed to start`.
    Reading both -- or matching the word "Failed" -- doubles every count, which
    would turn one flaky night into a fabricated trend."""
    doubled = JOURNAL + "\n" + jrec("hyxlab-qa.service", 1788861607714999, mid=START_FAIL_ID)
    assert len(health.parse_unit_failures(doubled, HYXLAB_UNITS)) == 5


def test_attribution_reads_user_unit_because_the_manager_is_the_logger():
    """(2) The user manager logs ABOUT the unit, so `_SYSTEMD_USER_UNIT` is
    `init.scope` and `UNIT` is unset. A reader keying on either sees nothing."""
    rec = json.dumps(
        {
            "MESSAGE_ID": FAIL_ID,
            "USER_UNIT": "hyxlab-qa.service",
            "UNIT": None,
            "_SYSTEMD_USER_UNIT": "init.scope",
            "MESSAGE": "hyxlab-qa.service: Failed with result 'exit-code'.",
            "__REALTIME_TIMESTAMP": "1788861607714922",
        }
    )
    (only,) = health.parse_unit_failures(rec, HYXLAB_UNITS)
    assert only.unit == "hyxlab-qa.service" and only.result == "exit-code"


def test_another_projects_units_are_not_this_projects_health():
    """(3) The same journal holds `hylshi-*` and a Chromium scope. Both are in
    the fixture and neither may appear -- the `SYSTEMD_UNIT_PATH` lesson."""
    names = {f.unit for f in health.parse_unit_failures(JOURNAL, HYXLAB_UNITS)}
    assert not any("hylshi" in n or "chromium" in n.lower() for n in names)
    # And the scope is DISCOVERED, so a unit added tomorrow is covered by it.
    assert {u for u, _ in health.discover_units()} == HYXLAB_UNITS


def test_a_clean_count_states_the_window_it_actually_covered():
    """(4) journald vacuums. Reporting an empty buffer as a quiet night is the
    lie this module exists to stop telling, so a floor inside the window
    downgrades the claim instead of the count."""
    floor = FAIL_NOW.replace(hour=12)  # 12:19Z, so 2.0h of retention
    (line,) = health.failure_history_lines([], [], FAIL_NOW, floor)
    assert "0 unit failures" in line and "last 2.0h ONLY" in line
    # Retention wider than the window makes the full claim, unqualified.
    wide = FAIL_NOW.replace(day=6)
    (full,) = health.failure_history_lines([], [], FAIL_NOW, wide)
    assert "last 24h" in full and "ONLY" not in full


def test_the_window_is_at_least_the_readers_own_interval():
    """A window shorter than the six-hour autoloop would drop faults through the
    same hole this section closes. Derived from the timer, not asserted at it."""
    fires = re.findall(
        r"OnCalendar=[^\n]*?(\d\d(?:,\d\d)*):",
        (REPO / "scripts/systemd/hyxlab-autoloop.timer").read_text(),
    )
    hours = sorted(int(h) for h in fires[0].split(","))
    gap = max(b - a for a, b in zip(hours, hours[1:] + [hours[0] + 24], strict=True))
    assert gap <= health.FAILURE_WINDOW_H, f"reader fires every {gap}h"


def test_a_still_failed_unit_is_recurrence_not_a_second_alarm():
    """`hyxlab-qa` is already in ATTENTION. The line must not restate the fault
    -- what it adds is how OFTEN, which the unit row cannot carry."""
    failures = health.parse_unit_failures(JOURNAL, HYXLAB_UNITS)
    rows = [health.UnitHealth("hyxlab-qa.service", "FAILED", "result=exit-code")]
    qa_line = next(
        ln for ln in health.failure_history_lines(failures, rows, FAIL_NOW) if "hyxlab-qa" in ln
    )
    assert "still failed" in qa_line and "CLEARED" not in qa_line


def test_the_section_prints_even_when_nothing_failed():
    """An absent section and a broken reader look identical. Zero is a fact."""
    assert "0 unit failures" in health.failure_history_lines([], [], FAIL_NOW)[-1]


def test_an_unparseable_record_is_skipped_not_raised():
    """The 09-06 rule: a new reader must not be able to take the digest down."""
    junk = "\n".join(
        [
            "not json at all",
            json.dumps({"MESSAGE_ID": FAIL_ID, "USER_UNIT": "hyxlab-qa.service"}),  # no timestamp
            json.dumps({"MESSAGE_ID": FAIL_ID}),  # no unit
            jrec("hyxlab-qa.service", 1788861607714922),
        ]
    )
    assert len(health.parse_unit_failures(junk, HYXLAB_UNITS)) == 1
    assert health.parse_unit_failures("", HYXLAB_UNITS) == []


def test_a_result_systemd_words_differently_is_kept_not_dropped():
    """The result is a label, not a whitelist: an unrecognised message must
    still be COUNTED. Losing a failure to a wording change is the bug."""
    odd = json.dumps(
        {
            "MESSAGE_ID": FAIL_ID,
            "USER_UNIT": "hyxlab-qa.service",
            "MESSAGE": "hyxlab-qa.service: something new systemd says.",
            "__REALTIME_TIMESTAMP": "1788861607714922",
        }
    )
    (only,) = health.parse_unit_failures(odd, HYXLAB_UNITS)
    assert only.result == "failed"


def test_the_journal_read_asks_for_the_field_it_filters_on():
    """Caught in production 2026-09-08: `--output-fields` restricts what comes
    BACK, so a list without MESSAGE_ID makes every record arrive missing the key
    the parser branches on -- and the section reports a clean box, silently."""
    src = (REPO / "collector/health.py").read_text()
    fields = re.search(r'"--output-fields=([A-Z_,a-z]+)"', src).group(1).split(",")
    assert "MESSAGE_ID" in fields and "USER_UNIT" in fields
    assert "__REALTIME_TIMESTAMP" in fields


def test_the_message_id_match_is_pushed_down_to_journalctl():
    """It is an INDEXED field. Filtering in Python instead makes the cheapest
    fact in the digest cost a full journal scan."""
    src = (REPO / "collector/health.py").read_text()
    assert 'f"MESSAGE_ID={UNIT_FAILURE_MESSAGE_ID}"' in src


def test_the_digest_still_exits_zero_with_failures_in_the_window():
    """This section adds no gate. A report that starts failing is a report that
    stops being read (and `promote.sh` branches on `--drift-only`, not on this)."""
    assert health.cli([]) == 0


# --------------------------------------------------------------------------
# The finding that reached no reader: qa's own `prior QA run was read`.
#
# `qa._own_findings` strips that check out of the record's `failures` list --
# correctly, or one unread failure re-arms the report of itself, every day,
# forever. `judge_qa` read `failures`. So the digest could not name that check
# on ANY run, and the run it fires on is by construction the run where every
# other check is green: `FAILED hyxlab-qa.service` from the unit line, `clean`
# from the QA line, and no name between them.
#
# MEASURED, not hypothesised: the 2026-09-10 10:00Z run journalled 6 FAILURES
# and recorded 5. The sixth was this one.
# --------------------------------------------------------------------------


def _unread_record(at: str, unread: str, **kw) -> dict:
    rec = _record(at, **kw)
    rec["run"]["unread"] = unread
    return rec


def test_the_digest_names_the_check_qa_keeps_off_its_own_failure_list() -> None:
    """The whole defect, in the shape it lands in: every section green, the run
    exits 1 on `prior QA run was read` alone, and `failures` is empty."""
    now = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
    line = health.judge_qa(
        _unread_record(
            "2026-09-11T10:00:00+00:00",
            "prior run FAILED ['collection continuous over last 24h'] and is"
            " green today — nothing read it",
            skipped=["collect-skips"],
        ),
        svc(Result="exit-code", ExecMainStatus="1"),
        now,
    )
    assert "UNREAD" in line
    assert "prior QA run was read" in line
    assert "collection continuous over last 24h" in line


def test_a_run_that_exited_one_is_never_called_clean() -> None:
    """`clean` sat directly under the unit line reading FAILED. The verdict word
    must not contradict the two lines around it."""
    line = health.judge_qa(
        _unread_record("2026-09-11T10:00:00+00:00", "QA DID NOT RUN — ..."),
        svc(Result="exit-code", ExecMainStatus="1"),
        datetime(2026, 9, 11, 14, 0, tzinfo=UTC),
    )
    assert "clean" not in line
    assert "no check failed" in line


def test_the_unread_line_survives_a_run_that_also_failed_other_checks() -> None:
    """Independent of what the other sections found, so it must not be
    suppressed under a failure list -- that hides it on exactly the busy run
    nobody re-reads."""
    line = health.judge_qa(
        _unread_record(
            "2026-09-11T10:00:00+00:00",
            "prior run SKIPPED ['collect-skips'] and ran today — nothing read it",
            failures=["stream fresh (trades < 5 min old)"],
        ),
        svc(Result="exit-code", ExecMainStatus="1"),
        datetime(2026, 9, 11, 14, 0, tzinfo=UTC),
    )
    assert "FAILURES ['stream fresh (trades < 5 min old)']" in line
    assert "UNREAD" in line and "collect-skips" in line


def test_a_run_with_nothing_unread_reads_exactly_as_before() -> None:
    """The field is additive. An empty `unread` must not perturb the verdict."""
    line = health.judge_qa(
        _unread_record("2026-09-11T10:00:00+00:00", ""),
        svc(),
        datetime(2026, 9, 11, 14, 0, tzinfo=UTC),
    )
    assert "clean" in line
    assert "UNREAD" not in line


def test_a_record_predating_the_field_reads_as_a_pass_not_a_finding() -> None:
    """Every record on disk today lacks the key. The conservative direction is a
    digest that stays silent about one historical run, never one that invents a
    finding for it -- which is how a check gets switched off."""
    parsed = _prior_run(_record("2026-09-10T10:00:00+00:00"))
    assert parsed is not None and parsed.unread == ""
    assert "UNREAD" not in health.judge_qa(
        _record("2026-09-10T10:00:00+00:00"), svc(), datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
    )


def test_a_non_string_unread_does_not_crash_the_digest() -> None:
    """Same posture as the rest of the parser: a hand-edited or older-format
    record reads as absent rather than taking the only reader down with it."""
    assert _prior_run({"run": {"last_run": "2026-09-10T10:00:00+00:00", "unread": 7}}).unread == ""


# --------------------------------------------------------------------------
# A failure the manager FORGOT (2026-10-05): the 09:01Z boot's catch-up sweeps
# failed on DNS, the box rebooted at 09:17Z, and the fresh manager answered
# `Result=success` with no ExecMain timestamps. The persisted timer stamp said
# the slot was taken, so both read OK and the failure section said CLEARED.
# --------------------------------------------------------------------------

BOOT_FAIL = datetime.fromtimestamp(1_788_704_100.4, UTC)


def _forgotten() -> dict[str, str]:
    return svc(ExecMainStartTimestamp="", ExecMainExitTimestamp="")


def test_a_failure_the_manager_forgot_is_not_ok() -> None:
    raw = health.judge("hyxlab-sweep.service", _forgotten(), timer(), NOW)
    assert raw.state == "OK"  # the defect: judge alone cannot see it
    fails = [health.UnitFailure("hyxlab-sweep.service", BOOT_FAIL, "exit-code")]
    row = health.outlived_by_failure(raw, _forgotten(), timer(), fails)
    assert row.state == "FAILED" and "journal" in row.detail
    (line, _) = health.failure_history_lines(fails, [row], FAIL_NOW)
    assert "still failed" in line


def test_a_run_started_after_the_failure_clears_it() -> None:
    fails = [health.UnitFailure("hyxlab-sweep.service", BOOT_FAIL, "exit-code")]
    later = timer(LastTriggerUSec="@1788704300", NextElapseUSecRealtime="@1788790800")
    raw = health.judge("hyxlab-sweep.service", _forgotten(), later, NOW)
    assert health.outlived_by_failure(raw, _forgotten(), later, fails).state == "OK"


def test_another_units_failure_is_not_this_ones() -> None:
    fails = [health.UnitFailure("hyxlab-qa.service", BOOT_FAIL, "exit-code")]
    raw = health.judge("hyxlab-sweep.service", _forgotten(), timer(), NOW)
    assert health.outlived_by_failure(raw, _forgotten(), timer(), fails) is raw


def test_a_restarted_daemon_clears_its_own_failure() -> None:
    up = svc(ActiveState="active", SubState="running", ExecMainStartTimestamp="@1788704200")
    fails = [health.UnitFailure("hyxlab-stream.service", BOOT_FAIL, "signal")]
    raw = health.judge("hyxlab-stream.service", up, None, NOW)
    assert health.outlived_by_failure(raw, up, None, fails).state == "OK"


# 2026-10-07 15:31Z, verbatim fields: collect refused by a sibling project's
# read-only attach. The manager logs the exit and the failure as two records
# of ONE invocation; only the first carries the status.
EXIT_ID = "98e322203f7a4ed290d09fe03c09fe15"


def exit_rec(unit: str, inv: str, status: str, code: str = "exited") -> str:
    return json.dumps(
        {
            "MESSAGE_ID": EXIT_ID,
            "USER_UNIT": unit,
            "USER_INVOCATION_ID": inv,
            "EXIT_CODE": code,
            "EXIT_STATUS": status,
            "MESSAGE": f"{unit}: Main process exited, code={code}, status={status}/TEMPFAIL",
            "__REALTIME_TIMESTAMP": "1791473462000000",
        }
    )


def fail_rec(unit: str, inv: str, result: str = "exit-code") -> str:
    rec = json.loads(jrec(unit, 1791473462000100, result))
    rec["USER_INVOCATION_ID"] = inv
    return json.dumps(rec)


def test_a_designed_skip_is_labelled_apart_from_a_crash():
    """Both read `exit-code`; the exit record of the SAME invocation says 75."""
    journal = "\n".join(
        [
            exit_rec("hyxlab-collect.service", "870eb6f9", "75"),
            fail_rec("hyxlab-collect.service", "870eb6f9"),
            exit_rec("hyxlab-breadth.service", "aaaa", "1"),
            fail_rec("hyxlab-breadth.service", "aaaa"),
        ]
    )
    collect, breadth = sorted(
        health.parse_unit_failures(journal, HYXLAB_UNITS), key=lambda f: f.unit, reverse=True
    )
    assert (collect.unit, collect.status) == ("hyxlab-collect.service", "75")
    assert breadth.status == "1"
    lines = health.failure_history_lines([collect, breadth], [], FAIL_NOW)
    assert any("exit-code/75 " in ln and "hyxlab-collect" in ln for ln in lines)
    assert any("exit-code/1 " in ln and "hyxlab-breadth" in ln for ln in lines)


def test_an_exit_record_is_context_never_a_failure_of_its_own():
    """A unit that exited 0 also logs `Main process exited`; it must not count,
    and an exit with no matching failure must not lend its status elsewhere."""
    journal = "\n".join(
        [
            exit_rec("hyxlab-collect.service", "other", "75"),
            fail_rec("hyxlab-collect.service", "870eb6f9"),
        ]
    )
    (only,) = health.parse_unit_failures(journal, HYXLAB_UNITS)
    assert only.status is None and only.label == "exit-code"


def test_a_signal_death_carries_no_exit_status():
    """`code=killed status=9` beside `oom-kill` is the same fact said twice."""
    journal = "\n".join(
        [
            exit_rec("hyxlab-stream.service", "k", "9", code="killed"),
            fail_rec("hyxlab-stream.service", "k", "oom-kill"),
        ]
    )
    (only,) = health.parse_unit_failures(journal, HYXLAB_UNITS)
    assert only.label == "oom-kill"


def test_the_journal_read_asks_for_the_exit_record_and_its_join_key():
    src = (REPO / "collector/health.py").read_text()
    assert 'f"MESSAGE_ID={UNIT_EXIT_MESSAGE_ID}"' in src
    fields = re.search(r'"--output-fields=([A-Z_,a-z]+)"', src).group(1).split(",")
    assert {"USER_INVOCATION_ID", "EXIT_CODE", "EXIT_STATUS"} <= set(fields)


def test_a_crash_loop_folds_but_keeps_its_count_and_its_interruption():
    """2026-10-07: 189 streamd failures, one `exit-code/1` among 188 OOM kills,
    printed as 189 timestamps on one line. The fold must keep the total, keep
    the odd one out IN ORDER, and leave short runs verbatim."""
    base = datetime(2026, 10, 7, 11, 12, tzinfo=UTC)
    unit = "hyxlab-stream.service"
    fails = [health.UnitFailure(unit, base + timedelta(minutes=i), "oom-kill") for i in range(4)]
    fails.append(health.UnitFailure(unit, base + timedelta(minutes=4), "exit-code", "1"))
    fails += [
        health.UnitFailure(unit, base + timedelta(minutes=5 + i), "oom-kill") for i in range(2)
    ]
    (line, _) = health.failure_history_lines(list(reversed(fails)), [], FAIL_NOW)
    assert "7x in 24h" in line
    assert line.endswith(
        "oom-kill x4 10-07 11:12Z..11:15Z, exit-code/1 10-07 11:16Z, "
        "oom-kill 10-07 11:17Z, oom-kill 10-07 11:18Z"
    )
    # A run crossing midnight names the day it ended on.
    late = [
        health.UnitFailure(
            unit, base.replace(hour=23, minute=58) + timedelta(minutes=i), "oom-kill"
        )
        for i in range(3)
    ]
    (line, _) = health.failure_history_lines(late, [], FAIL_NOW)
    assert "oom-kill x3 10-07 23:58Z..10-08 00:00Z" in line


# ---- KALSHI429: both ledgers of the one bucket (mistakes #145) ----------------


def _jl(**fields: object) -> str:
    return json.dumps(fields)


def test_kalshi_429_is_counted_per_unit_and_only_for_kalshi() -> None:
    out = "\n".join(
        [
            _jl(
                MESSAGE="[kalshi] WARNING: rate-limit retry in 5s for .../markets: HTTP 429",
                _SYSTEMD_USER_UNIT="hyxlab-collect.service",
            ),
            _jl(
                MESSAGE="[kalshi] WARNING: rate-limit retry in 5s for .../candlesticks: HTTP 429",
                _SYSTEMD_USER_UNIT="hyxlab-sweep.service",
            ),
            _jl(
                MESSAGE="[kalshi] WARNING: rate-limit retry in 5s for .../markets: HTTP 429",
                _SYSTEMD_USER_UNIT="hyxlab-sweep.service",
            ),
            # Polymarket's bucket, not Kalshi's: the poly sweep's own 429 wording.
            _jl(
                MESSAGE="[poly] trades_tail 0xabc stopped early at 500 prints (non-list body, status 429)",
                _SYSTEMD_USER_UNIT="hyxlab-poly-sweep.service",
            ),
            # A unit that merely TALKS about 429s in prose.
            _jl(
                MESSAGE="[collect] kalshi_req=65 kalshi_429=0",
                _SYSTEMD_USER_UNIT="hyxlab-collect.service",
            ),
            "-- No entries --",
            _jl(MESSAGE=None, _SYSTEMD_USER_UNIT="hyxlab-stream.service"),
        ]
    )
    assert health.parse_kalshi_429(out) == {"hyxlab-collect.service": 1, "hyxlab-sweep.service": 2}
    assert health.parse_kalshi_429("-- No entries --\n") == {}


def test_sibling_trace_counts_slots_and_429s_since_the_window() -> None:
    lines = [
        _jl(ts=100.0, lane="collector", kind="slot", wait_s=0.5),
        _jl(ts=101.0, lane="collector", kind="429"),
        _jl(ts=99.0, lane="collector", kind="429"),  # before the window
        _jl(ts=102.0, lane="collector", kind="defer"),  # neither a grant nor a refusal
        "not json",
        _jl(ts="102", kind="slot"),  # a string ts is not a time
        "[]",
    ]
    assert health.parse_sibling_trace(lines, since_ts=100.0) == (1, 1)


def test_sibling_trace_is_read_across_rotations_and_absent_is_none(tmp_path: Path) -> None:
    base = tmp_path / "public_api_governor.trace.jsonl"
    assert health.read_sibling_429(0.0, base) is None, "no file must read as unknown, not as zero"
    base.write_text(_jl(ts=10.0, kind="slot") + "\n" + _jl(ts=11.0, kind="429") + "\n")
    base.with_name(base.name + ".1").write_text(
        _jl(ts=5.0, kind="slot") + "\n" + _jl(ts=6.0, kind="429") + "\n"
    )
    base.with_name(base.name + ".2").write_text(_jl(ts=1.0, kind="429") + "\n")
    assert health.read_sibling_429(0.0, base) == (3, 2)
    assert health.read_sibling_429(5.5, base) == (2, 1)
    assert health.sibling_trace_paths(base) == [
        base,
        base.with_name(base.name + ".1"),
        base.with_name(base.name + ".2"),
    ]


def test_kalshi_429_line_carries_both_ledgers_and_names_an_absent_one() -> None:
    counts = {
        "hyxlab-collect.service": 18,
        "hyxlab-sweep.service": 104,
        "hyxlab-breadth.service": 4,
    }
    line = health.kalshi_429_line(counts, (41, 14619), hours=24, trace_dir=Path("/x"))
    assert line == (
        "24h: hyxlab 126 (hyxlab-sweep.service 104, hyxlab-collect.service 18, "
        "hyxlab-breadth.service 4) | sibling family-books 41 x 429 of 14619 requests | host 167"
    )
    # Every field moves the line (#95): a renderer that drops one reads the same.
    base = health.kalshi_429_line(counts, (41, 14619), hours=24, trace_dir=Path("/x"))
    assert (
        health.kalshi_429_line(
            {**counts, "hyxlab-collect.service": 19}, (41, 14619), 24, Path("/x")
        )
        != base
    )
    assert health.kalshi_429_line(counts, (42, 14619), 24, Path("/x")) != base
    assert health.kalshi_429_line(counts, (41, 14620), 24, Path("/x")) != base
    assert health.kalshi_429_line(counts, (41, 14619), 12, Path("/x")) != base
    absent = health.kalshi_429_line(counts, None, hours=24, trace_dir=Path("/x"))
    assert "sibling trace absent (/x)" in absent and "host" not in absent, (
        "no total from one ledger"
    )
    unread = health.kalshi_429_line(None, (41, 14619), hours=24, trace_dir=Path("/x"))
    assert unread.startswith("24h: hyxlab journal unreadable |") and "host" not in unread
    assert (
        health.kalshi_429_line({}, (0, 100), 24, Path("/x"))
        == "24h: hyxlab 0 | sibling family-books 0 x 429 of 100 requests | host 0"
    )


def test_the_digest_prints_the_kalshi429_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(health, "read_kalshi_429", lambda: {"hyxlab-collect.service": 2})
    monkeypatch.setattr(health, "read_sibling_429", lambda since: (3, 40))
    monkeypatch.setattr(health, "discover_units", lambda: [])
    monkeypatch.setattr(health, "read_unit_failures", lambda units: [])
    monkeypatch.setattr(health, "live_cgmem", lambda units: [])
    monkeypatch.setattr(health, "journal_floor", lambda: None)
    monkeypatch.setattr(health, "drift_report", lambda: [])
    monkeypatch.setattr(health, "show", lambda unit: {})
    monkeypatch.setattr(health, "judge_qa", lambda rec, svc, now: "QA (stubbed)")
    health.main()
    out = capsys.readouterr().out
    assert (
        "KALSHI429  (host)  24h: hyxlab 2 (hyxlab-collect.service 2) | sibling family-books 3 x 429 of 40 requests | host 5"
        in out
    )


# ---- KALSHI429 by the hour (status 2026-10-28 20:xxZ) --------------------------


def _jlt(ts_s: float, **fields) -> str:
    return json.dumps({"__REALTIME_TIMESTAMP": str(int(ts_s * 1e6)), **fields})


_H = 1_793_200_000 - (1_793_200_000 % 3600)  # an exact UTC hour boundary, 2026-10-28


def test_collect_cycles_are_binned_by_hour_with_failing_and_429_counts() -> None:
    out = "\n".join(
        [
            _jlt(_H + 10, MESSAGE="[collect] ... timings={'kalshi_req': 65, 'kalshi_429': 0, ...}"),
            _jlt(_H + 310, MESSAGE="[collect] ... timings={'kalshi_req': 67, 'kalshi_429': 2, ...}"),
            _jlt(
                _H + 3610, MESSAGE="[collect] ... timings={'kalshi_req': 66, 'kalshi_429': 1, ...}"
            ),
            _jlt(_H + 3620, MESSAGE="[collect] no counter on this line"),
            "-- No entries --",
        ]
    )
    cycles = health.parse_collect_cycles(out)
    assert cycles == {health._hour_key(_H): (2, 1, 2), health._hour_key(_H + 3600): (1, 1, 1)}


def test_hourly_429s_keep_the_venue_predicate_and_the_unit() -> None:
    out = "\n".join(
        [
            _jlt(
                _H + 1,
                MESSAGE="[kalshi] WARNING: ... HTTP 429",
                _SYSTEMD_USER_UNIT="hyxlab-sweep.service",
            ),
            _jlt(
                _H + 2,
                MESSAGE="[kalshi] WARNING: ... HTTP 429",
                _SYSTEMD_USER_UNIT="hyxlab-collect.service",
            ),
            _jlt(
                _H + 3,
                MESSAGE="[poly] trades_tail stopped early (status 429)",
                _SYSTEMD_USER_UNIT="hyxlab-poly-sweep.service",
            ),
        ]
    )
    assert health.parse_kalshi_429_hourly(out) == {
        health._hour_key(_H): {"hyxlab-sweep.service": 1, "hyxlab-collect.service": 1}
    }


def test_active_hours_name_only_the_sweep_units_not_systemd_edges() -> None:
    out = "\n".join(
        [
            _jlt(_H + 1, _SYSTEMD_USER_UNIT="init.scope"),  # `Started hyxlab-sweep.service`
            _jlt(_H + 2, _SYSTEMD_USER_UNIT="hyxlab-sweep.service"),
            _jlt(_H + 3700, _SYSTEMD_USER_UNIT="hyxlab-poly-sweep.service"),
            _jlt(_H + 3701, _SYSTEMD_USER_UNIT="hyxlab-collect.service"),
        ]
    )
    assert health.parse_active_hours(out) == {
        health._hour_key(_H): {"hyxlab-sweep.service"},
        health._hour_key(_H + 3600): {"hyxlab-poly-sweep.service"},
    }


def test_sibling_trace_hourly_bins_grants_and_429s_since_the_window(tmp_path: Path) -> None:
    lines = [
        json.dumps({"ts": _H - 1, "kind": "slot"}),  # before the window
        json.dumps({"ts": _H + 1, "kind": "slot"}),
        json.dumps({"ts": _H + 2, "kind": "429"}),
        json.dumps({"ts": _H + 3, "kind": "yield"}),
        json.dumps({"ts": _H + 3601, "kind": "slot"}),
        "garbage",
    ]
    assert health.parse_sibling_trace_hourly(lines, since_ts=_H) == {
        health._hour_key(_H): (1, 1),
        health._hour_key(_H + 3600): (0, 1),
    }
    base = tmp_path / "t.jsonl"
    assert health.read_sibling_429_hourly(0.0, base) is None, "no trace must read as unknown"
    base.write_text("\n".join(lines[:3]) + "\n")
    (tmp_path / "t.jsonl.1").write_text(lines[4] + "\n")
    assert health.read_sibling_429_hourly(_H, base) == {
        health._hour_key(_H): (1, 1),
        health._hour_key(_H + 3600): (0, 1),
    }


def test_hourly_lines_carry_every_term_and_name_each_absence() -> None:
    now = datetime.fromtimestamp(_H + 3600 + 100, UTC)
    h0 = health._hour_key(_H)
    cycles = {h0: (12, 3, 5)}
    ours = {h0: {"hyxlab-sweep.service": 4, "hyxlab-collect.service": 5}}
    active = {h0: {"hyxlab-sweep.service"}}
    sibling = {h0: (7, 2190)}
    lines = health.kalshi_429_hourly_lines(cycles, ours, active, sibling, now, hours=2)
    assert len(lines) == 3 and lines[0].startswith("KALSHI429 by the hour, last 2h")
    assert "collect 12 cyc  3 fail ( 75% clean)   5 x 429" in lines[1]
    assert "sweeps   4 other   5" in lines[1]
    assert "sibling  2190 req   7 x 429" in lines[1]
    assert lines[1].endswith("sweeps sweep")
    # the hour with no rows: each ledger's absence named, never printed as zero
    assert (
        "collect --" in lines[2] and "sibling --" in lines[2] and lines[2].endswith("sweeps idle")
    )
    # perturbation: every term moves the line (#95 -- a scan of the source cannot prove this)
    base = lines[1]
    for c, o, a, s in [
        ({h0: (12, 4, 5)}, ours, active, sibling),
        ({h0: (12, 3, 6)}, ours, active, sibling),
        ({h0: (11, 3, 5)}, ours, active, sibling),
        (cycles, {h0: {"hyxlab-sweep.service": 5, "hyxlab-collect.service": 5}}, active, sibling),
        (cycles, {h0: {"hyxlab-sweep.service": 4, "hyxlab-collect.service": 6}}, active, sibling),
        (cycles, ours, {h0: {"hyxlab-poly-sweep.service"}}, sibling),
        (cycles, ours, active, {h0: (8, 2190)}),
        (cycles, ours, active, {h0: (7, 2191)}),
    ]:
        assert health.kalshi_429_hourly_lines(c, o, a, s, now, hours=2)[1] != base
    # unreadable ledgers are named on every line, not rendered as a clean hour
    blind = health.kalshi_429_hourly_lines(None, None, None, None, now, hours=2)
    for ln in blind[1:]:
        assert "collect journal unreadable" in ln and "journal unreadable" in ln
        assert "sibling trace absent" in ln and ln.endswith("sweeps unknown")


def test_cli_hourly_flag_parses_hours_and_rejects_nonsense(monkeypatch, capsys) -> None:
    seen: list[int] = []
    monkeypatch.setattr(health, "kalshi_429_hourly_main", lambda hours: seen.append(hours) or 0)
    assert health.cli(["--kalshi429-hourly"]) == 0
    assert health.cli(["--kalshi429-hourly", "6"]) == 0
    assert seen == [health.KALSHI_429_HOURLY_H, 6]
    assert health.cli(["--kalshi429-hourly", "zero"]) == health.USAGE_ERROR
    assert health.cli(["--kalshi429-hourly", "0"]) == health.USAGE_ERROR
    assert health.cli(["--kalshi429-hourly", "6", "7"]) == health.USAGE_ERROR
    assert "usage:" in capsys.readouterr().err
