"""Every DuckDB attach is sized by the cgroup it runs in, not by host RAM.

EXP-1374, the rung above `test_sidecar_discipline.py`. That guard made
every attach spill into a directory of its own. It says nothing about
WHEN an attach spills, and DuckDB decides that from `memory_limit` —
which it derives from PHYSICAL memory, 80% of `/proc/meminfo`. A capped
systemd service does not have physical memory. `hyxlab-stream.service`
declares `MemoryMax=2G` on a 60 GiB box, so DuckDB opened the 15.7 GB
stream archive believing it had 48.2 GiB, and the boundary it actually
had answered with SIGKILL instead of a spill.

**The measurement is the premise, so it is a test here and not a memory**
(`test_the_default_limit_is_host_ram_and_not_the_cgroup_cap`). If DuckDB
ever reads the cgroup itself, that reddens and this whole file can go,
instead of outliving its reason.

WHAT WAS MEASURED 2026-08-26. The daemon's own startup query — the
`last_recv_ts` read that bounds a `seq_reset` gap — peaked at 2899 MB RSS
in 1.55 s against a 2048 MB cap. `hyxlab-stream` was OOM-killed 17 s into
the promote restart, one second after `kalshi-books: 614 open tickers`,
which is where that read happens; the successor recorded a 2 GiB
`MemoryPeak` for the same startup and lived. Under a pinned limit the
identical query returns the identical answer, bounded (1 GiB -> 1126 MB,
512 MB -> 654 MB, 256 MB -> 415 MB) and FASTER (0.27 s), spilling zero
bytes at every one of them. Nothing needed 2.8 GB — a streaming aggregate
keeps whatever the buffer manager is told it may keep, so the limit is
not a budget the work fits into, it IS the footprint.

THE GUARD IS MECHANICAL, NOT A JUDGEMENT. Same shape as the rung below,
and for the same reason — "this query is small" is a claim about a query
plan, and the plan is the thing that changes:

  DERIVED — every function in the kernel that calls `duckdb.connect`
            also calls `cgroup_memory_limit`. A fourth attach that
            forgets it reddens on entry, by AST.
  CLAIMED — verified by RUNNING under a REAL cgroup: a child launched by
            `systemd-run --scope -p MemoryMax=...` reports a limit
            derived from that cap, not from the box's RAM.

NO CAP MEANS NO CHANGE, and that is asserted too: an uncapped process
keeps DuckDB's own default. This rung tightens only where a cap was
declared and DuckDB could not see it.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from hyxlab.memcap import DUCK_SHARE, FLOOR_BYTES, cgroup_memory_max, duck_memory_limit

ROOT = Path(__file__).resolve().parent.parent

#: The one module allowed to attach DuckDB (enforced by
#: `test_sidecar_discipline.py`), and so the one place the limit can be
#: applied unbypassably.
KERNEL = ROOT / "hyxlab" / "store.py"
LIMIT_CALL = "cgroup_memory_limit"


# -- the premise ----------------------------------------------------------


def test_the_default_limit_is_host_ram_and_not_the_cgroup_cap():
    """DuckDB sizes itself from `/proc/meminfo`. That is the whole bug.

    Asserted, not remembered: the day DuckDB reads `memory.max` itself,
    this reddens and `hyxlab.memcap` has no reason to exist.
    """
    total = None
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1]) * 1024
            break
    assert total, "no MemTotal — cannot state the premise"
    with duckdb.connect() as conn:
        raw = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    got = _parse_size(raw)
    # DuckDB's documented default is 80% of physical memory.
    assert 0.75 * total <= got <= 0.85 * total, (
        f"default memory_limit {raw} is not ~80% of host RAM {total}B —"
        " DuckDB may now be cgroup-aware; re-read hyxlab/memcap.py"
    )


def _parse_size(raw: str) -> int:
    """'48.2 GiB' -> bytes."""
    num, _, unit = raw.strip().partition(" ")
    scale = {"B": 1, "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}[unit or "B"]
    return int(float(num) * scale)


# -- derived half ---------------------------------------------------------


def test_every_kernel_attach_also_sets_the_memory_limit():
    """A new attach in the kernel cannot forget the cap.

    The repo's only `duckdb.connect` calls live in `hyxlab/store.py`
    (`test_sidecar_discipline.py` holds that line). Each of the functions
    making one must apply the limit in the same body — which is what
    makes this unbypassable rather than a convention.
    """
    tree = ast.parse(KERNEL.read_text())
    attachers: dict[str, bool] = {}

    class V(ast.NodeVisitor):
        def _scoped(self, node):
            names, calls = set(), set()

            class Inner(ast.NodeVisitor):
                def visit_Call(self, c):
                    f = c.func
                    if (
                        isinstance(f, ast.Attribute)
                        and f.attr == "connect"
                        and getattr(f.value, "id", "") == "duckdb"
                    ):
                        names.add("attach")
                    if isinstance(f, ast.Name):
                        calls.add(f.id)
                    self.generic_visit(c)

            for child in node.body:
                Inner().visit(child)
            if "attach" in names:
                attachers[node.name] = LIMIT_CALL in calls
            self.generic_visit(node)

        visit_FunctionDef = visit_AsyncFunctionDef = _scoped

    V().visit(tree)
    assert attachers, f"no duckdb.connect found in {KERNEL} — has the kernel moved?"
    missing = sorted(n for n, ok in attachers.items() if not ok)
    assert not missing, (
        f"{KERNEL.name}: these attach DuckDB without {LIMIT_CALL}(): {missing}."
        " Every connection gets the cgroup's limit, not the box's RAM."
    )


# -- the derivation -------------------------------------------------------


def test_the_tightest_ancestor_wins_not_the_leaf():
    """A leaf with no cap of its own still inherits its slice's.

    Reading only the leaf would report 'unlimited' for a process the
    kernel will happily kill.
    """
    import tempfile

    import hyxlab.memcap as memcap

    def build(root: Path, chain: dict[str, str]) -> None:
        for rel, val in chain.items():
            d = root / rel.lstrip("/")
            d.mkdir(parents=True, exist_ok=True)
            (d / "memory.max").write_text(val + "\n")

    with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as td:
        root = Path(td)
        build(root, {"/": "max", "/a": "4000", "/a/b": "max", "/a/b/c": "9000"})
        mp.setattr(memcap, "_CGROUP_ROOT", root)
        mp.setattr(memcap, "_self_cgroup", lambda: "/a/b/c")
        assert memcap.cgroup_memory_max() == 4000
        # ...and 'max' everywhere means no cap at all.
        build(root, {"/a": "max", "/a/b/c": "max"})
        assert memcap.cgroup_memory_max() is None


def test_no_cgroup_cap_leaves_duckdb_alone():
    """An uncapped box should use its RAM; this rung only tightens."""
    import hyxlab.memcap as memcap

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(memcap, "cgroup_memory_max", lambda: None)
        assert memcap.duck_memory_limit() is None


def test_the_share_is_a_fraction_and_the_floor_never_exceeds_the_cap():
    import hyxlab.memcap as memcap

    with pytest.MonkeyPatch.context() as mp:
        cap = 2 * 1024**3
        mp.setattr(memcap, "cgroup_memory_max", lambda: cap)
        got = memcap.duck_memory_limit()
        assert got == int(cap * DUCK_SHARE)
        assert got < cap, "DuckDB must not be handed the whole cap — see hyxlab/memcap.py"
        # A cap tighter than the floor gets the CAP, never the floor: a
        # limit above the cgroup limit is the bug with a smaller number.
        tiny = FLOOR_BYTES // 4
        mp.setattr(memcap, "cgroup_memory_max", lambda: tiny)
        assert memcap.duck_memory_limit() == tiny


def test_the_live_process_agrees_with_its_own_cgroup():
    """Whatever this box reports, the two functions must not disagree."""
    cap = cgroup_memory_max()
    got = duck_memory_limit()
    if cap is None:
        assert got is None
    else:
        assert got is not None and got <= cap


# -- claimed half: verified by RUNNING under a real cgroup ----------------


_CHILD = """
import sys
sys.path.insert(0, {root!r})
from hyxlab.store import connect_retry
conn = connect_retry({db!r}, read_only=False)
print(conn.execute("SELECT current_setting('memory_limit')").fetchone()[0])
"""


def _systemd_run_works() -> bool:
    if not shutil.which("systemd-run"):
        return False
    r = subprocess.run(
        ["systemd-run", "--user", "--scope", "-q", "true"],
        capture_output=True,
        timeout=60,
    )
    return r.returncode == 0


@pytest.mark.skipif(not _systemd_run_works(), reason="no usable systemd --user scope on this host")
def test_a_capped_child_is_sized_by_the_cap_and_not_by_the_box(tmp_path):
    """The end of the chain, run rather than reasoned about.

    A real cgroup, a real attach through the real kernel helper. The box
    has tens of GiB; the scope allows 512 MiB; DuckDB must land near half
    of the latter and nowhere near the former.
    """
    cap = 512 * 1024**2
    db = tmp_path / "capped.duckdb"
    src = _CHILD.format(root=str(ROOT), db=str(db))
    r = subprocess.run(
        [
            "systemd-run",
            "--user",
            "--scope",
            "-q",
            "-p",
            f"MemoryMax={cap}",
            sys.executable,
            "-c",
            src,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert r.returncode == 0, f"capped child failed: {r.stderr}"
    got = _parse_size(r.stdout.strip().splitlines()[-1])
    expected = int(cap * DUCK_SHARE)
    assert abs(got - expected) <= expected * 0.05, (
        f"child under MemoryMax={cap} reports memory_limit {got}B,"
        f" expected ~{expected}B — the cgroup cap did not reach DuckDB"
    )
    assert got < cap
