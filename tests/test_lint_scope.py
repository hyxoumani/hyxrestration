"""The lint set must have no blind spot.

`scripts/` was never linted. Not by decision -- the documented command
(CLAUDE.md) enumerated five directories, `scripts/` was created later, and an
enumeration omits by default. `scripts/daemon_imports.py` carried a live SIM108
finding that no run of the documented command could ever print.

That is the same shape as the bug the probe pass found on 2026-09-03: a check
whose scope silently differs from the thing it claims to cover. There the gap
was sys.path (pytest gave the import a root the operator never gets); here it is
the file set. Both were green forever while being wrong, because nothing asserted
that the checked set equals the real set.

So the lint scope is now DERIVED -- `ruff check .`, i.e. the repo minus
pyproject's `extend-exclude` -- and these tests hold it that way:

  * the documented command must stay derived, not re-enumerate directories;
  * every git-tracked .py file must be either in ruff's scope or in the one
    exemption named below, so a new directory cannot become invisible;
  * the repo must be clean under it.

The exemption is spelled out here rather than merely read off the config: adding
one has to be a deliberate edit to a test that says why, not a quiet line in a
settings file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Directories whose Python files are deliberately outside the lint set.
#: phase0 is a closed historical record of a falsified thesis (CLAUDE.md: "do not
#: build on it"). Its files are frozen, so the only way to answer a lint finding
#: there is to edit a record that must not change -- the finding would be noise
#: with no legal fix. It has 4 such findings today (UP035, I001, 2x SIM102).
EXEMPT_PREFIXES = ("phase0/",)


def _ruff_scope() -> set[str]:
    """Repo-relative paths ruff would check under the documented command."""
    p = subprocess.run(
        [str(REPO / ".venv/bin/ruff"), "check", "--show-files", "."],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, p.stderr
    return {
        str(Path(line).resolve().relative_to(REPO))
        for line in p.stdout.splitlines()
        if line.strip()
    }


def _tracked_python_files() -> set[str]:
    p = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return {line for line in p.stdout.splitlines() if line.strip()}


def _documented_lint_command() -> str:
    """The single `ruff check` line in CLAUDE.md's command block."""
    lines = [
        ln.strip()
        for ln in (REPO / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
        if "ruff check" in ln and not ln.strip().startswith("#")
    ]
    assert len(lines) == 1, f"expected exactly one documented lint command, got {lines}"
    return lines[0]


def test_documented_lint_command_is_derived_not_enumerated():
    """CLAUDE.md must document `ruff check .`.

    An enumerated command is the defect itself: it cannot fail when a new
    directory appears, because a directory it does not name is not a finding.
    """
    cmd = _documented_lint_command()
    assert cmd == ".venv/bin/ruff check .", (
        f"documented lint command is {cmd!r}. It must be `.venv/bin/ruff check .` -- "
        "an enumerated directory list omits whatever is added next, which is how "
        "scripts/ went unlinted from the day it was created."
    )


def test_every_tracked_python_file_is_linted_or_exempt():
    """No tracked .py file may fall outside both ruff's scope and the exemption.

    This is the assertion that would have failed on the day scripts/ appeared.
    """
    scope = _ruff_scope()
    blind = {
        f for f in _tracked_python_files() if f not in scope and not f.startswith(EXEMPT_PREFIXES)
    }
    assert not blind, (
        f"tracked Python files that no lint run covers: {sorted(blind)}. Either they "
        "belong in the lint set (check why ruff excludes them) or add a documented "
        "exemption to EXEMPT_PREFIXES saying why the finding would have no legal fix."
    )


def test_exempt_paths_are_really_out_of_scope():
    """The exemption must describe reality.

    If phase0 were in fact linted, the test above would pass by excusing files that
    need no excuse, and the exemption list would rot into a comment about nothing.
    """
    scope = _ruff_scope()
    exempt = {f for f in _tracked_python_files() if f.startswith(EXEMPT_PREFIXES)}
    assert exempt, "EXEMPT_PREFIXES matches no tracked file -- drop it or fix the prefix"
    still_linted = sorted(f for f in exempt if f in scope)
    assert not still_linted, (
        f"{still_linted} are declared exempt but ruff checks them anyway; "
        "pyproject's extend-exclude and EXEMPT_PREFIXES disagree"
    )


def test_repo_is_clean_under_the_documented_lint_command():
    """Scope is only half of it -- the widened set has to actually pass."""
    p = subprocess.run(
        [str(REPO / ".venv/bin/ruff"), "check", "."],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, f"lint findings in the documented scope:\n{p.stdout}{p.stderr}"


def test_scripts_are_inside_the_lint_set():
    """The concrete regression: scripts/ is what the enumeration missed."""
    scope = _ruff_scope()
    scripts = {f for f in _tracked_python_files() if f.startswith("scripts/")}
    assert scripts, "no tracked scripts/*.py -- this test has lost its subject"
    assert scripts <= scope, f"unlinted operator scripts: {sorted(scripts - scope)}"
