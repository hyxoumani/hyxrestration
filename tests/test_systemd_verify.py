"""The unit files had no SYNTAX gate -- only readers that treat them as text.

`tests/test_shell_lint.py` closed the shell blind spot the same way
`tests/test_lint_scope.py` closed the Python one. It closed it for `*.sh`
only. `scripts/systemd/hyxlab-*` -- 21 files that `scripts/promote.sh` copies
verbatim into ~/.config/systemd/user/ -- were parsed by nothing before install.
`tests/test_systemd_units.py` reads them as TEXT: it greps `ExecStart` for a
`-m module` name, greps `OnCalendar` for a timezone suffix, greps `Description`
for prose. Every one of those checks passes on a unit systemd itself refuses to
load, and passes on a unit whose interpreter does not exist on this box.

`systemd-analyze verify` parses them the way the manager does. Three things
about it had to be measured before it could be a gate, and all three shaped
this file.

**(1) THE EXIT CODE IS A LIAR FOR WARNINGS.** Measured 2026-09-06, systemd 260:
a unit with `Type=oneshot` + `RuntimeMaxSec=` prints
`RuntimeMaxSec= has no effect in combination with Type=oneshot. Ignoring.`
and exits **0**. Hard errors (a missing `ExecStart` binary, an unparseable
`OnCalendar`) exit 1. So a gate that reads only the return code would let
through exactly the class of finding that is silently ignored at runtime --
a directive that is present, looks effective, and does nothing. This gate
asserts an EMPTY report as well as a zero exit;
`test_a_warning_only_finding_exits_zero` pins the measurement so the day
systemd starts failing on warnings, the extra arm is known to be redundant
rather than quietly load-bearing forever.

**(2) THE TOOL IS NOT HERMETIC BY DEFAULT.** `systemd-analyze verify --user`
resolves dependencies through the full user unit search path, which includes
`~/.config/systemd/user`. On this box that directory holds an UNRELATED
project's units, and a bare run reports two findings that belong to it
(`hylshi-exchange-reconcile.service`, `hylshi-archive-reconcile.service`;
measured 2026-09-06). A gate reading stdout -- see (1), it must -- would then
fail on files this repo does not own and cannot fix. `SYSTEMD_UNIT_PATH`
replaces the search path with the repo's own directory plus the SYSTEM user-unit
dirs; those are needed because without them every unit fails on
`Unit basic.target not found`, which is an artefact of the isolation and not a
defect in the unit.

**(3) WHAT IT DOES NOT CHECK.** `WorkingDirectory=` and `EnvironmentFile=`
pointing at nonexistent paths both verify clean and exit 0 (measured). The one
filesystem fact it does check is the `ExecStart` command's existence -- which is
the fact that matters here, because every hyxlab unit's ExecStart is an ABSOLUTE
path into the stable worktree's venv
(`/home/devs/workspace/hyxrestration-stable/.venv/bin/python`) and nothing in
the tree checked that it exists. promote.sh installs and restarts from the dev
tree; a stable venv rebuilt, renamed or half-deleted leaves every daemon failing
at start with the units looking perfect in `git diff`.

That ExecStart arm is why this suite is deliberately NOT portable to a machine
without the stable worktree. On such a machine it fails, and the failure is
correct: these units are vendored with absolute paths and are unrunnable there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO / "scripts" / "systemd"

SYSTEMD_ANALYZE = shutil.which("systemd-analyze") or "/usr/bin/systemd-analyze"

#: System-wide user-unit directories, needed only so `basic.target` and friends
#: resolve. Deliberately does NOT include ~/.config/systemd/user -- see (2).
SYSTEM_UNIT_DIRS = ("/etc/systemd/user", "/usr/lib/systemd/user")

#: The glob `scripts/promote.sh` installs with. The verified set must equal it.
PROMOTE_GLOB = "hyxlab-*"


def _unit_files(directory: Path = UNIT_DIR) -> list[Path]:
    """Discovered, never enumerated -- a listed set cannot fail on a new unit."""
    return sorted(p for p in directory.glob(PROMOTE_GLOB) if p.is_file())


def unit_search_path(unit_dir: Path) -> str:
    """The `SYSTEMD_UNIT_PATH` the gate runs under.

    Pure, so the isolation property below is asserted against the value itself
    rather than against whatever this box happens to have installed.
    """
    return os.pathsep.join([str(unit_dir), *SYSTEM_UNIT_DIRS])


def _verify(paths: list[Path], unit_dir: Path) -> subprocess.CompletedProcess:
    """Run `systemd-analyze verify` with the search path pinned to `unit_dir`."""
    env = dict(os.environ)
    env["SYSTEMD_UNIT_PATH"] = unit_search_path(unit_dir)
    return subprocess.run(
        [SYSTEMD_ANALYZE, "verify", "--user", *(str(p) for p in paths)],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
    )


def _report(p: subprocess.CompletedProcess) -> str:
    return (p.stdout + p.stderr).strip()


def _documented_verify_command() -> list[str]:
    """The documented invocation from CLAUDE.md, backslash continuations joined."""
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8").replace("\\\n", " ")
    lines = [
        " ".join(ln.split())
        for ln in text.splitlines()
        if "systemd-analyze verify" in ln and not ln.lstrip().startswith("#")
    ]
    assert len(lines) == 1, f"expected exactly one documented verify command, got {lines}"
    return lines[0].split()


def test_systemd_analyze_is_available():
    """No skip-when-missing: that makes the file green where the check does not run."""
    assert Path(SYSTEMD_ANALYZE).exists(), (
        f"{SYSTEMD_ANALYZE} missing -- it ships with systemd, which this project's "
        "entire scheduling layer depends on. A box without it cannot run hyxlab."
    )


def test_the_verified_set_is_exactly_what_promote_installs():
    """A unit promote.sh copies but this gate skips is an ungated install.

    promote.sh's install line is the source of truth for the set; the gate reads
    the same glob out of the script rather than trusting that they agree.
    """
    script = (REPO / "scripts" / "promote.sh").read_text(encoding="utf-8")
    installs = [
        ln.strip()
        for ln in script.splitlines()
        if "systemd/user" in ln and ln.strip().startswith("cp ")
    ]
    assert len(installs) == 1, f"expected one unit-install line in promote.sh, got {installs}"
    assert f"scripts/systemd/{PROMOTE_GLOB}" in installs[0], (
        f"promote.sh installs {installs[0]!r}, which is not the "
        f"`scripts/systemd/{PROMOTE_GLOB}` set this gate verifies. Widen both together."
    )
    assert _unit_files(), "no unit files found -- this suite has lost its subject"


def test_documented_verify_command_matches_the_gate():
    """The operator's command must be the gate's command, isolation included.

    Two failure modes in one assertion. A command that LISTS unit names cannot
    fail the day a new unit appears -- the defect the ruff and shellcheck
    commands were both fixed for. And a command run without `SYSTEMD_UNIT_PATH`
    answers a different question than the gate does: on this box it reports two
    findings belonging to an unrelated project, which teaches the operator that
    the tool is noisy and can be ignored.
    """
    argv = _documented_verify_command()
    env, _, cmd = argv[0].partition("=")
    assert env == "SYSTEMD_UNIT_PATH", (
        f"the documented command starts with {argv[0]!r}; it must pin "
        "SYSTEMD_UNIT_PATH or it reads other projects' units."
    )
    dirs = cmd.split(os.pathsep)
    assert dirs[0] == "$PWD/scripts/systemd", f"unit dir under test must come first, got {dirs}"
    assert list(dirs[1:]) == list(SYSTEM_UNIT_DIRS), (
        f"documented system unit dirs {dirs[1:]} differ from the gate's "
        f"{list(SYSTEM_UNIT_DIRS)}; the two must not drift."
    )
    assert argv[1:] == [
        "systemd-analyze",
        "verify",
        "--user",
        f"scripts/systemd/{PROMOTE_GLOB}",
    ], f"documented command is {argv[1:]}; it must verify the whole installed glob."


def test_units_verify_clean_and_silent():
    """The gate proper: zero exit AND an empty report.

    The second half is not belt-and-braces -- warnings exit 0 (see the module
    docstring, and `test_a_warning_only_finding_exits_zero` below).
    """
    p = _verify(_unit_files(), UNIT_DIR)
    assert p.returncode == 0, f"systemd-analyze verify failed:\n{_report(p)}"
    assert not _report(p), (
        "systemd-analyze verify reported findings on the vendored units "
        f"(it still exited {p.returncode} -- warnings do):\n{_report(p)}"
    )


def test_the_search_path_excludes_other_projects_units(tmp_path):
    """The isolation is the reason the stdout arm above can exist at all.

    A run inheriting the default search path reads ~/.config/systemd/user, which
    on this box holds an unrelated project. This asserts the property rather than
    the box: the path the gate builds must not reach the user unit dir.
    """
    dirs = unit_search_path(tmp_path).split(os.pathsep)
    user_dir = str(Path.home() / ".config/systemd/user")
    assert user_dir not in dirs, (
        f"{user_dir} is in the gate's search path; findings from unrelated "
        "projects installed there would fail this repo's suite."
    )
    assert "" not in dirs, (
        "an empty component in SYSTEMD_UNIT_PATH appends the DEFAULT search "
        "path, which re-admits the user unit dir."
    )
    assert dirs[0] == str(tmp_path), "the unit dir under test must come first"
    assert all(Path(d).is_dir() for d in dirs[1:]), (
        f"a system unit dir in {dirs[1:]} does not exist; without a real one "
        "every unit fails on `Unit basic.target not found`, which is an "
        "artefact of the isolation and not a defect in the unit."
    )


def test_a_missing_execstart_binary_is_caught(tmp_path):
    """The arm that justifies the whole file, proven against a real defect.

    Every hyxlab unit's ExecStart is an absolute path into the stable worktree's
    venv, and no other check in the tree opens that path. Without this the arm
    would be untested code that passes because the repo happens to be healthy.
    """
    src = UNIT_DIR / "hyxlab-stream.service"
    broken = tmp_path / "hyxlab-stream.service"
    broken.write_text(
        "\n".join(
            "ExecStart=/nonexistent/bin/python -u -m collector.streamd"
            if ln.startswith("ExecStart=")
            else ln
            for ln in src.read_text().splitlines()
        )
        + "\n"
    )
    p = _verify([broken], tmp_path)
    assert p.returncode != 0, f"a missing ExecStart binary verified clean:\n{_report(p)}"
    assert "/nonexistent/bin/python" in _report(p)


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("[Timer]\nOnCalendar=not-a-time\n", "calendar"),
        ("[Service]\nType=oneshot\nExecStart=/bin/true\nNoSuchKey=1\n", "NoSuchKey"),
    ],
)
def test_malformed_units_are_caught(tmp_path, body, needle):
    """Shapes `test_systemd_units.py`'s text greps cannot see.

    An unparseable OnCalendar and an unknown directive both survive a regex that
    only asks what a line SAYS. systemd refuses the first outright and silently
    drops the second -- which is how a MemoryMax typo becomes an uncapped daemon.
    """
    suffix = ".timer" if "[Timer]" in body else ".service"
    unit = tmp_path / f"hyxlab-probe{suffix}"
    unit.write_text(body)
    p = _verify([unit], tmp_path)
    assert needle.lower() in _report(p).lower(), (
        f"systemd-analyze verify said nothing about {needle}:\n{_report(p)}"
    )


def test_a_warning_only_finding_exits_zero(tmp_path):
    """The measurement that makes the empty-report assertion load-bearing.

    `Type=oneshot` + `RuntimeMaxSec=` is ignored at runtime and reported by the
    tool, which still exits 0 (systemd 260, measured 2026-09-06). If this ever
    starts failing, systemd began failing on warnings and the report arm in
    `test_units_verify_clean_and_silent` became redundant -- say so there rather
    than leaving a second assertion nobody can account for.
    """
    unit = tmp_path / "hyxlab-probe.service"
    unit.write_text("[Service]\nType=oneshot\nRuntimeMaxSec=60\nExecStart=/bin/true\n")
    p = _verify([unit], tmp_path)
    assert p.returncode == 0, (
        "systemd-analyze now FAILS on warning-only findings. The empty-report "
        "assertion in test_units_verify_clean_and_silent is redundant; record that "
        "and simplify it, do not just delete this test."
    )
    assert "RuntimeMaxSec" in _report(p), (
        f"the warning shape this measurement rests on was not reported:\n{_report(p)}"
    )
