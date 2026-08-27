"""Every DuckDB attach in this repo spills into a directory of its own.

EXP-1373, the rung below `test_owned_db_discipline.py`. That guard made
the two sim-side databases OWNED — one writer each, holding
`<db>.owner.lock` for its whole life. It says nothing about the files
written NEXT TO a database, and the biggest of them is not written by us
at all: DuckDB spills to disk when a query exceeds `memory_limit`, and
with no `temp_directory` set an on-disk database spills to `<db>.tmp` —
one directory shared by every process that opens the file, holding temp
files whose names carry no pid (`duckdb_temp_storage_DEFAULT-0.tmp`).

**The measurement is the premise, so it is a test here and not a memory**
(`test_a_shared_spill_directory_does_not_contend_it_corrupts`): two
processes sorting past their memory limit into ONE directory failed in
every trial — SIGSEGV, and
`InvalidInputException: Invalid unicode (byte sequence mismatch) detected
in segment statistics update`, which is one process reading the other's
blocks AS ITS OWN DATA. The identical pair pointed at two directories
succeeded, both, every time. If DuckDB ever makes a shared directory
safe, this reddens instead of outliving its reason.

WHY NO LOCK COVERED IT. The owner lock is file-scoped ON PURPOSE, so a
side run against a copied database is not blocked by the daemon — and a
READER takes no owner lock at all. Two readers of one archive are
legitimate and always will be; what must not be shared is their scratch.
`simulator.shadow` had pinned `temp_directory` to the constant
`data/duckspill-shadow`, and `simulator.run_l2` — a by-hand CLI — reads
the same archive through the same `stream_conn`, so an L2 replay run
while the shadow daemon was live pointed two spilling processes at one
directory BY DESIGN.

THE GUARD IS MECHANICAL, NOT A JUDGEMENT. "This query is too small to
spill" is a claim about a query plan, and the plan is the thing that
changes; so no site is exempted for being small. Instead every attach
goes through the kernel — `connect_retry`/`open_retry`/`Store`, or
`hyxlab.store.duck_connect` for the sites that hand-roll a retry budget,
degrade on error, or own the file — and each gets `<db>.tmp/pid-<pid>`.
Two halves, because either alone leaves the question open:

  DERIVED — nothing outside `hyxlab/store.py` calls `duckdb.connect`,
            and no module anywhere names a spill directory. A new attach
            or a new constant reddens on entry.
  CLAIMED — verified by RUNNING, not by reading: two processes on ONE
            database report two directories, and the reaper removes an
            abandoned one while leaving a live one alone.

Reaping is by flock and not by a pid heuristic: a reaper that WINS
`<dir>.owner.lock` has proved the owner is gone, because the kernel
releases an flock on process death. A pid check would have to guess
about pid reuse.
"""

from __future__ import annotations

import ast
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hyxlab.lockid import holder_path, owner_lock_path
from hyxlab.scratch import duck_scratch_dir, reap_stale_scratch, scratch_root
from hyxlab.streamstore import StreamStore

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("collector", "simulator", "strategies", "hyxlab")

#: The one module allowed to attach DuckDB directly. Everything else goes
#: through it, which is what makes private spill unbypassable.
KERNEL = "hyxlab/store.py"

#: The one site allowed to name a spill directory, and the exact form it
#: must use — parameterised, so the value can only come from
#: `hyxlab.scratch.duck_scratch_dir` at run time.
SPILL_SITE = "hyxlab/store.py::private_spill"
SPILL_SQL = "SET temp_directory = ?"


# -- derived half ---------------------------------------------------------


def _sites(tree: ast.AST, rel: str, want: str) -> dict[str, int]:
    """`rel::qualname` -> line, for every call named `want` in `tree`."""
    found: dict[str, int] = {}
    stack: list[str] = []

    class V(ast.NodeVisitor):
        def _scoped(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

        def visit_Call(self, node):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "connect"
                and getattr(f.value, "id", "") == want
            ):
                found.setdefault(f"{rel}::{'.'.join(stack)}", node.lineno)
            self.generic_visit(node)

    V().visit(tree)
    return found


