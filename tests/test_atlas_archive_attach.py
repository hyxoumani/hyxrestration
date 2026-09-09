"""The atlas report's attach to the shared archive (EXP-1379).

MEASURED DEFECT this exists to fix, hit live on 2026-09-09T08:20Z while
running the standing reports: `python -m simulator.atlas` died on a raw
`_duckdb.IOException` traceback after exactly 30 seconds, because
`hyxlab-poly-sweep` was four hours into a ~7 hour run and held the
archive. The report hand-rolled a 15 x 2.0s flat wait loop -- byte for
byte `connect_retry`'s DEFAULT budget, which that helper's own docstring
already says is calibrated for a brief reader and is NOT adequate against
a long-lived writer. So the site took the bare-attach escape hatch, whose
price is a budget or a diagnostic the helper cannot serve, and spent it
on neither.

The archive is not held CONTINUOUSLY -- in the same hour the breadth
collector, a writer and so a stricter test, waited 39s for the same file
and got in. It is held in bursts longer than 30s, and 30s was never a
margin over them: 24 reader attaches sampled once the sweep had RELEASED
gave p50 0.0s, p90 7.6s, max 22.6s, so even a quiet archive spends three
quarters of the old budget on its worst attach. Two things follow, and
each is a test below: the budget must outlast a burst, and exhausting it
must produce an ANSWER rather than a stack trace naming a bare PID.
"""

from __future__ import annotations

import time

import duckdb
import pytest

from hyxlab.store import connect_retry, lock_holder
from simulator import atlas

_HELD = "Conflicting lock is held in /usr/bin/python3.14 (PID {pid})"


def test_lock_holder_names_a_live_writer():
    """PID 1 always exists on Linux, so this is the live-holder branch."""
    assert lock_holder(duckdb.Error(_HELD.format(pid=1))) == "/usr/bin/python3.14 pid 1"


def test_lock_holder_rejects_a_dead_pid():
    """A lock naming a PID that is gone is a lock left BEHIND -- the
    unreachable case wearing the routine case's message. Answering "a live
    writer holds it" there tells an operator to wait for a process that
    will never release."""
    dead = 4_000_000  # above /proc/sys/kernel/pid_max on any stock kernel
    assert lock_holder(duckdb.Error(_HELD.format(pid=dead))) is None


def test_lock_holder_is_none_for_an_unrelated_error():
    assert lock_holder(duckdb.Error("Catalog Error: Table with name x does not exist")) is None


def test_attach_budget_outlasts_a_burst_the_old_loop_lost(monkeypatch, tmp_path):
    """The regression, in isolation. Simulate a writer that releases after
    35 seconds of holding -- longer than the 30s the old loop allowed, and
    shorter than the 39s wait the breadth collector actually served. The
    new budget must get in; the old one provably would not have."""
    slept = {"s": 0.0}

    def fake_connect(path, read_only=False):
        if slept["s"] < 35.0:
            raise duckdb.IOException(_HELD.format(pid=1))
        return object()

    monkeypatch.setattr(duckdb, "connect", fake_connect)
    # `connect_retry` imports `time` inside its body, so this is its sleep.
    monkeypatch.setattr(time, "sleep", lambda s: slept.__setitem__("s", slept["s"] + s))

    # Absolute even though `duckdb.connect` is faked: on SUCCESS
    # `connect_retry` runs `private_spill`, which builds `<db>.tmp/pid-<pid>`
    # next to the database. A relative name puts that in the repo root.
    db = str(tmp_path / "x.duckdb")
    assert connect_retry(db, read_only=True, **atlas.ARCHIVE_ATTACH) is not None

    # ...and the budget the site used to hand-roll: 15 attempts x 2.0s flat.
    slept["s"] = 0.0
    with pytest.raises(duckdb.Error):
        connect_retry(db, read_only=True, retries=15, delay=2.0, backoff=1.0)


def test_budget_backs_off_rather_than_beating_against_a_flush_period():
    """`connect_retry`'s docstring asks a caller attaching a writer-held
    database for `backoff > 1.0` and a `max_delay`. A flat period can
    sample a fixed flush period at exactly the wrong phase every time."""
    assert atlas.ARCHIVE_ATTACH["backoff"] > 1.0
    assert atlas.ARCHIVE_ATTACH["max_delay"] is not None


def test_exhausted_budget_names_the_live_holder_and_does_not_traceback(monkeypatch, tmp_path):
    """A held archive is NOT an incident -- the poly sweep holds it for
    hours by design. The report must say who has it and that waiting is
    the remedy, instead of handing over `IOException ... (PID 1106694)`
    and a duckdb.org URL, which is a path where a decision was owed."""
    def refuse(*a, **kw):
        raise duckdb.IOException(_HELD.format(pid=1))

    monkeypatch.setattr(atlas, "connect_retry", refuse)
    monkeypatch.setattr("sys.argv", ["atlas", "--db", str(tmp_path / "a.duckdb")])
    with pytest.raises(SystemExit) as e:
        atlas.main()
    msg = str(e.value)
    assert "live writer holds" in msg
    assert "pid 1" in msg
    assert "Nothing is wrong" in msg


def test_no_holder_is_the_opposite_verdict_and_says_so(monkeypatch, tmp_path):
    """Same exception class, opposite fact: nothing holds the lock, so
    waiting cannot help and the archive is genuinely unreachable. One
    verdict covering both is how a broken archive reads as a busy one."""
    def refuse(*a, **kw):
        raise duckdb.IOException("IO Error: Could not open database: permission denied")

    monkeypatch.setattr(atlas, "connect_retry", refuse)
    monkeypatch.setattr("sys.argv", ["atlas", "--db", str(tmp_path / "a.duckdb")])
    with pytest.raises(SystemExit) as e:
        atlas.main()
    msg = str(e.value)
    assert "unreachable" in msg
    assert "waiting will not help" in msg
    assert "live writer holds" not in msg
