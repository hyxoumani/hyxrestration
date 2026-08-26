"""Every read-only attach to a shared DuckDB file is enumerated here.

The 2026-07-12 audit escalated a lock-race that fired three times in one
day to a rule: a read-only connect to a database another process may
hold must not be bare. It stayed UNENFORCED, and on 2026-08-25 a fourth
and a fifth instance surfaced (`simulator.divergence`'s archive attach,
`collector.trades_backfill.pending_markets`) — both in files that
already applied the rule elsewhere. A grep cannot enforce it: most raw
connects are legitimate (a writer owning its own file, a backup copying
one, a CLI that is the sole writer), so the rule needs an allowlist with
a written-down reason per site, not a ban.

That is what this file is. A site is invisible to the guard if it goes
through `connect_retry`/`open_retry`; otherwise it must appear below
with a disposition. `hyxlab.store.duck_connect` — the raw attach every
non-helper site now uses, so that its spill directory is process-private
(EXP-1373) — counts as bare here ON PURPOSE: it fixes where a query
spills and says nothing about who else holds the file. Two dispositions are acceptable:

  RETRY   — hand-rolled wait loop, because the site needs a budget or a
            diagnostic the helper does not offer.
  DEGRADE — catches `duckdb.Error` and continues without the data.
  OWNER   — the process is the file's sole writer; nobody else holds it.

KNOWN LIMIT, PINNED RATHER THAN LEFT SILENT: the scanner matches a
LITERAL `read_only=True`, so a site that forwards a variable is
invisible to it. `test_only_the_helpers_forward_read_only` asserts that
the only three such sites in the repo are inside `hyxlab/store.py` — the
helpers, which forward by construction. If that test reddens, a caller
has learned to hide from this one.

The assertion is set EQUALITY, both directions: adding a site reddens
the suite, and removing one reddens it too, so the enumeration cannot go
stale. Mistakes #34's corollary: a sweep is finished only when the
enumeration is written down.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("collector", "simulator", "strategies", "hyxlab")

#: site -> (disposition, why). Keys are `relpath::qualname`, which is
#: stable under edits inside the function (line numbers are not).
ALLOWED: dict[str, tuple[str, str]] = {
    # -- owner: the process is the sole writer of this file ---------------
    "hyxlab/streamstore.py::StreamStore.counts": (
        "OWNER",
        "only production caller is collector.streamd, which owns the file",
    ),
    # -- hand-rolled retry, each for a reason the helper cannot serve -----
    "collector/backup.py::backup_one": (
        "RETRY",
        "30x2s; must not silently skip a database it was asked to back up",
    ),
    "collector/qa.py::_connect_ro": (
        "RETRY",
        "names the lock holder from the error text, so QA can distinguish a"
        " live writer from an unreachable file instead of crying wolf",
    ),
    "simulator/atlas.py::main": ("RETRY", "15x2s report attach"),
    "simulator/simui/session.py::_connect_ro": (
        "RETRY",
        "8x0.75s — a UI must fail fast, not hang for the helper's 30s",
    ),
    "collector/sweep.py::main": (
        "RETRY",
        "5 attempts then exit nonzero, so systemd records a failed doctor run",
    ),
    # -- degrade: continues without the data ------------------------------
    "simulator/simui/session.py::_try_load_markets": (
        "DEGRADE",
        "UI falls back to bare tickers",
    ),
    "collector/sweep.py::doctor": ("DEGRADE", "prints the archive line without stream counts"),
    "simulator/shadow.py::ShadowRunner._try_load_markets": (
        "DEGRADE",
        "returns None; the poll reuses the previous market map",
    ),
}


#: site -> why. A read-WRITE attach takes DuckDB's exclusive file lock and
#: excludes every reader AND every other writer. That is correct for a
#: process that owns the file and fatal for one that merely reads it: on
#: 2026-07-12 an ad-hoc read-write connect crashed the shadow daemon
#: mid-persist and ended a 1d20h run (mistakes #20). `simulator.run_sim`
#: and `simulator.run_backtest` were doing exactly that against the live
#: archive while only ever calling readers — and `run_backtest` is one of
#: the commands CLAUDE.md tells people to run, so the hazard was not
#: ad-hoc at all. Every site below must OWN the file it opens.
#: EXP-1370 removed two entries from here rather than adding any:
#: `collector/backfill.py::main` and `hyxlab/migrate.py::main` both owned
#: their read-write open on paper while holding it with no writer lock.
#: Both now route through the helpers (a burst, a flock), which makes
#: them invisible to THIS guard by construction — see
#: tests/test_writer_lock_discipline.py, which owns that half.
#: EXP-1372: "owns" was a CLAIM here, not a guarantee — both daemons open
#: their file in BURSTS, so DuckDB's file lock excluded no second copy
#: between them (measured: two StreamStore writers, 35 flushes, zero
#: declines). Each entrypoint now holds `<db>.owner.lock` for its whole
#: life; tests/test_owned_db_discipline.py enumerates and enforces it.
WRITE_ALLOWED: dict[str, str] = {
    "collector/streamd.py::main": "the stream daemon owns hyxstream.duckdb",
    "hyxlab/streamstore.py::StreamStore.__init__": "creates its own schema",
    "hyxlab/streamstore.py::StreamStore.flush": "the daemon's write path",
    "hyxlab/streamstore.py::StreamStore.last_recv_ts": "same connection discipline as flush",
    "hyxlab/streamstore.py::StreamStore.mark_startup_gap": "records the gap it just found",
    "simulator/shadow.py::ShadowLedger.__init__": "the daemon owns hyxshadow.duckdb",
    "simulator/shadow.py::ShadowLedger.persist": "the ledger's write path",
    "simulator/shadow.py::ShadowLedger.set_anchor": "ledger write",
    "simulator/shadow.py::ShadowLedger.start_run": "ledger write",
}


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _attach_mode(node: ast.Call) -> str | None:
    """Classify a duckdb.connect / Store / StreamStore call by how it takes
    the file: "ro" (literal read_only=True), "rw" (literal False or absent),
    "forward" (read_only is a variable — see the known limit above), or None
    if this is not an attach at all. Calls through connect_retry/open_retry
    are not attaches by this definition; that is the point of the helpers."""
    f = node.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
    if name == "connect":
        if not (isinstance(f, ast.Attribute) and getattr(f.value, "id", "") == "duckdb"):
            return None
    elif name == "duck_connect":
        pass  # the kernel's raw attach: private spill, but no retry/degrade
    elif name in ("Store", "StreamStore"):
        if len(node.args) > 1:
            return "ro" if _is_true(node.args[1]) else "rw"
    else:
        return None
    for k in node.keywords:
        if k.arg == "read_only":
            if not isinstance(k.value, ast.Constant):
                return "forward"
            return "ro" if k.value.value is True else "rw"
    return "rw"


def _attach_sites(mode: str) -> dict[str, int]:
    """`relpath::qualname` -> first line, for every attach of this mode."""
    sites: dict[str, int] = {}
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text())
            stack: list[str] = []

            class V(ast.NodeVisitor):
                def _scoped(self, node):
                    stack.append(node.name)
                    self.generic_visit(node)
                    stack.pop()

                visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

                def visit_Call(self, node):
                    if _attach_mode(node) == mode:
                        key = f"{rel}::{'.'.join(stack)}"
                        sites.setdefault(key, node.lineno)
                    self.generic_visit(node)

            V().visit(tree)
    return sites


def readonly_attach_sites() -> dict[str, int]:
    return _attach_sites("ro")


def write_attach_sites() -> dict[str, int]:
    return _attach_sites("rw")


def test_every_readonly_attach_is_enumerated():
    found = set(readonly_attach_sites())
    allowed = set(ALLOWED)
    new = found - allowed
    assert not new, (
        "bare read-only attach with no written-down disposition: "
        + ", ".join(sorted(new))
        + " — use hyxlab.store.connect_retry/open_retry, or add it to ALLOWED"
        " with RETRY/DEGRADE/OWNER and the reason"
    )
    gone = allowed - found
    assert not gone, (
        "ALLOWED names sites that no longer exist: "
        + ", ".join(sorted(gone))
        + " — delete the entries so the enumeration stays honest"
    )


def test_every_disposition_is_one_of_the_three():
    bad = {k: d for k, (d, _) in ALLOWED.items() if d not in ("RETRY", "DEGRADE", "OWNER")}
    assert not bad, bad
    thin = {k: why for k, (_, why) in ALLOWED.items() if len(why) < 10}
    assert not thin, f"a disposition without a real reason is a rubber stamp: {thin}"


def test_the_guard_can_actually_see_a_bare_attach():
    """A guard that matches nothing passes forever. Pin the classifier on
    every shape it must catch, in both directions, and on the calls it must
    ignore — the helpers, whose whole purpose is to be invisible here."""
    src = (
        "import duckdb\n"
        "ro_kw = duckdb.connect(p, read_only=True)\n"
        "ro_store_kw = Store(p, read_only=True)\n"
        "ro_store_positional = Store(p, True)\n"
        "rw_bare = duckdb.connect(p)\n"
        "rw_explicit = duckdb.connect(p, read_only=False)\n"
        "rw_store_bare = Store(p)\n"
        "rw_stream = StreamStore(p)\n"
        "rw_store_positional = Store(p, False)\n"
        "fwd = duckdb.connect(p, read_only=flag)\n"
        "skip_helper_ro = connect_retry(p, read_only=True)\n"
        "skip_helper_rw = open_retry(p)\n"
        "skip_unrelated = requests.connect(p, read_only=True)\n"
    )
    modes: dict[str, str | None] = {}
    for stmt in ast.parse(src).body:
        if isinstance(stmt, ast.Assign):
            modes[stmt.targets[0].id] = _attach_mode(stmt.value)
    assert modes == {
        "ro_kw": "ro",
        "ro_store_kw": "ro",
        "ro_store_positional": "ro",
        "rw_bare": "rw",
        "rw_explicit": "rw",
        "rw_store_bare": "rw",
        "rw_stream": "rw",
        "rw_store_positional": "rw",
        "fwd": "forward",
        "skip_helper_ro": None,
        "skip_helper_rw": None,
        "skip_unrelated": None,
    }


def test_only_the_helpers_forward_read_only():
    """The scanner reads a literal `read_only=True`. Anything forwarding a
    variable escapes it — so pin that the only sites doing so are the
    helpers in hyxlab/store.py, where forwarding IS the job."""
    forwarding: list[str] = []
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if name not in ("connect", "Store", "StreamStore"):
                    continue
                for k in node.keywords:
                    if k.arg == "read_only" and not isinstance(k.value, ast.Constant):
                        forwarding.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert all(s.startswith("hyxlab/store.py:") for s in forwarding), (
        "a non-helper forwards read_only as a variable, which the enumeration"
        f" cannot see: {forwarding}"
    )


def test_every_write_attach_owns_its_file():
    """A pure reader must never take the writer lock on a shared database.
    Same set-equality discipline as the read-only enumeration."""
    found = set(write_attach_sites())
    new = found - set(WRITE_ALLOWED)
    assert not new, (
        "read-write attach by a module that may not own the file: "
        + ", ".join(sorted(new))
        + " — if it only reads, use open_retry(..., read_only=True); if it"
        " owns the file, add it to WRITE_ALLOWED with the reason"
    )
    gone = set(WRITE_ALLOWED) - found
    assert not gone, f"WRITE_ALLOWED names sites that no longer exist: {sorted(gone)}"


def test_the_sim_side_readers_do_not_take_the_writer_lock():
    """Pins the EXP-1368 finding by name rather than only by set difference,
    so a revert reads as what it is instead of as an allowlist edit."""
    for mod in ("simulator/run_sim.py", "simulator/run_backtest.py"):
        assert not [k for k in write_attach_sites() if k.startswith(mod)], (
            f"{mod} reads the live archive; a read-write open takes the lock"
            " that the collector, streamd and the shadow daemon need"
        )
