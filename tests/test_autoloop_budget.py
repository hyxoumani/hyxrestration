"""The autoloop launcher owns its budget; the unit's TimeoutStartSec is a backstop.

Measured 2026-10-14: `scripts/autoloop.sh` ran `claude -p` with no bound of
its own, the never-stop working mode kept that one process alive across
passes, and both runs that day reached the unit's `TimeoutStartSec=3h` --
`Failed with result 'timeout'`, SIGTERM at an arbitrary point in a pass
(mistakes #122). The launcher now runs claude under `timeout` strictly
inside the unit's budget, exports `AUTOLOOP_DEADLINE` so the pass can stop
before it, and files a TERM-honoured stop as exit 0.

These tests run the real script against a fake `claude` on PATH, in a
throwaway root, so the mechanism is exercised rather than scraped. The one
scraped fact is the pair of default constants, pinned against the unit
file -- the precondition lives there (`test_unit_memory_caps` precedent).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "autoloop.sh"
PROMPT = REPO / "scripts" / "autoloop-prompt.md"
UNIT = REPO / "scripts" / "systemd" / "hyxlab-autoloop.service"

FAKE_CLAUDE = """#!/usr/bin/env bash
# Records what the launcher handed it, then behaves as FAKE_MODE says.
args="$*"; printf 'deadline=%s\\nargs=%s\\n' "${AUTOLOOP_DEADLINE:-UNSET}" "${args//$'\\n'/ }" > "$FAKE_OUT"
case "${FAKE_MODE:-hang}" in
  hang) exec sleep 60 ;;
  exit) exit "${FAKE_RC:-0}" ;;
  rewrite) cat "$FAKE_REWRITE" > "$FAKE_SCRIPT"; exit 0 ;;
esac
"""


def _systemd_seconds(spec: str) -> int:
    """`3h`, `90min`, `45s`, `30` -> seconds. Only the units the unit uses."""
    total = 0
    for num, unit in re.findall(r"(\d+)\s*(h|min|m|s|)", spec.strip()):
        total += int(num) * {"h": 3600, "min": 60, "m": 60, "s": 1, "": 1}[unit]
    return total


def _unit_timeout_start_s() -> int:
    text = UNIT.read_text()
    m = re.search(r"^TimeoutStartSec=(.+)$", text, re.M)
    assert m, "the unit must pin TimeoutStartSec -- it is the backstop this test measures against"
    return _systemd_seconds(m.group(1))


def _script_defaults() -> tuple[int, int]:
    text = SCRIPT.read_text()
    budget = re.search(r"AUTOLOOP_BUDGET_S:-(\d+)", text)
    grace = re.search(r"AUTOLOOP_GRACE_S:-(\d+)", text)
    assert budget and grace, "the launcher must default both AUTOLOOP_BUDGET_S and AUTOLOOP_GRACE_S"
    return int(budget.group(1)), int(grace.group(1))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A throwaway AUTOLOOP_ROOT with the prompt, a data/ dir and a fake claude."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "data").mkdir()
    shutil.copy(PROMPT, tmp_path / "scripts" / "autoloop-prompt.md")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def _run(
    root: Path, env_extra: dict[str, str], script: Path = SCRIPT
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{root / 'bin'}:{env['PATH']}"
    env["AUTOLOOP_ROOT"] = str(root)
    env["FAKE_OUT"] = str(root / "fake.out")
    env.update(env_extra)
    return subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=60)


def _fake_out(root: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (root / "fake.out").read_text().splitlines())


def test_budget_spent_is_a_designed_stop(root: Path) -> None:
    """A claude that outlives the budget is SIGTERMed, and the launcher
    exits 0 with the stop named -- so the unit lands `inactive`, not
    `failed/timeout`, and the digest's RECENT line does not file it."""
    t0 = int(time.time())
    proc = _run(root, {"AUTOLOOP_BUDGET_S": "1", "AUTOLOOP_GRACE_S": "2", "FAKE_MODE": "hang"})
    assert proc.returncode == 0, proc.stderr
    assert "budget 1s spent; bounded stop on SIGTERM" in proc.stdout
    out = _fake_out(root)
    deadline = int(out["deadline"])
    assert t0 + 1 <= deadline <= t0 + 3, (
        "AUTOLOOP_DEADLINE must be start + budget, exported to the child"
    )
    assert "--max-turns 120" in out["args"]


def test_child_exit_code_passes_through(root: Path) -> None:
    """A claude that finishes inside the budget reports ITS exit code; the
    budget wrapper adds nothing on the normal path."""
    ok = _run(root, {"FAKE_MODE": "exit", "FAKE_RC": "0"})
    assert ok.returncode == 0, ok.stderr
    assert "bounded stop" not in ok.stdout
    bad = _run(root, {"FAKE_MODE": "exit", "FAKE_RC": "3"})
    assert bad.returncode == 3, bad.stderr


def test_lock_holder_skips(root: Path) -> None:
    """The flock guard is unchanged: a held lock skips with exit 0 and never
    reaches claude."""
    lock = root / "data" / "autoloop.lock"
    with open(lock, "w") as fh:
        import fcntl

        fcntl.flock(fh, fcntl.LOCK_EX)
        proc = _run(root, {"FAKE_MODE": "exit"})
    assert proc.returncode == 0
    assert "holds the lock; skipping" in proc.stdout
    assert not (root / "fake.out").exists()


