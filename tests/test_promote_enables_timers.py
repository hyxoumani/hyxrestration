"""A promoted timer that was never enabled is installed and INERT.

Measured 2026-09-10. `hyxlab-divergence.timer` was added to
`scripts/systemd/`, promoted, and reported clean by every gate the repo
has: `systemd-analyze verify` passed, the unit-file suite passed, and
`collector.health --drift-only` said `23/23 loaded unit files match the
repo`. `systemctl --user is-enabled hyxlab-divergence.timer` said
`disabled`, and it would never have fired.

Nothing could have caught it. `install_units()` did `cp` +
`daemon-reload`, and enablement is neither: it is the
`timers.target.wants` symlink that only `enable` writes. Every drift arm
compares unit TEXT, and the text was perfect. The defect is invisible to
a file comparison by construction -- the same reason the SHADOWED and
DROP-IN arms exist in `health.judge_drift`.

Lexical on purpose, like `test_systemd_units.py`: it must fail
identically on a box where every timer already happens to be enabled,
because that is exactly the box the next new timer ships onto.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROMOTE = REPO / "scripts" / "promote.sh"
UNIT_DIR = REPO / "scripts" / "systemd"


def _install_units_body() -> str:
    """The body of promote.sh's install_units(), the only place that ships
    unit files to the manager."""
    src = PROMOTE.read_text()
    start = src.index("install_units() {")
    end = src.index("\n}\n", start)
    return src[start:end]


def test_the_repo_actually_ships_timers():
    """Non-vacuity. Every assertion below is about timers; if the repo stops
    shipping any, they all pass while proving nothing."""
    assert list(UNIT_DIR.glob("hyxlab-*.timer"))


def test_install_units_enables_the_timers_it_installs():
    body = _install_units_body()
    assert "systemctl --user enable" in body, (
        "promote.sh copies unit files and reloads, but `enable` is what writes "
        "the timers.target.wants symlink. Without it a NEWLY ADDED timer is "
        "installed, verifies clean, matches the repo byte-for-byte, and never "
        "fires (2026-09-10, hyxlab-divergence.timer)."
    )


def test_the_enabled_set_is_discovered_and_not_enumerated():
    """The set must come from a glob over `scripts/systemd/`.

    An enumeration cannot fail when a new timer appears -- it just omits
    it, which is the very failure this file exists to prevent, restated
    one level up. Same rule the lint scope, the shell-lint set and the
    systemd verify set are each held to.
    """
    body = _install_units_body()
    assert re.search(r"scripts/systemd/hyxlab-\*\.timer", body), (
        "the enabled set must be globbed from scripts/systemd/, so a timer "
        "added tomorrow is enabled by the promotion that installs it"
    )
    named = {p.name for p in UNIT_DIR.glob("hyxlab-*.timer")}
    # CODE only. The comment above the enable explains itself by naming the
    # timer that produced this rule, and prose that cites its own evidence is
    # the thing this repo asks for -- a check that forbade it would push the
    # measurement out of the file it justifies.
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    hardcoded = sorted(n for n in named if n in code)
    assert not hardcoded, (
        f"promote.sh names specific timers ({hardcoded}); the next timer added "
        "would be installed and left inert"
    )


def test_timer_backed_services_are_not_enabled():
    """`enable` on a oneshot .service makes it run at BOOT as well, outside
    the schedule its timer exists to impose. Only timers may be enabled."""
    body = _install_units_body()
    enable_lines = [ln for ln in body.splitlines() if "systemctl --user enable" in ln]
    assert enable_lines
    for line in enable_lines:
        assert ".service" not in line, (
            f"{line.strip()!r} enables a service; a timer-triggered oneshot "
            "would then also fire at boot"
        )
