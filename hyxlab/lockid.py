"""Holder identity for `data/writer.lock` — who holds it, from when.

EXP-944. On 2026-08-03 the writer lock was continuously unavailable from
12:47:03Z to ~13:06:20Z (measured: `collector.breadth` reported
`total_s=1169.7, fetch_s=3.0`, and every second of the remainder is spent
inside `writer_burst`). Three 5-min `collect` cycles waited their full
240 s and journalled a skip — a 15-minute hole in the snapshot tape.

**The holder could not be named.** Nothing anywhere recorded who held the
lock, so the outage was attributable only by inference from write volume.
The failure mode this module closes is not "we lack a lock" but "we lack
a WITNESS": an outage whose culprit is unnamed gets re-litigated with
circumstantial evidence, and circumstantial evidence about locks is how
`fuser`'s open handle and `ps -o etime`'s PROCESS AGE previously got
mistaken for a hold duration. An open handle is not a held flock and an
elapsed time is not a hold; only a record written AT ACQUIRE TIME is.

Mechanism: the process that has just won the flock truncates a tiny
sidecar next to it with its pid/unit/cmd and the acquire timestamp. The
write is serialised by the very lock it describes, so there is no race
and no lock of its own — the holder is the only writer by construction.
A waiter that times out reads the sidecar and names its blocker.

The record is deliberately LAST-WRITER-WINS rather than append-only: the
question it answers is "who holds it NOW", the file stays one line
forever, and an append-only history here would be a second unbounded
journal for a question nobody asks retrospectively.

The same witness serves the other lock in this repo, the single-INSTANCE
guard (`acquire_instance_lock`): "another sweep holds the lock; aborting"
names nothing, and an operator who cannot see which pid, unit and start
time is blocking them has the 08-03 problem in miniature. Instance locks
are job-scoped ON PURPOSE — one shared file would make the ~7h poly sweep
exclude the 06:10 incremental sweep every single day.

`read_holder` reports `alive`, because a stale record from a crashed
holder is the one way this instrument could lie. flock releases on
process death but the sidecar does not, so liveness is re-derived from
/proc at READ time rather than trusted from the file.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "HOLDER_SUFFIX",
    "INSTANCE_SUFFIX",
    "OWNER_SUFFIX",
    "acquire_instance_lock",
    "acquire_owner_lock",
    "db_owner_lock_or_reason",
    "describe_holder",
    "instance_lock_or_reason",
    "holder_path",
    "instance_lock_path",
    "owner_lock_path",
    "note_holder",
    "read_holder",
    "self_ident",
]

HOLDER_SUFFIX = ".holder"
INSTANCE_SUFFIX = ".instance.lock"
OWNER_SUFFIX = ".owner.lock"
DATA_DIR = "data"


def holder_path(lock_file: str) -> str:
    """Sidecar path for `lock_file` (`data/writer.lock.holder`)."""
    return str(lock_file) + HOLDER_SUFFIX


def _read_proc(pid: int, name: str) -> str:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _unit(pid: int) -> str:
    """systemd unit from the cgroup path, e.g. `hyxlab-sweep.service`.

    Falls back to "" rather than guessing: an empty unit reads as "not a
    systemd job", which is true for an ad-hoc shell run or an agent, and
    that distinction is exactly what an attribution needs.
    """
    cg = _read_proc(pid, "cgroup")
    leaf = cg.rsplit("/", 1)[-1].strip() if cg else ""
    return leaf if leaf.endswith((".service", ".scope", ".slice")) else ""


def self_ident(pid: int | None = None, now: datetime | None = None) -> dict:
    """The identity record for `pid` (default: this process)."""
    pid = os.getpid() if pid is None else pid
    now = now or datetime.now(UTC)
    return {
        "pid": pid,
        "unit": _unit(pid),
        "cmd": _read_proc(pid, "cmdline"),
        "at": now.isoformat(),
    }


def note_holder(lock_file: str, pid: int | None = None, now: datetime | None = None) -> None:
    """Record that this process now holds `lock_file`. Never raises.

    Call IMMEDIATELY after the flock is won and BEFORE any other work —
    notably before `open_retry`, which can spin for up to 300 s WHILE
    HOLDING the flock when a read-only DuckDB reader is attached. A
    holder stuck in that spin is precisely the one worth naming, so
    naming it must not be deferred until after the open succeeds.

    Failure is swallowed on purpose: this is a witness, not a
    participant. An instrument that can abort the write it observes
    would trade a data hole for a bigger one.
    """
    try:
        p = Path(holder_path(lock_file))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self_ident(pid=pid, now=now)) + "\n")
    except OSError:
        pass


def read_holder(lock_file: str) -> dict | None:
    """The last recorded holder of `lock_file`, or None if unrecorded.

    Adds `alive`: True when a process with that pid is running AND its
    cmdline still matches the record. The cmdline check is what makes
    this safe against pid reuse — a recycled pid running something else
    is not the holder, and reporting it as one would name an innocent.
    """
    try:
        raw = Path(holder_path(lock_file)).read_text()
    except OSError:
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(rec, dict) or "pid" not in rec:
        return None
    rec["alive"] = bool(rec.get("cmd")) and _read_proc(int(rec["pid"]), "cmdline") == rec["cmd"]
    return rec


def instance_lock_path(job: str, data_dir: str = DATA_DIR) -> str:
    """Lock file for the single-instance guard of `job`.

    Job-scoped, not archive-scoped. The sweep's original lock was
    `<db>.lock`, so any second job adopting that path would have made the
    ~7h `poly_sweep` (04:15Z) exclude the incremental `sweep` (06:10Z)
    every day — a starvation bug wearing a safety fix's clothes.
    """
    return str(Path(data_dir) / f"{job}{INSTANCE_SUFFIX}")


def acquire_instance_lock(job: str, data_dir: str = DATA_DIR):
    """Exclusive non-blocking flock for `job`; None if one already runs.

    flock releases on process death — no stale-file failure mode (the old
    touch()/exists() lock survived SIGKILL and blocked every later sweep
    until removed by hand). The handle must outlive this call: closing it
    releases the lock, so callers hold it for the run.

    This lock blocks no collector. It is held for the whole multi-hour
    run and guards a job against ITSELF — a second copy of a fetch-paced
    worklist pass duplicates hours of HTTP, doubles writer-lock
    contention and re-walks the same watermarks.
    """
    return _flock_or_none(instance_lock_path(job, data_dir))


def _flock_or_none(path: str):
    """Win `path`'s exclusive flock and record the holder, or return None.

    The handle must outlive this call: closing it releases the lock.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")  # noqa: SIM115 — handle must outlive this call
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    note_holder(path)
    return f


