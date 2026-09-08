"""What the checkers already decided, read back for the one channel that leaves this box.

    python -m collector.health

Why this exists (2026-09-06). Five status passes in a row built a checker
and then discovered its verdict was read by nothing: qa's `sys.exit(1)`
set a `failed` state nothing queried, its `NOT a full pass` line exits 0,
the shell gate and the unit gate are consumed by a test run. The
`qa_prior_run` pass closed the smallest version of that loop by making the
NEXT qa run the reader, and immediately opened the honest successor -- the
new line is read by nothing either. Three passes carried the question
forward unanswered: **does any signal LEAVE this box?**

Measured 2026-09-06, and the answer has two halves.

**NOTHING NOTIFIES.** The tree contains no smtp, no webhook, no mail, no
push client of any kind -- zero hits across `*.py`, `*.sh` and `*.service`.
`collector.backup` defaults `--dest` to `data/backups`, and
`HYXLAB_BACKUP_DIR` (the documented off-box hook, named in
`hyxlab-backup.service`'s own Description) is set nowhere, so even the
backups stay on the disk they protect.

**ONE THING LEAVES, AND IT CARRIES ONLY PROSE.** `scripts/autoloop.sh`
pushes to `origin main` every six hours. That is the whole egress. And
`/data/` and `/reports/` are gitignored -- rooted, deliberately -- so no
checker's OUTPUT can ride it. What leaves is exactly what an agent typed
into `docs/wiki/status.md` from memory.

So the smallest honest egress is not a new channel. It is a machine-written
INPUT to the channel that already exists: a digest the autoloop reads at
cold start, next to status.md, so the line that does leave is derived from
the manager's own persisted state instead of from recall. That is the
"a file the autoloop reads" option those three passes named, and it is
preferred to a `WantedBy` on a checker unit for the reason the readback
pass gave: another reader inside journald is another unread verdict.

This module therefore RUNS NO CHECK. It re-reads what the manager and qa
already wrote down. Four properties of that state had to be measured before
they could be reported, and each one is a way a naive digest would lie.

**(1) `Result` IS THE PRIOR RUN'S WHILE A UNIT IS IN FLIGHT.** At 09:15Z
`hyxlab-sweep`, `hyxlab-poly-sweep` and `hyxlab-autoloop` all read
`ActiveState=activating` with `Result=success` -- success belonging to
yesterday's run, with today's still executing. A digest that printed that
would report a stale fact in the present tense, on exactly the long
multi-hour units whose failures matter most. RUNNING is its own state here
and no result is quoted in it.

**(2) `ExecMainStatus=0` IS A DEFAULT, NOT A MEASUREMENT.** Every unit that
has never exited reports 0 -- including `hyxlab-stream` and `hyxlab-shadow`,
which have been up since 08-29, and including a unit that has never run at
all. The exit status is quoted only when `ExecMainExitTimestamp` is
populated, which is the field that distinguishes "exited cleanly" from
"never exited".

**(3) AN EMPTY `NextElapseUSecRealtime` IS NOT A BROKEN TIMER.** systemd
clears the next elapse while the triggered service is still active; that is
why `systemctl list-timers` prints `-` for the sweeps. A staleness rule
reading next-elapse alone would fire on every long sweep -- daily, on a
healthy box, which is the noise that trains an operator to stop reading.
Lateness is asked ONLY of a service that is not currently running.

**(4) AN UNINSTALLED UNIT ANSWERS QUIETLY.** `systemctl show` on a name the
manager has never heard of exits 0 and reports `LoadState=not-found`. Since
the unit set is discovered from `scripts/systemd/` -- the repo's canonical
copies, the set `promote.sh` installs -- a unit committed but never promoted
reads UNLOADED rather than vanishing from the digest.

**(5) THE INSTALLED UNITS WERE OWNED BY NOBODY, AND A FILE DIFF CANNOT
ANSWER IT.** Added 2026-09-06 as the unit-gate pass's named successor:
`test_systemd_units.py` greps the REPO's unit files and
`test_systemd_verify.py` parses them, `promote.sh` copies them into
`~/.config/systemd/user/` and never looks back, so between two promotes
nothing checks that what the manager LOADED is what the repo contains.
The obvious check -- diff the repo file against the installed file -- is
clean in three of the four states that matter, each measured against a
throwaway probe unit (never a hyxlab unit): a copy in a higher-priority
search-path directory is what actually loaded while both diffed files are
correct (`FragmentPath`); an ACTIVE unit whose fragment was edited without
a `daemon-reload` keeps reporting the OLD text -- what a promote whose
reload failed leaves behind -- and only `NeedDaemonReload=yes` says so;
and a `<unit>.d/*.conf` drop-in replaces `ExecStart` outright with the
fragment byte-identical (`DropInPaths`), which is a live practice on this
box. Reported as a second section rather than as a sixth checker, for the
reason this module exists: another verdict nothing reads is not a check.

The digest opens no DuckDB. A health report that took the archive lock to
say things look fine would be able to hurt the thing it watches (ops rule,
mistakes #20); everything here comes from the manager and from one JSON
file. The digest exits 0 unconditionally: a gate whose failure nothing reads
is the defect this pass exists to answer, and the digest's only job is to be
READ. Its one sub-command does not, and the difference is the same rule read
forward -- `--drift-only` exists BECAUSE it has a caller that branches on the
status (`promote.sh --units-only`, 2026-09-07), which is what separates a gate
from another unread verdict.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from collector.qa import STANDING_SKIPS, _load_state, _prior_run
from collector.qa import STATE as QA_STATE

REPO = Path(__file__).resolve().parent.parent
UNIT_DIR = REPO / "scripts/systemd"

# The properties the digest reads. Named once so the `systemctl show` call and
# the pure verdict function cannot drift apart.
PROPS = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "NRestarts",
    "ExecMainStatus",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
    "LastTriggerUSec",
    "NextElapseUSecRealtime",
    "WorkingDirectory",
    "FragmentPath",
    "DropInPaths",
    "NeedDaemonReload",
)

# Where `promote.sh` installs: `cp "$DEV"/scripts/systemd/hyxlab-* ~/.config/systemd/user/`.
# The drift arms compare the manager's loaded unit against this repo's copies, and
# `hyxlab-autoloop.service` sets `WorkingDirectory` to the DEV tree -- the same tree
# promote.sh copies FROM -- so the digest's reader compares against the right source.
INSTALL_DIR = Path.home() / ".config/systemd/user"

# WHAT THE BOX IS SUPPOSED TO BE RUNNING (2026-09-08).
# `promote.sh` installs from the DEV tree, but only AFTER fast-forwarding the
# `stable` branch, so between an edit and its promotion the correct contents of
# INSTALL_DIR are `stable`'s copies, not this working tree's. Judging the box
# against the dev tree alone made every unpromoted unit edit read DRIFT -- and
# since `tests/test_unit_drift.py` has a live arm on that verdict and
# `promote.sh` gates on the full suite, the only way to install a unit change
# was through a suite that could not go green until it was installed. Measured
# 2026-09-08 by appending one comment line to `scripts/systemd/hyxlab-backup.service`:
# the live arm failed instantly, uncommitted, with no daemon involved.
# The ref, not the worktree path: `git show stable:...` answers "what was last
# promoted" from either checkout, needs no hardcoded second path, and in the
# STABLE worktree itself resolves to that tree's own HEAD -- so there, where any
# difference really IS a fault, PENDING-PROMOTE cannot fire at all.
DEPLOY_REF = "stable"

# WHICH DRIFT STATES A RE-INSTALL CAN ACTUALLY CLEAR (2026-09-07).
# The unit-drift pass detected four states and repaired none: the repair --
# `cp` + `daemon-reload` -- existed only inside `promote.sh`'s happy path, so
# the only way to fix drift was to promote CODE. `promote.sh --units-only` is
# that repair on its own, and the point of naming these sets is that it must
# not claim to have done more than it did.
#   DRIFT            the installed file's text is wrong -> the `cp` rewrites it.
#   STALE-IN-MEMORY  the manager holds older text than the disk -> `daemon-reload`
#                    re-reads it. This is precisely the state a promote whose
#                    reload failed leaves behind, so re-running the reload IS the
#                    repair.
# and the two it cannot, both for structural reasons, not for want of trying:
#   SHADOWED         the winning copy is in a HIGHER-priority search-path
#                    directory that this repo does not own -- copying into
#                    INSTALL_DIR again leaves exactly the same file loaded. The
#                    repair is deleting someone else's file, which is an operator
#                    decision, not a script's.
#   DROP-IN          a `<unit>.d/*.conf` overrides directives WITHOUT touching
#                    the fragment, so it survives every fragment rewrite by
#                    construction. Same call, same reason.
#   UNREADABLE       the loaded fragment could not be read at all; a script that
#                    overwrites a path it cannot read is guessing.
# A naive `--units-only` would run the `cp`, reload, exit 0, and leave a SHADOWED
# box reporting success -- the same shape of lie the digest was built to stop
# telling. So the repair re-runs the judge and reports what SURVIVED it.
REPAIRABLE = ("DRIFT", "STALE-IN-MEMORY")
UNREPAIRABLE = ("SHADOWED", "DROP-IN", "UNREADABLE")
# The states that are not a fault to be repaired at all: agreement, an
# uninstalled unit, and a box correctly running the last promotion while this
# checkout is ahead of it. PENDING-PROMOTE is REPORTED (it is the "committed but
# not promoted" fact you want at cold start) and is not counted as agreement --
# it is simply not something a repair could act on, because nothing is broken.
NOT_A_FAULT = ("OK", "SKIP", "PENDING-PROMOTE")

# `--drift-only` exit codes. The digest itself still exits 0 unconditionally --
# it is a report -- but this mode has a CALLER that reads the status
# (`promote.sh --units-only`), which is the whole difference between a gate and
# an unread verdict.
DRIFT_CLEAN, DRIFT_REPAIRABLE, DRIFT_OPERATOR = 0, 1, 2
# A usage error is not a drift verdict. Sharing `2` would make a mistyped flag
# print promote.sh's "needs an operator" paragraph about a box that is fine.
USAGE_ERROR = 64  # EX_USAGE

# The unit whose record carries qa's verdict, and the record itself. `QA_STATE`
# is imported rather than spelled out so the path cannot drift from the writer's.
QA_UNIT = "hyxlab-qa.service"

# How far qa's recorded timestamp may sit from the unit's START before the digest
# calls the record stale. `_record_run` stores `main()`'s `now`, taken before any
# check runs, so record ~= unit start + interpreter startup -- sub-second in
# production (measured: start 10:00:00, record 10:00:00.318). Ten minutes is
# orders of magnitude above that and orders of magnitude below the daily cadence
# it must not confuse.
QA_RECORD_LAG_S = 600.0

# How far past systemd's OWN computed next elapse an idle timer must sit before
# the digest calls it LATE. The shortest cadence in the tree is `OnCalendar=*:0/5`
# (hyxlab-collect, 5 min), so one full period of the fastest timer is both larger
# than any scheduling jitter and small enough that a stopped timer shows up within
# one missed cycle. Applies only when the service is idle -- see (3) above.
LATE_SLACK_S = 300.0

# States that mean the unit is executing right now. `activating` is the one that
# matters: a Type=oneshot service sits in it for its whole run.
BUSY = ("active", "activating", "reloading", "deactivating")


@dataclass(frozen=True)
class UnitDrift:
    """One unit file's agreement with the repo. `state` is the word; `detail` says
    what the manager reported that the repo does not contain."""

    unit: str
    state: str
    detail: str

    @property
    def ok(self) -> bool:
        """The manager holds THIS REPO's text. Deliberately narrower than
        `fault`: the digest counts `ok` under the words "match the repo", and a
        PENDING-PROMOTE unit does not match it -- it matches what was promoted."""
        return self.state == "OK"

    @property
    def fault(self) -> bool:
        """Something the repo did not do happened to this unit.

        The gate reads THIS, not `not ok`. A unit whose installed text is
        exactly what `promote.sh` last shipped is not a fault however far the
        dev tree has moved ahead of it; calling it one is what deadlocked the
        promote (see DEPLOY_REF)."""
        return self.state not in NOT_A_FAULT


@dataclass(frozen=True)
class UnitHealth:
    """One unit's verdict. `state` is the word; `detail` is what it was read from."""

    unit: str
    state: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.state in ("OK", "RUNNING")


