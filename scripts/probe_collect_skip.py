"""Prove the LIVE collect-skip producer by forcing one lock-wait, in-window.

`qa_collect_skips` reads `data/collect_skips.jsonl` against systemd's own
count of exit-75 cycles, and when neither witness has seen a skip it prints
UNVERIFIED -- correctly: a quiet sidecar is what a dead producer and a
healthy archive both look like. After the sweep stopped holding the lock
across 07:28-07:34Z (last real skip 2026-08-07 07:44Z) that line stood for
26 days with no cycle ever exercising `record_skip()` in production.

This probe exercises it WITHOUT costing a scheduled cycle:

  1. it refuses unless THIS tick's scheduled cycle has already exited --
     asked of systemd, not assumed from a constant (see below);
  2. it refuses outside a coarse clock band, as a cheap outer guard;
  3. it takes `data/writer.lock` itself, naming itself as holder exactly as
     a real writer does;
  4. it runs the STABLE tree's collector -- the installed unit's own
     interpreter, module and working directory -- with `--once` and a
     `--lock-wait` of a few seconds;
  5. it asserts exit 75 and a NEW sidecar row whose `holder.pid` is this
     process.

WHY SYSTEMD AND NOT A CONSTANT (measured 2026-09-03, one full 300 s period
sampled at 4 Hz against the real lock). The version shipped 09-02 opened its
window at +40 s because "a scheduled cycle finishes ~25 s after its tick".
Both halves of that are wrong:

  * A cycle is FETCH -> acquire lock -> WRITE -> release (`collect.py`), so
    what matters is when it TAKES the lock, which is `fetch_s` after the
    tick -- and `fetch_s` measured 18.5-54.3 s over 20 consecutive cycles
    (`total_s` 27-113 s, not 17-23 s). A cycle can therefore reach for the
    lock at ~+54 s, INSIDE the old window. A probe holding the lock across
    that moment makes the scheduled cycle wait -- and, on its production
    budget, possibly skip. That is precisely the harm this probe promises
    never to do, so the guard may not be a guess about the clock.
  * "Every scheduled cycle after the probe finds the lock free" ignored that
    FOUR units write this archive. In one period the lock was taken by
    `hyxlab-sweep` (7 bursts, +0.3-8.1 s), `hyxlab-collect` (+29.3-32.7 s),
    `hyxlab-poly-sweep` (+83.6-106.6 s, a 23 s hold) and `hyxlab-breadth`
    (+122.0-123.4 s). The non-blocking flock below is what keeps that safe;
    a refusal naming another writer is normal, and means "retry next tick".

The row the probe leaves is a real row from the real producer, and the next
QA reads it as "1 skipped cycle in 24h (max 3)" -- the rate check, i.e. the
section measured something. Its `holder.cmd` names this script, so nobody
reads it as sweep contention.

Usage (from the dev tree):  .venv/bin/python scripts/probe_collect_skip.py
Exit 0 on proof, 1 on refusal or a failed assertion. Never touches DuckDB.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# `python scripts/probe_collect_skip.py` puts scripts/ on sys.path -- NOT the
# repo root -- so the project packages are unimportable from the invocation this
# module's own docstring prescribes. pytest hid that: `pythonpath = ["."]` makes
# the unit tests import a module the operator cannot run.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyxlab import lockid  # noqa: E402

STABLE = Path("/home/devs/workspace/hyxrestration-stable")
LOCK_FILE = Path("data/writer.lock")
SKIP_LOG = Path("data/collect_skips.jsonl")
COLLECT_UNIT = "hyxlab-collect.service"
#: Seconds past a `*:0/5` boundary inside which a probe may START. This is
#: only the OUTER guard -- `collect_cycle_settled()` is what actually decides
#: the low edge. Low: past the latest measured lock RELEASE of a scheduled
#: cycle (fetch_s max 54.3 + write ~3.5 = ~58 s). High: the probe's own worst
#: case is the subprocess timeout, so hi + SUBPROCESS_TIMEOUT_S must clear the
#: next tick at +300 with room to spare.
WINDOW_S = (60.0, 170.0)
LOCK_WAIT_S = 5.0
#: The collector's own worst measured cycle is fetch 54.3 s + LOCK_WAIT_S;
#: 90 s covers that with margin and keeps the probe's WORST case inside
#: the tick. 120 s did not: 170 + 120 + 10 lands exactly on +300.
SUBPROCESS_TIMEOUT_S = 90.0
SKIP_EXIT = 75

#: Sentinel: a systemctl read that FAILED must not be spellable as "not
#: supplied", or a test injecting "unreadable" would silently query the host.
_QUERY_SYSTEMD = object()


def seconds_past_tick(now: float | None = None) -> float:
    now = time.time() if now is None else now
    return now % 300.0


def in_window(now: float | None = None, window: tuple[float, float] = WINDOW_S) -> bool:
    lo, hi = window
    return lo <= seconds_past_tick(now) <= hi


def _systemd_show(unit: str = COLLECT_UNIT) -> str | None:
    """`systemctl --user show` for the unit, or None if it could not be read."""
    try:
        p = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "-p",
                "ActiveState",
                "-p",
                "ExecMainExitTimestampMonotonic",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def collect_cycle_settled(
    show: str | None | object = _QUERY_SYSTEMD,
    now: float | None = None,
    mono: float | None = None,
) -> tuple[bool, str]:
    """Has THIS tick's scheduled collect cycle already exited?

    The one question the old constant window was really trying to answer, asked
    of the only witness that knows. systemd reports the unit's last exit on the
    CLOCK_MONOTONIC scale (microseconds), so the current `*:0/5` boundary is
    projected onto that scale and the exit must fall AFTER it.

    An unreadable or unparsable answer is NOT permission. The probe's whole
    claim is that it never costs a real cycle; a guard that opens when it
    cannot see is not a guard (`--restart-young`'s "an unknown age never
    defers", pointed the same way).
    """
    now = time.time() if now is None else now
    mono = time.clock_gettime(time.CLOCK_MONOTONIC) if mono is None else mono
    text = _systemd_show() if show is _QUERY_SYSTEMD else show
    if text is None:
        return False, f"could not read {COLLECT_UNIT} from systemd"
    props = {}
    for line in text.splitlines():
        k, _, v = line.partition("=")
        if _:
            props[k.strip()] = v.strip()
    state = props.get("ActiveState")
    if state is None:
        return False, f"{COLLECT_UNIT} reported no ActiveState"
    if state not in ("inactive", "failed"):
        return False, f"{COLLECT_UNIT} is {state} -- this tick's cycle is still running"
    raw = props.get("ExecMainExitTimestampMonotonic")
    try:
        exit_mono = int(raw) / 1e6
    except (TypeError, ValueError):
        return False, f"{COLLECT_UNIT} reported no usable exit timestamp ({raw!r})"
    if exit_mono <= 0:
        return False, f"{COLLECT_UNIT} has never run on this boot"
    # Project the current tick boundary onto the monotonic scale.
    tick_mono = mono - (now % 300.0)
    if exit_mono <= tick_mono:
        age = tick_mono - exit_mono
        return False, (
            f"{COLLECT_UNIT} last exited {age:.0f}s BEFORE this tick -- "
            "this tick's cycle has not run yet"
        )
    return True, f"{COLLECT_UNIT} exited {mono - exit_mono:.0f}s ago, after this tick"


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def main() -> int:
    if not in_window():
        past = seconds_past_tick()
        print(
            f"REFUSED: {past:.0f}s past the last *:0/5 tick, window is "
            f"{WINDOW_S[0]:.0f}-{WINDOW_S[1]:.0f}s; try again in the dead band",
            flush=True,
        )
        return 1
    if not (STABLE / ".venv/bin/python").exists():
        print(f"REFUSED: no stable interpreter under {STABLE}", flush=True)
        return 1
    settled, why = collect_cycle_settled()
    if not settled:
        print(f"REFUSED: {why}", flush=True)
        return 1

    before = len(_rows(SKIP_LOG))
    LOCK_FILE.parent.mkdir(exist_ok=True)
    with open(LOCK_FILE, "a") as holder:
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("REFUSED: writer lock already held by someone else", flush=True)
            return 1
        lockid.note_holder(str(LOCK_FILE))
        t0 = time.monotonic()
        try:
            p = subprocess.run(
                [
                    str(STABLE / ".venv/bin/python"),
                    "-m",
                    "collector.collect",
                    "--once",
                    "--lock-wait",
                    str(LOCK_WAIT_S),
                ],
                cwd=STABLE,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_S,
            )
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
    held_s = time.monotonic() - t0

    rows = _rows(SKIP_LOG)
    new = rows[before:]
    print(p.stdout.strip(), flush=True)
    if p.stderr.strip():
        print(p.stderr.strip(), file=sys.stderr, flush=True)
    ok_exit = p.returncode == SKIP_EXIT
    # "a row naming ME", not "exactly one row": three other units write this
    # archive, so a genuine concurrent skip is possible and must not be read as
    # the producer misbehaving -- that would fail the probe for being right.
    mine = [r for r in new if (r.get("holder") or {}).get("pid") == os.getpid()]
    ok_row = len(mine) == 1
    print(
        f"probe {datetime.now(UTC).isoformat(timespec='seconds')}: exit={p.returncode} "
        f"(want {SKIP_EXIT}) new_rows={len(new)} mine={len(mine)} (want 1, pid {os.getpid()}) "
        f"held={held_s:.1f}s",
        flush=True,
    )
    if ok_exit and ok_row:
        print("PROVEN: the live producer records a skip and names its blocker", flush=True)
        return 0
    print("FAILED: the live producer did not behave as the unit tests say it does", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
