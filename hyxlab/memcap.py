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

__all__ = [
    "cgroup_memory_max",
    "duck_memory_limit",
    "glibc_arena_heaps",
    "glibc_arena_heaps_from_smaps",
    "rss_anon_mb",
]

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


def sock_throttled_from_events(text: str | None) -> int | None:
    """`memory.events` contents -> the `sock_throttled` counter, or None when
    the kernel did not expose one (never 0: a zero is a measurement).

    The counter is the number of times the kernel throttled this cgroup's
    SOCKETS because the cgroup was under memory pressure -- clamping the
    transmit window, refusing receive/send buffers room to grow. Every
    other `memory.events` key says reclaim HAPPENED; this one says reclaim
    had a COST, and on a daemon whose whole job is to receive it is the one
    counter that separates the ops-rule reading (`peak == cap` with a large
    `max` is harmless page-cache reclaim) from a daemon being slowed by its
    own cap. Measured 2026-10-14: `hyxlab-stream`'s 10-10 .. 10-14 run left
    `sock_throttled=441` on its stop line and nothing read the field.
    """
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "sock_throttled" and parts[1].isdigit():
            return int(parts[1])
    return None


def cgroup_sock_throttled(root: Path | None = None, rel: str | None = None) -> int | None:
    """This process's cgroup `sock_throttled` counter, or None when there is
    no cgroup v2, no `memory.events`, or no such key. Read from the LEAF
    only: the events file is hierarchical, so the leaf's count already
    includes everything charged under it, and an ancestor's would fold in
    sibling units."""
    rel = _self_cgroup() if rel is None else rel
    if rel is None:
        return None
    node = (root or _CGROUP_ROOT) / rel.lstrip("/")
    try:
        text = (node / "memory.events").read_text()
    except OSError:
        return None
    return sock_throttled_from_events(text)


def rss_anon_mb() -> float | None:
    """This process's ANONYMOUS resident set in MiB, or None off Linux.

    Measured at the holder, because nothing else can: the cgroup's
    `memory.current` charges the page cache this daemon's own file reads
    pull in, and `MemoryPeak` sits at the cap for any healthy reader (ops
    rule). The kill that motivated it -- 2026-10-23 14:08Z, mistakes #134
    -- left `anon-rss:1039464kB` in the kernel's line and nothing in the
    daemon's own journal about how fast it got there. Every daemon under a
    `MemoryMax` samples this on its own stats line: the shadow since
    2026-10-23, the stream since 2026-10-25 (its anon ran 734M -> 1049M
    over the first 18h of the expanded book universe with no record of
    the climb but the operator's ad-hoc cgroup reads).
    """
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("RssAnon:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


#: glibc reserves every non-main malloc arena heap as a HEAP_MAX_SIZE
#: (64 MiB on 64-bit) region aligned to its own size, with the uncommitted
#: tail PROT_NONE. Nothing else in this process maps anonymous memory on a
#: 64 MiB boundary of at least a MiB: DuckDB's jemalloc extents are 2 MiB-
#: aligned, pymalloc arenas are 1 MiB mmaps. Measured 2026-10-26 02:22Z on
#: the live stream (pid 89373, 11.8h up): 76 such heaps carried 753 MiB of
#: a 987 MiB anon RSS; pymalloc 23 MiB, jemalloc 14 MiB, brk heap 38 MiB.
GLIBC_HEAP_ALIGN = 64 << 20
_GLIBC_HEAP_MIN = 1 << 20


def glibc_arena_heaps_from_smaps(text: str) -> tuple[int, float]:
    """(heap count, resident MiB) of glibc's non-main malloc arenas in a
    `/proc/<pid>/smaps` text. Pure, so a fixture can drive it."""
    heaps = 0
    rss_kb = 0
    in_heap = False
    for line in text.splitlines():
        if not line:
            continue
        if line[0] in "0123456789abcdef" and ":" not in line.split(None, 1)[0]:
            parts = line.split()
            lo, _, hi = parts[0].partition("-")
            try:
                start, end = int(lo, 16), int(hi, 16)
            except ValueError:
                in_heap = False
                continue
            # Five fields = anonymous (no pathname); `[heap]`/`[stack]` have six.
            in_heap = (
                len(parts) == 5 and start % GLIBC_HEAP_ALIGN == 0 and end - start >= _GLIBC_HEAP_MIN
            )
            if in_heap:
                heaps += 1
        elif in_heap and line.startswith("Rss:"):
            rss_kb += int(line.split()[1])
    return heaps, rss_kb / 1024


def glibc_arena_heaps() -> tuple[int, float] | None:
    """This process's glibc arena heaps as (count, resident MiB), or None.

    THE READING `rss_anon_mb` CANNOT SPLIT. The stream's anon RSS was read
    as load for two days ("734M -> 1049M over the first 18h of the expanded
    universe") while it was in fact uptime: regressed over 136 stats lines
    of one 12h run, anon = 401 + 0.38 x rate_k/min + 50.2 x hours (R2
    0.90); rate alone explains 0.54, hours alone 0.89. The memory was not
    Python objects (pymalloc 23 MiB) and not DuckDB's buffers (freed with
    the per-flush connection) but glibc malloc arenas -- 24 DuckDB worker
    threads plus the asyncio pool each own one, and a freed chunk inside
    one is retained, not returned. The collector's drain showed the same
    shape on 2026-10-11 ("native allocator fragmentation across DuckDB's
    24 threads"). So the holder publishes the split: a climb in this
    figure is allocator retention, not state the daemon is holding.
    """
    try:
        with open("/proc/self/smaps") as fh:
            return glibc_arena_heaps_from_smaps(fh.read())
    except (OSError, ValueError, IndexError):
        return None