def discover_units() -> list[tuple[str, bool]]:
    """Every vendored service, and whether a timer drives it.

    Discovered from `scripts/systemd/`, never enumerated -- the same reason
    `test_shell_lint.py` globs and `promote.sh` copies a glob: a list cannot
    fail on the day a unit is added to it.
    """
    out = []
    for svc in sorted(UNIT_DIR.glob("hyxlab-*.service")):
        out.append((svc.name, (UNIT_DIR / f"{svc.stem}.timer").exists()))
    return out


def _epoch(raw: str) -> float | None:
    """`--timestamp=unix` renders a set timestamp as `@<epoch>` and an unset one
    as the empty string. Anything else is treated as unset rather than guessed at."""
    raw = raw.strip()
    if not raw.startswith("@"):
        return None
    try:
        return float(raw[1:])
    except ValueError:
        return None


def show(unit: str) -> dict[str, str]:
    """The manager's persisted view of one unit. An unknown unit is not an
    error (see (4)) -- it comes back with `LoadState=not-found`."""
    cmd = ["systemctl", "--user", "show", "--timestamp=unix", unit]
    cmd += [f"--property={p}" for p in PROPS]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    props = {}
    for line in proc.stdout.splitlines():
        key, _, val = line.partition("=")
        props[key] = val
    return props


