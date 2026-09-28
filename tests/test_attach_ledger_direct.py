"""The direct attach records a row, and it lets go when the tuning fails.

TWO FINDINGS, ONE CAUSE -- the three lines between `duckdb.connect` and
the caller.

(1) THE ROW. `duck_connect` and a bare `Store(...)` take the same file
lock as `connect_retry`/`open_retry` and recorded nothing, so a hold on
one could not be charged to anything (`charge_hold` credits
`_LAST_ATTACH`, and charging a hold to an unrelated attach is a WRONG
reading, not a missing one). `tests/test_hold_discipline.py` therefore
enumerated four sites as debt instead of covering them -- among them a
24/7 daemon reading `data/hyxlab.duckdb`, the file the 5-minute
collector writes, every hour. A direct attach is a ladder of one, so it
records `attempts 1` and no budget.

(2) THE LEAK. The open takes the lock; the tuning runs after it. An
exception from the tuning escaped with the connection open and no
reference left to close it, so the file stayed locked until the process
exited. `simulator.shadow.stream_conn` was fixed for this shape on
2026-09-27 and the same bug was still live one level down, in
`connect_retry`'s retry loop: the `except duckdb.Error` that catches a
tuning failure SLEEPS and connects again, leaking one attach per attempt
-- fifteen on the default ladder, all of them on a file some daemon owns.

The release is proved from OUTSIDE the attach helper, by taking the file
read-write afterwards: a test that only checked `close()` was called
passes against code that closed a different connection.
"""

from __future__ import annotations

import subprocess
import sys
import time

import duckdb
import pytest

