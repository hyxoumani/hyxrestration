"""How much DISK a spilling attach may take is bounded, and by what.

EXP-1375, the rung above `test_memcap_discipline.py`. That guard sized
every attach's buffer manager from the cgroup, so a capped service
spills instead of being SIGKILLed. It says nothing about how big the
spill may get, and DuckDB's answer is `max_temp_directory_size` =
**"90% of available disk space"** — 1.26 TB of the 1.4 TB volume the
market archive, the stream archive and the collector share. That is the
whole disk with a rounding error, and the collector's next flush is what
pays for reaching it.

WHAT A REPLAY ACTUALLY SPILLS, MEASURED 2026-08-27 against the live
15.7 GB stream archive and the 13.3 GB market archive, at the 512 MiB
limit a 1 GiB service cgroup now yields:

  sliced walk (`bookreplay.stream_events`), 72 h / 26.0M rows
                                     45 MiB   ok, 22.5 s
  unsliced sort, 12 h / 3.5M rows    53 MiB   ok
  unsliced sort, 24 h / 10.4M rows   275 MiB  DIES (out of memory)
  unsliced sort, 48 h                712 MiB  DIES (out of memory)
  atlas BUCKET_SQL                   0 MiB    DIES AT 0.1 s, spilling nothing
  atlas BUCKET_SQL at a 1 GiB limit  266 MiB  ok

**THE LARGEST SPILL BY ANY QUERY THAT SUCCEEDED IS 266 MiB, AND SPILL IS
NOT WHAT SAVES THE BIG ONES.** A wide sort exceeds the limit in memory
the buffer manager cannot offload and raises `OutOfMemoryException`
first — reproduced here by
`test_a_wide_sort_dies_in_memory_rather_than_spilling`, which is the
premise the cap's SIZE rests on. So this rung is not sized to rescue a
query; nothing measured is anywhere near it. It exists because the
number DuckDB picks in its place is the disk.

THE GUARD, same shape as the two rungs below:

  DERIVED — every function in the kernel that calls `duckdb.connect`
            also calls `spill_cap`, AND calls it AFTER
            `cgroup_memory_limit`, by AST. The order is load-bearing:
            the cap is a multiple of the limit in force, so setting it
            first would derive it from the limit we are about to
            replace.
  CLAIMED — verified by RUNNING: a query that spills 121 MiB with room
            to spare is refused under a 32 MiB cap, and the bytes on
            disk never exceed the cap. WITH ITS CONTROL — the same
            query under the default runs and spills more than the cap,
            so a green here cannot come from a query that never spilled.

**The premise is a test, not a memory**
(`test_duckdbs_own_default_is_the_disk_itself`): the default is asserted
to be the un-parseable prose string, so the day DuckDB ships a real
bound this reddens and `hyxlab/spillcap.py` can go instead of outliving
its reason.
"""

from __future__ import annotations

import ast
import contextlib
import os
import threading
import time
from pathlib import Path

import duckdb
import pytest

from hyxlab.spillcap import (
    DISK_SHARE,
    SPILL_MULTIPLE,
    duck_spill_limit,
    free_bytes,
    parse_size,
)

ROOT = Path(__file__).resolve().parent.parent

#: The one module allowed to attach DuckDB (`test_sidecar_discipline.py`
#: holds that line), and so the one place the cap can be unbypassable.
KERNEL = ROOT / "hyxlab" / "store.py"
CAP_CALL = "spill_cap"
LIMIT_CALL = "cgroup_memory_limit"

#: A GROUP BY wide enough to spill at a small limit and gracefully enough
#: to SURVIVE it — measured 121 MiB spilled, 0.2 s, at a 256 MiB limit.
_FIXTURE_ROWS = 4_000_000
_SPILLER = "SELECT count(*) FROM (SELECT h, i, repeat('z', 40) p FROM t GROUP BY 1, 2, 3)"
_FIXTURE_MEM = "256MB"
_TINY_CAP = 32 * 2**20


# -- the premise ----------------------------------------------------------


def test_duckdbs_own_default_is_the_disk_itself():
    """Unset, `max_temp_directory_size` is prose, not a number.

    Asserted rather than remembered: the day DuckDB ships a real default
    bound, this reddens and `hyxlab.spillcap` has no reason to exist.
    """
    with duckdb.connect() as conn:
        raw = conn.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0]
    assert parse_size(str(raw)) is None, (
        f"max_temp_directory_size now defaults to a parseable size ({raw!r}) —"
        " DuckDB may bound its own spill now; re-read hyxlab/spillcap.py"
    )
    assert "disk" in str(raw), f"unexpected default {raw!r}"


