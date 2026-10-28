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

import bisect
import json
import re
import subprocess
import sys
import time as _time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from collector.qa import QA_RUN_CHECK as QA_UNREAD_CHECK
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
    "ActiveEnterTimestamp",
    "ExecMainExitTimestamp",
    "LastTriggerUSec",
    "NextElapseUSecRealtime",
    "WorkingDirectory",
    "FragmentPath",
    "DropInPaths",
    "NeedDaemonReload",
    "UnitFileState",
    "ControlGroup",
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
REPAIRABLE = ("DRIFT", "STALE-IN-MEMORY", "INERT")
# Where the person this verdict is handed to finds the procedure. A remedy that
# points at source ("see health.py") asks the operator to derive the steps from
# the code that just declined to take them.
OPERATOR_REMEDY = "OPERATOR — docs/wiki/unit-drift-runbook.md"
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
        return self.state in ("OK", "RUNNING", "FIRST-DUE")


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


def _until(ts: float, now: float) -> str:
    """`_age` for a future instant: how long until it, in the same units."""
    h = (ts - now) / 3600.0
    return f"{h:.1f}h" if h >= 1 else f"{(ts - now) / 60.0:.0f}m"


def _age(ts: float | None, now: float) -> str:
    if ts is None:
        return "never"
    h = (now - ts) / 3600.0
    return f"{h:.1f}h ago" if h >= 1 else f"{(now - ts) / 60.0:.0f}m ago"


# --- live cgroup memory -----------------------------------------------------
# The ops rule: a capped unit's health is `memory.events`, never `MemoryPeak`.
# `scripts/cgmem.sh` publishes that at STOP (ExecStopPost is the last reader
# before the cgroup is removed), which covers every oneshot and covers a
# daemon only when it dies. A healthy daemon never stops -- `hyxlab-shadow`
# was 209h up on 2026-10-14 -- so its verdict was reachable only by hand.
# The cgroup is kernel state under /sys/fs/cgroup, readable by this user with
# no lock and no process touched, so the digest reads it for every unit the
# manager says is RUNNING. Measured 2026-10-14 02:20Z, the first such reading:
# stream peak 2.0G == cap 2.0G with 37,742 `max` events and anon 162M
# (reclaim, 89h up); shadow 1.0G == 1.0G, 986 events, anon 257M; simui 34M of
# 1.0G, 0 events. All three oom_kill 0.
CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class CgMem:
    """One running unit's cgroup memory counters. `None` is a counter the
    kernel did not expose (never 0: a zero is a measurement), except `cap`,
    where `None` means `memory.max` reads `max`, i.e. uncapped."""

    unit: str
    anon: int | None
    peak: int | None
    cap: int | None
    max_events: int | None
    oom_kill: int | None
    #: Times the kernel throttled the cgroup's SOCKETS under memory
    #: pressure. `max` says reclaim happened; this says it had a cost, and
    #: on a daemon that receives for a living it is the counter that tells
    #: harmless page-cache reclaim from a daemon slowed by its own cap.
    #: Cumulative over the run -- the timed increment is on streamd's own
    #: stats line (`SockThrottleWatch`); measured 2026-10-14: 441 on the
    #: stream's 10-10..10-14 run, 0 on the next, read by nobody until now.
    sock_throttled: int | None = None
    #: `memory.pressure` `full ... total=` -- microseconds EVERY runnable task
    #: in the cgroup spent stalled on memory (PSI). This is the COST of the
    #: reclaim that `max_events` only counts: measured 2026-10-14 15:07Z, the
    #: stream's 43,761 `max` events (all accrued at boot; flat 90s later with
    #: file cache at 833M of 2G) cost 0.14s of full stall over a 36-minute
    #: run, and poly-sweep's 5.18M events cost 27.9s over 10.8h (0.07%). A
    #: count of events says reclaim HAPPENED; this says what it took.
    stall_full_us: int | None = None
    #: Seconds since the unit entered `active` (systemd's clock), the base
    #: the stall is a share of. None when the manager did not say.
    uptime_s: float | None = None


def _int_or_none(text: str | None) -> int | None:
    if text is None:
        return None
    text = text.strip()
    return int(text) if text.isdigit() else None


