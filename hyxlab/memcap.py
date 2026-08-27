"""DuckDB's memory limit, taken from the cgroup this process runs in.

EXP-1374. DuckDB sizes `memory_limit` from PHYSICAL RAM — 80% of what
`/proc/meminfo` reports — and physical RAM is not what a systemd service
is allowed to use. `hyxlab-stream.service` sets `MemoryMax=2G` on a
60 GiB box, so DuckDB opens the stream archive believing it has **48.2
GiB** and buffers accordingly. There is no spill and no back-pressure at
the boundary it actually has: the cgroup answers with SIGKILL.

**MEASURED 2026-08-26** (reproduced by `test_memcap_discipline.py`): the
daemon's own startup query — `SELECT max(recv_ts) FROM book_events WHERE
venue = 'kalshi'`, over the 15.7 GB stream archive — peaks at **2899 MB
RSS in 1.55 s** under the default limit. The cap is 2048 MB. That is the
kill: `hyxlab-stream` was OOM-killed 17 s into the 2026-08-26 20:19Z
promote restart, one second after `kalshi-books: 614 open tickers`, which
is exactly where `_kalshi_loop` reads `last_recv_ts` to bound its
`seq_reset` gap. The successor recorded a 2 GiB `MemoryPeak` for the same
startup and survived. Every restart was a coin flip against the cap, and
a promote is what forces restarts.

**THE SAME QUERY UNDER A PINNED LIMIT IS BOUNDED AND FASTER**: 1 GiB ->
1126 MB peak in 0.27 s, 512 MB -> 654 MB, 256 MB -> 415 MB, all returning
the identical answer, and `duckdb_temporary_files()` reports ZERO bytes
spilled at every one of them. Nothing here needed 2.8 GB. A streaming
aggregate simply keeps whatever the buffer manager is told it may keep,
so the limit is not a budget the work fits into — it IS the footprint.
Lowering it therefore costs no disk either: this rung does not make
`max_temp_directory_size` (still unset, still 90% of the disk) any more
urgent than it already was.

WHY A FRACTION AND NOT THE CAP. DuckDB's 80% assumes it is the machine's
main consumer. Inside a service cgroup it shares the cap with the Python
process that hosts it, the websocket buffers, the store's pending rows —
and with the PAGE CACHE its own reads pull in, which `memory.max` counts
and which the live daemon holds 133 MB of. Measured overhead above the
limit was ~155 MB across all three probes. Half of a 2 GiB cap leaves a
gigabyte for all of that; 80% would leave 400 MB for a process that has
already been killed once. Exceeding `memory_limit` costs a spill into a
private directory; exceeding `memory.max` costs the run.

NO CAP MEANS NO CHANGE. An ad-hoc query in a login shell is under no
memory cgroup, `cgroup_memory_max` returns None, and DuckDB keeps its own
default — a 60 GiB box should use its RAM. This tightens only where a cap
was declared and DuckDB could not see it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["cgroup_memory_max", "duck_memory_limit"]

#: Share of the cgroup cap handed to DuckDB's buffer manager. See the
#: module docstring: the rest is the host process, its buffers, and the
#: page cache that `memory.max` charges to us.
DUCK_SHARE = 0.5

#: Never propose less than this. DuckDB clamps absurdly small limits
#: anyway, and a cap tighter than the floor gets the cap instead.
FLOOR_BYTES = 256 * 1024 * 1024

_CGROUP_ROOT = Path("/sys/fs/cgroup")


def _self_cgroup() -> str | None:
    """This process's cgroup-v2 path, e.g. `/user.slice/....service`.

    None for cgroup v1 or no cgroups at all, which callers read as
    "unknown cap" — the same answer as "no cap", and the same safe
    outcome: leave DuckDB's own default alone.
    """
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            hid, _, rest = line.partition(":")
            if hid == "0":  # the unified hierarchy
                return rest.partition(":")[2] or "/"
    except OSError:
        return None
    return None


def cgroup_memory_max() -> int | None:
    """Tightest `memory.max` over this cgroup AND every ancestor, or None.

    Ancestors matter because any of them can be the binding limit: a
    service with no `MemoryMax` of its own still inherits whatever its
    slice declares, and the kernel kills against the tightest one. Reading
    only the leaf would report "unlimited" for a process that is not.

    None means no finite limit was found anywhere up the chain.
    """
    rel = _self_cgroup()
    if rel is None:
        return None
    best: int | None = None
    node = _CGROUP_ROOT / rel.lstrip("/")
    while True:
        try:
            raw = (node / "memory.max").read_text().strip()
        except OSError:
            raw = ""
        if raw and raw != "max":
            try:
                val = int(raw)
            except ValueError:
                val = -1
            if val > 0 and (best is None or val < best):
                best = val
        if node == _CGROUP_ROOT or _CGROUP_ROOT not in node.parents:
            break
        node = node.parent
    return best


def duck_memory_limit() -> int | None:
    """Bytes to give DuckDB's `memory_limit`, or None to leave it alone.

    None whenever no cgroup cap binds this process — an uncapped box
    keeps DuckDB's own host-RAM default, deliberately.
    """
    cap = cgroup_memory_max()
    if cap is None:
        return None
    return max(int(cap * DUCK_SHARE), min(FLOOR_BYTES, cap))
