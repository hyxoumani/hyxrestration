"""Every unit that opens a DuckDB must declare what it may not exceed.

2026-09-25 10:00Z: `hyxlab-qa.service` took **48.1 GiB anon** and the
kernel's GLOBAL OOM killer answered by shooting **21 processes across 10
unrelated services** on this box -- prometheus, alertmanager, redis,
argocd, node_exporter, local-path-provisioner, gpg-agent. QA itself was
killed LAST, 71 seconds after it invoked the OOM killer, because the k8s
besteffort pods sharing the box advertise `oom_score_adj=1000` and are
chosen first regardless of size, at 10-27 MB each.

`hyxlab.memcap` exists to prevent exactly this. It sizes DuckDB's
`memory_limit` from the cgroup rather than from host RAM (EXP-1374,
2026-08-26, after `hyxlab-stream` was killed by its own 2G cap), and
`hyxlab.store` applies it at every connect chokepoint. It was working.
`tests/test_memcap_discipline.py` proves it in 10 tests, all green, and
they were green on 09-25 too.

THEY WERE GREEN BECAUSE THE MECHANISM IS CORRECT AND WAS SWITCHED OFF.
`duck_memory_limit()` returns None when no cgroup cap binds, deliberately
-- "an uncapped box keeps DuckDB's own default". `hyxlab-qa.service`
declared no `MemoryMax`, no ancestor slice declared one, so QA opened the
26 GB stream archive believing it had 48.2 GiB and helped itself.
`test_no_cgroup_cap_leaves_duckdb_alone` even certifies that no-op as
correct -- it asks whether the OFF state behaves, never whether
production is IN it. A mechanism's tests check the mechanism; nothing
checked its PRECONDITION, and the precondition lives in a file the Python
tests never open.

So this file tests the units, not the mechanism: if a unit's entrypoint
can reach a DuckDB attach, it must declare `MemoryMax` -- or be named in
`UNCAPPED` with the peak it was measured at and the reason it is still
open. The unit set is DISCOVERED from scripts/systemd/, and the reach-a-
DuckDB question is answered by walking the entrypoint's real import
graph, because an enumeration cannot fail when a new unit appears -- the
house rule that `test_lint_scope.py`, `test_shell_lint.py` and
`test_systemd_verify.py` already enforce for their own file sets.

Measured peaks (systemd's own `Consumed ... memory peak`, whole journal):
the one batch unit that DID declare a cap, `hyxlab-divergence` at 8G,
runs at p50 1.9G / max 4.2G. Every uncapped batch unit runs at p50
11-18G. They are not doing more work; they are being handed a bigger
buffer and filling it.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT_DIR = ROOT / "scripts" / "systemd"

#: Repo packages worth walking into. An import outside these is a
#: third-party leaf: `duckdb` itself is the one we are looking for, and
#: nothing else can reach a hyxlab attach without going through them.
PACKAGES = ("collector", "simulator", "strategies", "hyxlab")

#: What "opens a DuckDB" means, as module names an import can name.
DUCK_MARKERS = ("duckdb", "hyxlab.store")

#: Units whose entrypoint reaches DuckDB and which still declare no cap,
#: each with the peak MEASURED from its own journal on 2026-09-25 and the
#: reason it is still open. This is a debt list, not an exemption class:
#: an entry must name a real unit that is really uncapped (see
#: `test_the_debt_list_does_not_outlive_its_debt`), so paying one off
#: deletes its line and cannot be forgotten.
#:
#: They are not capped in the same pass that capped QA because a cap that
#: is too TIGHT turns an OutOfMemoryException into a failed collection
#: cycle, and these three are the 24/7 capture path. Each needs its own
#: measured floor, the way QA got one, before it gets a number.
UNCAPPED = {
    "hyxlab-poly-sweep.service": "p50 17.7G / max 30.1G over 19 runs; ~15h "
    "multi-hour writer, floor unmeasured",
    "hyxlab-sweep.service": "p50 11.8G / max 20.5G over 18 runs; floor unmeasured",
    "hyxlab-tradepass.service": "p50 10.7G / max 12.1G over 19 runs; floor unmeasured",
    "hyxlab-breadth.service": "p50 0.29G / max 4.70G over 5445 runs; "
    "fires every 5 min, floor unmeasured",
    "hyxlab-collect.service": "p50 0.30G / max 0.53G over 5458 runs; "
    "smallest margin to a real cap, floor unmeasured",
    "hyxlab-signals.service": "p50 0.21G / max 0.33G over 19 runs; floor unmeasured",
    "hyxlab-backup.service": "no journal samples yet; floor unmeasured",
}

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)$")
_SCALE = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _units():
    return {p.name: p.read_text() for p in sorted(UNIT_DIR.glob("*.service"))}


def _field(text, key):
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            out.append(v.strip())
    return out


def parse_size(v: str) -> int | None:
    """systemd's size syntax -> bytes. None for `infinity` or junk."""
    m = _SIZE_RE.match(v.strip().rstrip("B"))
    if not m:
        return None
    return int(float(m.group(1)) * _SCALE[m.group(2)])