from hyxlab import store
from hyxlab.store import (
    Store,
    attach_wait_block,
    attach_waits,
    duck_connect,
    held_duck,
    held_store,
    open_retry,
    reset_attach_waits,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_attach_waits()
    yield
    reset_attach_waits()


def _db(tmp_path, name="a.duckdb"):
    p = tmp_path / name
    duck_connect(str(p)).close()
    reset_attach_waits()
    return p


# --------------------------------------------------------------- the row


def test_duck_connect_records_a_one_attempt_attach(tmp_path):
    p = _db(tmp_path)
    duck_connect(str(p), read_only=True).close()
    (w,) = attach_waits()
    assert w.db == "a.duckdb"
    assert w.attempts == 1
    assert w.ok is True
    assert w.slept_s == 0.0
    # No ladder, so no budget -- and `budget_frac` must stay None rather
    # than 0.0, which reads as "a budget existed and was never entered".
    assert w.budget_s == 0.0
    assert w.budget_frac is None
    # The hold is unmeasured until somebody measures it; never 0.0.
    assert w.held_s is None


def test_a_bare_store_records_an_attach(tmp_path):
    p = _db(tmp_path)
    Store(str(p), read_only=True).close()
    (w,) = attach_waits()
    assert (w.db, w.attempts, w.ok) == ("a.duckdb", 1, True)


def test_open_retry_records_one_row_and_not_two(tmp_path):
    """`open_retry` builds a `Store`, so both could record. The ladder's
    row is the true one -- attempts, sleep and budget are properties of
    the ladder -- and Store's would read as a second opening of one file,
    inflating `n` and every per-attach average computed from it."""
    p = _db(tmp_path)
    open_retry(str(p), read_only=True).close()
    (w,) = attach_waits()
    assert w.budget_s > 0, "the ladder's row, not Store's"


def test_held_store_charges_its_hold_to_its_own_row(tmp_path):
    p = _db(tmp_path)
    with held_store(str(p), read_only=True):
        pass
    (w,) = attach_waits()
    assert w.held_s is not None
    assert attach_wait_block()["held_n"] == 1


def test_held_duck_charges_its_hold_to_its_own_row(tmp_path):
    p = _db(tmp_path)
    with held_duck(str(p), read_only=True):
        pass
    (w,) = attach_waits()
    assert w.held_s is not None
    assert attach_wait_block()["held_unknown_n"] == 0


# -------------------------------------------------------------- the leak


def _boom(*_a, **_k):
    raise duckdb.Error("tuning failed")


#: The probe is a SUBPROCESS on purpose (mistakes #90). DuckDB serves a
#: second same-process attach from the database instance it already has
#: open, so an in-process re-attach succeeds against a leaked connection
#: and the test passes equally against code that never released anything
#: -- measured: every leak arm below stayed green under the reverted fix
#: until the probe moved out of process.
WRITER = "import duckdb,sys; duckdb.connect(sys.argv[1]).close()"


def _writable_from_outside(db) -> bool:
    """True when another PROCESS can take `db` read-write right now."""
    proc = subprocess.run(
        [sys.executable, "-c", WRITER, str(db)], capture_output=True, timeout=120
    )
    return proc.returncode == 0


def test_a_held_attach_is_refused_from_outside(tmp_path):
    """Vacuity guard, and it is the whole reason the arms below mean
    anything: without it, every release test passes against a helper that
    takes no lock at all."""
    p = _db(tmp_path)
    conn = duck_connect(str(p))
    try:
        assert _writable_from_outside(p) is False
    finally:
        conn.close()
    assert _writable_from_outside(p) is True


def test_the_retry_ladder_holds_nothing_while_it_sleeps(tmp_path, monkeypatch):
    """THE ARM WITH A DURATION, and the reason this is a bug rather than
    an untidiness.

    `connect_retry`'s `except duckdb.Error` cannot tell a refused open
    from a tuning failure, and on the second it has ALREADY opened the
    file -- so `conn` stays bound to a live connection for the rest of the
    loop: across the sleep, across every later attempt, up to the full
    30s of the default ladder. Probed from outside at the moment the
    ladder sleeps, which is where the seconds are.
    """
    p = _db(tmp_path)
    monkeypatch.setattr(store, "spill_cap", _boom)
    seen: list[bool] = []
    monkeypatch.setattr(time, "sleep", lambda _s: seen.append(_writable_from_outside(p)))
    with pytest.raises(duckdb.Error):
        store.connect_retry(str(p), retries=3, delay=0.01)
    monkeypatch.undo()
    assert seen == [True, True], (
        "the retry ladder slept holding an attach it had already opened:"
        f" {seen}"
    )


def test_a_tuning_failure_releases_the_file_rather_than_holding_it(tmp_path, monkeypatch):
    """The same leak with no ladder around it, and the hold lasts as long
    as the TRACEBACK does.

    A local of a frame an exception passed through stays alive while
    anything holds that exception -- which is every `except ... as e` that
    logs, retries, or re-raises later. `excinfo` is that holder here; on
    the daemon path it is the caller. Without it CPython's refcount closes
    the connection the moment the temporary traceback is dropped, and this
    test passes against the leak.
    """
    p = _db(tmp_path)
    monkeypatch.setattr(store, "spill_cap", _boom)
    with pytest.raises(duckdb.Error) as excinfo:
        duck_connect(str(p))
    monkeypatch.undo()
    assert excinfo.value is not None  # the traceback is still held here
    assert _writable_from_outside(p) is True


def test_a_half_built_store_releases_the_file(tmp_path, monkeypatch):
    """`Store.__init__` runs the schema after the open, and a Store that
    raises is never returned -- so nobody holds the reference that would
    close it."""
    p = _db(tmp_path)
    monkeypatch.setattr(store, "_SCHEMA", "SELECT nonexistent_function()")
    with pytest.raises(duckdb.Error) as excinfo:
        Store(str(p))
    monkeypatch.undo()
    assert excinfo.value is not None
    assert _writable_from_outside(p) is True


# ------------------------------------------------------------- the scope


def test_a_db_scoped_block_is_only_that_file(tmp_path):
    """What ledgering the direct attach broke, and how it is paid for.

    A process-wide block used to BE one file's block for any daemon whose
    only ledgered attaches were its ladder. `simulator.shadow` publishes
    `shadow_stream_holds` on exactly that assumption.
    """
    a, b = _db(tmp_path, "a.duckdb"), _db(tmp_path, "b.duckdb")
    with held_duck(str(a), read_only=True):
        pass
    duck_connect(str(b), read_only=True).close()
    duck_connect(str(b), read_only=True).close()
    assert attach_wait_block()["n"] == 3
    assert attach_wait_block(db="a.duckdb")["n"] == 1
    assert attach_wait_block(db="a.duckdb")["held_n"] == 1
    # b's two attaches are unmeasured holds -- and they must not be
    # counted against a, whose one hold IS measured.
    assert attach_wait_block(db="b.duckdb")["held_unknown_n"] == 2
    assert attach_wait_block(db="a.duckdb")["held_unknown_n"] == 0
    assert attach_wait_block(db="never-attached.duckdb") is None


def test_a_scoped_block_reports_the_population_and_not_the_retained_rows(tmp_path):
    """The #72 trap, one field over: the sample rows are capped at 256 and
    a per-db total computed by filtering THEM would describe the tail of
    the run under the run's name."""
    reset_attach_waits()
    n = store._ATTACH_WAITS_MAX + 5
    for _ in range(n):
        store._record_attach("a.duckdb", 1, 0.01, 0.0, True)
    block = attach_wait_block(db="a.duckdb", rows=False)
    assert block["n"] == n
    assert block["retained_n"] == store._ATTACH_WAITS_MAX
    assert block["dropped_n"] == 5
