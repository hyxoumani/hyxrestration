"""Invariants over the vendored systemd units in scripts/systemd/.

2026-07-20 incident: an unrelated workspace saturated the box (60G RAM,
no swap) and the kernel OOM killer shot hyxlab-stream twice, then the
poly sweep and QA. The capture daemons are the only units whose death
loses unrecoverable data; every timer-driven oneshot self-heals on its
next firing. Unprivileged user units cannot LOWER a daemon's OOM score,
but they can RAISE the batch units' — so under global pressure the
kernel prefers sacrificing restartable batch work over live capture.

2026-08-03 incident (EXP-946/947/950): `hyxlab-sweep.timer` and
`hyxlab-tradepass.timer` carried `OnCalendar=*-*-* 06:10:00` /
`06:35:00` with NO timezone suffix while their own `Description=` lines
advertised "06:10 UTC" / "06:35 UTC". systemd defaults a suffix-less
OnCalendar to LOCAL time and the host is America/Chicago, so they fired
11:10Z / 11:35Z — five hours late. `hyxlab-qa.timer` DOES pin UTC and
fires 07:00Z, so QA audited the trade tape 4h10m BEFORE the sweep that
fills it, inverting the documented sweep -> tradepass -> qa order every
day. EXP-947 measured 30–85 min/day of collector outage from it.

Nothing existing could catch that: the repo shipped the SAME suffix-less
line in both worktrees, so installed and source agreed byte-for-byte
(EXP-946, 34/34) and a parity check is structurally blind; every run
still exited 0, so no exit-code or `--state=failed` check fired either.
The ONLY artefact disagreeing with the code was the code's own prose.
Hence `test_timers_pin_the_timezone_their_prose_claims`: if a
Description names a timezone, the OnCalendar must actually pin it.

This file is a promotion gate — `scripts/promote.sh` runs this suite in
the dev tree and copies `scripts/systemd/hyxlab-*` to
~/.config/systemd/user/ only if it is green, so a regression here cannot
reach the installed units.
"""

import re
from pathlib import Path

UNIT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "systemd"

# A timezone MENTION in prose: "06:10 UTC", "23:00-08:00Z".
_PROSE_TZ_RE = re.compile(r"\bUTC\b|\b\d{1,2}:\d{2}Z\b")

# A trailing timezone suffix on an OnCalendar spec: a zone name ("UTC",
# "America/Chicago") or a numeric offset ("+0200"). systemd accepts both.
_TZ_SUFFIX_RE = re.compile(
    r"\s(?:[A-Za-z]+(?:/[A-Za-z_+-]+)+|[A-Z]{2,5}|[+-]\d{2}:?\d{2})$"
)

# An hour-pinned spec: a concrete hour field followed by :MM. Specs like
# `*:0/5` and `*:2/5` pin no wall-clock hour, so they fire at the same
# instants in every zone and need no suffix.
_HOUR_PINNED_RE = re.compile(r"(?<![\d/*])\d{1,2}(?:,\d{1,2})*:\d{2}")


def _services():
    return {p.name: p.read_text() for p in UNIT_DIR.glob("*.service")}


def _timers():
    return {p.name: p.read_text() for p in UNIT_DIR.glob("*.timer")}


def _field(text, key):
    """Every value of `key` in a unit; systemd allows repeated keys."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            out.append(v.strip())
    return out


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


def test_timers_pin_the_timezone_their_prose_claims():
    """A Description naming a timezone must be backed by a pinned OnCalendar.

    Purely lexical on purpose: no systemd runtime and no dependence on the
    host's own timezone, so this fails identically on a UTC box (where the
    bug is invisible at runtime) and on the America/Chicago host where it
    cost us collection time.
    """
    timers = _timers()
    assert timers, "no timer units found"
    offenders = []
    for name, text in sorted(timers.items()):
        desc = " ".join(_field(text, "Description"))
        if not _PROSE_TZ_RE.search(desc):
            continue
        for spec in _field(text, "OnCalendar"):
            pinned = _HOUR_PINNED_RE.search(spec) or spec.lower() == "daily"
            if pinned and not _TZ_SUFFIX_RE.search(spec):
                offenders.append(f"{name}: Description={desc!r} but OnCalendar={spec!r}")
    assert not offenders, (
        "timer prose names a timezone the OnCalendar does not pin; systemd "
        "defaults a suffix-less hour-pinned OnCalendar to LOCAL time, so the "
        "unit fires at a different instant than its own Description "
        "advertises:\n  " + "\n  ".join(offenders)
    )
