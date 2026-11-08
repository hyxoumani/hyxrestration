"""`scripts/cgmem.sh`: the last reader of a unit's cgroup memory counters.

A capped unit's `memory.events` -- `max` (times the cgroup hit its cap and
the kernel reclaimed rather than killed) and `oom_kill` -- is removed with
the cgroup the moment the unit stops. systemd's `Consumed ... memory peak`
survives, but it is `memory.peak`: total charge INCLUDING page cache, which
reads == the cap for any healthy unit doing heavy file I/O (ops.md). So
after the fact a comfortable run, a run that reclaimed all day and a run
that was nearly killed print the same number. Measured 2026-10-13: the
poly-sweep's first capped run read `max 2,800,000 / oom_kill 0` live and
left only `6G memory peak` behind; the sweep's first capped run read
`max 0` live 2h in. Neither fact is recoverable from the journal.

`ExecStopPost` runs INSIDE the service's cgroup, after the main process is
gone and before the cgroup is, so a script there is the one reader with
access. Proved 2026-10-13 with transient units: a clean 200M unit logged
`max=0 oom_kill=0`; a 60M unit killed by a 300 MiB ballast logged
`max=37 oom=1 oom_kill=1` AFTER systemd's own "killed some processes" line.

These tests drive the script on a fake cgroup directory (the parse), on a
directory missing every counter (a reporter must never fail the unit it
reports on), and on the real `/proc/self/cgroup` default (the production
path, which must resolve on this box).
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "cgmem.sh"

EVENTS = "low 0\nhigh 3\nmax 2800000\noom 0\noom_kill 1\noom_group_kill 0\nsock_throttled 0\n"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=30)


def _fake(tmp_path: Path, **files: str) -> Path:
    d = tmp_path / "cg"
    d.mkdir(parents=True)
    for name, body in files.items():
        (d / name.replace("_", ".")).write_text(body)
    return d


def test_script_is_executable_and_shellcheck_discovered():
    assert SCRIPT.is_file() and SCRIPT.stat().st_mode & 0o111, f"{SCRIPT} must be executable"
    tracked = subprocess.run(
        ["git", "ls-files", "scripts/cgmem.sh"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    assert tracked.strip() == "scripts/cgmem.sh", "must be tracked so test_shell_lint discovers it"


def test_every_counter_lands_on_one_line(tmp_path):
    d = _fake(tmp_path, memory_events=EVENTS, memory_peak="6442450944\n", memory_max="6442450944\n")
    p = _run(str(d))
    assert p.returncode == 0, p.stderr
    lines = p.stdout.splitlines()
    assert len(lines) == 1, p.stdout
    line = lines[0]
    assert line.startswith(f"[cgmem] dir={d} ")
    for field in (
        "peak=6442450944",
        "cap=6442450944",
        "max=2800000",
        "oom_kill=1",
        "oom=0",
        "high=3",
        "low=0",
        "oom_group_kill=0",
    ):
        assert f" {field}" in line, (field, line)
    # Perturbation: each counter reaches the line (a renderer that reads a
    # value into a local and drops it would pass a presence scan, #95).
    d2 = _fake(
        tmp_path / "b",
        memory_events=EVENTS.replace("max 2800000", "max 7"),
        memory_peak="1\n",
        memory_max="2\n",
    )
    other = _run(str(d2)).stdout
    assert " max=7 " in other and " peak=1 " in other and " cap=2 " in other, other
    assert other != p.stdout


def test_missing_counters_print_as_unknown_and_never_fail(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    p = _run(str(d))
    assert p.returncode == 0, "a reporter must not turn a clean run into a failed one"
    (line,) = p.stdout.splitlines()
    assert " peak=? " in line and " cap=? " in line and " events=? " in line, line
    assert line.endswith(" psi_full_us=?"), line
    # `?` is a stated unknown, never a fabricated zero (ops.md: an unmeasured
    # hold is None, never 0.0).
    assert "=0" not in line, line


def _own_cgroup_dir() -> Path:
    rel = next(
        ln[3:] for ln in Path("/proc/self/cgroup").read_text().splitlines() if ln.startswith("0::")
    )
    return Path("/sys/fs/cgroup" + rel)


def test_default_resolves_the_callers_own_cgroup():
    """Production passes no argument: the dir comes from /proc/self/cgroup.

    The line is judged against the FILESYSTEM, not against an assumption
    about where the suite runs. This test used to assert `events=?` never
    appears, i.e. that the CALLER's cgroup has the memory controller. That
    holds inside a systemd unit (production) and inside a capped slot, and
    is false in an interactive shell whose leaf cgroup has no controllers
    delegated -- e.g. `.../app-org.chromium.Chromium-NNNN.scope/init.scope`,
    whose parent's `cgroup.subtree_control` is empty. promote.sh runs the
    full suite from exactly such a shell, so the old assertion refused
    promotes on 2026-11-06 and 2026-11-08 with the script behaving correctly
    (it printed `?` for a file that does not exist, as designed). EXP-2305.
    """
    p = _run()
    assert p.returncode == 0, p.stderr
    (line,) = p.stdout.splitlines()
    own = _own_cgroup_dir()
    assert line.startswith(f"[cgmem] dir={own} "), (own, line)
    events = own / "memory.events"
    if events.is_file():
        # The production case: every counter in the real file reaches the line,
        # with its real value (a presence scan would pass a dropped value, #95).
        assert " events=?" not in line, f"memory.events exists but was not read: {line}"
        for ln in events.read_text().splitlines():
            key, val = ln.split()
            if key in ("max", "oom", "oom_kill"):  # monotone or rarely-moving
                assert f" {key}=" in line, (key, line)
            if key == "oom_kill":
                # oom_kill can only rise, and only on a kill in this cgroup.
                got = int(line.split(" oom_kill=")[1].split()[0])
                assert got <= int(events.read_text().split("oom_kill ")[1].split()[0]), line
    else:
        # A real cgroup without the memory controller: a stated unknown, never
        # a fabricated zero and never a failure.
        assert " events=? " in line, line
        assert " oom_kill=" not in line, line
    peak = own / "memory.peak"
    assert (" peak=? " in line) == (not peak.is_file()), (peak, line)


PRESSURE = (
    "some avg10=0.00 avg60=0.00 avg300=0.00 total=141016\n"
    "full avg10=0.00 avg60=0.00 avg300=0.00 total=140367\n"
)


def test_psi_full_stall_reaches_the_line(tmp_path):
    """`max` counts reclaim events; `memory.pressure` says what they cost
    (stream, 2026-10-14: 43,761 events, 0.14s of full stall). The stop line
    is the only reader after the cgroup is gone, so it carries the total."""
    d = _fake(
        tmp_path,
        memory_events=EVENTS,
        memory_pressure=PRESSURE,
        memory_peak="1\n",
        memory_max="2\n",
    )
    line = _run(str(d)).stdout
    assert line.rstrip().endswith(" psi_full_us=140367"), line
    d2 = _fake(
        tmp_path / "b", memory_events=EVENTS, memory_pressure=PRESSURE.replace("140367", "7")
    )
    assert _run(str(d2)).stdout.rstrip().endswith(" psi_full_us=7")
