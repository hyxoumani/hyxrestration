"""A standing report's output directory must not depend on which worktree ran it.

The box runs two checkouts of this repo. The stable one is every systemd
unit's `WorkingDirectory`; the dev one is where an agent types commands.
They share `data/`, `.env` and `.secrets` through symlinks, but each has
its OWN real `reports/` directory -- so a cwd-relative `reports/<x>` path
means the by-hand writer and the scheduled reader are looking at two
different places.

Measured cost before this was fixed (2026-09-11): the divergence report
for shadow run `20260829T191841` was produced by hand in the dev tree on
09-09; `hyxlab-qa` FAILED "unmeasured" against it on 09-10 from the
stable tree, and `hyxlab-divergence --if-new` -- a flag that exists
purely to skip work already done -- re-derived the same 38,143-fill
report at 29min/4.2G on 09-11.

Two things are pinned here:

  * the helper's semantics (`hyxlab.reportdir`), including that an EMPTY
    variable is an unset one, because `Environment=VAR=` is how systemd
    spells "clear this";
  * the class, DERIVED and never enumerated -- every module reachable
    from a unit's ExecStart is parsed, and any relative `reports/`
    literal that is not the one documented per-tree record reddens. That
    is what catches the NEXT report family the day it gets a timer,
    which is the only day the split does damage.
"""

import ast
import re
from pathlib import Path

import pytest

from hyxlab.reportdir import (
    DEFAULT_REPORTS_ROOT,
    SHARED_REPORTS_ENV,
    reports_root,
    shared_reports,
)
from tests.test_boundaries import PACKAGES, ROOT

UNIT_DIR = ROOT / "scripts" / "systemd"
ENV_LINE_RE = re.compile(rf"^Environment={SHARED_REPORTS_ENV}=(.*)$", re.M)
EXEC_MODULE_RE = re.compile(r"^ExecStart=.*? -m (\S+)", re.M)
REPORTS_LITERAL_RE = re.compile(r"""["'](reports/[^"']*)["']""")

# The ONE relative `reports/` path that is correct. `collector.qa.STATE`
# is a log of what this checkout did, not an artifact derived from the
# shared archive: the dev tree and the stable tree are supposed to keep
# separate section-completion records (CLAUDE.md, tests/test_health_digest.py).
# Anything else reaching a unit must go through `shared_reports`.
PER_TREE_RECORDS = {("collector.qa", "reports/qa/sections.json")}


# --------------------------------------------------------------------------
# helper semantics


def test_default_is_cwd_relative(monkeypatch):
    """Unset -- every dev-tree and in-test call -- behaves exactly as before."""
    monkeypatch.delenv(SHARED_REPORTS_ENV, raising=False)
    assert reports_root() == Path(DEFAULT_REPORTS_ROOT)
    assert shared_reports("shadow_divergence") == Path("reports/shadow_divergence")


def test_env_overrides_root(monkeypatch, tmp_path):
    monkeypatch.setenv(SHARED_REPORTS_ENV, str(tmp_path / "shared"))
    assert shared_reports("shadow_divergence") == tmp_path / "shared" / "shadow_divergence"


def test_empty_env_reads_as_unset(monkeypatch):
    """`Environment=VAR=` is systemd's "clear this", not a path of "" -- which
    would silently root every report at the cwd itself."""
    monkeypatch.setenv(SHARED_REPORTS_ENV, "")
    assert reports_root() == Path(DEFAULT_REPORTS_ROOT)


def test_root_is_read_at_call_time(monkeypatch, tmp_path):
    """So a test can redirect it; a caller freezing it in a constant is the
    one making that choice."""
    monkeypatch.setenv(SHARED_REPORTS_ENV, str(tmp_path / "a"))
    first = reports_root()
    monkeypatch.setenv(SHARED_REPORTS_ENV, str(tmp_path / "b"))
    assert reports_root() != first