def _docstrings(tree: ast.AST) -> set[int]:
    """Node ids of bare string expressions — docstrings and comment
    strings. Prose that NAMES the hazard is how the hazard stays
    understood; only strings the interpreter acts on are code."""
    return {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, (ast.Constant, ast.JoinedStr))
    }


def _code_strings(tree: ast.AST, rel: str) -> dict[str, list[str]]:
    """`rel::qualname` -> every non-docstring string the module evaluates.

    An f-string is reported as its literal segments joined by `{}`, so an
    interpolated path can never pass for a parameterised one.
    """
    docs = _docstrings(tree)
    found: dict[str, list[str]] = {}
    stack: list[str] = []

    def _text(node) -> str | None:
        if id(node) in docs:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                p.value if isinstance(p, ast.Constant) else "{}" for p in node.values
            )
        return None

    class V(ast.NodeVisitor):
        def _scoped(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

        def generic_visit(self, node):
            txt = _text(node)
            if txt is not None:
                found.setdefault(f"{rel}::{'.'.join(stack)}", []).append(txt)
            super().generic_visit(node)

    V().visit(tree)
    return found


def _strings_naming(tree: ast.AST, rel: str, needle: str) -> dict[str, str]:
    """`rel::qualname` -> the string, for code strings containing `needle`."""
    return {
        k: t
        for k, texts in _code_strings(tree, rel).items()
        for t in texts
        if needle in t
    }


#: `temp_directory` as a WORD. The sibling setting
#: `max_temp_directory_size` contains it as a substring and names a SIZE,
#: not a directory (`hyxlab/store.py::spill_cap`, EXP-1375) — matching it
#: here would report the disk bound as a hard-coded spill path. The
#: boundary is what keeps the scan tight; a directory still cannot be
#: named anywhere, because naming one requires this word.
_TEMP_DIR_RE = re.compile(r"(?<![A-Za-z0-9_])temp_directory")


def _temp_dir_sites(tree: ast.AST, rel: str) -> dict[str, str]:
    return {
        k: t
        for k, texts in _code_strings(tree, rel).items()
        for t in texts
        if _TEMP_DIR_RE.search(t)
    }


def _walk(fn) -> dict:
    out: dict = {}
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            out.update(fn(ast.parse(path.read_text()), rel))
    return out


def test_only_the_kernel_attaches_duckdb_directly():
    """A bare `duckdb.connect` gets the SHARED default `<db>.tmp`."""
    outside = {k: v for k, v in _walk(lambda t, r: _sites(t, r, "duckdb")).items()
               if not k.startswith(KERNEL + "::")}
    assert not outside, (
        "raw duckdb.connect outside the kernel, so it spills into the shared"
        f" default <db>.tmp: {sorted(outside)} — use hyxlab.store.duck_connect"
        " (or connect_retry/open_retry/Store), which is the same attach with"
        " a process-private temp_directory"
    )


def test_no_module_names_a_spill_directory():
    """A constant path is a shared path, whatever it is named."""
    sites = _walk(_temp_dir_sites)
    assert set(sites) == {SPILL_SITE}, (
        "a spill directory may only be named at the kernel chokepoint,"
        f" which is the only place it can be made process-private: {sorted(sites)}"
    )
    assert sites[SPILL_SITE] == SPILL_SQL, (
        "the chokepoint must PARAMETERISE the directory — a literal or an"
        f" f-string here is a shared directory again: {sites[SPILL_SITE]!r}"
    )


def test_the_derived_scan_can_actually_see_both_violations():
    """A scanner that matches nothing passes forever."""
    src = (
        "import duckdb\n"
        "def opener():\n"
        "    c = duckdb.connect(p)\n"
        "    c.execute(\"SET temp_directory = 'data/duckspill-shadow'\")\n"
        "    c.execute(f'SET temp_directory = {d}')\n"
        "def clean():\n"
        "    return connect_retry(p)\n"
    )
    tree = ast.parse(src)
    assert set(_sites(tree, "x.py", "duckdb")) == {"x.py::opener"}
    # ...and a docstring that merely DESCRIBES the setting is not a site.
    prose = ast.parse('def f():\n    """sets temp_directory somewhere"""\n')
    assert _temp_dir_sites(prose, "x.py") == {}
    assert set(_temp_dir_sites(tree, "x.py")) == {"x.py::opener"}
    # An f-string must never be mistaken for the parameterised form.
    assert _temp_dir_sites(tree, "x.py")["x.py::opener"] != SPILL_SQL


def test_the_retired_shared_constant_is_gone_from_the_packages():
    """`data/duckspill-shadow` was THE shared directory. Repointing it
    would have kept the bug; it is deleted, not moved."""
    hits = _walk(lambda t, r: _strings_naming(t, r, "duckspill-shadow"))
    assert not hits, (
        f"the shared spill constant is still live code at {sorted(hits)} —"
        " the prose that explains why it is gone is welcome; a string the"
        " interpreter can reach is not"
    )


def test_the_other_sidecars_stay_derived_from_their_resource(tmp_path):
    """The three sidecar families, each named after what it belongs to —
    which is what makes "who may write this" answerable at all."""
    db = tmp_path / "hyxstream.duckdb"
    store = StreamStore(db)
    assert store._spill_path == tmp_path / "hyxstream.duckdb.spill.jsonl"
    assert scratch_root(db) == f"{db}.tmp"
    assert holder_path("data/writer.lock") == "data/writer.lock.holder"


# -- claimed half: verified by running ------------------------------------

_SPILLER = """
import resource, sys, duckdb
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # the crash IS the result
tmp, db = sys.argv[1], sys.argv[2]
c = duckdb.connect(db)
c.execute("SET temp_directory = ?", [tmp])
c.execute("SET memory_limit='200MiB'")
c.execute("SET threads=2")
c.execute("SET max_temp_directory_size='8GiB'")
try:
    c.execute("CREATE OR REPLACE TABLE t AS SELECT i, repeat('x',300) s"
              " FROM range(1500000) tbl(i) ORDER BY hash(i)")
    print("OK", c.execute("SELECT count(*) FROM t").fetchone()[0])
except Exception as e:
    print("FAIL", type(e).__name__)
"""


def _spill_pair(tmp_path: Path, tag: str, shared: bool) -> list[tuple[int, str]]:
    """Two spilling processes; `(returncode, stdout)` each."""
    script = tmp_path / "spiller.py"
    script.write_text(_SPILLER)
    dirs = [tmp_path / f"{tag}-shared"] * 2 if shared else [
        tmp_path / f"{tag}-a", tmp_path / f"{tag}-b"
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(d), str(tmp_path / f"{tag}-{i}.duckdb")],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for i, d in enumerate(dirs)
    ]
    return [(p.wait(), p.stdout.read().strip()) for p in procs]