def _with_oom(detail: str, note: str) -> str:
    return f"{detail} — {note}" if note else detail


def _age(ts: float | None, now: float) -> str:
    if ts is None:
        return "never"
    h = (now - ts) / 3600.0
    return f"{h:.1f}h ago" if h >= 1 else f"{(now - ts) / 60.0:.0f}m ago"


# --- OOM attribution -------------------------------------------------------
# Measured 2026-09-07, after `result=oom-kill` on hyxlab-poly-sweep cost a
# whole pass in kernel forensics to interpret. `Result=oom-kill` names the
# VERB, never the CULPRIT: a global OOM kills by badness score, and this
# fleet's batch units carry an `OOMScoreAdjust` that makes them preferred
# victims ON PURPOSE. So the digest cannot report an oom-kill as a fault of
# the unit -- on 09-07 poly-sweep died holding 333 MiB while the process
# that actually exhausted the box held 24.0 GiB and was killed one second
# LATER. The unit was executed as a hostage, and nothing said so.
#
# `MemoryPeak` cannot answer this either (the standing rule in
# .claude/rules/ops.md): it charges the page cache the unit's own reads
# pulled in, so poly-sweep's 21.3G "peak" was ~98% file cache. The kernel
# writes the only honest number -- `anon-rss` at the moment of the kill --
# and it writes it next to every other victim of the same episode.
OOM_WINDOW_DAYS = 7  # journald retention is the real bound; this just caps the read
OOM_EPISODE_S = 120.0  # kills from one global OOM land within seconds of each other

