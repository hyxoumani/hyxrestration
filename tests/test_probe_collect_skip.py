"""The skip probe must never START where a scheduled cycle could find the
lock held: a probe that itself skipped a real cycle would be the defect it
exists to rule out."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "probe_collect_skip", Path(__file__).resolve().parents[1] / "scripts/probe_collect_skip.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


#: Measured 2026-09-03 over 20 consecutive production cycles: the collector
#: FETCHES first and only then takes the writer lock, so this is how late after
#: its tick a scheduled cycle can still reach for the lock.
MAX_MEASURED_FETCH_S = 54.3
MAX_MEASURED_WRITE_S = 3.5


def test_window_opens_only_after_a_scheduled_cycle_can_still_want_the_lock():
    """The 09-02 window opened at +40 s on the belief that a cycle is done by
    +25 s. Measured, `fetch_s` reaches 54.3 s and the lock is taken AFTER the
    fetch, so a cycle can grab it at ~+58 s -- inside that window. A probe
    holding the lock then makes a real cycle wait, which is the one thing this
    script promises not to do."""
    assert probe.WINDOW_S[0] >= MAX_MEASURED_FETCH_S + MAX_MEASURED_WRITE_S

    tick = 1_756_000_000 - (1_756_000_000 % 300)  # an exact *:0/5 boundary
    assert not probe.in_window(tick + 0)  # the unit is starting right now
    assert not probe.in_window(tick + 40)  # a cycle may still take the lock
    assert not probe.in_window(tick + 58)  # the latest measured release
    assert probe.in_window(tick + 60)
    assert probe.in_window(tick + 170)
    assert not probe.in_window(tick + 200)  # worst-case hold would cross +300
    assert not probe.in_window(tick + 299)


def test_probe_worst_case_hold_clears_the_next_tick():
    """The probe's real worst case is its subprocess timeout, not a typical
    fetch: budgeting with the typical number is how a guard passes review and
    still overruns in production."""
    assert probe.WINDOW_S[1] + probe.SUBPROCESS_TIMEOUT_S + 10 < 300


def _show(state="inactive", exit_us=None):
    return f"ActiveState={state}\nExecMainExitTimestampMonotonic={exit_us}\n"


# now = 200 s past a tick; monotonic clock reads 500_000 s, so the tick
# boundary sits at 499_800 s on the monotonic scale.
NOW = 1_756_000_000 + 200 - (1_756_000_000 % 300)
MONO = 500_000.0
TICK_MONO = 499_800.0


def _settled(show):
    return probe.collect_cycle_settled(show=show, now=NOW, mono=MONO)


def test_gate_opens_when_this_ticks_cycle_has_exited():
    ok, why = _settled(_show(exit_us=int((TICK_MONO + 33) * 1e6)))
    assert ok, why


def test_gate_refuses_when_the_last_exit_predates_this_tick():
    """`inactive` alone is not enough: between the tick and the cycle's start
    the unit is also inactive, still holding LAST tick's exit stamp."""
    ok, why = _settled(_show(exit_us=int((TICK_MONO - 270) * 1e6)))
    assert not ok
    assert "has not run yet" in why


def test_gate_refuses_while_the_cycle_is_running():
    ok, why = _settled(_show(state="activating", exit_us=int((TICK_MONO + 10) * 1e6)))
    assert not ok
    assert "still running" in why


def test_gate_refuses_when_systemd_cannot_be_READ():
    """An unreadable witness is not permission -- a guard that opens when it
    cannot see is not a guard."""
    ok, why = _settled(None)
    assert not ok
    assert "could not read" in why


def test_gate_refuses_a_unit_that_never_ran_this_boot():
    ok, why = _settled(_show(exit_us=0))
    assert not ok
    assert "never run" in why


def test_gate_refuses_an_unparsable_timestamp():
    ok, why = _settled(_show(exit_us="n/a"))
    assert not ok
    assert "no usable exit timestamp" in why


def test_unreadable_systemd_is_not_spellable_as_not_supplied():
    """`show=None` must MEAN unreadable. If None doubled as "not supplied" the
    test above would quietly query the real host and pass for the wrong
    reason."""
    assert probe._QUERY_SYSTEMD is not None


def test_documented_invocation_can_import_the_project():
    """`.venv/bin/python scripts/probe_collect_skip.py` -- the invocation this
    module's docstring prescribes -- puts scripts/ on sys.path[0], NOT the repo
    root, so `from hyxlab import lockid` raises ModuleNotFoundError unless the
    script bootstraps the root itself.

    The probe shipped 2026-09-02 unrunnable for exactly this reason and its unit
    tests were green the whole time: pytest's `pythonpath = ["."]` puts the root
    on sys.path for the in-process import above, so nothing here ever saw the
    path the operator actually gets. This runs the module top-level in a
    subprocess under the REAL invocation's sys.path, with `run_name` set to
    something other than "__main__" so `main()` -- which takes the live writer
    lock and spends a real fetch -- never executes.
    """
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts/probe_collect_skip.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    p = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; sys.path[0] = sys.argv[1]; "
            "runpy.run_path(sys.argv[2], run_name='probe_import_check')",
            str(script.parent),
            str(script),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
    )
    assert p.returncode == 0, p.stderr
    assert "PROVEN" not in p.stdout and "REFUSED" not in p.stdout, (
        f"main() ran during an import check: {p.stdout}"
    )


def test_main_refuses_and_spawns_nothing_when_the_gate_is_shut(tmp_path, monkeypatch, capsys):
    """The gate is only worth having if `main()` actually turns back on it --
    and it must turn back BEFORE taking the lock or spending a fetch, since a
    probe that reaches for the writer lock during a live cycle is the harm."""
    fake = tmp_path / ".venv/bin"
    fake.mkdir(parents=True)
    (fake / "python").write_text("")
    monkeypatch.setattr(probe, "STABLE", tmp_path)
    monkeypatch.setattr(probe, "in_window", lambda *a, **k: True)
    monkeypatch.setattr(probe, "collect_cycle_settled", lambda *a, **k: (False, "cycle is running"))

    def explode(*a, **k):  # pragma: no cover - the assertion is that it is unreachable
        raise AssertionError("main() spawned the collector with the gate shut")

    monkeypatch.setattr(probe.subprocess, "run", explode)
    monkeypatch.setattr(probe.fcntl, "flock", explode)

    assert probe.main() == 1
    assert "REFUSED: cycle is running" in capsys.readouterr().out