def test_a_shared_spill_directory_does_not_contend_it_corrupts(tmp_path):
    """THE PREMISE OF THIS WHOLE FILE, re-measured rather than remembered.

    Stated as "the control is clean AND the shared pair is not", because
    a shared-pair failure on a machine where the control also fails would
    prove nothing about sharing.
    """
    control = _spill_pair(tmp_path, "control", shared=False)
    assert all(rc == 0 and out.startswith("OK") for rc, out in control), (
        f"the control pair must survive or the experiment says nothing: {control}"
    )
    trials = [_spill_pair(tmp_path, f"shared{i}", shared=True) for i in range(3)]
    assert not all(
        all(rc == 0 and out.startswith("OK") for rc, out in t) for t in trials
    ), (
        "three pairs of DuckDB processes shared one temp_directory and ALL"
        f" survived: {trials}. If DuckDB now isolates temp files per process,"
        " this file's premise is gone — delete the guard rather than keep it"
        " for a reason that stopped being true."
    )


_REPORTER = """
import sys, time
sys.path.insert(0, "ROOT_PATH")
from hyxlab.store import connect_retry
c = connect_retry(sys.argv[1], read_only=True)
print(c.execute("SELECT current_setting('temp_directory')").fetchone()[0], flush=True)
time.sleep(float(sys.argv[2]))
""".replace("ROOT_PATH", str(ROOT))