_MEMCG_RE = re.compile(
    r"oom-kill:constraint=.*?task_memcg=(?P<memcg>\S+?),task=(?P<task>[^,]+),pid=(?P<pid>\d+)"
)
_KILLED_RE = re.compile(
    r"Out of memory: Killed process (?P<pid>\d+) \((?P<comm>[^)]*)\)"
    r".*?anon-rss:(?P<anon>\d+)kB.*?oom_score_adj:(?P<adj>-?\d+)"
)


@dataclass(frozen=True)
class OomVictim:
    """One process the kernel killed, as the kernel itself recorded it."""

    ts: float
    pid: str
    comm: str
    owner: str  # the cgroup leaf -- a unit name, or a scope for anything else
    anon_kb: int
    adj: int


def _gib(kb: int) -> str:
    mib = kb / 1024.0
    return f"{mib / 1024.0:.1f} GiB" if mib >= 1024 else f"{mib:.0f} MiB"


def parse_kernel_oom(text: str) -> list[OomVictim]:
    """Victims from `journalctl -k -o short-iso`. Pure, so the shapes below are
    asserted against the real 2026-09-07 lines rather than against a live box.

    The kernel emits two lines per victim: an `oom-kill:` line carrying the
    cgroup, then a `Killed process` line carrying the footprint. They are
    joined on pid rather than on adjacency -- interleaved episodes are common
    and the pid is the only field that actually identifies the victim.
    """
    owners: dict[str, str] = {}
    out: list[OomVictim] = []
    for line in text.splitlines():
        stamp, _, rest = line.partition(" ")
        if m := _MEMCG_RE.search(rest):
            leaf = m.group("memcg").rstrip("/").rsplit("/", 1)[-1]
            owners[m.group("pid")] = leaf
            continue
        if m := _KILLED_RE.search(rest):
            try:
                ts = datetime.fromisoformat(stamp).timestamp()
            except ValueError:
                continue
            pid = m.group("pid")
            out.append(
                OomVictim(
                    ts=ts,
                    pid=pid,
                    comm=m.group("comm"),
                    owner=owners.get(pid, "?"),
                    anon_kb=int(m.group("anon")),
                    adj=int(m.group("adj")),
                )
            )
    return out


