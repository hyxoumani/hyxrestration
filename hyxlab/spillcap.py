"""How much disk a spilling query may take — bounded, from the limit in force.

EXP-1375, the rung above `hyxlab.memcap`. That rung sized the buffer
manager from the cgroup, so a capped service spills instead of being
SIGKILLed. It says nothing about how big the spill may get, and DuckDB's
answer is `max_temp_directory_size` = **"90% of available disk space"** —
1.26 TB of the 1.4 TB the archive, the collector and the stream daemon
all share. That is not a bound; it is the whole disk with a rounding
error, and the collector's next flush is what pays for reaching it.

**WHAT A REPLAY ACTUALLY SPILLS, MEASURED 2026-08-27** on the live
15.7 GB stream archive and the 13.3 GB market archive, at the 512 MiB
limit a 1 GiB service cgroup now yields (reproduced by
`test_spillcap_discipline.py`):

  sliced walk, 72 h / 26.0M rows   45 MiB   ok, 22.5 s
  unsliced sort, 12 h / 3.5M rows  53 MiB   ok
  unsliced sort, 24 h / 10.4M rows 275 MiB  DIES (out of memory)
  unsliced sort, 48 h              712 MiB  DIES (out of memory)
  atlas BUCKET_SQL                 0 MiB    DIES INSTANTLY, spilling nothing
  atlas BUCKET_SQL at 1 GiB        266 MiB  ok

**THE LARGEST SPILL BY ANY QUERY THAT SUCCEEDED IS 266 MiB, AND SPILL IS
NOT WHAT SAVES THE BIG ONES** — they exceed the limit in memory the
buffer manager cannot spill and raise `OutOfMemoryException` first, in
one case at 0.1 s with zero bytes written. So this cap is not sized to
rescue a query; nothing measured is anywhere near it. It exists because
the number DuckDB picks in its place is the disk.

WHY A MULTIPLE OF `memory_limit`. Spill IS the buffer manager's
overflow, so the limit is the only quantity the spill is dimensionally
related to. Measured spill/limit: 0.09 (sliced walk), 0.10, 0.27
(atlas) for work that completed, and 0.54 / 1.39 for the two runaways
that died. `SPILL_MULTIPLE = 8` is therefore ~6x the worst number ever
observed here, including the failures — a plan that wants more than 8x
its own buffer pool on disk is not a plan we have measured, and refusing
it costs a query that was already doomed.

WHY A DISK FRACTION AS WELL, and what it does NOT promise. The multiple
is derived from the limit, so an UNCAPPED process inherits DuckDB's
48.2 GiB default and 8x of that is 385 GiB. `DISK_SHARE = 0.25` of free
space is what binds there. Its guarantee is honestly smaller than the
multiple's: not "the spill is small" but "three quarters of the free
space stays free", which is the thing the collector actually needs. The
two terms bind in different regimes on purpose, and the tighter wins.

WE READ THE LIMIT, WE DO NOT RECOMPUTE IT. The input is
`current_setting('memory_limit')` on the connection itself, after
`cgroup_memory_limit` has had its say. So this composes with the rung
below instead of duplicating its derivation, and it is correct in the
case that rung deliberately leaves alone: an ad-hoc query in a login
shell keeps its host-RAM limit and gets a spill cap derived from THAT,
rather than no cap at all.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["DISK_SHARE", "SPILL_MULTIPLE", "duck_spill_limit", "free_bytes", "parse_size"]

#: Spill allowed per byte of buffer pool. See the module docstring: ~6x
#: the worst spill/limit ratio ever measured here, failures included.
SPILL_MULTIPLE = 8

#: Share of CURRENT free space a single spill may take. Binds where the
#: multiple is derived from an uncapped host-RAM limit.
DISK_SHARE = 0.25

_UNITS = {
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "KIB": 2**10,
    "MIB": 2**20,
    "GIB": 2**30,
    "TIB": 2**40,
}


def parse_size(raw: str) -> int | None:
    """DuckDB's own size rendering ('48.2 GiB', '512MB') -> bytes.

    None for anything not a size — notably `max_temp_directory_size`'s
    default, the prose string "90% of available disk space", which is
    exactly the value this module exists to replace.
    """
    s = raw.strip()
    if not s:
        return None
    num = s
    unit = "B"
    for i, ch in enumerate(s):
        if not (ch.isdigit() or ch in ".-"):
            num, unit = s[:i], s[i:].strip()
            break
    try:
        val = float(num)
    except ValueError:
        return None
    scale = _UNITS.get(unit.upper())
    if scale is None or val < 0:
        return None
    return int(val * scale)


def free_bytes(path: str | Path) -> int | None:
    """Free space on the filesystem holding `path`, or its nearest parent.

    The nearest EXISTING parent, because the spill directory is created
    by whoever attaches and may not be there yet when the cap is set;
    the filesystem is the same either way.
    """
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return None
    while True:
        try:
            st = os.statvfs(p)
            return st.f_bavail * st.f_frsize
        except (OSError, ValueError):
            # ValueError too: a path is a caller's string, and a bound
            # that raises on a bad one is a bound that does not hold.
            if p.parent == p:
                return None
            p = p.parent


def duck_spill_limit(memory_limit: int, spill_dir: str | Path | None) -> int:
    """Bytes to allow on disk for a buffer pool of `memory_limit`.

    The tighter of the two bounds. `spill_dir` None (or on a filesystem
    that cannot be stat'd) drops the disk term and leaves the multiple,
    which is the conservative direction: the multiple is the tighter one
    wherever a cgroup cap binds, which is every case that has ever hurt.
    """
    cap = SPILL_MULTIPLE * max(memory_limit, 0)
    free = free_bytes(spill_dir) if spill_dir is not None else None
    if free is not None:
        cap = min(cap, int(free * DISK_SHARE))
    return max(cap, 0)
