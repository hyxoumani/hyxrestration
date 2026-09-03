"""The skip probe must never START where a scheduled cycle could find the
lock held: a probe that itself skipped a real cycle would be the defect it
exists to rule out."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "probe_collect_skip", Path(__file__).resolve().parents[1] / "scripts/probe_collect_skip.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_window_excludes_the_scheduled_cycle_and_the_next_tick():
    tick = 1_756_000_000 - (1_756_000_000 % 300)  # an exact *:0/5 boundary
    assert not probe.in_window(tick + 0)  # the unit is starting right now
    assert not probe.in_window(tick + 25)  # measured cycle end (total_s 17-23)
    assert probe.in_window(tick + 40)
    assert probe.in_window(tick + 200)
    assert not probe.in_window(tick + 250)  # a fetch + wait would cross +300
    assert not probe.in_window(tick + 299)


def test_probe_budget_fits_inside_the_window():
    """Fetch (~20 s) + lock wait must end before the next tick even from the
    late edge of the window, or the scheduled cycle waits on the probe."""
    assert probe.WINDOW_S[1] + 20 + probe.LOCK_WAIT_S + 10 < 300