# --------------------------------------------------------------------------
# the import graph, derived from the tree


def _module_files() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for pkg in sorted(PACKAGES):
        for f in sorted((ROOT / pkg).rglob("*.py")):
            parts = list(f.relative_to(ROOT).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            out[".".join(parts)] = f
    return out


MODULES = _module_files()


def _first_party_imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in PACKAGES:
                out.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in PACKAGES:
                    out.add(alias.name)
    return out


IMPORTS = {name: _first_party_imports(path) for name, path in MODULES.items()}


def _reachable(entry: str) -> set[str]:
    seen: set[str] = set()
    stack = [entry]
    while stack:
        mod = stack.pop()
        if mod in seen or mod not in IMPORTS:
            continue
        seen.add(mod)
        stack.extend(IMPORTS[mod])
    return seen


def _units() -> list[tuple[Path, str]]:
    out = []
    for unit in sorted(UNIT_DIR.glob("*.service")):
        match = EXEC_MODULE_RE.search(unit.read_text())
        if match and match.group(1) in MODULES:
            out.append((unit, match.group(1)))
    return out


UNITS = _units()


def test_units_were_discovered():
    """The two guards below are vacuous if the ExecStart pattern stops
    matching -- a silent pass is the failure mode they exist to prevent."""
    assert len(UNITS) >= 8, [u.name for u, _ in UNITS]
    assert any(entry == "simulator.divergence" for _, entry in UNITS)
    assert any(entry == "collector.qa" for _, entry in UNITS)


@pytest.mark.parametrize("unit,entry", UNITS, ids=lambda v: getattr(v, "name", v))
def test_unit_reaching_a_report_path_declares_the_root(unit: Path, entry: str):
    """If a unit can touch a shared report, its unit file must say where
    those live. A unit that cannot must not carry the line -- an
    unexplained environment variable is the next reader's dead end."""
    needs = "hyxlab.reportdir" in _reachable(entry)
    declared = ENV_LINE_RE.search(unit.read_text())
    assert bool(declared) == needs, (
        f"{unit.name} reaches hyxlab.reportdir={needs} but declares"
        f" {SHARED_REPORTS_ENV}={bool(declared)}"
    )


def test_declared_roots_are_absolute_and_agree():
    """Two units writing and reading the same artifact must name the same
    directory, and a relative value would re-introduce the split it fixes."""
    values = {
        unit.name: match.group(1)
        for unit, _ in UNITS
        if (match := ENV_LINE_RE.search(unit.read_text()))
    }
    assert values, "no unit declares the shared reports root"
    for name, value in values.items():
        assert Path(value).is_absolute(), f"{name}: {value!r} is not absolute"
    assert len(set(values.values())) == 1, values


@pytest.mark.parametrize("unit,entry", UNITS, ids=lambda v: getattr(v, "name", v))
def test_no_cwd_relative_report_path_reachable_from_a_unit(unit: Path, entry: str):
    """The class guard. A literal `reports/...` anywhere a unit can reach
    resolves against whichever worktree happened to run it."""
    offenders = []
    for mod in sorted(_reachable(entry)):
        for literal in REPORTS_LITERAL_RE.findall(MODULES[mod].read_text()):
            if (mod, literal) not in PER_TREE_RECORDS:
                offenders.append(f"{mod}: {literal!r}")
    assert not offenders, (
        f"{unit.name} reaches cwd-relative report path(s) {offenders} --"
        " use hyxlab.reportdir.shared_reports, or document it in"
        " PER_TREE_RECORDS if it is genuinely per-checkout state"
    )


def test_per_tree_record_allowlist_is_live():
    """An allowlist entry that no longer matches anything is a rule about a
    file that moved -- it must redden, not quietly excuse nothing."""
    for mod, literal in PER_TREE_RECORDS:
        assert mod in MODULES, mod
        assert literal in MODULES[mod].read_text(), f"{mod}: {literal!r} is gone"
