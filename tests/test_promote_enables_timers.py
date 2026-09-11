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
import shlex
import subprocess
from pathlib import Path

from collector import health

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
    assert re.search(r"scripts/systemd/hyxlab-\*", body), (
        "the enabled set must be globbed from scripts/systemd/, so a timer "
        "added tomorrow is enabled by the promotion that installs it"
    )
    named = {p.name for p in UNIT_DIR.glob("hyxlab-*")}
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


# --------------------------------------------------------------------------
# WHICH units, and the rule that decides (2026-09-11).
#
# This file used to say "timers only", because enabling a timer-backed oneshot
# .service would also fire it at boot, outside the schedule its timer exists to
# impose. That is true, and it is a PROXY. `enable` does exactly one thing --
# create the symlinks a unit's `[Install]` section names -- so a unit with no
# `[Install]` cannot be enabled at all (systemd: `static`), and no timer-backed
# service here has one. Selecting on `[Install]` excludes the same nine services
# and additionally covers the three DAEMONS the timer glob silently omitted.
#
# The tests below run promote.sh's OWN selection code over the repo's real unit
# files and compare the result with `health._wants_enabling`, because the INERT
# verdict is classified REPAIRABLE on the promise that this script repairs it.
# A re-implementation of the rule in python would be a second rule.
# --------------------------------------------------------------------------


def _selected() -> tuple[list[str], list[str]]:
    """The (timers, services) promote.sh would enable, from promote.sh's code."""
    body = _install_units_body()
    start = body.index("local timers=()")
    end = body.index("systemctl --user enable --now", start)
    loop = body[start:end]
    script = (
        f'DEV={shlex.quote(str(REPO))}\n'
        # Printed INSIDE the function: the arrays are `local` in promote.sh, and
        # reading them after it returns would report an empty set for every
        # possible selection rule -- a test that passes on no evidence.
        "sel() {\n" + loop + 'printf "timers:%s\\n" "${timers[*]-}"\n'
        'printf "svcs:%s\\n" "${svcs[*]-}"\n}\nsel\n'
    )
    out = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    got = dict(ln.split(":", 1) for ln in out.stdout.splitlines() if ":" in ln)
    return got["timers"].split(), got["svcs"].split()


def test_promote_and_the_digest_select_the_same_units():
    """One rule, two consumers. `health.judge_drift` reports INERT for a unit the
    repo says should be enabled and the manager says is not, and classifies that
    REPAIRABLE -- a promise that THIS script enables it. If the two sets drift
    apart, `promote.sh --units-only` reports "repairable drift SURVIVED its own
    repair" on a unit it never intended to touch."""
    timers, svcs = _selected()
    selected = set(timers) | set(svcs)
    expected = {f.name for f in UNIT_DIR.glob("hyxlab-*") if health._wants_enabling(f.read_text())}
    assert expected, "no vendored unit declares [Install]; this arm proves nothing"
    assert selected == expected, (
        f"promote.sh enables {sorted(selected)}, health expects {sorted(expected)}; "
        "the difference is units that would be reported INERT and never repaired"
    )


def test_the_selection_is_by_install_section_not_by_filename():
    """The discriminator must be the `[Install]` section itself. A `*.timer` glob
    gets today's answer right and omits the daemons; a hardcoded service list
    omits whatever is added next."""
    body = _install_units_body()
    assert "[Install]" in body
    timers, svcs = _selected()
    assert svcs, "the daemons declare [Install] and must be in the set"
    assert all(s.endswith(".service") for s in svcs)
    assert all(t.endswith(".timer") for t in timers)


def test_timer_backed_services_are_not_enabled():
    """`enable` on a oneshot .service makes it run at BOOT as well, outside
    the schedule its timer exists to impose. Those services have no `[Install]`,
    which is both why systemd refuses to enable them and how the selection
    excludes them -- asserted here against the repo's actual timer-backed set."""
    _, svcs = _selected()
    backed = {
        f"{t.stem}.service" for t in UNIT_DIR.glob("hyxlab-*.timer")
        if (UNIT_DIR / f"{t.stem}.service").exists()
    }
    assert backed, "no timer-backed service in the repo; this arm proves nothing"
    assert not (backed & set(svcs)), (
        f"{sorted(backed & set(svcs))} would also fire at boot, outside its timer's schedule"
    )


def test_a_daemon_is_enabled_but_not_started_by_the_repair():
    """`--now` for timers only. Enabling a daemon writes the boot symlink, which
    is the INERT state being repaired; STARTING one the operator stopped is a
    side effect a unit-FILE repair has no business having -- and these daemons
    own DuckDB files under a lock. The restart stage is where a daemon is
    deliberately brought up."""
    body = _install_units_body()
    now_lines = [ln for ln in body.splitlines() if "enable --now" in ln]
    assert len(now_lines) == 1 and "timers" in now_lines[0], now_lines
    svc_lines = [
        ln for ln in body.splitlines()
        if "systemctl --user enable" in ln and "svcs" in ln
    ]
    assert len(svc_lines) == 1 and "--now" not in svc_lines[0], svc_lines
