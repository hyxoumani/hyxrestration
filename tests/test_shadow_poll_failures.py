"""The shadow says WHY a poll of the stream archive failed (mistakes #211).

`poll_once` swallowed every `duckdb.Error` as "daemon mid-flush"; after each
stream restart the shadow lost whole 28 s ladders and no line recorded who
refused it -- or whether it was a refusal at all."""

from __future__ import annotations

import inspect

import duckdb

from simulator import shadow

LOCKED = duckdb.IOException(
    'IO Error: Could not set lock on file "/x/data/hyxstream.duckdb": Conflicting lock is'
    " held in /usr/bin/python3.14 (PID 792) by user devs. See also\nhttps://duckdb.org"
)


def _fails(holder="hyxlab-stream.service pid 792"):
    clock = iter(range(0, 100_000, 30))
    return shadow.PollFailures(clock=lambda: next(clock), holder=lambda exc: holder)


def test_the_first_failure_of_an_episode_names_the_holder_and_recovery_closes_it():
    pf = _fails()
    assert pf.ok() is None  # nothing failed: nothing to say
    line = pf.failed(LOCKED)
    assert line.startswith(
        "[shadow] poll FAILED x1 (refused by hyxlab-stream.service pid 792; IOException: IO Error:"
    )
    assert "\n" not in line  # one journal line
    assert pf.failed(LOCKED) is None and pf.failed(LOCKED) is None  # said once
    assert pf.ok() == "[shadow] poll recovered after 3 failed poll(s) over 30s"
    assert pf.ok() is None
    # A new episode speaks again, from one.
    assert " x1 " in pf.failed(LOCKED)


def test_an_error_that_is_not_a_refusal_says_so_and_a_permanent_one_repeats():
    pf = _fails()
    line = pf.failed(duckdb.BinderException('Binder Error: column "sid" not found'))
    assert line == (
        "[shadow] poll FAILED x1 (NOT a lock refusal; BinderException: Binder Error:"
        ' column "sid" not found)'
    )
    said = [pf.failed(LOCKED) for _ in range(2 * shadow.POLL_FAIL_REPEAT)]
    spoke = [i + 2 for i, s in enumerate(said) if s is not None]
    assert spoke == [shadow.POLL_FAIL_REPEAT, 2 * shadow.POLL_FAIL_REPEAT]
    assert f" x{shadow.POLL_FAIL_REPEAT} " in said[shadow.POLL_FAIL_REPEAT - 2]
    # A holder that exited before /proc could name it is not "unknown error".
    gone = _fails(holder=None).failed(LOCKED)
    assert "refused by a holder no longer alive" in gone


def test_poll_once_prints_the_reason_instead_of_swallowing_it(tmp_path, capsys, monkeypatch):
    from simulator.shadow import ShadowLedger, ShadowRunner

    runner = ShadowRunner(
        [],
        latency=0.0,
        stream_db=str(tmp_path / "stream.duckdb"),
        archive_db=str(tmp_path / "archive.duckdb"),
        ledger=ShadowLedger(tmp_path / "shadow.duckdb"),
    )
    runner.poll_failures = _fails()
    calls = {"n": 0}

    def read_new():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise LOCKED
        return [], []

    monkeypatch.setattr(runner, "_read_new", read_new)
    capsys.readouterr()
    assert runner.poll_once() == 0 and runner.poll_once() == 0
    out = capsys.readouterr().out
    assert out.count("[shadow] poll FAILED x1 (refused by hyxlab-stream.service pid 792") == 1
    assert out.count("poll FAILED") == 1 and "recovered" not in out
    runner.poll_once()
    assert "[shadow] poll recovered after 2 failed poll(s) over 30s" in capsys.readouterr().out
    # The swallow is gone from the source: one handler, and it binds the error.
    src = inspect.getsource(ShadowRunner.poll_once)
    assert "except duckdb.Error as exc:" in src and "except duckdb.Error:" not in src
