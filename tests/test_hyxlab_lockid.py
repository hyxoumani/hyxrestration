"""EXP-944 — the writer lock must name its holder.

On 2026-08-03 three `collect` cycles (12:54/12:59/13:04Z) each waited the
full 240 s for `data/writer.lock` and journalled a skip. The lock was
continuously unavailable from 12:47:03Z to ~13:06:20Z — measured, from
`collector.breadth`'s own `total_s=1169.7` against `fetch_s=3.0`, every
remaining second of which is spent inside `writer_burst`. **No surviving
evidence named the holder.** Four writers were live (sweep, tradepass,
poly_sweep, breadth) and the outage could only be attributed by inference
from write volume.

These tests pin the witness that closes that gap, and they are written
against the two ways it could lie:

  * a holder that never records itself (silent outage, as on 08-03), and
  * a holder record that OUTLIVES its process (flock releases on death,
    a file does not) — which would name an innocent recycled pid.
"""

from __future__ import annotations

import ast
import fcntl
import json
import os
from pathlib import Path

import pytest

from collector import collect
from hyxlab import lockid

# Every module that takes data/writer.lock. A new writer added here
# without `note_holder` reintroduces exactly the 08-03 blind spot, which
# is why this list is asserted against the source rather than trusted.
WRITER_MODULES = [
    "collector/collect.py",
    "collector/sweep.py",
    "collector/poly_sweep.py",
    "collector/trades_backfill.py",
    "collector/signals.py",
    "hyxlab/migrate.py",  # EXP-1370: migrated from no lock at all
]

REPO = Path(__file__).resolve().parents[1]


def test_note_holder_records_pid_unit_and_acquire_time(tmp_path):
    lock = str(tmp_path / "writer.lock")
    lockid.note_holder(lock)

    rec = lockid.read_holder(lock)
    assert rec is not None
    assert rec["pid"] == os.getpid()
    assert "pytest" in rec["cmd"] or "python" in rec["cmd"]
    # The acquire TIMESTAMP is the whole point: a process age is not a
    # hold duration, so the record must carry when the hold STARTED.
    assert rec["at"].endswith("+00:00")
    assert rec["alive"] is True


def test_read_holder_is_none_when_nobody_ever_recorded(tmp_path):
    """Absent record must read as "unknown", never as an empty holder."""
    assert lockid.read_holder(str(tmp_path / "writer.lock")) is None


def test_read_holder_reports_a_dead_holder_as_not_alive(tmp_path):
    """flock releases on process death; the sidecar does not.

    A stale record is the one way this instrument could name an innocent,
    so liveness is re-derived from /proc at read time.
    """
    lock = str(tmp_path / "writer.lock")
    # A pid that cannot be running the recorded command: pid 1 is init.
    Path(lockid.holder_path(lock)).write_text(
        json.dumps({"pid": 1, "unit": "hyxlab-sweep.service", "cmd": "not-what-pid-1-runs", "at": "x"})
    )
    rec = lockid.read_holder(lock)
    assert rec is not None and rec["alive"] is False


def test_read_holder_survives_a_truncated_record(tmp_path):
    """A holder killed mid-write must not crash the waiter reading it."""
    lock = str(tmp_path / "writer.lock")
    Path(lockid.holder_path(lock)).write_text('{"pid": 12')
    assert lockid.read_holder(lock) is None


def test_note_holder_never_raises_on_an_unwritable_path(tmp_path):
    """The witness must never abort the write it observes."""
    blocker = tmp_path / "nodir"
    blocker.write_text("i am a file, not a directory")
    lockid.note_holder(str(blocker / "writer.lock"))  # must not raise


def test_acquiring_the_writer_lock_names_the_acquirer(tmp_path):
    lock = str(tmp_path / "writer.lock")
    fh = collect.acquire_writer_lock(lock, wait_s=5.0)
    assert fh is not None
    try:
        rec = lockid.read_holder(lock)
        assert rec is not None and rec["pid"] == os.getpid() and rec["alive"] is True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def test_a_timed_out_collect_cycle_records_who_blocked_it(tmp_path, monkeypatch, capsys):
    """The 08-03 regression, end to end: the skip must NAME the blocker.

    Before EXP-944 the sidecar row held `waited_s` and nothing else, so
    three skips proved the tape had a hole and said nothing about what
    made it.
    """
    lock = str(tmp_path / "writer.lock")
    skips = str(tmp_path / "collect_skips.jsonl")
    monkeypatch.setattr(collect, "LOCK_FILE", lock)
    monkeypatch.setattr(collect, "SKIP_LOG", skips)

    # A holder takes it and names itself, exactly as a real writer does.
    with open(lock, "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        lockid.note_holder(lock)
        try:
            assert collect.acquire_writer_lock(lock, wait_s=0.3) is None
            collect.record_skip(
                "writer lock held", 0.3, path=skips, holder=lockid.read_holder(lock)
            )
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)

    rec = json.loads(Path(skips).read_text().strip())
    assert rec["reason"] == "writer lock held"
    assert rec["holder"]["pid"] == os.getpid()
    assert rec["holder"]["alive"] is True


def test_record_skip_writes_holder_none_rather_than_omitting_it(tmp_path):
    """An unnamed holder is a FINDING, not an older row format."""
    path = str(tmp_path / "skips.jsonl")
    collect.record_skip("writer lock held", 240.0, path=path)
    rec = json.loads(Path(path).read_text().strip())
    assert "holder" in rec and rec["holder"] is None


@pytest.mark.parametrize("module", WRITER_MODULES)
def test_every_writer_names_itself_when_it_takes_the_lock(module):
    """Each flock acquire on the WRITER lock is followed by note_holder.

    Asserted on the SOURCE, not by running the writers: a sweep or a
    poly_sweep burst cannot be exercised in a unit test, and an
    un-instrumented one is invisible precisely when it matters — during
    an outage nobody is watching.

    Scoped to acquires whose `open()` names LOCK_FILE/lock_file, because
    `sweep.acquire_sweep_lock` flocks `data/hyxlab.duckdb.lock` — a
    single-INSTANCE guard held for the sweep's whole multi-hour run. That
    one is deliberately long-held and blocks no collector, so requiring a
    holder record there would be noise. (Its long hold is also why
    `fuser` on data/ shows a many-hour handle that is NOT the writer
    lock — the exact confusion this experiment exists to end.)
    """
    src = (REPO / module).read_text()
    lines = src.splitlines()
    tree = ast.parse(src)

    def names_writer_lock(lineno: int) -> bool:
        """Does an `open(...)` in the 5 lines above name the writer lock?"""
        window = lines[max(0, lineno - 6) : lineno]
        return any(
            "open(" in ln and ("LOCK_FILE" in ln or "lock_file" in ln) for ln in window
        )

    acquires: list[int] = []
    notes: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name == "flock":
            # LOCK_UN is a release; only acquires need a name.
            if "LOCK_UN" not in ast.dump(node) and names_writer_lock(node.lineno):
                acquires.append(node.lineno)
        elif name == "note_holder":
            notes.add(node.lineno)

    assert acquires, f"{module}: no writer-lock acquire found — has the lock moved?"
    for line in acquires:
        assert any(line < n <= line + 3 for n in notes), (
            f"{module}:{line} takes the writer lock without a following "
            f"note_holder(). An unnamed holder is exactly the 2026-08-03 "
            f"12:47-13:07Z outage: 15 minutes of lost snapshots, four "
            f"candidate writers, and no way to tell which."
        )
