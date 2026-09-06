"""The shell scripts had no lint equivalent at all.

`tests/test_lint_scope.py` closed the Python blind spot (an enumerated directory
list that could not fail when `scripts/` appeared). It closed it only for Python.
Four tracked shell scripts and fourteen harness hooks were checked by nothing --
not by ruff, which does not read shell, and not by any other gate: there was no
shellcheck in the project.

That set includes `scripts/restart_decision.sh`, which gates EVERY daemon restart
promote.sh performs, and whose misjudgment costs a live shadow run (bound 14 in
that file). The first run of the gate found two findings in exactly that file:

  * SC2148 (error) -- no shebang, so shellcheck could not tell what dialect it
    was reading. Correct in substance: the file is sourced, never executed. The
    fix is the `# shellcheck shell=bash` directive, which states out loud what
    promote.sh has always assumed.
  * SC1010 (warning) -- `local ts now then` declared a variable named after a
    reserved word. Measured on this box's bash: it does localize, so this was
    latent, not live. Renamed to `started` rather than left as a parser dare.

and one in `.claude/evals/run-smoke.sh` (SC2155: `local X=$(cmd)` masks the
command's exit status, so `set -e` cannot see it fail).

THE SEVERITY FLOOR IS `warning`, AND THE MEASUREMENT IS WHAT DECIDED IT.
At shellcheck's default (style and up) the tree carried 8 findings: the 3 above,
and 5 below the floor -- SC2001 x3 ("see if you can use ${var//a/b} instead of
sed") and SC2016 x2 ("expressions don't expand in single quotes"). The SC2016
pair is a false positive by construction: both sites are sed/grep SCRIPTS
containing backticks, which must NOT expand, so the only way to satisfy the
finding is a `disable` comment. A gate whose findings are answered by disable
comments teaches the operator to write disable comments. SC2001 is an idiom
preference with no failure mode; one of its three sites (`sed 's/^/    /'` over
multi-line input) has no parameter-expansion form at all.

`test_the_floor_hides_only_the_families_it_was_measured_against` is what keeps
that from being a permanent excuse: a NEW below-floor family fails the suite and
has to be looked at, rather than silently inheriting the exemption.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SHELLCHECK = REPO / ".venv/bin/shellcheck"

#: Severity floor for the gate. See the module docstring for the measurement.
SEVERITY = "warning"

#: Finding codes deliberately below the floor, with the reason each is excused.
#: Adding one is a deliberate edit here, never a quiet flag in a config file.
BELOW_FLOOR_FAMILIES = {
    "SC2001": "style: prefer ${var//a/b} over sed. No failure mode, and the "
    "multi-line `sed 's/^/    /'` site has no expansion form.",
    "SC2016": "info: backticks inside single quotes. False positive at both "
    "sites -- they are sed/grep scripts that must not expand.",
}


def _shebang_line(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            return fh.readline(256).decode("utf-8", "replace").rstrip("\n")
    except OSError:
        return ""


def is_shell_shebang(first_line: str) -> bool:
    """True iff a file's first line hands the file to a POSIX-ish shell.

    Split out as a pure function so the discovery arm below is exercised by
    strings rather than only by whatever the repo happens to contain today.
    """
    if not first_line.startswith("#!"):
        return False
    interp = first_line[2:].strip().split()
    if not interp:
        return False
    # `#!/usr/bin/env bash` -> the interpreter is the argument.
    words = [w for w in interp if not w.startswith("-")]
    name = Path(words[0]).name
    if name == "env" and len(words) > 1:
        name = Path(words[1]).name
    return name in {"sh", "bash", "dash", "ksh", "zsh"}


def _tracked() -> list[str]:
    p = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def _dot_sh_files() -> list[str]:
    """The set the documented command checks."""
    return sorted(f for f in _tracked() if f.endswith(".sh"))


def _shebang_only_shell_files() -> list[str]:
    """Tracked shell scripts the `*.sh` glob would MISS."""
    out = []
    for f in _tracked():
        if f.endswith(".sh"):
            continue
        path = REPO / f
        if path.is_file() and is_shell_shebang(_shebang_line(path)):
            out.append(f)
    return sorted(out)


def _documented_shell_lint_command() -> str:
    lines = [
        ln.strip()
        for ln in (REPO / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
        if "shellcheck" in ln and not ln.lstrip().startswith("#")
    ]
    assert len(lines) == 1, f"expected exactly one documented shellcheck command, got {lines}"
    return lines[0]


def _run(severity: str) -> subprocess.CompletedProcess:
    files = _dot_sh_files()
    assert files, "no tracked *.sh files -- this suite has lost its subject"
    return subprocess.run(
        [str(SHELLCHECK), f"--severity={severity}", *files],
        cwd=REPO,
        capture_output=True,
        text=True,
    )


def _codes(p: subprocess.CompletedProcess) -> set[str]:
    """Finding codes shellcheck printed, e.g. {"SC2001", "SC2148"}."""
    return {
        w
        for line in p.stdout.splitlines()
        for w in line.split()
        if w.startswith("SC") and w[2:].isdigit()
    }


def test_shellcheck_is_installed():
    """The gate is only a gate if the tool is pinned, not optionally present.

    A skip-when-missing would make this file green on the one machine where the
    check does not run -- which is the failure mode the whole file is about.
    """
    assert SHELLCHECK.exists(), (
        f"{SHELLCHECK} missing. shellcheck-py is pinned in requirements.txt; "
        "install it (`.venv/bin/pip install -r requirements.txt`)."
    )


def test_documented_shell_lint_command_is_derived_not_enumerated():
    """CLAUDE.md must name the file set by discovery, not by listing scripts.

    Same defect as the ruff command's old directory list: a command that names
    files cannot fail when a new file appears.
    """
    cmd = _documented_shell_lint_command()
    assert cmd == f".venv/bin/shellcheck --severity={SEVERITY} $(git ls-files '*.sh')", (
        f"documented shell lint command is {cmd!r}; it must discover its file set "
        f"with `git ls-files '*.sh'` at severity {SEVERITY}."
    )


def test_no_tracked_shell_script_hides_from_the_glob():
    """A shell script without a `.sh` name is invisible to the documented command.

    Rather than complicate the command, the convention is enforced: shell scripts
    in this repo are named `*.sh`. The day one is not, this fails and names it --
    rename it, or widen the command AND this test together.
    """
    hidden = _shebang_only_shell_files()
    assert not hidden, (
        f"tracked shell scripts the `*.sh` glob misses: {hidden}. "
        "Rename them to *.sh, or widen the documented command and this test."
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("#!/bin/bash", True),
        ("#!/usr/bin/env bash", True),
        ("#!/bin/sh", True),
        ("#!/usr/bin/env -S bash -eu", True),
        ("#!/usr/bin/zsh", True),
        ("#!/usr/bin/env python3", False),
        ("#!/usr/bin/python", False),
        ("# not a shebang", False),
        ("", False),
        ("#!", False),
    ],
)
def test_shebang_discovery_recognises_shells(line: str, expected: bool):
    """The discovery arm above must work on the day it has a subject.

    It finds nothing in the repo today (measured 2026-09-05), so without this it
    would be untested code that passes by matching nothing.
    """
    assert is_shell_shebang(line) is expected


def test_repo_is_clean_under_the_documented_shell_lint_command():
    """Scope is half of it -- the set has to actually pass."""
    p = _run(SEVERITY)
    assert p.returncode == 0, f"shellcheck findings at severity {SEVERITY}:\n{p.stdout}{p.stderr}"


def test_the_floor_hides_only_the_families_it_was_measured_against():
    """A below-floor finding may only be one of the families excused above.

    The floor was chosen against a measured set of 5 findings in 2 families. This
    is what stops it from becoming a blanket amnesty for whatever style finding
    appears next: a new code fails here and has to be answered -- fixed, or added
    to BELOW_FLOOR_FAMILIES with its reason.
    """
    below = _codes(_run("style")) - _codes(_run(SEVERITY))
    unexcused = sorted(below - set(BELOW_FLOOR_FAMILIES))
    assert not unexcused, (
        f"below-floor shellcheck findings in families nobody has ruled on: {unexcused}\n"
        "Fix them, or add each code to BELOW_FLOOR_FAMILIES with the "
        "reason it cannot fail."
    )