def owner_lock_path(db_path: str) -> str:
    """Ownership lock for the database at `db_path` (`<db>.owner.lock`).

    Named after the FILE, not after a job. The instance lock is job-scoped
    because two archive jobs share one archive and must not exclude each
    other; here the resource IS the file, so two writers of the same file
    must exclude each other and two runs pointing at different files
    (a test tmpdir, a side experiment) must not.
    """
    return str(db_path) + OWNER_SUFFIX


def acquire_owner_lock(db_path: str):
    """Exclusive non-blocking flock on `<db_path>.owner.lock`; None if held.

    For a database owned by a single long-lived writer that opens it in
    BURSTS — `collector.streamd` (flush every ~15 s) and the shadow
    ledger (a write per ~20 s poll). DuckDB's file lock is held only for
    the duration of each burst, so it excludes nothing between them:
    measured 2026-08-26, two StreamStore writers on one file completed 20
    and 15 flushes with ZERO declines, interleaving duplicate rows into an
    append-only archive that never dedupes. This lock is held for the
    whole process lifetime, which is the guard the file lock only looked
    like.
    """
    return _flock_or_none(owner_lock_path(db_path))


def describe_holder(lock_file: str) -> str:
    """One line naming who holds `lock_file`, for an abort message.

    Shared by the waiter that times out on the writer lock and the job
    that finds its instance lock taken: both are asking the same question
    ("who is blocking me"), and both used to answer it with prose that
    named nobody.
    """
    rec = read_holder(lock_file)
    if not rec:
        return "holder unrecorded"
    stale = "" if rec.get("alive") else " (DEAD/stale record)"
    return f"{rec.get('unit') or 'no unit'} pid={rec['pid']} since {rec.get('at')}{stale}"


def instance_lock_or_reason(job: str, data_dir: str = DATA_DIR) -> tuple[object | None, str]:
    """`(lock, "")` when `job` is free, `(None, why)` when it is not.

    The refusal path is the whole point of pairing these two calls: every
    caller of this helper is a job that has just decided not to run, and
    "another sweep holds the lock" told the operator nothing about which
    one, since when, or whether the holder is even alive.
    """
    lock = acquire_instance_lock(job, data_dir)
    if lock is not None:
        return lock, ""
    return None, (
        f"another {job} holds the instance lock:"
        f" {describe_holder(instance_lock_path(job, data_dir))}"
    )


def db_owner_lock_or_reason(db_path: str) -> tuple[object | None, str]:
    """`(lock, "")` when nothing owns `db_path`, `(None, why)` when it is.

    Paired like `instance_lock_or_reason`, for the same reason: the caller
    is a daemon that has just decided not to run, and an operator who
    started it by hand needs to know which pid, which unit and since when
    — not that "it is already running".
    """
    lock = acquire_owner_lock(db_path)
    if lock is not None:
        return lock, ""
    return None, (
        f"another writer owns {db_path}: {describe_holder(owner_lock_path(db_path))}"
    )
