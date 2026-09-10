"""No sidecar constant may reach a test unredirected.

`hyxrestration-stable/data` is a SYMLINK to the dev repo's `data/`, so a
test that drives real code with the repo as its cwd does not leave litter
in a scratch tree -- it appends to production's telemetry, which the QA
checks then read as evidence. Three have done it:

  EXP-1333  kalshi's 429 header sink, fixed with one hand-written
            monkeypatch in conftest
  2026-09-10  `collector.spool`, on the FIRST suite run after it landed:
            eight fabricated `spooled`/`drained` events into the file
            `qa_collect_spool` counts, plus two cycle payloads

THE OBVIOUS GUARD DOES NOT WORK HERE, and that is why this one is static.
Snapshotting `data/` around each test and failing on a change was written
first, and it is unsound for the same reason the leak is dangerous: the
LIVE collector writes those files every five minutes from the other end of
the symlink, so the check fails on production doing its job. There is no
stat that separates our append from its append.

So the invariant moves from the files to the paths: every module-level
constant under `data/` that is not a database and not a lock is a sidecar
this repo writes, and `conftest.SIDECAR_CONSTANTS` must redirect it. The
set is DERIVED from the source, never listed here -- an enumeration cannot
fail when someone adds the twelfth sidecar, which is precisely the moment
it needs to.

The two exclusions are covered elsewhere and are not judgement calls:
`*.duckdb` by `test_owned_db_discipline.py` + `test_connect_discipline.py`
(and its `.tmp` scratch by `conftest._no_cwd_rooted_duckdb_scratch`), and
`data/writer.lock` by `hyxlab.lockid` -- a test that took the production
writer lock would stall the collector, not corrupt a record, and every
site that opens it already resolves the path at call time.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from tests.conftest import SIDECAR_CONSTANTS

REPO = Path(__file__).resolve().parent.parent
PACKAGES = ("collector", "hyxlab", "simulator", "strategies")


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPO).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def discovered() -> set[tuple[str, str]]:
    """Every module-level `data/`-rooted constant that is a sidecar."""
    found: set[tuple[str, str]] = set()
    for pkg in PACKAGES:
        for f in sorted((REPO / pkg).rglob("*.py")):
            for node in ast.parse(f.read_text()).body:
                if not isinstance(node, ast.Assign | ast.AnnAssign):
                    continue
                value = node.value
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                v = value.value
                if not v.startswith("data/") or v.endswith((".duckdb", ".lock")):
                    continue
                targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
                for t in targets:
                    if isinstance(t, ast.Name):
                        found.add((_module_name(f), t.id))
    return found


def test_every_sidecar_constant_is_redirected():
    missing = sorted(discovered() - set(SIDECAR_CONSTANTS))
    assert not missing, (
        f"sidecar constants no test redirects: {missing}. Add them to "
        "tests/conftest.py::SIDECAR_CONSTANTS -- the dev repo's data/ IS "
        "production's, so an unredirected default writes live telemetry."
    )


def test_the_redirect_list_has_no_dead_entries():
    """A constant that was renamed or deleted leaves an entry that redirects
    nothing, and the fixture would still pass -- silently protecting a name
    that no longer exists while the real one leaks."""
    stale = sorted(set(SIDECAR_CONSTANTS) - discovered())
    assert not stale, f"SIDECAR_CONSTANTS entries that no longer exist in the source: {stale}"


def test_the_fixture_actually_moves_the_paths(tmp_path):
    """The autouse fixture is in force for THIS test too, so every constant
    it names must already read as a redirected path. Verifying the list is
    complete says nothing about whether the redirect ran."""
    for mod_name, attr in SIDECAR_CONSTANTS:
        value = Path(getattr(importlib.import_module(mod_name), attr))
        assert value.is_absolute(), (mod_name, attr, str(value))
        assert not str(value).startswith(str(REPO / "data")), (mod_name, attr, str(value))