def parse_cgmem(
    unit: str,
    stat_text: str | None,
    events_text: str | None,
    peak_text: str | None,
    max_text: str | None,
    pressure_text: str | None = None,
    uptime_s: float | None = None,
) -> CgMem:
    """Pure: the files' contents -> counters. `memory.stat` and
    `memory.events` are `name value` lines; `memory.peak` and `memory.max`
    are one integer, or the word `max` for no cap; `memory.pressure` is
    `some avg10=.. avg60=.. avg300=.. total=<us>` then the same for `full`."""

    def table(text: str | None) -> dict[str, int]:
        out: dict[str, int] = {}
        for line in (text or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                out[parts[0]] = int(parts[1])
        return out

    stat = table(stat_text)
    events = table(events_text)
    return CgMem(
        unit=unit,
        anon=stat.get("anon"),
        peak=_int_or_none(peak_text),
        cap=_int_or_none(max_text),
        max_events=events.get("max"),
        oom_kill=events.get("oom_kill"),
        sock_throttled=events.get("sock_throttled"),
        stall_full_us=_psi_full_total(pressure_text),
        uptime_s=uptime_s,
    )


def _psi_full_total(text: str | None) -> int | None:
    for line in (text or "").splitlines():
        parts = line.split()
        if parts and parts[0] == "full":
            for kv in parts[1:]:
                k, _, v = kv.partition("=")
                if k == "total" and v.isdigit():
                    return int(v)
    return None


def read_cgmem(
    unit: str, control_group: str, root: Path = CGROUP_ROOT, uptime_s: float | None = None
) -> CgMem | None:
    """Read one unit's cgroup under `root`; None if the directory is gone
    (the unit stopped between `show` and this read -- normal, not a fault)."""
    d = root / control_group.lstrip("/")
    if not control_group or not d.is_dir():
        return None
    return parse_cgmem(
        unit,
        _read(d / "memory.stat"),
        _read(d / "memory.events"),
        _read(d / "memory.peak"),
        _read(d / "memory.max"),
        _read(d / "memory.pressure"),
        uptime_s,
    )


def _mib(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / 2**30:.1f}G" if n >= 2**30 else f"{n // 2**20}M"


def _stall_words(stall_us: int | None, uptime_s: float | None) -> str:
    """PSI full stall as seconds and, when the uptime is known, as a share
    of it -- the number that says whether `N reclaim events` cost anything.
    `?` for an unmeasured stall; a share is never printed over an unknown
    base (0.14s over `?` is not 0.00%)."""
    if stall_us is None:
        return "memory stall ?"
    out = f"memory stall {stall_us / 1e6:.1f}s"
    if uptime_s is not None and uptime_s > 0:
        out += f" ({100.0 * stall_us / 1e6 / uptime_s:.2f}% of {uptime_s / 3600:.1f}h)"
    return out


def judge_cgmem(c: CgMem) -> UnitHealth:
    """Render the ops rule: `oom_kill > 0` is the fault; `peak == cap` with
    `max > 0` is reclaim and is said so in words, because the number alone
    reads as a footprint at the cap (mistakes: ops.md `MemoryPeak`)."""
    if c.cap is None:
        shape = f"peak {_mib(c.peak)}, uncapped"
    elif c.peak is not None and c.peak >= c.cap:
        shape = (
            f"peak {_mib(c.peak)} == cap {_mib(c.cap)} "
            f"({c.max_events if c.max_events is not None else '?'} reclaim events, not kills)"
        )
    else:
        shape = f"peak {_mib(c.peak)} of cap {_mib(c.cap)} (cap never reached)"
    sock = "?" if c.sock_throttled is None else str(c.sock_throttled)
    if c.sock_throttled:
        sock += " (reclaim clamped its sockets)"
    detail = (
        f"anon {_mib(c.anon)}, {shape}, oom_kill {c.oom_kill if c.oom_kill is not None else '?'}"
        f", sock_throttled {sock}, {_stall_words(c.stall_full_us, c.uptime_s)}"
    )
    state = "OOM-KILLED" if c.oom_kill else "CGMEM"
    return UnitHealth(c.unit, state, detail)


def live_cgmem(units: list[str] | None = None) -> list[CgMem]:
    """Counters for every unit the manager reports BUSY with a cgroup. A
    unit that is idle has no cgroup; `cgmem.sh` already published its last."""
    names = units if units is not None else [u for u, _ in discover_units()]
    out: list[CgMem] = []
    for unit in names:
        props = show(unit)
        if props.get("ActiveState") not in BUSY:
            continue
        # A daemon's base is when it became active; a RUNNING oneshot never
        # enters `active` (it is `activating` for its whole run, so that
        # field is empty) and its base is the main process's start.
        since = _epoch(props.get("ActiveEnterTimestamp", "")) or _epoch(
            props.get("ExecMainStartTimestamp", "")
        )
        uptime = None if since is None else max(0.0, _time.time() - since)
        c = read_cgmem(unit, props.get("ControlGroup", ""), uptime_s=uptime)
        if c is not None:
            out.append(c)
    return out


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
    nxt = _epoch(timer.get("NextElapseUSecRealtime", ""))
    if last is None and exited is None:
        # A timer the manager has SCHEDULED and simply not reached is a new
        # unit waiting for its first slot, not a fault: `hyxlab-divergence`
        # sat in NEVER-RAN across three digests (2026-09-09..10) and
        # `hyxlab-atlas` would have sat there for a week, each read outranking
        # the ladder for a unit whose next elapse was printed right there.
        # The #47 shape -- installed but never enabled -- is TIMER-OFF above;
        # a first slot that has come and gone without a trigger stays here.
        if nxt is not None and nxt > now:
            return UnitHealth(
                unit, "FIRST-DUE", f"never triggered; first run due in {_until(nxt, now)}"
            )
        return UnitHealth(unit, "NEVER-RAN", "timer active but never triggered")

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


# WHAT THE TWO UNREPAIRABLE VERDICTS HAVE TO SAY TO BE ACTIONABLE (2026-09-09).
# SHADOWED and DROP-IN are the states `promote.sh --units-only` cannot clear, so
# both hand the box to a person -- and for nine passes the thing they handed over
# was a PATH. "the manager loaded /etc/systemd/user/x" and "1 override: o.conf"
# name a file and stop, which makes the first move of any operator procedure
# written for them "go open it and work out whether it matters". A runbook whose
# first step is the investigation the checker just declined to do is not a
# procedure; it is the checker's missing half, written in prose.
#
# The decision each verdict has to support, and the fact it needs to support it:
#
#   SHADOWED -> is the box behaving differently RIGHT NOW, or is this only the
#   wrong file winning? `drift_report` has already read the winning fragment
#   (that read is what the DRIFT arm below runs on) and the repo's copy, so the
#   comparison costs nothing and separates "someone left a stale copy in a
#   higher-priority directory and it happens to be identical" -- ownership
#   hygiene, fix at leisure -- from "systemd is running text this repo does not
#   contain", which is an incident. Both are SHADOWED; they are not the same
#   night's work, and the old wording could not tell them apart.
#
#   DROP-IN -> which directives were overridden. The measured hijack was
#   `ExecStart=` + `ExecStart=/bin/echo hijacked`: an empty assignment RESETS the
#   directive, and for a list-valued one like ExecStart that is the difference
#   between adding a second command and replacing the unit's command outright.
#   A drop-in that sets `MemoryMax=` and one that resets `ExecStart=` are one
#   word in this report and opposite events on the box.
#
# Two things this must not do. It must not call a drop-in harmless -- it reports
# the directive names and the reset, and the judgement of whether a MemoryMax
# this repo did not write is fine stays with the operator. And an unreadable
# override must never render as "sets nothing": a conf whose contents cannot be
# read is the one that most deserves a human, and printing an empty directive
# list for it would be the "silently empty" lie the 09-06 truncation and the
# 09-08 failure-history passes each caught a different version of.


def _shadow_effect(loaded_text: str | None, vendored: str) -> str:
    """Whether the file that WON is running this repo's text anyway."""
    if loaded_text is None:
        return "could not read it — cannot say whether its text differs from the repo's"
    if loaded_text == vendored:
        return "its text is byte-identical to the repo's copy (wrong file wins, same behaviour)"
    return "and its text DIFFERS from the repo's copy — the box is running a unit this repo does not contain"


def _dropin_directives(text: str) -> list[str]:
    """The directives a drop-in sets, in file order, deduped, resets marked.

    `Key=` with an empty value is systemd's RESET for a list-valued directive,
    which is what makes an ExecStart drop-in a replacement rather than an
    addition -- so it is never collapsed with an ordinary assignment. Section
    headers, comments and continuations are not directives.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, sep, val = line.partition("=")
        key = key.strip()
        if not sep or not key or " " in key:
            continue
        name = f"{key}(reset)" if not val.strip() else key
        if name not in out:
            out.append(name)
    return out


def _dropin_effect(path: str, text: str | None) -> str:
    """One override file, and what it changes."""
    name = Path(path).name
    if text is None:
        return f"{name} UNREADABLE — cannot say what it overrides"
    keys = _dropin_directives(text)
    if not keys:
        return f"{name} sets no directive"
    hijack = " [RESET of the unit's command]" if "ExecStart(reset)" in keys else ""
    return f"{name} sets {', '.join(keys)}{hijack}"


# WHICH UNITS ARE SUPPOSED TO BE ENABLED, AND WHY THE FILE ANSWERS IT (2026-09-11).
# Left open by mistake #47: the INERT arm needs a rule for which repo units the
# manager is supposed to have wired up, and #47 guessed "timers yes, timer-backed
# services no". That is a proxy for the real thing, and the real thing is written
# in each unit file. `enable` does exactly one thing -- it creates the symlinks a
# unit's `[Install]` section names -- so a unit WITHOUT an `[Install]` section
# cannot be enabled at all; systemd calls it `static`. Measured 2026-09-11 across
# all 23 vendored unit files: `UnitFileState` is `enabled` for exactly the 14 that
# contain `[Install]` (11 timers + the three daemons stream/shadow/simui) and
# `static` for exactly the 9 that do not (every timer-backed .service). No
# exception, so the proxy and the file agree today -- and the FILE is what stays
# right when the next daemon or the next timer-backed service arrives.
#
# One rule, two consumers: `promote.sh` enables the same discovered set, which is
# what makes INERT `REPAIRABLE` rather than an operator hand-off.
# `tests/test_promote_enables_timers.py` pins that they cannot diverge.
ENABLED_STATES = ("enabled", "enabled-runtime")


def _wants_enabling(vendored: str) -> bool:
    """Does the REPO's copy of this unit declare an `[Install]` section.

    The repo's copy, not the loaded one, on purpose: this arm asks what the unit
    is SUPPOSED to be, and a loaded file whose `[Install]` differs from the repo's
    is a text fault the DRIFT arm below already reports under its own name.
    """
    return any(ln.strip() == "[Install]" for ln in vendored.splitlines())


def judge_drift(
    unit: str,
    props: dict[str, str],
    loaded_text: str | None,
    vendored: str,
    deployed: str | None = None,
    dropin_texts: dict[str, str | None] | None = None,
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

    **INERT.** A unit file can be installed, loaded, textually perfect, and
    wired to nothing: `enable` writes the symlinks the `[Install]` section names,
    and that symlink is not part of any file this repo ships. So every text arm
    above reads clean on a timer that will never fire -- measured 2026-09-10,
    `hyxlab-divergence.timer` (mistake #47). See ENABLED_STATES for which units
    the arm expects to be enabled and why the unit FILE answers that question.

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
            f"manager loaded {frag or '(no fragment path)'}, not {INSTALL_DIR}/{unit}"
            f" — {_shadow_effect(loaded_text, vendored)}",
        )

    if props.get("NeedDaemonReload") == "yes":
        return UnitDrift(
            unit, "STALE-IN-MEMORY", "fragment on disk changed since load; daemon-reload never ran"
        )

    # AFTER the reload arm, and that ordering is the whole subtlety. `UnitFileState`
    # is read from the manager's cached view of the unit file, so a unit that gains
    # an `[Install]` section and has not been reloaded still reports `static` --
    # which would fire this arm on a unit that is merely mid-promotion. Reporting
    # the pending reload first states the only fact that is certain there.
    #
    # BEFORE the text arms, because those are questions about what the unit would
    # do and this one is whether it can ever run. `hyxlab-divergence.timer` shipped
    # with byte-perfect text, passed every gate this repo has, and would never have
    # fired (mistake #47).
    if _wants_enabling(vendored) and props.get("UnitFileState") not in ENABLED_STATES:
        return UnitDrift(
            unit,
            "INERT",
            f"installed, loaded, and NOT enabled (UnitFileState="
            f"{props.get('UnitFileState') or '?'}); its [Install] symlinks are absent, "
            "so nothing will ever trigger it",
        )

    drops = [d for d in (props.get("DropInPaths") or "").split() if d]
    if drops:
        return UnitDrift(
            unit,
            "DROP-IN",
            f"{len(drops)} override(s) not in the repo: "
            + "; ".join(_dropin_effect(d, (dropin_texts or {}).get(d)) for d in drops),
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
                {d: _read(Path(d)) for d in (props.get("DropInPaths") or "").split() if d},
            )
        )
    return out


# THE FAULTS THAT LEFT NO TRACE (2026-09-08).
# The digest reads `Result`, and `Result` is ONE run's -- the last one. Every
# timer unit here reruns on a cadence (collect every 5 min, breadth every 5,
# qa daily), so a unit that fails and then succeeds is reported OK and its
# failure is gone from the digest permanently. Not a latency problem, a LOSS
# problem: the six-hour reader arrives and there is nothing left to read.
# Measured 2026-09-08 against this box's own journal -- five in-scope unit
# failures in the trailing 24h, of which the digest showed exactly ONE
# (`hyxlab-qa`, the only one still failed):
#   09-07 17:50Z  poly-sweep / breadth / collect  oom-kill   (the system-wide
#                 event whose fourth victim was Chromium -- all four cleared)
#   09-08 10:00Z  qa                              exit-code  (still failed)
#   09-08 12:47Z  breadth                         exit-code  (a Kalshi
#                 ReadTimeout; one lost cycle, recovered on the next run, and
#                 nothing anywhere would ever have named it)
# The last one is the class this section exists for: individually benign,
# invisible by construction, and the only place a rising rate could show up.
#
# Four things had to be measured before this could be reported honestly.
#
# **(1) SYSTEMD LOGS EACH FAILURE TWICE, UNDER TWO MESSAGE IDS.** Every failure
# emits `d9b373ed...` ("Failed with result 'X'") AND `be02cf68...` ("Failed to
# start <Description>"). Matching on the word "Failed", or on both ids, doubles
# every count. Only the first is read -- it is the one that carries the RESULT,
# and the result is the difference between "this unit is broken" and "the box
# ran out of memory and this unit was standing there" (the 09-07 lesson).
#
# **(2) THE ATTRIBUTION FIELD IS `USER_UNIT`, NOT `UNIT`.** The message is
# emitted BY the user manager ABOUT the unit, so `_SYSTEMD_USER_UNIT` reads
# `init.scope` and `UNIT` is unset entirely. A reader keying on either gets
# nothing, or gets everything filed under `init.scope`. Parsing the unit name
# back out of the message text would work and is not done: the structured field
# is there, and a regex over an operator-facing string is a promise about
# systemd's prose.
#
# **(3) THIS JOURNAL IS NOT THIS PROJECT'S.** The same 24h window holds
# `hylshi-cli-products.service` and an `app-org.chromium...scope` -- two of the
# seven records. The set is intersected with `discover_units()`, the repo's own
# vendored units, for the same reason `SYSTEMD_UNIT_PATH` is pinned on the
# verify command: a health report for THIS project that reports another
# project's faults is noise that trains an operator to stop reading.
#
# **(4) "0 FAILURES IN 24h" IS A CLAIM ABOUT THE JOURNAL, NOT ABOUT THE BOX.**
# journald vacuums. If retention is shorter than the window, a clean count is
# reporting an empty buffer as a quiet night -- the exact shape of lie this
# module exists to stop telling. The floor is measured (oldest retained entry,
# 09-06 14:42Z here, ~48h) and the summary states the window it ACTUALLY
# covered whenever that is less than the one asked for.
#
# This section adds NO gate and moves no headline. The unread-verdict rule
# cuts both ways: a unit already printed in ATTENTION must not be printed as a
# second alarm for the same fact, so what the line adds there is RECURRENCE --
# how often, and whether this is the first time.
UNIT_FAILURE_MESSAGE_ID = "d9b373ed55a64feb8242e02dbe79a49c"

# `exit-code` alone cannot tell a crash from a designed skip: `collector.collect`
# and `collector.breadth` exit 75 (EX_TEMPFAIL) when a LIVE reader outside the
# flock holds the archive, and `scripts/autoloop.sh` exits 75 when `claude -p`
# refused the call over the account's usage limit (a USER item with a reset
# time; both 2026-10-24 runs were filed `exit-code/1` here before it did) --
# all of them read `Failed with result 'exit-code'`. On
# 2026-10-07 15:31-16:09Z the digest printed four collect and two breadth
# failures that were all a sibling project's read-only attach, and the reader
# had to dig the journal to learn that. The status is in a SECOND record, the
# manager's "Main process exited" (this id), joined to the failure by
# `USER_INVOCATION_ID` -- one invocation, one exit, one failure.
UNIT_EXIT_MESSAGE_ID = "98e322203f7a4ed290d09fe03c09fe15"

# The window must be at least the reader's interval or faults fall through it
# exactly as they do now: `hyxlab-autoloop.timer` fires 02/08/14/20:15 UTC, so
# six hours is the floor. 24h is four periods of margin, matches the window qa's
# own continuity checks use, and sits inside this box's measured retention.
FAILURE_WINDOW_H = 24


@dataclass(frozen=True)
class UnitFailure:
    """One `Failed with result` record: which unit, when, and which result."""

    unit: str
    at: datetime
    result: str
    #: The process's exit status for an `exit-code` result ("75" is a skip,
    #: not a crash); None when the result is not an exit or the record of it
    #: was not read -- never guessed.
    status: str | None = None

    @property
    def label(self) -> str:
        return f"{self.result}/{self.status}" if self.status else self.result


_RESULT_RE = re.compile(r"Failed with result '(?P<result>[^']+)'")


def parse_unit_failures(stdout: str, units: set[str]) -> list[UnitFailure]:
    """Journal JSON -> in-scope failures, oldest first. Pure, and forgiving by
    design: a record that cannot be parsed is skipped, never guessed at, and
    never raised out of a digest whose job is to print."""
    recs = []
    for line in stdout.splitlines():
        try:
            recs.append(json.loads(line))
        except ValueError:
            continue
    statuses = {
        rec.get("USER_INVOCATION_ID"): str(rec.get("EXIT_STATUS"))
        for rec in recs
        if isinstance(rec, dict)
        and rec.get("MESSAGE_ID") == UNIT_EXIT_MESSAGE_ID
        and rec.get("EXIT_CODE") == "exited"
        and rec.get("USER_INVOCATION_ID")
        and rec.get("EXIT_STATUS") is not None
    }
    out = []
    for rec in recs:
        if not isinstance(rec, dict) or rec.get("MESSAGE_ID") != UNIT_FAILURE_MESSAGE_ID:
            continue
        unit = rec.get("USER_UNIT")
        if unit not in units:
            continue
        try:
            at = datetime.fromtimestamp(int(rec["__REALTIME_TIMESTAMP"]) / 1e6, UTC)
        except (KeyError, TypeError, ValueError):
            continue
        m = _RESULT_RE.search(str(rec.get("MESSAGE", "")))
        result = m.group("result") if m else "failed"
        status = statuses.get(rec.get("USER_INVOCATION_ID")) if result == "exit-code" else None
        out.append(UnitFailure(unit, at, result, status))
    return sorted(out, key=lambda f: f.at)


def read_unit_failures(units: set[str], hours: int = FAILURE_WINDOW_H) -> list[UnitFailure]:
    """The manager's own failure log for the window. The `MESSAGE_ID=` match is
    given to journalctl rather than filtered in Python -- it is an indexed field,
    so the read stays proportional to the number of FAILURES and not to the size
    of the journal. Never raises (see `read_kernel_oom`)."""
    try:
        proc = subprocess.run(
            [
                "journalctl",
                "--user",
                "--no-pager",
                "--since",
                f"-{hours}h",
                f"MESSAGE_ID={UNIT_FAILURE_MESSAGE_ID}",
                # Same field twice is an OR in journalctl: the exit record
                # that carries the status (see UNIT_EXIT_MESSAGE_ID).
                f"MESSAGE_ID={UNIT_EXIT_MESSAGE_ID}",
                "-o",
                "json",
                # MESSAGE_ID is in this list even though it is also the server-side
                # match, and that is not redundancy: `--output-fields` restricts
                # what comes BACK, so omitting it makes every record arrive with
                # the field the parser keys on absent, and the section silently
                # reports a clean box. Caught here 2026-09-08 by a "0 failures"
                # against five that had just been measured by hand.
                "--output-fields=MESSAGE_ID,USER_UNIT,MESSAGE,__REALTIME_TIMESTAMP,USER_INVOCATION_ID,EXIT_CODE,EXIT_STATUS",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_unit_failures(proc.stdout, units)


# ---- Kalshi 429s, host-wide (mistakes #145) ----------------------------------
#
# The Kalshi public-API budget is ONE bucket per host, and two projects draw
# on it: this repo's units, and the sibling `hylshi-family-books` pass on the
# same `*:0/5` tick, whose stdout never reaches journald. A 429 count read from
# this repo's journal alone is therefore this repo's SHARE of the refusals,
# not the bucket's state -- the night of 2026-10-27/28 read 18 here against
# 41 in the sibling's own trace, paired by the second. So the digest prints
# both ledgers on one line: the journal by unit, and the sibling governor's
# grant trace (one JSON line per `slot` grant and per `429`) by count. The
# trace is read as a file, never through the sibling's code; an absent trace
# is reported as absent, never as zero.

KALSHI_429_WINDOW_H = 24
SIBLING_TRACE = Path.home() / ".local/state/hylshi-research/public_api_governor.trace.jsonl"
SIBLING_TRACE_BACKUPS = 9  # the governor rotates `.1` .. `.N`; read whatever exists


def parse_kalshi_429(stdout: str) -> dict[str, int]:
    """`[kalshi] ... HTTP 429` lines per user unit from `journalctl -o json`.

    The predicate names the venue: the poly sweep also prints `status 429`,
    and that is Polymarket's bucket, not this one. Non-JSON lines (journalctl's
    `-- No entries --` goes to stdout, #99) are skipped, not counted.
    """
    counts: dict[str, int] = {}
    for line in stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg, unit = rec.get("MESSAGE"), rec.get("_SYSTEMD_USER_UNIT")
        if not isinstance(msg, str) or not isinstance(unit, str):
            continue
        if "[kalshi]" in msg and "HTTP 429" in msg:
            counts[unit] = counts.get(unit, 0) + 1
    return counts


def read_kalshi_429(hours: int = KALSHI_429_WINDOW_H) -> dict[str, int] | None:
    """This repo's refusals in the window, by unit; None when the journal
    cannot be read (never an empty dict -- that would read as a clean box)."""
    try:
        proc = subprocess.run(
            [
                "journalctl",
                "--user",
                "--no-pager",
                "--since",
                f"-{hours}h",
                "--grep=HTTP 429",
                "-o",
                "json",
                "--output-fields=MESSAGE,_SYSTEMD_USER_UNIT",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):  # 1 is journalctl's "no match"
        return None
    return parse_kalshi_429(proc.stdout)


def sibling_trace_paths(base: Path = SIBLING_TRACE) -> list[Path]:
    """The live trace plus its rotated backups, whichever of them exist."""
    paths = [base] + [
        base.with_name(f"{base.name}.{k}") for k in range(1, SIBLING_TRACE_BACKUPS + 1)
    ]
    return [p for p in paths if p.is_file()]


def parse_sibling_trace(lines, since_ts: float) -> tuple[int, int]:
    """(429s, grants) at or after `since_ts` from governor trace lines.

    A `slot` record is one granted request; a `429` record is one refusal the
    sibling took. Anything else (malformed, other kinds, older) is skipped.
    """
    n429 = slots = 0
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ts = rec.get("ts") if isinstance(rec, dict) else None
        if not isinstance(ts, (int, float)) or ts < since_ts:
            continue
        kind = rec.get("kind")
        if kind == "429":
            n429 += 1
        elif kind == "slot":
            slots += 1
    return n429, slots


def read_sibling_429(since_ts: float, base: Path = SIBLING_TRACE) -> tuple[int, int] | None:
    """The sibling's (429s, grants) since `since_ts`, or None when no trace
    file exists or none could be read."""
    paths = sibling_trace_paths(base)
    if not paths:
        return None
    n429 = slots = 0
    read_any = False
    for path in paths:
        try:
            with path.open(errors="replace") as fh:
                a, b = parse_sibling_trace(fh, since_ts)
        except OSError:
            continue
        read_any = True
        n429 += a
        slots += b
    return (n429, slots) if read_any else None


def kalshi_429_line(
    counts: dict[str, int] | None,
    sibling: tuple[int, int] | None,
    hours: int = KALSHI_429_WINDOW_H,
    trace_dir: Path = SIBLING_TRACE.parent,
) -> str:
    """One line, both ledgers, the host total only when both are known."""
    if counts is None:
        ours = "hyxlab journal unreadable"
        mine = None
    else:
        mine = sum(counts.values())
        by_unit = ", ".join(
            f"{u} {n}" for u, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        ours = f"hyxlab {mine}" + (f" ({by_unit})" if by_unit else "")
    if sibling is None:
        theirs = f"sibling trace absent ({trace_dir})"
        host = ""
    else:
        n429, slots = sibling
        theirs = f"sibling family-books {n429} x 429 of {slots} requests"
        host = f" | host {mine + n429}" if mine is not None else ""
    return f"{hours}h: {ours} | {theirs}{host}"


# ---- the same two ledgers by the HOUR ----------------------------------------
#
# The 24h line above says how much of the bucket each tenant lost; it cannot
# say WHEN, and when is the discriminator. Measured 2026-10-28 over the first
# sweep-idle stretch under the 0.35s floor (10-27 21Z -> 10-28 04Z and 10-28
# 18Z -> 20Z): the sibling's grants were a constant 2,179-2,211 per hour across
# every idle hour while the collector's clean-cycle rate swung 100% (21-22Z,
# 19Z) to 67% (00Z, 02Z), and the sibling's OWN 429s rose with ours (0 -> 10/h
# at the same grant rate). A constant cannot carry that variance: the
# sibling's traffic on our seconds is refuted as the night residual's cause,
# the residual is exchange-side tightness in 00-05Z that both tenants pay,
# and the pre-registered `*:3/5` phase shift (status 10-28 08:35Z (5)) was
# left untaken -- its upside is bounded to those hours and its cost is a
# capture-grid change across ~10 pinned sites. That reading took an awk over
# the journal and a script over the trace; this is the same table behind one
# flag (`--kalshi429-hourly [HOURS]`), so the next drift question is a command
# rather than an afternoon. Each hour carries which sweeps logged anything in
# it, because the sweeps are the dominant daytime 429 source and an hour with
# one running is a different population from an idle one.

KALSHI_429_HOURLY_H = 30
SWEEP_UNITS = ("hyxlab-sweep.service", "hyxlab-poly-sweep.service")
_CYCLE_429_RE = re.compile(r"'kalshi_429': (\d+)")


def _hour_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H")


def _journal_json(stdout: str):
    """Yield (epoch_s, record) for each JSON line carrying a realtime stamp."""
    for line in stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        try:
            ts = int(rec["__REALTIME_TIMESTAMP"]) / 1e6
        except (KeyError, TypeError, ValueError):
            continue
        yield ts, rec


def parse_collect_cycles(stdout: str) -> dict[str, tuple[int, int, int]]:
    """hour -> (cycles, failing cycles, 429s) from the collector's stats lines.

    A cycle is one `'kalshi_429': N` stats line; it is failing when N > 0. The
    per-cycle count is the collector's own ledger of retried refusals, so the
    429 column here can differ from the `[kalshi] ... HTTP 429` line count by
    the retries that never printed (none observed, but two ledgers are two).
    """
    out: dict[str, list[int]] = {}
    for ts, rec in _journal_json(stdout):
        msg = rec.get("MESSAGE")
        m = _CYCLE_429_RE.search(msg) if isinstance(msg, str) else None
        if m is None:
            continue
        n = int(m.group(1))
        row = out.setdefault(_hour_key(ts), [0, 0, 0])
        row[0] += 1
        row[1] += n > 0
        row[2] += n
    return {h: (a, b, c) for h, (a, b, c) in out.items()}


def parse_kalshi_429_hourly(stdout: str) -> dict[str, dict[str, int]]:
    """hour -> unit -> `[kalshi] ... HTTP 429` lines; the 24h predicate, binned."""
    out: dict[str, dict[str, int]] = {}
    for ts, rec in _journal_json(stdout):
        msg, unit = rec.get("MESSAGE"), rec.get("_SYSTEMD_USER_UNIT")
        if not isinstance(msg, str) or not isinstance(unit, str):
            continue
        if "[kalshi]" in msg and "HTTP 429" in msg:
            per = out.setdefault(_hour_key(ts), {})
            per[unit] = per.get(unit, 0) + 1
    return out


def parse_active_hours(stdout: str, units: tuple[str, ...] = SWEEP_UNITS) -> dict[str, set[str]]:
    """hour -> units in `units` that logged ANY line in it (the sweeps' running
    hours). `journalctl -u X` also returns systemd's own start/stop lines about
    X, stamped `_SYSTEMD_USER_UNIT=init.scope`; those are the unit's edges,
    not its output, and are dropped here rather than printed as a tenant."""
    out: dict[str, set[str]] = {}
    for ts, rec in _journal_json(stdout):
        unit = rec.get("_SYSTEMD_USER_UNIT")
        if isinstance(unit, str) and unit in units:
            out.setdefault(_hour_key(ts), set()).add(unit)
    return out


def parse_sibling_trace_hourly(lines, since_ts: float) -> dict[str, tuple[int, int]]:
    """hour -> (429s, grants) from governor trace lines at or after `since_ts`."""
    out: dict[str, list[int]] = {}
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ts = rec.get("ts") if isinstance(rec, dict) else None
        if not isinstance(ts, (int, float)) or ts < since_ts:
            continue
        kind = rec.get("kind")
        if kind not in ("429", "slot"):
            continue
        row = out.setdefault(_hour_key(ts), [0, 0])
        row[0 if kind == "429" else 1] += 1
    return {h: (a, b) for h, (a, b) in out.items()}


def _journal_hourly(args: list[str], hours: int) -> str | None:
    try:
        proc = subprocess.run(
            ["journalctl", "--user", "--no-pager", "--since", f"-{hours}h", *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode in (0, 1) else None


def read_kalshi_429_hourly(hours: int = KALSHI_429_HOURLY_H):
    """The three journal reads for the hourly table; each None when unreadable."""
    cycles = _journal_hourly(
        [
            "-u",
            "hyxlab-collect.service",
            "--grep=kalshi_429",
            "-o",
            "json",
            "--output-fields=MESSAGE,__REALTIME_TIMESTAMP",
        ],
        hours,
    )
    ours = _journal_hourly(
        [
            "--grep=HTTP 429",
            "-o",
            "json",
            "--output-fields=MESSAGE,_SYSTEMD_USER_UNIT,__REALTIME_TIMESTAMP",
        ],
        hours,
    )
    active = _journal_hourly(
        [
            *[a for u in SWEEP_UNITS for a in ("-u", u)],
            "-o",
            "json",
            "--output-fields=_SYSTEMD_USER_UNIT,__REALTIME_TIMESTAMP",
        ],
        hours,
    )
    return (
        parse_collect_cycles(cycles) if cycles is not None else None,
        parse_kalshi_429_hourly(ours) if ours is not None else None,
        parse_active_hours(active) if active is not None else None,
    )


def read_sibling_429_hourly(
    since_ts: float, base: Path = SIBLING_TRACE
) -> dict[str, tuple[int, int]] | None:
    """The sibling's (429s, grants) by hour since `since_ts`; None when no
    trace could be read (never an empty dict -- that would read as idle)."""
    paths = sibling_trace_paths(base)
    if not paths:
        return None
    out: dict[str, tuple[int, int]] = {}
    read_any = False
    for path in paths:
        try:
            with path.open(errors="replace") as fh:
                part = parse_sibling_trace_hourly(fh, since_ts)
        except OSError:
            continue
        read_any = True
        for h, (a, b) in part.items():
            c, d = out.get(h, (0, 0))
            out[h] = (c + a, d + b)
    return out if read_any else None


def kalshi_429_hourly_lines(
    cycles: dict[str, tuple[int, int, int]] | None,
    ours: dict[str, dict[str, int]] | None,
    active: dict[str, set[str]] | None,
    sibling: dict[str, tuple[int, int]] | None,
    now: datetime,
    hours: int = KALSHI_429_HOURLY_H,
) -> list[str]:
    """One line per clock hour, oldest first, both ledgers plus the sweeps.

    An hour with no collector cycle prints `collect --` (the journal has
    rotated past it, or the timer was down -- the digest cannot tell which,
    and says neither). An unreadable ledger prints as unreadable on every
    line; a sibling hour with no trace rows prints `sibling --`, since the
    governor writes nothing when the sibling is idle.
    """
    start = int(now.timestamp()) // 3600 * 3600 - (hours - 1) * 3600
    lines = [
        f"KALSHI429 by the hour, last {hours}h (collect cycles / failing / "
        f"collector 429s | this repo's `[kalshi] HTTP 429` by unit | sibling grants / 429s | sweeps logging)"
    ]
    for k in range(hours):
        ts = start + k * 3600
        h = _hour_key(ts)
        label = datetime.fromtimestamp(ts, UTC).strftime("%m-%d %HZ")
        if cycles is None:
            col = "collect journal unreadable"
        elif h in cycles:
            n, fail, n429 = cycles[h]
            clean = 100.0 * (n - fail) / n
            col = f"collect {n:2d} cyc {fail:2d} fail ({clean:3.0f}% clean) {n429:3d} x 429"
        else:
            col = "collect --"
        if ours is None:
            mine = "journal unreadable"
        else:
            per = ours.get(h, {})
            sweep = sum(v for u, v in per.items() if u in SWEEP_UNITS)
            other = sum(v for u, v in per.items() if u not in SWEEP_UNITS)
            mine = f"sweeps {sweep:3d} other {other:3d}"
        if sibling is None:
            sib = "sibling trace absent"
        elif h in sibling:
            s429, slots = sibling[h]
            sib = f"sibling {slots:5d} req {s429:3d} x 429"
        else:
            sib = "sibling --"
        if active is None:
            run = "sweeps unknown"
        else:
            names = sorted(
                u.removeprefix("hyxlab-").removesuffix(".service") for u in active.get(h, ())
            )
            run = "sweeps " + (",".join(names) if names else "idle")
        lines.append(f"{label}  {col} | {mine} | {sib} | {run}")
    return lines


def kalshi_429_hourly_main(hours: int) -> int:
    now = datetime.now(UTC)
    cycles, ours, active = read_kalshi_429_hourly(hours)
    sibling = read_sibling_429_hourly(now.timestamp() - hours * 3600)
    for line in kalshi_429_hourly_lines(cycles, ours, active, sibling, now, hours):
        print(line, flush=True)
    return 0


# ---- the same two ledgers by the SECOND --------------------------------------
#
# The hour table says the two tenants' refusals rise and fall together; the
# second says whether they are the SAME refusal event. Measured 2026-10-28
# 20Z (sweeps idle, both tenants at flat own-load): hyxlab's three `HTTP 429`
# lines fell at 20:35:02, 20:35:09 and 20:45:21, the sibling's four at
# 20:35:02, 20:35:04, 20:35:09 and 20:45:20 -- every one of ours inside 1s
# of one of theirs. Two counters that refuse in the same second are one
# bucket closing on the host, not one tenant's load pushing the other over;
# #145 found the same by hand (7 of 14 failing cycles within 10s). This is
# that pairing as a flag: each of this repo's 429s with the signed seconds
# to the nearest sibling 429, and the share inside `PAIR_WINDOW_S`.

PAIR_WINDOW_S = 2.0


def parse_kalshi_429_events(stdout: str) -> list[tuple[float, str]]:
    """(epoch_s, unit) per `[kalshi] ... HTTP 429` line, oldest first."""
    out = []
    for ts, rec in _journal_json(stdout):
        msg, unit = rec.get("MESSAGE"), rec.get("_SYSTEMD_USER_UNIT")
        if (
            isinstance(msg, str)
            and isinstance(unit, str)
            and "[kalshi]" in msg
            and "HTTP 429" in msg
        ):
            out.append((ts, unit))
    return sorted(out)


def parse_sibling_429_events(lines, since_ts: float) -> list[float]:
    """Epoch seconds of the sibling's `429` records at or after `since_ts`."""
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ts = rec.get("ts") if isinstance(rec, dict) else None
        if isinstance(ts, (int, float)) and ts >= since_ts and rec.get("kind") == "429":
            out.append(float(ts))
    return sorted(out)


def read_sibling_429_events(since_ts: float, base: Path = SIBLING_TRACE) -> list[float] | None:
    paths = sibling_trace_paths(base)
    if not paths:
        return None
    out: list[float] = []
    read_any = False
    for path in paths:
        try:
            with path.open(errors="replace") as fh:
                out.extend(parse_sibling_429_events(fh, since_ts))
        except OSError:
            continue
        read_any = True
    return sorted(out) if read_any else None


def nearest_delta(ts: float, others: list[float]) -> float | None:
    """Signed seconds from `ts` to the nearest of `others` (sorted); None if empty."""
    if not others:
        return None
    i = bisect.bisect_left(others, ts)
    cands = [others[j] for j in (i - 1, i) if 0 <= j < len(others)]
    return min((o - ts for o in cands), key=abs)


def kalshi_429_pair_lines(
    ours: list[tuple[float, str]] | None,
    sibling: list[float] | None,
    hours: int,
    window_s: float = PAIR_WINDOW_S,
) -> list[str]:
    """One line per hyxlab 429 with its nearest sibling 429, then the share
    inside `window_s`. Unreadable ledgers are named; an empty one is a fact."""
    head = f"KALSHI429 paired by the second, last {hours}h (window +/-{window_s:g}s)"
    if ours is None:
        return [head, "hyxlab journal unreadable"]
    if sibling is None:
        return [head, f"sibling trace absent; {len(ours)} hyxlab 429(s) unpaired"]
    lines = [head]
    inside = 0
    for ts, unit in ours:
        d = nearest_delta(ts, sibling)
        when = datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M:%S")
        short = unit.removeprefix("hyxlab-").removesuffix(".service")
        if d is None:
            lines.append(f"{when}Z {short:<10} sibling: none in window")
            continue
        hit = abs(d) <= window_s
        inside += hit
        lines.append(
            f"{when}Z {short:<10} nearest sibling 429 {d:+7.1f}s{'  <- same event' if hit else ''}"
        )
    n = len(ours)
    share = f"{inside}/{n}" + (f" = {100.0 * inside / n:.0f}%" if n else "")
    lines.append(f"paired inside {window_s:g}s: {share}; sibling 429s in window: {len(sibling)}")
    return lines


def kalshi_429_pairs_main(hours: int) -> int:
    now = datetime.now(UTC)
    ours = _journal_hourly(
        [
            "--grep=HTTP 429",
            "-o",
            "json",
            "--output-fields=MESSAGE,_SYSTEMD_USER_UNIT,__REALTIME_TIMESTAMP",
        ],
        hours,
    )
    events = parse_kalshi_429_events(ours) if ours is not None else None
    sibling = read_sibling_429_events(now.timestamp() - hours * 3600)
    for line in kalshi_429_pair_lines(events, sibling, hours):
        print(line, flush=True)
    return 0


def journal_floor() -> datetime | None:
    """Oldest entry journald still holds, or None if it cannot be read.

    Read by streaming and stopping after ONE line: `journalctl` emits
    oldest-first and this journal is large, so capturing it whole to look at its
    head would make the cheapest fact in the digest the most expensive. The
    first record is a lower bound on retention rather than retention itself --
    it is the oldest entry, and the true vacuum point is somewhere at or before
    it -- which is the safe direction: it can only make the reported window
    SHORTER than what was really covered.
    """
    try:
        proc = subprocess.Popen(
            [
                "journalctl",
                "--user",
                "--no-pager",
                "-o",
                "json",
                "--output-fields=__REALTIME_TIMESTAMP",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        line = proc.stdout.readline() if proc.stdout else ""
        rec = json.loads(line)
        return datetime.fromtimestamp(int(rec["__REALTIME_TIMESTAMP"]) / 1e6, UTC)
    except (KeyError, TypeError, ValueError):
        return None
    finally:
        proc.kill()
        proc.wait()


#: Shortest run of one label that the RECENT line folds into `label xN a..b`.
#: Measured 2026-10-08: streamd's 10-07 OOM loop printed 189 timestamps on one
#: line, burying the single `exit-code/1` among them. A fold keeps every count
#: and every change of label in order -- that interruption is the reading.
FOLD_RUN = 3


def _render_hits(hits: list[UnitFailure]) -> str:
    """Chronological failures, consecutive identical labels folded into a span."""
    runs: list[list[UnitFailure]] = []
    for h in sorted(hits, key=lambda f: f.at):
        if runs and runs[-1][0].label == h.label:
            runs[-1].append(h)
        else:
            runs.append([h])
    parts = []
    for run in runs:
        if len(run) < FOLD_RUN:
            parts.extend(f"{h.label} {h.at:%m-%d %H:%M}Z" for h in run)
            continue
        first, last = run[0].at, run[-1].at
        end = f"{last:%H:%M}Z" if last.date() == first.date() else f"{last:%m-%d %H:%M}Z"
        parts.append(f"{run[0].label} x{len(run)} {first:%m-%d %H:%M}Z..{end}")
    return ", ".join(parts)


def failure_history_lines(
    failures: list[UnitFailure],
    rows: list[UnitHealth],
    now: datetime,
    floor: datetime | None = None,
    hours: int = FAILURE_WINDOW_H,
    width: int = 0,
) -> list[str]:
    """The recent-failure section: per-unit recurrence, then one summary.

    Printed even when the count is zero. An absent section and a broken reader
    look identical, and "nothing failed since yesterday" is a fact worth having
    at cold start -- it is the one the digest could not previously state.
    """
    still_bad = {r.unit for r in rows if not r.ok}
    by_unit: dict[str, list[UnitFailure]] = {}
    for f in failures:
        by_unit.setdefault(f.unit, []).append(f)
    lines = []
    for unit in sorted(by_unit):
        hits = by_unit[unit]
        # CLEARED is the whole point of the section: it is the state in which the
        # unit rows above say nothing at all. `still failed` is not a second
        # alarm -- the count beside it is recurrence, which the row cannot carry.
        status = "still failed" if unit in still_bad else "CLEARED"
        when = _render_hits(hits)
        lines.append(f"{'RECENT':<10} {unit:<{width}}  {len(hits)}x in {hours}h, {status}: {when}")
    covered = hours
    if floor is not None:
        covered = min(covered, (now - floor).total_seconds() / 3600.0)
    window = (
        f"last {hours}h"
        if covered >= hours
        else f"last {covered:.1f}h ONLY — journald retains no further back"
    )
    cleared = sum(1 for f in failures if f.unit not in still_bad)
    lines.append(
        f"[health] {len(failures)} unit failure{'' if len(failures) == 1 else 's'} in the "
        f"{window}" + (f"; {cleared} on units now healthy (invisible above)" if cleared else "")
    )
    return lines


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

    **`prior.unread` IS A FINDING, AND IT USED TO REACH NOBODY.** qa strips its
    own `prior QA run was read` result out of `failures` before recording --
    correctly, or one unread failure re-arms the report of itself forever
    (`qa._own_findings`) -- and this line read `failures`. So the digest could
    not name that check on any run, and the run it fires on is by construction
    the run where every OTHER check is green: the operator got `FAILED
    hyxlab-qa.service` from the unit above and `clean` from this line, with no
    name anywhere between them. That is the shape the whole module exists to
    close, one layer up from where it was closed. qa now records the reason in
    its own field and this reads it; "clean" is withheld, because the run
    exited 1 and the unit line says so.
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
    elif prior.unread:
        verdict = "no check failed"
    if standing:
        verdict += f" (standing skip{'s' if len(standing) > 1 else ''}: {', '.join(standing)})"
    line = f"QA         last run {prior.at:%m-%d %H:%M}Z ({age_h:.1f}h ago): {verdict}"
    if prior.unread:
        # Always, not only when the rest is green: it is independent of what
        # the other sections found today, so suppressing it under a failure
        # list would hide it on exactly the busy run nobody re-reads.
        line += f"\nUNREAD     {QA_UNREAD_CHECK} — {prior.unread}"
    if started is not None and started - prior.at.timestamp() > QA_RECORD_LAG_S:
        gap_h = (started - prior.at.timestamp()) / 3600.0
        line += (
            f"\nRECORD-STALE {QA_UNIT} started {gap_h:.1f}h AFTER this record — "
            "the run wrote its record somewhere else, or died before recording"
        )
    return line


def outlived_by_failure(
    row: UnitHealth,
    svc: dict[str, str],
    timer: dict[str, str] | None,
    failures: list[UnitFailure],
) -> UnitHealth:
    """An OK row the journal contradicts: the unit's LAST failure is newer than
    any run the manager still remembers.

    `judge` reads the manager's memory, and the manager forgets on restart.
    Measured 2026-10-05: the 09:01Z boot's catch-up runs of `hyxlab-sweep` and
    `hyxlab-poly-sweep` failed on DNS, the box crashed and rebooted at 09:17Z,
    and the fresh manager answered `Result=success` with NO `ExecMain*`
    timestamps -- the default for a unit that has not run this boot, which is
    #2's shape exactly. The timer's `LastTriggerUSec` survives (`Persistent=`
    keeps it on disk), so the slot read as taken and the unit as OK, and the
    failure section below called both CLEARED -- for eleven hours, on two units
    that had not succeeded that day. A failure is cleared only by a run that
    STARTED after it; the journal is read across boots, the manager is not.
    """
    if row.state != "OK":
        return row
    mine = [f for f in failures if f.unit == row.unit]
    if not mine:
        return row
    last = max(mine, key=lambda f: f.at)
    seen = [
        _epoch(svc.get("ExecMainStartTimestamp", "")),
        _epoch(svc.get("ExecMainExitTimestamp", "")),
        _epoch((timer or {}).get("LastTriggerUSec", "")),
    ]
    latest_run = max((t for t in seen if t is not None), default=None)
    if latest_run is not None and latest_run > last.at.timestamp():
        return row
    return UnitHealth(
        row.unit,
        "FAILED",
        f"last run failed ({last.result}) {last.at:%m-%d %H:%M}Z per the journal; "
        "the manager remembers no run since (state reset by a restart)",
    )


def report(now: float | None = None, failures: list[UnitFailure] | None = None) -> list[UnitHealth]:
    now = now if now is not None else datetime.now(UTC).timestamp()
    seen = [
        (svc_name, show(f"{Path(svc_name).stem}.timer") if has_timer else None, show(svc_name))
        for svc_name, has_timer in discover_units()
    ]
    # The kernel journal is read at most once per digest, and only when the
    # manager already says something was oom-killed. No fault, no cost.
    victims = read_kernel_oom() if any(s.get("Result") == "oom-kill" for _, _, s in seen) else []
    return [
        outlived_by_failure(
            judge(
                svc_name,
                svc_props,
                timer,
                now,
                oom_attribution(victims, svc_name) if svc_props.get("Result") == "oom-kill" else "",
            ),
            svc_props,
            timer,
            failures or [],
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
        remedy = "promote.sh --units-only" if d.state in REPAIRABLE else OPERATOR_REMEDY
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
    mains = {
        "--kalshi429-hourly": kalshi_429_hourly_main,
        "--kalshi429-pairs": kalshi_429_pairs_main,
    }
    if argv and argv[0] in mains and len(argv) <= 2:
        try:
            hours = int(argv[1]) if len(argv) == 2 else KALSHI_429_HOURLY_H
        except ValueError:
            hours = 0
        if hours >= 1:
            return mains[argv[0]](hours)
    if argv:
        print(
            "usage: python -m collector.health "
            "[--drift-only | --kalshi429-hourly [HOURS] | --kalshi429-pairs [HOURS]]",
            file=sys.stderr,
        )
        return USAGE_ERROR
    main()
    return 0


def main() -> None:
    now = datetime.now(UTC)
    print(f"[health] {now:%Y-%m-%d %H:%M}Z — persisted state only; no checks re-run", flush=True)
    units = {u for u, _ in discover_units()}
    failures = read_unit_failures(units)
    rows = report(now.timestamp(), failures)
    width = max((len(r.unit) for r in rows), default=0)
    for r in sorted(rows, key=lambda r: (r.ok, r.unit)):
        print(f"{r.state:<10} {r.unit:<{width}}  {r.detail}", flush=True)
    for line in failure_history_lines(failures, rows, now, journal_floor(), width=width):
        print(line, flush=True)
    killed: list[str] = []
    for c in live_cgmem(sorted(units)):
        v = judge_cgmem(c)
        print(f"{v.state:<10} {v.unit:<{width}}  {v.detail}", flush=True)
        if v.state == "OOM-KILLED":
            killed.append(v.unit)
    qa_svc = show(QA_UNIT)
    print(judge_qa(_load_state(qa_record_path(qa_svc)), qa_svc, now), flush=True)
    since = now.timestamp() - KALSHI_429_WINDOW_H * 3600
    print(
        f"{'KALSHI429':<10} {'(host)':<{width}}  "
        f"{kalshi_429_line(read_kalshi_429(), read_sibling_429(since))}",
        flush=True,
    )
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
    bad = [r.unit for r in rows if not r.ok] + killed
    print(
        f"[health] {len(rows) - len(bad)}/{len(rows)} units ok"
        + (f"; ATTENTION: {bad}" if bad else ""),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