def test_a_wide_sort_dies_in_memory_rather_than_spilling(tmp_path):
    """Spill is not the safety net the cap would otherwise be sized for.

    The measurement this rung's SIZE rests on: a sort whose payload does
    not fit raises out-of-memory instead of spilling its way through, so
    the cap is bounding a disk risk, not rationing a working query.
    """
    conn = _fixture(tmp_path, rows=_FIXTURE_ROWS, payload=True)
    with pytest.raises(duckdb.OutOfMemoryException):
        cur = conn.execute("SELECT i, h, p FROM t ORDER BY h")
        while cur.fetchmany(50_000):
            pass
    conn.close()


# -- derived half ---------------------------------------------------------


def _kernel_attachers() -> dict[str, list[str]]:
    """Kernel functions that call `duckdb.connect` -> the names they call.

    Call order is preserved, because this rung's correctness depends on
    it: `spill_cap` reads the limit `cgroup_memory_limit` sets.
    """
    tree = ast.parse(KERNEL.read_text())
    found: dict[str, list[str]] = {}

    class V(ast.NodeVisitor):
        def _scoped(self, node):
            attaches = False
            calls: list[str] = []

            class Inner(ast.NodeVisitor):
                def visit_Call(self, c):
                    nonlocal attaches
                    f = c.func
                    if (
                        isinstance(f, ast.Attribute)
                        and f.attr == "connect"
                        and getattr(f.value, "id", "") == "duckdb"
                    ):
                        attaches = True
                    if isinstance(f, ast.Name):
                        calls.append(f.id)
                    self.generic_visit(c)

            for child in node.body:
                Inner().visit(child)
            if attaches:
                found[node.name] = calls
            self.generic_visit(node)

        visit_FunctionDef = visit_AsyncFunctionDef = _scoped

    V().visit(tree)
    return found


def test_every_kernel_attach_also_caps_the_spill():
    """A new attach in the kernel cannot forget the disk bound."""
    attachers = _kernel_attachers()
    assert attachers, f"no duckdb.connect found in {KERNEL} — has the kernel moved?"
    missing = sorted(n for n, calls in attachers.items() if CAP_CALL not in calls)
    assert not missing, (
        f"{KERNEL.name}: these attach DuckDB without {CAP_CALL}(): {missing}."
        " Unbounded spill is 90% of the disk the collector writes to."
    )


def test_the_cap_is_set_after_the_limit_it_is_derived_from():
    """Order, not just presence: the cap is a multiple of the limit.

    Applied before `cgroup_memory_limit`, it would be derived from
    DuckDB's host-RAM default and left 90x too large on exactly the
    capped services this exists for.
    """
    for name, calls in _kernel_attachers().items():
        if CAP_CALL not in calls or LIMIT_CALL not in calls:
            continue
        assert calls.index(LIMIT_CALL) < calls.index(CAP_CALL), (
            f"{KERNEL.name}:{name} sets the spill cap before the memory limit —"
            " the cap would be derived from the limit it is about to replace"
        )


# -- the derivation -------------------------------------------------------


def test_the_multiple_binds_where_a_cgroup_cap_binds():
    """The regime that has actually hurt: a small limit on a big disk."""
    mem = 512 * 2**20
    got = duck_spill_limit(mem, None)
    assert got == SPILL_MULTIPLE * mem
    # ...and the disk term must not tighten it on a disk with room.
    free = free_bytes(ROOT)
    assert free is not None
    if free * DISK_SHARE > SPILL_MULTIPLE * mem:
        assert duck_spill_limit(mem, ROOT) == SPILL_MULTIPLE * mem


def test_the_disk_share_binds_where_the_limit_is_the_whole_box():
    """An uncapped attach inherits a host-RAM limit; 8x of that is not a bound."""
    free = free_bytes(ROOT)
    assert free is not None
    huge = free  # 8x free space, so the multiple cannot be the tighter one
    got = duck_spill_limit(huge, ROOT)
    assert got == int(free * DISK_SHARE)
    assert got < free, "a spill cap at or above free space is not a cap"


def test_an_unstattable_directory_keeps_the_multiple():
    """Losing the disk term must not lose the bound."""
    assert duck_spill_limit(100, "/definitely/not/a/mount/point\0") == SPILL_MULTIPLE * 100


def test_parse_size_reads_duckdbs_renderings_and_rejects_its_prose():
    assert parse_size("48.2 GiB") == int(48.2 * 2**30)
    assert parse_size("512MB") == 512 * 10**6
    assert parse_size("1024") == 1024
    assert parse_size("90% of available disk space") is None
    assert parse_size("") is None
    assert parse_size("banana") is None