def entrypoint(text: str) -> str | None:
    """The `python -m <module>` an ExecStart runs, or None.

    None for a unit that shells out (`hyxlab-autoloop` runs a script that
    drives Claude, not a query), which is exactly right: the import walk
    below can say nothing about it, so it must not be asked to.
    """
    for ex in _field(text, "ExecStart"):
        m = re.search(r"\s-m\s+([\w.]+)", ex)
        if m:
            return m.group(1)
    return None


def _module_file(mod: str) -> Path | None:
    base = ROOT / Path(*mod.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _imports(path: Path) -> set[str]:
    """Absolute module names imported by one file."""
    out: set[str] = set()
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def reaches_duckdb(mod: str) -> bool:
    """Does `mod`'s transitive import graph name a DuckDB attach?

    Walks only into repo packages; a third-party import is a leaf whose
    name is still inspected, so `import duckdb` anywhere in the graph is
    caught at the module that does it.
    """
    seen: set[str] = set()
    queue = [mod]
    while queue:
        cur = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        f = _module_file(cur)
        if f is None:
            continue
        for name in _imports(f):
            if any(name == d or name.startswith(d + ".") for d in DUCK_MARKERS):
                return True
            if name.startswith(PACKAGES) and name not in seen:
                queue.append(name)
    return False


def duckdb_units() -> dict[str, str]:
    return {n: t for n, t in _units().items() if (e := entrypoint(t)) and reaches_duckdb(e)}


def test_the_walk_finds_the_unit_that_actually_died():
    """Guard the discovery itself: a walk that found nothing would pass
    every assertion below while checking nothing at all -- the vacuous
    green that mistakes #82/#85/#86 are all instances of."""
    found = duckdb_units()
    assert "hyxlab-qa.service" in found, (
        "the import walk no longer reaches DuckDB from collector.qa -- the unit "
        f"that global-OOMed the box on 2026-09-25. Found: {sorted(found)}"
    )
    assert len(found) >= 8, f"only {len(found)} DuckDB units discovered: {sorted(found)}"


def test_autoloop_is_not_claimed_by_a_walk_that_cannot_see_it():
    """`hyxlab-autoloop` runs a shell script. Its 45.2G peak is Claude,
    not a query, and the import walk has no opinion about it -- so it
    must be neither capped-by-this-rule nor listed as DuckDB debt."""
    assert "hyxlab-autoloop.service" not in duckdb_units()
    assert "hyxlab-autoloop.service" not in UNCAPPED


def test_every_duckdb_unit_declares_a_memory_cap():
    """The precondition `hyxlab.memcap` needs and cannot check for itself."""
    missing = [
        n
        for n, t in duckdb_units().items()
        if not _field(t, "MemoryMax") and n not in UNCAPPED
    ]
    assert not missing, (
        f"{missing} open a DuckDB and declare no MemoryMax, so "
        "`hyxlab.memcap.duck_memory_limit()` returns None for them and DuckDB "
        "sizes itself from host RAM (80% = 48.2 GiB here). That is how "
        "hyxlab-qa took 48.1 GiB and global-OOMed 21 processes on 2026-09-25. "
        "Cap it against a MEASURED floor, or add it to UNCAPPED with its "
        "measured peak and the reason."
    )


def test_the_debt_list_does_not_outlive_its_debt():
    """An UNCAPPED entry that is capped, or gone, or was never a DuckDB
    unit, is a stale claim -- and a stale exemption silently un-tests
    whatever later takes its name."""
    found = duckdb_units()
    for name, reason in UNCAPPED.items():
        assert name in found, (
            f"UNCAPPED names {name}, which is not a DuckDB unit (renamed, "
            "deleted, or its entrypoint no longer reaches an attach). Delete it."
        )
        assert not _field(found[name], "MemoryMax"), (
            f"{name} now declares MemoryMax but is still listed as uncapped debt. "
            "Delete its UNCAPPED line."
        )
        assert reason.strip(), f"UNCAPPED[{name}] must say why it is still open"


def test_a_declared_cap_leaves_duckdb_more_than_the_floor():
    """A cap is only protection if the share it yields is a usable limit.
    Below `2 * FLOOR_BYTES` the DUCK_SHARE split lands on the floor, and
    the unit is then capped tighter than the buffer manager it hosts --
    which trades an OOM kill for a guaranteed OutOfMemoryException."""
    from hyxlab.memcap import DUCK_SHARE, FLOOR_BYTES

    for name, text in duckdb_units().items():
        for raw in _field(text, "MemoryMax"):
            n = parse_size(raw)
            if n is None:
                continue
            assert n * DUCK_SHARE >= FLOOR_BYTES, (
                f"{name} caps at {raw}, leaving DuckDB "
                f"{int(n * DUCK_SHARE)}B -- at or under the {FLOOR_BYTES}B floor"
            )
