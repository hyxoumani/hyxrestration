"""Invariants over the vendored systemd units in scripts/systemd/.

2026-07-20 incident: an unrelated workspace saturated the box (60G RAM,
no swap) and the kernel OOM killer shot hyxlab-stream twice, then the
poly sweep and QA. The capture daemons are the only units whose death
loses unrecoverable data; every timer-driven oneshot self-heals on its
next firing. Unprivileged user units cannot LOWER a daemon's OOM score,
but they can RAISE the batch units' — so under global pressure the
kernel prefers sacrificing restartable batch work over live capture.
"""

from pathlib import Path

UNIT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "systemd"


def _services():
    return {p.name: p.read_text() for p in UNIT_DIR.glob("*.service")}


def test_oneshot_units_are_preferred_oom_victims():
    services = _services()
    assert services, "no unit files found"
    for name, text in services.items():
        if "Type=oneshot" in text:
            assert "OOMScoreAdjust=500" in text, (
                f"{name}: timer-driven oneshot units must carry "
                "OOMScoreAdjust=500 so the kernel kills restartable batch "
                "work before the capture daemons"
            )


def test_daemons_are_not_oom_deprioritized():
    # Raising a capture daemon's score would invert the protection; a
    # negative value silently fails in unprivileged user units.
    services = _services()
    for name, text in services.items():
        if "Type=oneshot" not in text:
            assert "OOMScoreAdjust" not in text, (
                f"{name}: daemons must keep the default OOM score "
                "(negative adjusts are unavailable to user units)"
            )
