"""The RECENT line carries the failed run's own last line (2026-11-15).

`exit-code/1` is the manager's verdict; the cause is whatever the process
printed last. On 11-15 17:52Z `hyxlab-breadth` died on one Kalshi 500 and the
digest said `exit-code/1` -- the reader dug the journal for the rest. The
fixture below is that invocation's shape, measured: the main process's lines,
the manager's exit record between them and the `ExecStopPost` output, which is
logged under the SAME invocation and is therefore the invocation's last record.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

from collector import health

EXIT_US = 1794765120422239
CAUSE = "requests.exceptions.HTTPError: 500 Server Error: Internal Server Error for url: x"
NOW = datetime(2026, 11, 15, 20, 15, tzinfo=UTC)


def rec(us: int, pid: str, message) -> str:
    return json.dumps({"__REALTIME_TIMESTAMP": str(us), "_PID": pid, "MESSAGE": message})


INVOCATION = "\n".join(
    [
        rec(EXIT_US - 50000, "3730657", "[breadth] cycle failed: HTTPError: 500"),
        rec(EXIT_US - 17000, "3730657", "Traceback (most recent call last):"),
        rec(EXIT_US - 17000, "3730657", CAUSE),
        rec(EXIT_US + 24000, "3730684", "[cgmem] dir=/sys/fs/cgroup/x peak=35692544"),
    ]
)


def failure(unit="hyxlab-breadth.service", result="exit-code", inv="4fb8", exit_us=EXIT_US, at=NOW):
    return health.UnitFailure(unit, at, result, "1", inv, exit_us)


def test_the_cause_is_the_main_process_last_line_not_the_stop_posts():
    assert health.parse_last_line(INVOCATION, EXIT_US) == CAUSE


def test_a_line_journald_stamped_after_the_exit_record_still_counts():
    """A pipe's lines are stamped when journald reads them; the main PID is
    fixed by what precedes the exit record, its last line by the PID."""
    late = INVOCATION + "\n" + rec(EXIT_US + 5, "3730657", "the real last line")
    assert health.parse_last_line(late, EXIT_US) == "the real last line"


def test_a_stop_post_that_is_also_python_is_told_apart_by_pid():
    """QA's second ExecStopPost is `python -m collector.qa --clear-snapshot`."""
    qa = "\n".join(
        [
            rec(EXIT_US - 10, "100", "[qa] 3 FAILURES: ['archive disk headroom']"),
            rec(EXIT_US + 10, "101", "[cgmem] dir=x"),
            rec(EXIT_US + 20, "102", "[qa] stop-post: data/qa-snapshot already empty"),
        ]
    )
    assert health.parse_last_line(qa, EXIT_US) == "[qa] 3 FAILURES: ['archive disk headroom']"


def test_nothing_before_the_exit_record_is_no_cause_never_the_stop_posts():
    only_post = rec(EXIT_US + 24000, "3730684", "[cgmem] dir=x")
    assert health.parse_last_line(only_post, EXIT_US) is None
    assert health.parse_last_line("", EXIT_US) is None
    assert health.parse_last_line("-- No entries --", EXIT_US) is None


def test_a_long_line_is_cut_and_a_binary_message_is_skipped():
    long = rec(EXIT_US - 2, "7", "x" * 500) + "\n" + rec(EXIT_US - 1, "7", [104, 105])
    text = health.parse_last_line(long, EXIT_US)
    assert text is not None and len(text) == health.CAUSE_MAX_CHARS and text.endswith("…")


def test_the_parser_hands_the_failure_its_invocation_and_exit_instant():
    journal = "\n".join(
        [
            json.dumps(
                {
                    "MESSAGE_ID": health.UNIT_EXIT_MESSAGE_ID,
                    "USER_UNIT": "hyxlab-breadth.service",
                    "USER_INVOCATION_ID": "4fb8",
                    "EXIT_CODE": "exited",
                    "EXIT_STATUS": "1",
                    "__REALTIME_TIMESTAMP": str(EXIT_US),
                }
            ),
            json.dumps(
                {
                    "MESSAGE_ID": health.UNIT_FAILURE_MESSAGE_ID,
                    "USER_UNIT": "hyxlab-breadth.service",
                    "USER_INVOCATION_ID": "4fb8",
                    "MESSAGE": "hyxlab-breadth.service: Failed with result 'exit-code'.",
                    "__REALTIME_TIMESTAMP": str(EXIT_US + 24631),
                }
            ),
        ]
    )
    (only,) = health.parse_unit_failures(journal, {"hyxlab-breadth.service"})
    assert (only.invocation, only.exit_us, only.status) == ("4fb8", EXIT_US, "1")


def test_one_read_per_unit_for_its_newest_exit_and_none_for_a_kill(monkeypatch):
    asked = []

    def fake_run(cmd, **kw):
        asked.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=INVOCATION, stderr="")

    monkeypatch.setattr(health.subprocess, "run", fake_run)
    old = failure(inv="old", at=datetime(2026, 11, 15, 9, 0, tzinfo=UTC))
    new = failure(inv="new")
    killed = failure(unit="hyxlab-qa.service", result="oom-kill", inv="k")
    unkeyed = failure(unit="hyxlab-collect.service", inv=None)
    causes = health.read_failure_causes([new, old, killed, unkeyed])
    assert causes == {"hyxlab-breadth.service": (NOW, CAUSE)}
    (cmd,) = asked
    assert "_SYSTEMD_INVOCATION_ID=new" in cmd
    # `--output-fields` restricts what comes BACK: each field the parser keys on
    # must be asked for, or every record arrives without it and the cause is
    # silently absent (the 09-08 "0 failures" defect, one reader over).
    fields = next(a for a in cmd if a.startswith("--output-fields="))
    for needed in ("MESSAGE", "_PID", "__REALTIME_TIMESTAMP"):
        assert needed in fields.split("=", 1)[1].split(",")


def test_a_journal_that_cannot_be_read_leaves_the_line_as_it_was(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(health.subprocess, "run", boom)
    assert health.read_failure_causes([failure()]) == {}
    plain = health.failure_history_lines([failure()], [], NOW)
    assert plain == health.failure_history_lines([failure()], [], NOW, causes={})
    assert "last line" not in plain[0]


def test_the_recent_line_names_the_run_and_what_it_said():
    causes = {"hyxlab-breadth.service": (datetime(2026, 11, 15, 17, 52, tzinfo=UTC), CAUSE)}
    line, _ = health.failure_history_lines([failure()], [], NOW, causes=causes)
    assert line.endswith(f"| the 17:52Z run's last line: {CAUSE}")
    assert "exit-code/1 11-15 20:15Z" in line


def test_the_digest_reads_the_causes_it_prints():
    """The renderer takes `causes` as an optional argument, so a `main()` that
    stopped passing it would print the old line with every test above green."""
    import inspect

    src = inspect.getsource(health.main)
    assert "causes=read_failure_causes(failures)" in src
