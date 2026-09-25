"""A DuckDB lock holder has to be named while it is still alive.

MEASURED INCIDENT, 2026-09-25. `hyxlab-divergence` took a read-only
attach on the live `hyxstream.duckdb` at 01:20:09Z and released it at
02:05:54Z -- 45m45s -- because the report holds the connection across
the whole replay, not just its queries. A read-only DuckDB handle takes
a shared lock the writer cannot get past, so `collector.streamd` could
not flush for the duration: 22 stall episodes that day, the longest
3,057s, peak 400,154 rows held in RAM, and one episode past `SPILL_CAP`
that moved **387,856 rows** into the JSONL sidecar whose torn-append
path is a known archive-hole class.

THE LEDGER RECORDED ALL OF THAT AND COULD NOT NAME THE CULPRIT. What it
stored was DuckDB's own words -- `Conflicting lock is held in
/usr/bin/python3.14 (PID 3026564)` -- and on this box the divergence
replay, the shadow daemon, every sweep and every ad-hoc probe are all
`/usr/bin/python3.14`. The next pass attributed it by ELIMINATION over
which timers had fired in the window. That inference was right (the
journal confirms PID 3026564 is `hyxlab-divergence`), and it was luck:
the ledger's 250 episodes name at least three distinct long holders,
including the shadow DAEMON, which runs 24/7 and no window-elimination
can ever discriminate.

The name was available the whole time, in `/proc/<pid>/cgroup`, and only
while the holder lived -- minutes later the directory is gone and the
question is permanently unanswerable. So `lock_holder` resolves it AT
the collision, and verifies the PID against the holder's open
descriptors, because a PID is evidence only if it still refers to the
process DuckDB meant.

`hyxlab/store.py::_PROC` is indirected for these tests. The alternative
is asserting against whatever cgroup the suite happens to run in, which
is a `.scope` under a shell and `hyxlab-autoloop.service` under the
autonomous loop -- i.e. a test that passes for a reason unrelated to its
claim.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from hyxlab import store

DB = "/srv/data/hyxstream.duckdb"
HELD = (
    f'IO Error: Could not set lock on file "{DB}": Conflicting lock is held in'
    " /usr/bin/python3.14 (PID {pid}) by user devs. However, you would"
)


def _fake_proc(root: Path, pid: int, *, cgroup: str | None, cmdline: str | None, fds=()) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    if cgroup is not None:
        (d / "cgroup").write_text(cgroup + "\n")
    if cmdline is not None:
        (d / "cmdline").write_bytes(b"\0".join(x.encode() for x in cmdline.split()) + b"\0")
    fd = d / "fd"
    fd.mkdir()
    for i, target in enumerate(fds):
        (fd / str(i)).symlink_to(target)


@pytest.fixture
def proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(store, "_PROC", root)
    return root


UNIT_CGROUP = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/hyxlab-divergence.service"


def test_the_holder_is_named_by_its_unit_not_by_the_interpreter(proc):
    """The whole finding. `/usr/bin/python3.14` is every holder on this box;
    `hyxlab-divergence.service` is the one that spilled 387,856 rows."""
    _fake_proc(
        proc, 3026564, cgroup=UNIT_CGROUP, cmdline="python -m simulator.divergence", fds=[DB]
    )
    assert store.lock_holder(duckdb.Error(HELD.format(pid=3026564))) == (
        "hyxlab-divergence.service pid 3026564"
    )


def test_a_holder_with_no_unit_falls_back_to_its_command_line(proc):
    """An interactive `python -m ...` has no unit and is a REAL class of long
    holder here: three of the 2026-09-25 episodes were an autonomous pass's
    own probes against the live archive. Naming the interpreter for those is
    the same non-answer the unit case exists to replace."""
    _fake_proc(proc, 77, cgroup="0::/", cmdline="/usr/bin/python3.14 -m simulator.run_l2", fds=[DB])
    assert store.lock_holder(duckdb.Error(HELD.format(pid=77))) == (
        "python3.14 -m simulator.run_l2 pid 77"
    )


def test_neither_readable_falls_back_to_duckdbs_own_name(proc):
    """Degrade to the old string, never to nothing: a holder whose /proc is
    unreadable (another user's process) is still a live holder, and the
    verdict this function exists for must not change with the name."""
    _fake_proc(proc, 78, cgroup=None, cmdline=None, fds=[DB])
    assert store.lock_holder(duckdb.Error(HELD.format(pid=78))) == "/usr/bin/python3.14 pid 78"


def test_a_pid_that_does_not_hold_the_file_is_named_with_the_caveat(proc):
    """PID reuse is the one way this instrument could indict an innocent: the
    pid DuckDB printed is alive, but it is a DIFFERENT process now. The open
    descriptor is the discriminator -- a held flock IS an open fd -- so the
    claim is checked rather than asserted."""
    _fake_proc(proc, 79, cgroup=UNIT_CGROUP, cmdline="python -m x", fds=["/etc/hostname"])
    out = store.lock_holder(duckdb.Error(HELD.format(pid=79)))
    assert out == "hyxlab-divergence.service pid 79 (no longer holds hyxstream.duckdb)"


def test_an_unreadable_fd_table_is_not_read_as_a_refutation(proc):
    """ "I could not look" must not print as "it was not it". An empty or
    unreadable fd directory answers nothing, and a caveat there would
    discredit the true holder on exactly the runs where evidence is thinnest."""
    _fake_proc(proc, 80, cgroup=UNIT_CGROUP, cmdline="python -m x", fds=[])
    assert store.lock_holder(duckdb.Error(HELD.format(pid=80))) == (
        "hyxlab-divergence.service pid 80"
    )


def test_a_message_naming_no_file_is_named_without_a_verification(proc):
    """Not every DuckDB lock error carries the path. The name still resolves;
    only the verification is unavailable, and it is silently absent rather
    than reported as a failed check."""
    _fake_proc(proc, 81, cgroup=UNIT_CGROUP, cmdline="python -m x", fds=["/etc/hostname"])
    err = "Conflicting lock is held in /usr/bin/python3.14 (PID 81) by user devs"
    assert store.lock_holder(duckdb.Error(err)) == "hyxlab-divergence.service pid 81"


def test_the_verdict_is_unchanged_for_a_dead_pid(proc):
    """The caller's discriminator -- live writer (wait) vs lock left behind
    (incident) -- is the one thing this change must not touch. `/proc/<pid>`
    absent is still None, and None is still the only way to say it."""
    assert store.lock_holder(duckdb.Error(HELD.format(pid=4_000_000))) is None


def test_it_resolves_against_the_real_kernel_filesystem(tmp_path):
    """The fixtures above are a model of /proc; this is /proc. The suite's own
    process holds a file it really opened, and both halves -- a name from the
    cgroup and a True from the fd table -- are read from the kernel."""
    f = tmp_path / "held.duckdb"
    with f.open("wb") as fh:
        fh.write(b"x")
        fh.flush()
        pid = os.getpid()
        assert store._holds_file(str(pid), str(f)) is True
        assert store._holds_file(str(pid), str(tmp_path / "nope")) is False
        # A name comes back and it is NOT the bare interpreter path.
        err = (
            f'Could not set lock on file "{f}": Conflicting lock is held in'
            f" /usr/bin/python3.14 (PID {pid}) by user devs"
        )
        out = store.lock_holder(duckdb.Error(err))
    assert out is not None and out.endswith(f"pid {pid}")
    assert not out.startswith("/usr/bin/")


def test_the_command_line_is_bounded(proc):
    """A holder's argv is attacker-free but not length-free (the sweep's is
    long), and this string lands in a JSONL ledger written once per failed
    flush for the whole duration of a stall."""
    _fake_proc(
        proc,
        82,
        cgroup="0::/",
        cmdline="python " + " ".join(f"--flag{i}" for i in range(80)),
        fds=[DB],
    )
    out = store.lock_holder(duckdb.Error(HELD.format(pid=82)))
    assert len(out) < 120, out
