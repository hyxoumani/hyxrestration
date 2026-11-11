"""`promote.sh --help` must not promote (EXP-2305).

Measured 2026-11-08: the operator ran `scripts/promote.sh --help`. The flag
loop had no catch-all arm, so `--help` was silently dropped and the script
started a FULL promote -- the dev-tree suite ran, and only a red test (and a
SIGPIPE) stopped it before the fast-forward of `stable` and the daemon
restarts. Nothing was promoted, by luck.

These tests run a COPY of promote.sh in a sandbox: `DEV`/`STABLE` point at a
temp tree whose `.venv/bin/python` only records that it was called, and `git`,
`systemctl`, `cp` and `pip` on PATH are stubs that do the same. Any of them
firing means the script went past argument parsing. The no-argument CONTROL
shows the sandbox does detect a run that starts -- without it, a sandbox that
could never observe anything would pass every refusal test.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PROMOTE = REPO / "scripts" / "promote.sh"

STUBS = ("git", "systemctl", "cp", "pip", "sleep")


def _stub(path: Path, log: Path, rc: int = 0) -> None:
    path.write_text(f'#!/usr/bin/env bash\necho "{path.name} $*" >> "{log}"\nexit {rc}\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def sandbox(tmp_path: Path):
    log = tmp_path / "ran.log"
    dev = tmp_path / "dev"
    stable = tmp_path / "stable"
    for root in (dev, stable):
        (root / ".venv" / "bin").mkdir(parents=True)
        (root / "scripts" / "systemd").mkdir(parents=True)
        for unit in ("hyxlab-shadow.service", "hyxlab-stream.service"):
            (root / "scripts" / "systemd" / unit).write_text("[Service]\n")
        # Exits 1: in the control run, `set -e` stops the script at the suite,
        # so it reaches nothing past the first real step even in the sandbox.
        _stub(root / ".venv" / "bin" / "python", log, rc=1)
        _stub(root / ".venv" / "bin" / "pip", log)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in STUBS:
        _stub(bindir / name, log)

    src = PROMOTE.read_text()
    copy, n_dev = re.subn(r"^DEV=.*$", f"DEV={dev}", src, count=1, flags=re.M)
    copy, n_stable = re.subn(r"^STABLE=.*$", f"STABLE={stable}", copy, count=1, flags=re.M)
    assert (n_dev, n_stable) == (1, 1), "promote.sh no longer assigns DEV=/STABLE= on one line"
    assigned = [ln for ln in copy.splitlines() if ln.startswith(("DEV=", "STABLE="))]
    assert assigned == [f"DEV={dev}", f"STABLE={stable}"], assigned
    script = tmp_path / "promote.sh"
    script.write_text(copy)
    script.chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "LANG": "C",
    }

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(script), *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    return run, log


def _ran(log: Path) -> str:
    return log.read_text() if log.exists() else ""


def test_control_a_bare_invocation_is_seen_starting(sandbox):
    """Non-vacuity: with no argument the sandbox observes the suite start."""
    run, log = sandbox
    p = run()
    assert p.returncode != 0
    assert "== tests (dev tree) ==" in p.stdout
    assert "python -m pytest tests/" in _ran(log), _ran(log)


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage_and_runs_nothing(sandbox, flag):
    run, log = sandbox
    p = run(flag)
    assert p.returncode == 0, p.stderr
    assert p.stdout.startswith("Usage: scripts/promote.sh"), p.stdout
    for known in ("--restart-all", "--restart-young", "--defer=", "--units-only"):
        assert known in p.stdout, known
    assert "== tests" not in p.stdout
    assert _ran(log) == "", f"--help ran something: {_ran(log)}"


@pytest.mark.parametrize(
    "args",
    [
        ("--hlep",),
        ("--dry-run",),
        ("help",),
        ("--defer",),  # the value form is `--defer=UNIT`; a bare word must not be dropped
        ("--defer=",),
        ("--restart-all", "--bogus"),  # a known flag in front does not launder an unknown one
    ],
)
def test_an_unknown_argument_exits_2_and_runs_nothing(sandbox, args):
    run, log = sandbox
    p = run(*args)
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert "unknown argument" in p.stderr and "Usage:" in p.stderr, p.stderr
    assert p.stdout == "", p.stdout
    assert _ran(log) == "", f"{args} ran something: {_ran(log)}"


def test_the_known_flags_still_pass_the_parser(sandbox):
    """The refusal arm must not swallow a real flag: each known flag reaches
    the suite step (and stops there, on the stub python's exit 1)."""
    run, log = sandbox
    for args in (("--restart-all",), ("--restart-young",), ("--defer=hyxlab-shadow.service",)):
        log.unlink(missing_ok=True)
        p = run(*args)
        assert p.returncode not in (0, 2), (args, p.returncode, p.stderr)
        assert "python -m pytest tests/" in _ran(log), (args, _ran(log))
    log.unlink(missing_ok=True)
    p = run("--units-only")
    assert "== unit-file gates (dev tree) ==" in p.stdout, p.stdout
    assert "test_systemd_units.py" in _ran(log), _ran(log)


def test_the_refusal_comes_before_the_first_side_effect():
    """Lexical belt to the sandbox braces: the usage/refusal loop sits above
    the `cd "$DEV"` and the first `systemctl`/`.venv` call."""
    src = PROMOTE.read_text()
    refuse = src.index("unknown argument")
    for marker in ('\ncd "$DEV"', "\n.venv/bin/python", "systemctl --user enable"):
        assert refuse < src.index(marker), marker
    assert os.access(PROMOTE, os.X_OK)


def test_a_defer_that_names_no_unit_exits_2_and_runs_nothing(sandbox):
    """2026-11-11: `--defer=hyxlab-stream,hyxlab-shadow` matched nothing and
    the promote restarted the stream it was told to leave (mistakes #180)."""
    run, log = sandbox
    p = run("--defer=hyxlab-shadow.service,hyxlab-straem")
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert "names no unit" in p.stderr and "hyxlab-straem.service" in p.stderr, p.stderr
    assert _ran(log) == "", _ran(log)


def test_a_defer_without_the_service_suffix_is_the_same_unit(sandbox):
    """Reaches the suite step, i.e. the bare names were accepted as units."""
    run, log = sandbox
    p = run("--defer=hyxlab-stream,hyxlab-shadow")
    assert p.returncode not in (0, 2), (p.returncode, p.stderr)
    assert "python -m pytest tests/" in _ran(log), _ran(log)