def read_kernel_oom(days: int = OOM_WINDOW_DAYS) -> list[OomVictim]:
    """The kernel's own kill record. Never raises: an absent journal, a missing
    binary or an unreadable buffer degrades to "no attribution", never to a
    digest that fails to print. (The 09-06 lesson: a new reader must not be
    able to take the checks around it down.)"""
    try:
        proc = subprocess.run(
            ["journalctl", "-k", "-o", "short-iso", "--no-pager", "--since", f"-{days}d"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_kernel_oom(proc.stdout)


def oom_attribution(victims: list[OomVictim], unit: str) -> str:
    """Was this unit the memory, or was it the sacrifice? Pure.

    Reports the unit's own anon-rss next to the biggest thing that died in the
    same episode, because that comparison is the whole decision: a unit that
    was the largest victim is a leak to fix, and a unit dwarfed by a stranger
    is a scheduling-policy cost to price.
    """
    mine = [v for v in victims if v.owner == unit]
    if not mine:
        return ""
    last = max(mine, key=lambda v: v.ts)
    peers = [v for v in victims if abs(v.ts - last.ts) <= OOM_EPISODE_S]
    worst = max(peers, key=lambda v: v.anon_kb)
    if worst.pid == last.pid:
        return f"largest victim of its own OOM episode at {_gib(last.anon_kb)} anon — this unit WAS the memory"
    return (
        f"OOM BYSTANDER: held {_gib(last.anon_kb)} anon (adj={last.adj}) when killed; "
        f"the episode's largest victim was {worst.comm} at {_gib(worst.anon_kb)} "
        f"(adj={worst.adj}, {worst.owner})"
    )


def judge(
    unit: str,
    svc: dict[str, str],
    timer: dict[str, str] | None,
    now: float,
    oom_note: str = "",
) -> UnitHealth:
    """One unit's state, as a pure function of what the manager reported.

    Pure so the field semantics measured above are asserted against strings
    rather than against whatever this box happens to be doing at test time.
    """
    if svc.get("LoadState") != "loaded":
        return UnitHealth(
            unit, "UNLOADED", f"LoadState={svc.get('LoadState') or '?'} — not installed?"
        )

    started = _epoch(svc.get("ExecMainStartTimestamp", ""))
    exited = _epoch(svc.get("ExecMainExitTimestamp", ""))
    busy = svc.get("ActiveState") in BUSY

    if timer is None:
        # A daemon. Its liveness IS its ActiveState; there is no cadence to be late for.
        if not busy:
            return UnitHealth(
                unit,
                "DOWN",
                _with_oom(
                    f"{svc.get('ActiveState')}/{svc.get('SubState')}, result={svc.get('Result')}",
                    oom_note,
                ),
            )
        restarts = svc.get("NRestarts") or "0"
        return UnitHealth(unit, "OK", f"up since {_age(started, now)}, {restarts} restarts")

    if timer.get("LoadState") != "loaded" or timer.get("ActiveState") != "active":
        return UnitHealth(
            unit, "TIMER-OFF", f"timer {timer.get('LoadState')}/{timer.get('ActiveState')}"
        )

    if busy:
        # (1) Result here belongs to the PREVIOUS run. Quote nothing but the clock.
        return UnitHealth(unit, "RUNNING", f"started {_age(started, now)}")

    if svc.get("Result") != "success":
        return UnitHealth(
            unit,
            "FAILED",
            _with_oom(f"result={svc.get('Result')}, exit={svc.get('ExecMainStatus')}", oom_note),
        )
    # (2) ExecMainStatus is only a measurement once the unit has actually exited.
    if exited is not None and (svc.get("ExecMainStatus") or "0") != "0":
        return UnitHealth(
            unit, "FAILED", f"exit={svc.get('ExecMainStatus')} at {_age(exited, now)}"
        )

    last = _epoch(timer.get("LastTriggerUSec", ""))
    if last is None and exited is None:
        return UnitHealth(unit, "NEVER-RAN", "timer active but never triggered")

    nxt = _epoch(timer.get("NextElapseUSecRealtime", ""))
    if nxt is None:
        # (3) says an empty next elapse is normal WHILE RUNNING. This unit is idle,
        # so the manager has an active timer it has scheduled nothing for.
        return UnitHealth(
            unit, "NO-NEXT", f"timer active, idle, no next elapse; last ran {_age(last, now)}"
        )
    if now - nxt > LATE_SLACK_S:
        return UnitHealth(
            unit, "LATE", f"due {_age(nxt, now)}, still idle; last ran {_age(last, now)}"
        )
    return UnitHealth(unit, "OK", f"last ran {_age(last, now)}")


def discover_unit_files() -> list[str]:
    """Every vendored unit file -- services AND timers.

    `discover_units` answers "what runs"; drift asks the question of every FILE
    `promote.sh` installs, which is 21 names, not 12. Globbed for the same reason:
    a list cannot fail on the day a unit is added to it.
    """
    return sorted(f.name for f in UNIT_DIR.glob("hyxlab-*") if f.is_file())


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def deployed_text(unit: str) -> str | None:
    """The unit file as of the last promotion, read from the `stable` ref.

    None when the ref or the file is not there -- a fresh clone, a tree with no
    deployment, or a unit added since. None means "no deployed baseline", and
    the judge then falls back to the repo-only comparison, which is the 09-06
    behaviour: strictly more suspicious, never less.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO), "show", f"{DEPLOY_REF}:scripts/systemd/{unit}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def judge_drift(
    unit: str,
    props: dict[str, str],
    loaded_text: str | None,
    vendored: str,
    deployed: str | None = None,
) -> UnitDrift:
    """Whether the unit the MANAGER holds is the unit this repo contains.

    Pure, like `judge`: the four semantics below were measured on this box and
    are asserted against strings, not against what systemd happens to hold at
    test time.

    Arms in severity order -- most severe is "the manager is running something
    the repo does not contain at all":

    **SHADOWED.** `FragmentPath` is the file systemd actually loaded. Unit
    lookup walks a search path, so a copy in a higher-priority directory wins
    and the installed copy is never read -- while a `diff repo ~/.config/...`
    reports CLEAN, because both files it compares are correct and neither is
    the one running. Only the manager can answer which file it read.

    **STALE-IN-MEMORY.** Measured on an ACTIVE probe unit: edit the fragment on
    disk without `daemon-reload` and `systemctl show` keeps reporting the OLD
    text (`Description=probe` while the file said `EDITED BY HAND`), with
    `NeedDaemonReload=yes`. So repo == disk can be true while the manager runs
    older text -- the exact state a `promote.sh` whose `daemon-reload` failed
    would leave behind, and the one state no file comparison can see.

    CORRECTED 2026-09-07, by editing the installed copy of a real unit and
    reading the manager back. 09-06's complement said an INACTIVE unit always
    reads `no`, because systemd garbage-collects it and re-reads on demand.
    Too strong: it GCs the UNREFERENCED unit. `hyxlab-backup.service` was
    inactive/dead and still reported `NeedDaemonReload=yes` after its installed
    file was touched, because `hyxlab-backup.timer` references it (`TriggeredBy`)
    and it therefore stays loaded. Since every timer-backed service on this box
    is referenced by its timer, that is nearly all of them, and the arm is doing
    real work on units 09-06 thought it could not.

    Measured in the same pass: a hand edit to an installed file reads
    STALE-IN-MEMORY, not DRIFT, for as long as the unit stays loaded -- `show`
    reports the text the manager holds, so the fragment comparison below is
    comparing the OLD text to the repo and would say OK. The two repairable
    states are two phases of one fault, which is why the reload comes before
    the re-read in the repair and why both are in `REPAIRABLE`.

    **DROP-IN.** `DropInPaths` lists `<unit>.d/*.conf` files that override
    directives without touching the fragment; an `ExecStart=` reset there
    replaces the command entirely (measured: `/bin/true` became
    `/bin/echo hijacked` with the fragment untouched). Invisible to every
    fragment-text check in the tree, and a live practice on this box --
    `hylshi-watchdog.service.d` exists. Measured interaction: a drop-in added
    without a reload reads `DropInPaths=` empty and `NeedDaemonReload=yes`, so
    the two arms cover each other's blind window.

    **DRIFT / PENDING-PROMOTE.** The loaded fragment's text differs from the
    repo's copy, and until 2026-09-08 that was one verdict covering two opposite
    facts. In the DEV tree -- the tree `promote.sh` copies FROM and the tree the
    autoloop runs the digest in -- the ordinary cause is "edited here, not
    promoted yet", where the box is exactly where the last promotion left it and
    nothing is wrong with it. The alarming cause is that something outside this
    repo wrote to the installed file. `deployed` (the `stable` ref's copy, see
    DEPLOY_REF) separates them: matching it is PENDING-PROMOTE, matching neither
    is DRIFT. Conflating the two deadlocked the promote -- the live arm in
    `tests/test_unit_drift.py` went red the moment a unit file was edited, and
    the full suite it belongs to is the gate `promote.sh` must pass to install
    that very file.
    """
    if props.get("LoadState") != "loaded":
        # `judge` already reports this unit as UNLOADED; saying it twice is noise.
        return UnitDrift(unit, "SKIP", f"LoadState={props.get('LoadState') or '?'}")

    # One arm, not two: an empty FragmentPath needs no separate branch, since
    # `Path("").parent` is `.` and already fails the directory test. It is named
    # here only so the verdict says which of the two it was.
    frag = (props.get("FragmentPath") or "").strip()
    if not frag or Path(frag).parent != INSTALL_DIR:
        return UnitDrift(
            unit,
            "SHADOWED",
            f"manager loaded {frag or '(no fragment path)'}, not {INSTALL_DIR}/{unit}",
        )

    if props.get("NeedDaemonReload") == "yes":
        return UnitDrift(
            unit, "STALE-IN-MEMORY", "fragment on disk changed since load; daemon-reload never ran"
        )

    drops = [d for d in (props.get("DropInPaths") or "").split() if d]
    if drops:
        return UnitDrift(
            unit, "DROP-IN", f"{len(drops)} override(s) not in the repo: {' '.join(drops)}"
        )

    if loaded_text is None:
        return UnitDrift(unit, "UNREADABLE", f"cannot read {frag}")
    if loaded_text != vendored:
        # "unpromoted" and "hand-edited" were one verdict until 2026-09-08, and
        # they are opposite facts: one says the box is exactly where the last
        # promote left it, the other says something outside this repo wrote to
        # it. Only the second is a fault, and only the second should be able to
        # stop a promote.
        if deployed is not None and loaded_text == deployed:
            return UnitDrift(
                unit,
                "PENDING-PROMOTE",
                f"box holds the promoted ({DEPLOY_REF}) copy; scripts/systemd/{unit} "
                "in this tree is ahead of it — promote.sh installs it",
            )
        return UnitDrift(
            unit,
            "DRIFT",
            f"{frag} matches neither scripts/systemd/{unit} nor {DEPLOY_REF}'s copy "
            "(written by something outside this repo)",
        )
    return UnitDrift(unit, "OK", frag)


def drift_report() -> list[UnitDrift]:
    out = []
    for unit in discover_unit_files():
        props = show(unit)
        frag = (props.get("FragmentPath") or "").strip()
        out.append(
            judge_drift(
                unit,
                props,
                _read(Path(frag)) if frag else None,
                _read(UNIT_DIR / unit) or "",
                deployed_text(unit),
            )
        )
    return out


def qa_record_path(qa_svc: dict[str, str]) -> Path:
    """WHERE the timer's qa run writes its record.

    `qa.STATE` is a RELATIVE path, so the record belongs to whichever tree qa
    ran in. The timer runs from the stable worktree; a digest reading its own
    CWD reports a record production never wrote. Measured 2026-09-06 -- the two
    copies read 09-05T20:25Z (dev, a hand run) and 09-06T10:00Z (production) on
    the same morning, and the dev one is the one a naive reader would print.
    The unit's own `WorkingDirectory` is the authority; systemd prefixes it with
    `-` when the directory is optional.
    """
    wd = (qa_svc.get("WorkingDirectory") or "").lstrip("-").strip()
    return (Path(wd) if wd else REPO) / QA_STATE


def judge_qa(state: dict, qa_svc: dict[str, str], now: datetime) -> str:
    """qa's last recorded run, cross-checked against the unit that runs it.

    `_prior_run` parses the record -- imported, not reimplemented, for the
    reason it was written: one parser, so nothing can hold a second opinion
    about what it says. What the digest ADDS is the arm qa cannot have: qa
    reads its record from inside the same process that writes it, so it cannot
    notice that the manager started a run whose record never landed here. The
    unit's start time and the record's timestamp are two independent facts
    about the same run, and this is the only place both are in hand.

    **A STANDING SKIP IS NOT A PARTIAL RUN.** The first shape of this line read
    `skipped [...] — NOT a full pass` off any non-empty skip set, and
    `collect-skips` is SKIPPED on every run of a healthy box (`qa.STANDING_SKIPS`
    names it, at the one site that appends it). So the digest's QA line carried
    that phrase every night forever, on green -- and the day a REAL skip appeared
    it would still read `NOT a full pass`, differing only in the contents of a
    list, which is the alarm fatigue `qa_collect_skips` refuses to manufacture
    one layer down and `qa_prior_run` refuses one layer up. Standing names are
    still REPORTED -- unread is not the goal -- but as a parenthetical that does
    not move the verdict. Only a skip qa did NOT expect makes the run partial.
    """
    prior = _prior_run(state)
    started = _epoch(qa_svc.get("ExecMainStartTimestamp", ""))
    if prior is None:
        return "QA         no run on record"
    age_h = (now - prior.at).total_seconds() / 3600.0
    standing = [name for name in prior.skipped if name in STANDING_SKIPS]
    unexpected = [name for name in prior.skipped if name not in STANDING_SKIPS]
    verdict = "clean"
    if prior.failures:
        verdict = f"FAILURES {list(prior.failures)}"
    elif unexpected:
        verdict = f"skipped {unexpected} — NOT a full pass"
    if standing:
        verdict += f" (standing skip{'s' if len(standing) > 1 else ''}: {', '.join(standing)})"
    line = f"QA         last run {prior.at:%m-%d %H:%M}Z ({age_h:.1f}h ago): {verdict}"
    if started is not None and started - prior.at.timestamp() > QA_RECORD_LAG_S:
        gap_h = (started - prior.at.timestamp()) / 3600.0
        line += (
            f"\nRECORD-STALE {QA_UNIT} started {gap_h:.1f}h AFTER this record — "
            "the run wrote its record somewhere else, or died before recording"
        )
    return line


def report(now: float | None = None) -> list[UnitHealth]:
    now = now if now is not None else datetime.now(UTC).timestamp()
    seen = [
        (svc_name, show(f"{Path(svc_name).stem}.timer") if has_timer else None, show(svc_name))
        for svc_name, has_timer in discover_units()
    ]
    # The kernel journal is read at most once per digest, and only when the
    # manager already says something was oom-killed. No fault, no cost.
    victims = read_kernel_oom() if any(s.get("Result") == "oom-kill" for _, _, s in seen) else []
    return [
        judge(
            svc_name,
            svc_props,
            timer,
            now,
            oom_attribution(victims, svc_name) if svc_props.get("Result") == "oom-kill" else "",
        )
        for svc_name, timer, svc_props in seen
    ]


def drift_exit_code(drift: list[UnitDrift]) -> int:
    """Clean / repairable-by-script / needs-an-operator, in that order of mercy.

    A single unrepairable state outranks any number of repairable ones: the
    useful thing to tell the caller is the WORST thing it cannot fix, because
    that is the one that needs a human.
    """
    bad = [d for d in drift if d.fault]
    if any(d.state not in REPAIRABLE for d in bad):
        return DRIFT_OPERATOR
    return DRIFT_REPAIRABLE if bad else DRIFT_CLEAN


def drift_main() -> int:
    """The drift section alone, with a status a shell script can branch on."""
    drift = drift_report()
    checked = [d for d in drift if d.state != "SKIP"]
    bad = [d for d in checked if d.fault]
    for d in bad:
        remedy = "promote.sh --units-only" if d.state in REPAIRABLE else "OPERATOR — see health.py"
        print(f"{d.state:<16} {d.unit}  {d.detail}  [{remedy}]", flush=True)
    # Printed even though it is not a fault: a caller told only "clean" would
    # read that as "the box runs what I am looking at", which is the one thing
    # a pending promotion means it does not.
    for d in (d for d in checked if d.state == "PENDING-PROMOTE"):
        print(f"{d.state:<16} {d.unit}  {d.detail}  [promote.sh]", flush=True)
    code = drift_exit_code(drift)
    print(
        f"[drift] {sum(d.ok for d in checked)}/{len(checked)} loaded unit files "
        f"match the repo (exit {code})",
        flush=True,
    )
    return code


def cli(argv: list[str]) -> int:
    if argv == ["--drift-only"]:
        return drift_main()
    if argv:
        print("usage: python -m collector.health [--drift-only]", file=sys.stderr)
        return USAGE_ERROR
    main()
    return 0


def main() -> None:
    now = datetime.now(UTC)
    print(f"[health] {now:%Y-%m-%d %H:%M}Z — persisted state only; no checks re-run", flush=True)
    rows = report(now.timestamp())
    width = max((len(r.unit) for r in rows), default=0)
    for r in sorted(rows, key=lambda r: (r.ok, r.unit)):
        print(f"{r.state:<10} {r.unit:<{width}}  {r.detail}", flush=True)
    qa_svc = show(QA_UNIT)
    print(judge_qa(_load_state(qa_record_path(qa_svc)), qa_svc, now), flush=True)
    drift = drift_report()
    checked = [d for d in drift if d.state != "SKIP"]
    # `ok` is the count's subject, `fault` is the headline's: a pending promotion
    # is neither a match nor an alarm, and collapsing it into either loses the
    # only fact it carries.
    drifted = [d for d in checked if d.fault]
    pending = [d for d in checked if d.state == "PENDING-PROMOTE"]
    for d in drifted + pending:
        print(f"{d.state:<10} {d.unit:<{width}}  {d.detail}", flush=True)
    print(
        f"[health] {sum(d.ok for d in checked)}/{len(checked)} loaded unit files match the repo"
        + (f"; PENDING-PROMOTE: {[d.unit for d in pending]}" if pending else "")
        + (f"; DRIFT: {[d.unit for d in drifted]}" if drifted else ""),
        flush=True,
    )
    bad = [r.unit for r in rows if not r.ok]
    print(
        f"[health] {len(rows) - len(bad)}/{len(rows)} units ok"
        + (f"; ATTENTION: {bad}" if bad else ""),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