def test_budget_plus_grace_sits_inside_the_unit_backstop() -> None:
    """The designed stop must land BEFORE the unit's TimeoutStartSec, kill
    grace included, or the backstop fires first and the run is a failure
    again. Read from the unit file, where the precondition lives."""
    budget, grace = _script_defaults()
    backstop = _unit_timeout_start_s()
    assert backstop > 0
    assert budget + grace < backstop, (
        f"budget {budget}s + grace {grace}s = {budget + grace}s must be < TimeoutStartSec {backstop}s"
    )
    # And not so far inside that the timer's 6h cadence idles: the budget is
    # most of the backstop, not a token fraction of it.
    assert budget >= backstop * 0.8


def test_prompt_names_the_deadline() -> None:
    """The variable the launcher exports is the one the pass is told to read."""
    text = PROMPT.read_text()
    assert "AUTOLOOP_DEADLINE" in text
    assert "date +%s" in text


def test_in_place_rewrite_during_the_run_does_not_break_the_launcher(root: Path) -> None:
    """bash reads a script by byte offset AS it executes. A pass that commits
    a new launcher while `claude -p` runs rewrites the file under the bash
    that is waiting on claude; when claude returns, bash resumes at the old
    offset in the NEW text. Measured 2026-10-14 21:45Z: the 20:15Z run
    shipped 76a860c (this script, longer) and its own launcher died
    `line 13: syntax error near unexpected token ')'`, exit 2, unit
    `failed` (mistakes #123). The fake claude here replays that: it
    overwrites the running copy IN PLACE (same inode, so no rename saves
    it) with a valid launcher that has a long comment block prepended, the
    shape of the real commit, and exits 0. The launcher must still take
    its normal path and exit 0."""
    copy = root / "scripts" / "autoloop.sh"
    shutil.copy(SCRIPT, copy)
    copy.chmod(copy.stat().st_mode | stat.S_IXUSR)
    # ONE comment line, longer than the launcher: wherever bash resumes
    # inside it, the remainder is `)))...` with no `#` before it. Sixty
    # short lines looked the same and were not -- an unwrapped launcher
    # passed when its offset landed exactly on a line's `#` (1 in 71).
    body = SCRIPT.read_text().split("\n", 1)[1]
    assert len(SCRIPT.read_bytes()) < 8000, "grow the comment line with the launcher"
    prepended = "#!/usr/bin/env bash\n# " + ")" * 8000 + "\n" + body
    rewrite = root / "rewrite.sh"
    rewrite.write_text(prepended)
    rewrite.chmod(rewrite.stat().st_mode | stat.S_IXUSR)
    # The rewrite is itself a correct launcher: a FRESH run of it is fine.
    fresh = _run(root, {"FAKE_MODE": "exit", "FAKE_RC": "0"}, script=rewrite)
    assert fresh.returncode == 0, fresh.stderr
    proc = _run(
        root,
        {"FAKE_MODE": "rewrite", "FAKE_REWRITE": str(rewrite), "FAKE_SCRIPT": str(copy)},
        script=copy,
    )
    assert copy.read_text() == prepended, "the fake must have rewritten the running copy"
    assert proc.returncode == 0, f"rc={proc.returncode}\nstderr={proc.stderr}"
    assert "syntax error" not in proc.stderr


UNITS_DIR = REPO / "scripts" / "systemd"
LONG_RUN_S = 3600
PARSED_WHOLE_LAST_LINE = 'main "$@"; exit "$?"'


def _long_running_shell_launchers() -> list[tuple[str, Path]]:
    """(unit name, repo path) for every ExecStart that is a shell script on a
    unit that lives an hour or more: `Type=simple` daemons, or oneshots whose
    `TimeoutStartSec` is at least LONG_RUN_S. Paths are mapped from whichever
    worktree the unit names back into this repo."""
    found = []
    for unit in sorted(UNITS_DIR.glob("*.service")):
        text = unit.read_text()
        simple = re.search(r"^Type=simple$", text, re.M) is not None
        m = re.search(r"^TimeoutStartSec=(.+)$", text, re.M)
        long_oneshot = m is not None and _systemd_seconds(m.group(1)) >= LONG_RUN_S
        if not (simple or long_oneshot):
            continue
        for exe in re.findall(r"^ExecStart=(\S+)", text, re.M):
            if exe.endswith(".sh"):
                rel = exe.split("/scripts/", 1)[1]
                found.append((unit.name, REPO / "scripts" / rel))
    return found


def test_long_running_shell_launchers_are_parsed_whole() -> None:
    """bash reads a script by offset as it runs (mistakes #123), so a shell
    script that a unit keeps alive for hours must be one function called on
    its last line with `exit` on that line -- parsed whole before anything
    runs, and never read past. Enumerated from the unit files, so the next
    hour-long launcher is bound the day it is wired, not the day it dies."""
    launchers = _long_running_shell_launchers()
    assert ("hyxlab-autoloop.service", SCRIPT) in launchers, (
        "vacuity guard: the autoloop launcher is the measured case and must be found"
    )
    for unit, path in launchers:
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        assert lines[-1] == PARSED_WHOLE_LAST_LINE, (
            f"{unit} runs {path.relative_to(REPO)} for >= {LONG_RUN_S}s; its last "
            f"line must be {PARSED_WHOLE_LAST_LINE!r}, got {lines[-1]!r}"
        )
        assert re.search(r"^main\(\) \{$", path.read_text(), re.M), f"{path}: no main() body"