def test_a_live_attach_reports_a_finite_cap(tmp_path):
    """End to end through the real kernel helper: prose becomes a number."""
    from hyxlab.store import connect_retry

    conn = connect_retry(tmp_path / "live.duckdb", read_only=False)
    raw = conn.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0]
    mem = parse_size(conn.execute("SELECT current_setting('memory_limit')").fetchone()[0])
    conn.close()
    got = parse_size(str(raw))
    assert got is not None, f"live attach left the spill unbounded: {raw!r}"
    assert mem is not None
    assert got <= SPILL_MULTIPLE * mem
    free = free_bytes(tmp_path)
    assert free is not None
    # Rendering rounds ('3.8 GiB'), so allow a rounding step, not a regime.
    assert got <= int(free * DISK_SHARE) * 1.01


# -- claimed half: verified by RUNNING ------------------------------------


def _fixture(tmp_path: Path, *, rows: int = _FIXTURE_ROWS, payload: bool = False):
    """A DuckDB with a small limit, a private spill dir, and a big table."""
    conn = duckdb.connect(str(tmp_path / "spill.duckdb"))
    conn.execute(f"SET memory_limit='{_FIXTURE_MEM}'")
    conn.execute(f"SET temp_directory='{tmp_path / 'spill'}'")
    cols = "i, hash(i) h" + (", repeat('y', 60) p" if payload else "")
    conn.execute(f"CREATE OR REPLACE TABLE t AS SELECT {cols} FROM range({rows}) s(i)")
    return conn


def _watch_spill(d: Path, stop: threading.Event, peak: list[int]) -> threading.Thread:
    """Poll the spill directory from the FILESYSTEM, never the connection.

    `duckdb_temporary_files()` is connection state, and polling it on the
    connection under test makes the observer part of the measurement.
    """

    def run():
        while not stop.is_set():
            with contextlib.suppress(OSError):
                peak[0] = max(peak[0], sum(f.stat().st_size for f in d.glob("*") if f.is_file()))
            time.sleep(0.02)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _run_watched(conn, sql: str, spill_dir: Path):
    peak = [0]
    stop = threading.Event()
    t = _watch_spill(spill_dir, stop, peak)
    err = None
    try:
        conn.execute(sql).fetchone()
    except duckdb.Error as e:
        err = e
    finally:
        stop.set()
        t.join(timeout=5)
    return err, peak[0]


def test_the_control_the_same_query_spills_past_the_cap_when_uncapped(tmp_path):
    """Without this, a green cap test could just be a query that never spilled."""
    conn = _fixture(tmp_path)
    err, peak = _run_watched(conn, _SPILLER, tmp_path / "spill")
    conn.close()
    assert err is None, f"the fixture query must SUCCEED uncapped: {err}"
    assert peak > _TINY_CAP, (
        f"fixture spilled only {peak} B, below the {_TINY_CAP} B cap under test —"
        " the cap test below would pass without the cap doing anything"
    )


def test_the_cap_is_enforced_and_the_disk_never_exceeds_it(tmp_path):
    """The claim, run: refused at the bound, and refused BY the bound."""
    conn = _fixture(tmp_path)
    conn.execute(f"SET max_temp_directory_size='{_TINY_CAP}B'")
    err, peak = _run_watched(conn, _SPILLER, tmp_path / "spill")
    conn.close()
    assert err is not None, "a query spilling past the cap was allowed through"
    assert "offload" in str(err), (
        f"failed for some other reason than the spill cap: {str(err).splitlines()[0]}"
    )
    # The point of the cap is the bytes on disk, so assert the bytes on
    # disk. One block of slack: the offload that FAILS is counted before
    # it is refused.
    assert peak <= _TINY_CAP + 2**20, f"spilled {peak} B past a {_TINY_CAP} B cap"


@pytest.mark.skipif(os.environ.get("HYXLAB_SKIP_SLOW"), reason="HYXLAB_SKIP_SLOW")
def test_the_shipped_cap_lets_the_same_query_through(tmp_path):
    """The cap we actually ship must not ration work that fits.

    The fixture spills ~121 MiB at a 256 MiB limit; the shipped bound for
    that limit is 8x it. A rung that reddened here would be trading a
    disk risk for a broken replay.
    """
    conn = _fixture(tmp_path)
    mem = parse_size(conn.execute("SELECT current_setting('memory_limit')").fetchone()[0])
    assert mem is not None
    conn.execute(f"SET max_temp_directory_size='{duck_spill_limit(mem, tmp_path)}B'")
    err, peak = _run_watched(conn, _SPILLER, tmp_path / "spill")
    conn.close()
    assert err is None, f"the shipped cap refused a query that fits: {err}"
    assert peak > 0, "fixture stopped spilling — re-size it before trusting this"
