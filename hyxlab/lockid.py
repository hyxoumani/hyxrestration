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

`read_holder` reports `alive`, because a stale record from a crashed
holder is the one way this instrument could lie. flock releases on
process death but the sidecar does not, so liveness is re-derived from
/proc at READ time rather than trusted from the file.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["HOLDER_SUFFIX", "holder_path", "note_holder", "read_holder", "self_ident"]

HOLDER_SUFFIX = ".holder"


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