def _reporters(tmp_path: Path, n: int, hold: float) -> list[subprocess.Popen]:
    db = tmp_path / "shared.duckdb"
    if not db.exists():
        StreamStore(db)  # a real schema, so a read-only attach succeeds
    script = tmp_path / "reporter.py"
    script.write_text(_REPORTER)
    return [
        subprocess.Popen(
            [sys.executable, str(script), str(db), str(hold)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(n)
    ]


def test_two_processes_on_one_database_get_two_spill_directories(tmp_path):
    """Two READERS of one archive are legitimate — shadow polls it while
    an operator replays L2 out of it — so this is the case that must
    hold, and it is checked by running the real kernel attach."""
    procs = _reporters(tmp_path, 2, hold=1.5)
    try:
        dirs = [p.stdout.readline().strip() for p in procs]
    finally:
        for p in procs:
            p.wait(timeout=30)
    assert len(set(dirs)) == 2, dirs
    root = scratch_root(tmp_path / "shared.duckdb")
    assert all(d.startswith(root + "/pid-") for d in dirs), dirs
    for p, d in zip(procs, dirs, strict=True):
        assert d.endswith(f"pid-{p.pid}"), (d, p.pid)


def test_a_clean_exit_leaves_no_scratch_behind(tmp_path):
    procs = _reporters(tmp_path, 1, hold=0.0)
    d = procs[0].stdout.readline().strip()
    procs[0].wait(timeout=30)
    assert not Path(d).exists(), f"{d} survived a clean exit"
    assert not Path(owner_lock_path(d)).exists()


def test_the_reaper_spares_a_live_owner_and_takes_an_abandoned_one(tmp_path):
    """The whole point of locking the directory rather than pid-checking
    it: an owner that is still running must never be reaped, and one that
    was SIGKILLed (no atexit hook runs) must be."""
    procs = _reporters(tmp_path, 1, hold=30.0)
    proc = procs[0]
    d = proc.stdout.readline().strip()
    db = tmp_path / "shared.duckdb"
    assert Path(d).is_dir()

    assert reap_stale_scratch(db) == [], "a live owner's scratch was reaped"
    assert Path(d).is_dir()

    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)
    # The dead holder's record survives its process; liveness is re-derived
    # from the lock, not from the file.
    assert Path(d).is_dir()
    assert reap_stale_scratch(db) == [d]
    assert not Path(d).exists()
    assert not Path(owner_lock_path(d)).exists()
    assert not Path(holder_path(owner_lock_path(d))).exists()


def test_a_second_call_in_one_process_reuses_its_directory(tmp_path):
    """`StreamStore.flush` reopens every ~15 s. A new directory per open
    would be a leak with a lock file each; the same directory is right,
    because one process cannot corrupt itself across sequential opens."""
    db = tmp_path / "reuse.duckdb"
    first = duck_scratch_dir(db)
    assert first == str(Path(scratch_root(db)) / f"pid-{os.getpid()}")
    t0 = time.perf_counter()
    for _ in range(200):
        assert duck_scratch_dir(db) == first
    assert time.perf_counter() - t0 < 1.0, "the reopen path must not sweep"


def test_an_in_memory_database_has_no_scratch_to_make_private(tmp_path):
    """DuckDB gives an in-memory database no spill directory of its own,
    so there is nothing to divide; `None` tells the caller to leave the
    setting alone rather than invent a path next to a file that does not
    exist."""
    assert duck_scratch_dir(":memory:") is None
    assert duck_scratch_dir("") is None


@pytest.mark.parametrize("a,b", [("data/a.duckdb", "data/b.duckdb")])
def test_two_databases_do_not_share_a_scratch_root(a, b):
    """Per-database as well as per-process: one process holding two
    connections at once would otherwise collide with itself."""
    assert scratch_root(a) != scratch_root(b)
