"""Process-private DuckDB spill space — `<db>.tmp/pid-<pid>`.

EXP-1373. DuckDB spills to disk when a query exceeds `memory_limit`, and
the directory it spills into is SHARED BY DEFAULT: with no
`temp_directory` set, an on-disk database spills to `<db>.tmp`, so every
process that opens `data/hyxstream.duckdb` writes its scratch into one
directory. The file names inside it carry no pid — `duckdb_temp_storage_
DEFAULT-0.tmp`, `..._S64K-0.tmp` and so on — so two processes do not get
two sets of blocks, they get one set with two writers.

**MEASURED 2026-08-26** (reproduced by `test_sidecar_discipline.py`):
two processes sorting past their memory limit into ONE temp directory
failed in all three trials — two SIGSEGV-with-core, and one
`InvalidInputException: Invalid unicode (byte sequence mismatch) detected
in segment statistics update`, which is one process reading the other's
blocks as its own data. The same pair pointed at two directories
succeeded, both, every time. This is not contention that costs time; it
is a crash and a silent misread.

The hazard is not hypothetical here and it is not covered by any lock we
hold. `simulator.shadow` pinned `temp_directory` to the CONSTANT
`data/duckspill-shadow`, and `simulator.run_l2` — a by-hand CLI — reads
the stream archive through the same `stream_conn`, so replaying an L2
run while the shadow daemon was live pointed two spilling processes at
one directory by design. The owner lock cannot help: it is file-scoped
so that a side run on a copied database is NOT blocked, and a reader
takes no owner lock at all. Two READERS of one archive are legitimate,
which is exactly why their scratch must not be shared.

Scope: per process AND per database. Per process, because the collision
is between processes. Per database, because a process holding two
connections open at once would otherwise collide with itself — the
directory name is derived from the database path for the same reason
`<db>.spill.jsonl` is. (One live connection per database per process
remains an invariant: two simultaneous connections to the SAME file in
one process share a directory again, and nothing in this repo does that.)

Ownership is by flock, not by liveness heuristics: the owner holds
`<dir>.owner.lock` for its whole life, so a reaper that WINS that lock
has proved the owner is gone. A pid check would have to guess about pid
reuse; winning the lock does not guess.
"""

from __future__ import annotations

import atexit
import os
import shutil
from pathlib import Path

from hyxlab.lockid import OWNER_SUFFIX, acquire_owner_lock, holder_path, owner_lock_path

__all__ = ["duck_scratch_dir", "reap_stale_scratch", "scratch_root"]

# Databases with no file to sit next to. DuckDB gives in-memory databases
# no default spill directory either, so there is nothing to make private.
_NO_FILE = {"", ":memory:"}

# path -> open lock handle. Closing the handle releases the flock, so the
# handles must live as long as the process does.
_HELD: dict[str, object] = {}


def scratch_root(db_path: str | Path) -> str:
    """Spill root for `db_path` — DuckDB's own default, `<db>.tmp`.

    Deliberately the default location: spill for a 13 GB archive belongs
    on the archive's filesystem, and any root of our own choosing could
    put it on another volume.
    """
    return str(db_path) + ".tmp"


def duck_scratch_dir(db_path: str | Path) -> str | None:
    """This process's private spill directory for `db_path`.

    None when `db_path` has no file to sit next to (in-memory), which
    callers read as "leave `temp_directory` alone".

    Reaps abandoned siblings first, so the directory cannot grow one
    entry per process that ever crashed.
    """
    if str(db_path) in _NO_FILE:
        return None
    root = scratch_root(db_path)
    base = str(Path(root) / f"pid-{os.getpid()}")
    if base in _HELD:
        # Fast path: this process already owns its directory for this
        # database. Callers open per write burst (`StreamStore.flush`
        # every ~15 s), so the reap sweep must not run on every connect.
        Path(base).mkdir(parents=True, exist_ok=True)
        return base
    reap_stale_scratch(db_path)
    # Locked BEFORE the directory exists: a reaper only ever removes a
    # directory whose lock it has won, so there is no window in which our
    # scratch is visible and unclaimed.
    for n in range(64):
        d = str(Path(root) / (f"pid-{os.getpid()}" if n == 0 else f"pid-{os.getpid()}-{n}"))
        if d in _HELD:
            Path(d).mkdir(parents=True, exist_ok=True)
            return d
        lock = acquire_owner_lock(d)
        if lock is not None:
            _HELD[d] = lock
            Path(d).mkdir(parents=True, exist_ok=True)
            return d
    # Every name taken by a live holder means another process is running
    # as our pid, which cannot happen; refusing beats sharing silently.
    raise RuntimeError(f"no free scratch directory under {root}")


def reap_stale_scratch(db_path: str | Path) -> list[str]:
    """Remove `<db>.tmp/pid-*` entries whose owner is gone; return them.

    Winning the owner lock IS the proof of abandonment — flock is
    released by the kernel on process death, so a lock we can take is a
    lock nobody holds.
    """
    root = Path(scratch_root(db_path))
    reaped: list[str] = []
    for lock_file in sorted(root.glob(f"pid-*{OWNER_SUFFIX}")):
        d = str(lock_file)[: -len(OWNER_SUFFIX)]
        if d in _HELD:
            continue
        lock = acquire_owner_lock(d)
        if lock is None:  # a live owner holds it
            continue
        try:
            shutil.rmtree(d, ignore_errors=True)
            Path(holder_path(owner_lock_path(d))).unlink(missing_ok=True)
            Path(owner_lock_path(d)).unlink(missing_ok=True)
            reaped.append(d)
        finally:
            lock.close()
    return reaped


@atexit.register
def _release() -> None:
    """Drop this process's scratch on a clean exit.

    Best effort only — the reaper, not this hook, is what bounds the
    directory, because the exits that matter (OOM kill, SIGKILL) run no
    hook at all.
    """
    for d, lock in list(_HELD.items()):
        try:
            shutil.rmtree(d, ignore_errors=True)
            Path(holder_path(owner_lock_path(d))).unlink(missing_ok=True)
            Path(owner_lock_path(d)).unlink(missing_ok=True)
            lock.close()  # type: ignore[attr-defined]
        except OSError:
            pass
    _HELD.clear()
