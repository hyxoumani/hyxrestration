"""Every operator script must run under the invocation its docstring prescribes.

`scripts/probe_collect_skip.py` shipped 2026-09-02 unable to start: its own
documented invocation, `.venv/bin/python scripts/probe_collect_skip.py`, puts
`scripts/` on `sys.path[0]` -- not the repo root -- so `from hyxlab import lockid`
raised ModuleNotFoundError on the operator's first run. Its unit tests were green
throughout, because pytest's `pythonpath = ["."]` hands an in-process import a
path the operator never gets.

test_probe_collect_skip.py fixed that for the probe. This file asks the successor
question the 09-03 pass named: whether any OTHER script has the same defect. It
is discovery-based, not a list -- a new script is covered the moment it is added,
which is the only version of this check that cannot rot the way the enumerated
lint set did (tests/test_lint_scope.py).

Method: run each script's module top level in a subprocess whose `sys.path[0]` is
`scripts/` -- exactly what the shell gives it -- with PYTHONPATH stripped so a
stray environment cannot supply the root by accident. `run_name` is set to
something other than `"__main__"` so `main()` never executes: these scripts take
the live writer lock and spend real fetches, and an import check may not.

Measured 2026-09-03: 2 scripts, both clean. `daemon_imports.py` is stdlib-only,
so it never needed a bootstrap; the probe has one. The value here is the day a
third script imports `hyxlab` without one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"

#: run_name for the subprocess. Anything but "__main__"; named so a failure
#: message says why main() did not run.
IMPORT_CHECK = "entry_point_import_check"


def _scripts() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if not p.name.startswith("_"))


def test_there_are_scripts_to_check():
    """A glob that matches nothing passes every parametrised test below."""
    assert _scripts(), f"no operator scripts found under {SCRIPTS}"


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: p.name)
def test_documented_invocation_can_import_the_project(script: Path):
    """`.venv/bin/python scripts/<name>.py` must reach module top-level cleanly.

    A script that imports the project without bootstrapping the repo root fails
    here with ModuleNotFoundError -- on the operator's path, not pytest's.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    p = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; sys.path[0] = sys.argv[1]; "
            "runpy.run_path(sys.argv[2], run_name=sys.argv[3])",
            str(SCRIPTS),
            str(script),
            IMPORT_CHECK,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert p.returncode == 0, (
        f"{script.name} cannot start under its documented invocation "
        f"(`.venv/bin/python scripts/{script.name}`), where sys.path[0] is scripts/ "
        f"and not the repo root:\n{p.stderr}"
    )


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: p.name)
def test_import_does_no_work(script: Path):
    """Importing a script must not do the thing the script does.

    These scripts take `data/writer.lock` and run real collection cycles. Any
    output at import time means work happened on a mere import -- which is both a
    live-archive hazard and the reason the check above could not be trusted.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    p = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; sys.path[0] = sys.argv[1]; "
            "runpy.run_path(sys.argv[2], run_name=sys.argv[3])",
            str(SCRIPTS),
            str(script),
            IMPORT_CHECK,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert not p.stdout.strip(), (
        f"{script.name} printed at import time under run_name={IMPORT_CHECK!r}: "
        f"{p.stdout!r}. A script's work belongs behind `if __name__ == '__main__'`."
    )


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: p.name)
def test_entry_point_is_guarded(script: Path):
    """The guard the test above depends on must actually be present.

    Without `if __name__ == "__main__"`, `run_name` proves nothing and the import
    check would be running the script for real.
    """
    src = script.read_text(encoding="utf-8")
    assert '__name__ == "__main__"' in src or "__name__ == '__main__'" in src, (
        f"{script.name} has no __main__ guard, so importing it runs it"
    )
