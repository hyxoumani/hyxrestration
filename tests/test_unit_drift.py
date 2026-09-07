"""The units systemd has LOADED, against the units this repo contains.

The unit-gate pass (2026-09-06) parsed `scripts/systemd/*` the way the manager
does and closed by naming its own successor: "nothing checks that the units
systemd has LOADED match the repo; a `systemctl cat` drift after a hand edit is
invisible to both existing gates." Both gates read the REPO's files --
`test_systemd_units.py` greps them, `test_systemd_verify.py` parses them -- and
`promote.sh` copies them out and never looks back. Between the copy and the next
promote, the installed set is unowned.

The obvious check is `diff scripts/systemd/x ~/.config/systemd/user/x`, and three
of the four states below are states where that diff reports CLEAN and the manager
is still running something else. Each was measured on this box on 2026-09-06
against a throwaway `hyxprobe-drift.service`, never against a hyxlab unit:

  SHADOWED         unit lookup walks a search path; a copy in a higher-priority
                   directory wins and the installed file is never read. Both
                   files the diff compares are correct; neither is the one
                   running. Only `FragmentPath` answers which file was loaded.
  STALE-IN-MEMORY  measured on an ACTIVE probe: editing the fragment without
                   `daemon-reload` leaves `systemctl show` reporting the OLD
                   text (`Description=probe` while the file said `EDITED BY
                   HAND`) and `NeedDaemonReload=yes`. repo == disk, and the
                   manager runs neither.
  DROP-IN          `<unit>.d/*.conf` overrides directives without touching the
                   fragment: `ExecStart=` + `ExecStart=/bin/echo hijacked`
                   replaced the command outright, measured, fragment untouched.
                   A live practice on this box -- `hylshi-watchdog.service.d`.
  DRIFT            the fragment's text differs. The one state a naive diff does
                   catch.

The arms are asserted against `health.judge_drift`, which is pure, so they hold
regardless of what this box is running while the suite executes.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from collector import health

REPO = Path(__file__).resolve().parent.parent
VENDORED = "[Unit]\nDescription=probe\n[Service]\nExecStart=/bin/true\n"
INSTALLED = str(health.INSTALL_DIR / "hyxlab-probe.service")


def props(**over: str) -> dict[str, str]:
    """A unit loaded from the install dir, in sync, with no overrides."""
    base = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "FragmentPath": INSTALLED,
        "DropInPaths": "",
        "NeedDaemonReload": "no",
    }
    return base | over


def judge(loaded_text: str | None = VENDORED, **over: str) -> health.UnitDrift:
    return health.judge_drift("hyxlab-probe.service", props(**over), loaded_text, VENDORED)


# --------------------------------------------------------------------------
# The three states a repo-vs-install file diff reports CLEAN.
# --------------------------------------------------------------------------


def test_a_shadowing_copy_is_caught_although_both_diffed_files_are_correct():
    """Unit lookup walks a search path. With an identical file installed AND an
    identical file in the repo, a diff of those two is clean while systemd runs
    a third. `FragmentPath` is the only field that names what was read."""
    d = judge(FragmentPath="/etc/systemd/user/hyxlab-probe.service")
    assert d.state == "SHADOWED"
    assert "/etc/systemd/user" in d.detail, "the verdict must name the file the manager actually loaded"


def test_a_loaded_unit_with_no_fragment_path_is_not_silently_ok():
    """The empty string must not fall through to a text comparison against a
    file that was never opened -- that would read OK on no evidence at all. The
    directory test already covers this (`Path("").parent` is `.`), so what is
    pinned here is the WORDING: a reader told "manager loaded , not ..." cannot
    tell an empty field from a mangled one."""
    d = judge(FragmentPath="")
    assert d.state == "SHADOWED"
    assert "no fragment path" in d.detail


def test_an_unreloaded_edit_is_caught_although_repo_and_disk_agree():
    """The measured state: active unit, fragment edited on disk, no
    `daemon-reload`. `show` still reports the old text. This is what a
    `promote.sh` whose `daemon-reload` failed leaves behind, and no comparison
    of FILES can see it -- both files are the new text and the manager holds
    the old one."""
    d = judge(NeedDaemonReload="yes")
    assert d.state == "STALE-IN-MEMORY"
    assert "daemon-reload" in d.detail


def test_a_drop_in_is_caught_although_the_fragment_is_byte_identical():
    """Measured: `ExecStart=` + `ExecStart=/bin/echo hijacked` in a `.d/`
    override replaced the command outright with the fragment untouched. Every
    other unit check in the tree reads the fragment only."""
    d = judge(DropInPaths="/home/devs/.config/systemd/user/hyxlab-probe.service.d/o.conf")
    assert d.state == "DROP-IN"
    assert "o.conf" in d.detail, "the verdict must name the override file so it can be read"


def test_a_drop_in_added_without_a_reload_is_still_caught():
    """Measured interaction, and the reason neither arm is redundant: until the
    reload, `DropInPaths` is EMPTY and `NeedDaemonReload` is yes; after it, the
    reverse. Either way the unit is not OK -- the two arms cover each other's
    blind window."""
    assert not judge(DropInPaths="", NeedDaemonReload="yes").ok


def test_an_inactive_unit_reading_no_reload_needed_is_not_a_hole():
    """When an inactive unit does read `no`, the text comparison is the whole
    answer there -- and it still fires."""
    d = judge("[Unit]\nDescription=EDITED BY HAND\n", ActiveState="inactive", NeedDaemonReload="no")
    assert d.state == "DRIFT"


def test_an_inactive_unit_is_not_assumed_to_be_reload_clean():
    """CORRECTED 2026-09-07 by measurement. 09-06 recorded the complement as
    "for an INACTIVE unit the field ALWAYS reads no, because systemd
    garbage-collects the unreferenced unit". It GCs the UNREFERENCED unit:
    `hyxlab-backup.service`, inactive/dead, reported `NeedDaemonReload=yes`
    after its installed file was touched, because `hyxlab-backup.timer`
    references it and it stays loaded. Every timer-backed service here is
    referenced that way, so an `ActiveState`-conditioned arm would have gone
    quiet on nearly the whole set. The arm reads the field, never the state."""
    d = judge(ActiveState="inactive", NeedDaemonReload="yes")
    assert d.state == "STALE-IN-MEMORY"


def test_a_hand_edit_reads_stale_before_it_reads_drift():
    """Measured 2026-09-07: touching an installed file leaves the manager
    reporting the OLD text, so the fragment comparison sees repo vs stale-loaded
    and would answer OK. Ordering the reload arm first is what makes the fault
    visible at all; the two repairable states are two phases of one fault."""
    d = judge(VENDORED, NeedDaemonReload="yes")
    assert d.state == "STALE-IN-MEMORY", "a stale load must not be judged by text that is stale too"


# --------------------------------------------------------------------------
# The state a diff does catch, and the states that must stay quiet.
# --------------------------------------------------------------------------


def test_a_hand_edited_fragment_is_drift():
    d = judge("[Unit]\nDescription=EDITED BY HAND\n")
    assert d.state == "DRIFT"
    assert "scripts/systemd/hyxlab-probe.service" in d.detail


def test_an_unreadable_fragment_is_not_reported_as_agreement():
    """The manager named a file this process cannot open. That is not a match,
    and reporting OK would be an unmeasured claim."""
    assert judge(None).state == "UNREADABLE"


def test_an_uninstalled_unit_is_skipped_because_judge_already_reports_it():
    """`judge` prints UNLOADED for the same unit (health's measurement (4)).
    Two lines about one fact trains the reader to skim the section."""
    d = health.judge_drift("hyxlab-new.service", props(LoadState="not-found"), None, VENDORED)
    assert d.state == "SKIP"
    assert not d.ok, "SKIP is not agreement; it must not be counted as a match"


def test_an_in_sync_unit_is_ok_and_quotes_the_file_it_matched():
    d = judge()
    assert d.state == "OK" and d.ok
    assert d.detail == INSTALLED


# --------------------------------------------------------------------------
# The subject: the set, the fields, and the install path all come from the
# same places the installer uses.
# --------------------------------------------------------------------------


def test_the_checked_set_is_every_file_promote_installs_not_just_services():
    """`discover_units` answers "what runs" -- 12 services. `promote.sh` copies
    `hyxlab-*`, which is 21 files: a hand-edited TIMER changes when everything
    downstream runs and appears in no service."""
    files = health.discover_unit_files()
    vendored = sorted(f.name for f in (REPO / "scripts/systemd").glob("hyxlab-*") if f.is_file())
    assert files == vendored, "the set must be globbed, never enumerated"
    assert [f for f in files if f.endswith(".timer")], "timers are in the set promote installs"
    assert {u for u, _ in health.discover_units()} <= set(files)


def test_install_dir_is_the_directory_promote_sh_copies_into():
    """promote.sh's install line is the source of truth. If the destination
    moves, every drift verdict silently becomes SHADOWED against the old path,
    which is a checker that cries wolf on a healthy box."""
    script = (REPO / "scripts" / "promote.sh").read_text(encoding="utf-8")
    installs = [ln.strip() for ln in script.splitlines() if "systemd/user" in ln and ln.strip().startswith("cp ")]
    assert len(installs) == 1, f"expected one unit-install line in promote.sh, got {installs}"
    dest = installs[0].split()[-1].rstrip("/")
    assert Path(dest.replace("~", str(Path.home()))) == health.INSTALL_DIR, (
        f"promote.sh installs into {dest}, health.INSTALL_DIR is {health.INSTALL_DIR}"
    )


def test_the_fields_the_arms_read_are_fields_the_digest_asks_for():
    """`show` requests exactly `PROPS`. A field the arms read but `show` does not
    request comes back missing, and every arm keyed on it goes quiet -- the
    failure mode is a checker that always passes."""
    for field in ("FragmentPath", "DropInPaths", "NeedDaemonReload"):
        assert field in health.PROPS


def test_the_digest_prints_the_drift_section(capsys, monkeypatch):
    """The section must reach stdout: the digest's whole value is that the
    autoloop READS it (the egress pass's finding), so a verdict computed and not
    printed is the defect this module exists to answer."""
    monkeypatch.setattr(health, "drift_report", lambda: [health.UnitDrift("hyxlab-probe.service", "DROP-IN", "o.conf")])
    monkeypatch.setattr(health, "report", lambda now=None: [])
    monkeypatch.setattr(health, "show", lambda unit: {})
    monkeypatch.setattr(health, "_load_state", lambda path: {})
    health.main()
    out = capsys.readouterr().out
    assert "DROP-IN" in out and "hyxlab-probe.service" in out
    assert re.search(r"0/1 loaded unit files match the repo", out)
    assert "DRIFT: ['hyxlab-probe.service']" in out


# --------------------------------------------------------------------------
# Live: what this box is actually running.
# --------------------------------------------------------------------------


def test_the_installed_units_on_this_box_match_the_repo():
    """The gate itself. It is deliberately not portable: off this box the units
    are not installed and every one reads SKIP, which is vacuous but not wrong.
    Here, a non-OK verdict means the manager is running something no test in the
    tree has ever read."""
    bad = [(d.unit, d.state, d.detail) for d in health.drift_report() if not (d.ok or d.state == "SKIP")]
    assert not bad, f"units systemd has loaded do not match this repo: {bad}"


# --------------------------------------------------------------------------
# The repair (2026-09-07). Detection without repair is half a check.
# --------------------------------------------------------------------------
#
# The unit-drift pass shipped four verdicts and no way to act on them: the
# repair -- `cp` + `daemon-reload` -- lived only inside `promote.sh`'s happy
# path, so the only supported way to fix a hand-edited unit was to promote
# CODE, which runs the full suite, fast-forwards `stable`, and can restart a
# daemon whose cost is measured in days of span (EXP-961). `--units-only` is
# that repair with nothing else attached.
#
# The finding these tests exist to pin is that the repair is PARTIAL, and that
# a mode which did not say so would be the exact lie `collector.health` was
# built to stop telling: `cp` + reload rewrites the installed FILE and re-reads
# it, which clears DRIFT and STALE-IN-MEMORY and touches neither SHADOWED (the
# winning copy is in a directory this repo does not own) nor DROP-IN (an
# override that survives every fragment rewrite by construction). A naive
# repair would exit 0 on both.

PROMOTE = (REPO / "scripts" / "promote.sh").read_text(encoding="utf-8")
UNITS_ONLY_BLOCK = PROMOTE.split("if ((UNITS_ONLY)); then", 1)[-1].split("\nfi\n", 1)[0]
#: The block with its comment lines removed. The first draft of the scan below
#: matched `test_unit_drift.py` in the COMMENT that explains why the mode must
#: not run it -- the same failure as 2026-09-06's premise scan, which matched the
#: file documenting the premise. A scan that reads prose is testing the prose.
UNITS_ONLY_CODE = "\n".join(
    ln for ln in UNITS_ONLY_BLOCK.splitlines() if not ln.lstrip().startswith("#")
)


def test_the_states_a_reinstall_cannot_clear_are_not_promised():
    """SHADOWED and DROP-IN are structurally out of reach of `cp` + reload.
    Promising them is worse than not repairing at all: it converts a detected
    fault into a green line."""
    assert "SHADOWED" not in health.REPAIRABLE
    assert "DROP-IN" not in health.REPAIRABLE
    assert set(health.REPAIRABLE) == {"DRIFT", "STALE-IN-MEMORY"}


def test_every_state_the_judge_can_emit_is_classified():
    """Read the verdict words out of `judge_drift`'s SOURCE. A fifth state added
    later and left out of both sets would be silently treated as unrepairable by
    `drift_exit_code`'s `not in REPAIRABLE` -- a defensible default, but an
    UNSTATED one, and the membership question is exactly what this pass is
    about."""
    src = inspect.getsource(health.judge_drift)
    emitted = set(re.findall(r'UnitDrift\(\s*\n?\s*unit,\s*\n?\s*"([A-Z-]+)"', src))
    emitted |= set(re.findall(r'"(SKIP|OK|SHADOWED|STALE-IN-MEMORY|DROP-IN|DRIFT|UNREADABLE)",', src))
    assert {"SHADOWED", "STALE-IN-MEMORY", "DROP-IN", "DRIFT", "UNREADABLE"} <= emitted, emitted
    unclassified = emitted - {"OK", "SKIP"} - set(health.REPAIRABLE) - set(health.UNREPAIRABLE)
    assert not unclassified, f"judge_drift can emit {unclassified}, which no set classifies"
    assert not set(health.REPAIRABLE) & set(health.UNREPAIRABLE)


def test_a_clean_box_exits_clean_and_a_skip_is_not_a_fault():
    """An uninstalled unit reads SKIP -- vacuous off this box, and it must not
    make the repair mode fail on a laptop."""
    assert health.drift_exit_code([]) == health.DRIFT_CLEAN
    rows = [
        health.UnitDrift("a.service", "OK", "/x"),
        health.UnitDrift("b.service", "SKIP", "LoadState=not-found"),
    ]
    assert health.drift_exit_code(rows) == health.DRIFT_CLEAN


def test_a_repairable_state_asks_for_the_script_not_a_human():
    for state in health.REPAIRABLE:
        rows = [health.UnitDrift("a.service", state, "d")]
        assert health.drift_exit_code(rows) == health.DRIFT_REPAIRABLE, state


def test_a_state_no_script_can_clear_outranks_every_repairable_one():
    """The useful thing to report is the WORST thing the caller cannot fix. A
    box with one drifted unit and one shadowed unit is a box that needs a human,
    and a mode returning `repairable` there would send the operator away."""
    for state in health.UNREPAIRABLE:
        rows = [
            health.UnitDrift("a.service", "DRIFT", "d"),
            health.UnitDrift("b.service", state, "d"),
        ]
        assert health.drift_exit_code(rows) == health.DRIFT_OPERATOR, state


def test_drift_only_prints_the_remedy_per_unit_and_returns_the_status(capsys, monkeypatch):
    """The mode's two consumers are a shell `case` (the status) and whoever
    reads the log (the lines). Both, or it is another unread verdict."""
    monkeypatch.setattr(
        health,
        "drift_report",
        lambda: [
            health.UnitDrift("a.service", "DRIFT", "differs"),
            health.UnitDrift("b.service", "DROP-IN", "b.d/o.conf"),
            health.UnitDrift("c.service", "SKIP", "LoadState=not-found"),
            health.UnitDrift("d.service", "OK", "/x"),
        ],
    )
    assert health.drift_main() == health.DRIFT_OPERATOR
    out = capsys.readouterr().out
    assert "promote.sh --units-only" in out, "a repairable state must name its repair"
    assert "OPERATOR" in out, "an unrepairable state must not name a repair that cannot work"
    assert "1/3 loaded unit files match the repo" in out, "SKIP is not counted as checked"


def test_the_digest_itself_still_exits_zero_and_the_subcommand_does_not(monkeypatch):
    """The report/gate split. `main()` stays a report -- a gate whose failure
    nothing reads is the defect `collector.health` exists to answer -- and
    `--drift-only` is a gate precisely BECAUSE promote.sh branches on it."""
    monkeypatch.setattr(health, "drift_report", lambda: [health.UnitDrift("a.service", "DRIFT", "d")])
    monkeypatch.setattr(health, "report", lambda now=None: [])
    monkeypatch.setattr(health, "show", lambda unit: {})
    monkeypatch.setattr(health, "_load_state", lambda path: {})
    assert health.cli([]) == 0
    assert health.cli(["--drift-only"]) == health.DRIFT_REPAIRABLE
    assert health.cli(["--nonsense"]) == health.USAGE_ERROR, "an unknown flag must not read as clean"
    assert health.USAGE_ERROR not in (health.DRIFT_CLEAN, health.DRIFT_REPAIRABLE, health.DRIFT_OPERATOR), (
        "a mistyped flag sharing a drift code makes promote.sh explain a fault that does not exist"
    )


def test_units_only_verifies_its_own_repair_and_exits_with_the_verdict():
    """A `cp` that exits 0 is not evidence the box is fixed. Without the
    re-check, `--units-only` would report success on a SHADOWED box -- detection
    reduced to decoration by its own repair."""
    assert "--units-only" in PROMOTE, "the repair mode must exist"
    assert "collector.health --drift-only" in UNITS_ONLY_CODE, (
        "the repair must re-ask the judge; a copy is not a verdict"
    )
    assert re.search(r"exit \"?\$rc", UNITS_ONLY_CODE), (
        "the shell must propagate the verify status, not swallow it"
    )
    assert "install_units" in UNITS_ONLY_CODE


def test_the_repair_installs_through_the_same_line_the_promote_does():
    """Two spellings of the destination is how INSTALL_DIR and the installer
    drift apart, which turns every verdict into SHADOWED on a healthy box
    (test_install_dir_is_the_directory_promote_sh_copies_into)."""
    cps = [ln.strip() for ln in PROMOTE.splitlines() if ln.strip().startswith("cp ") and "systemd/user" in ln]
    assert len(cps) == 1, f"expected exactly one unit-install line, got {cps}"
    assert PROMOTE.count("install_units() {") == 1
    assert PROMOTE.count("systemctl --user daemon-reload") == 1


def test_the_repair_does_not_gate_on_the_check_it_repairs():
    """`tests/test_unit_drift.py` has a live arm asserting the installed units
    already match the repo. Gating the repair on it means it can only run on a
    box that does not need it -- the deadlock this line exists to keep out."""
    assert "test_unit_drift.py" not in UNITS_ONLY_CODE
    assert "pytest tests/ " not in UNITS_ONLY_CODE, "the full suite is the gate on CODE, which this mode does not move"
    for gate in ("test_systemd_units.py", "test_systemd_verify.py"):
        assert gate in UNITS_ONLY_CODE, f"{gate} reads the files this mode installs"


def test_units_only_moves_no_code_and_restarts_no_daemon():
    """The whole point is that a drift repair must not cost what a promote
    costs: the full suite, a fast-forward of `stable`, and a daemon restart
    priced in days of shadow span (EXP-961)."""
    assert "merge --ff-only" not in UNITS_ONLY_CODE
    assert "systemctl --user restart" not in UNITS_ONLY_CODE
    assert "needs_restart" not in UNITS_ONLY_CODE
    # and it must return before reaching any of them.
    assert PROMOTE.index("if ((UNITS_ONLY)); then") < PROMOTE.index("merge --ff-only")
