"""Prove the LIVE collect-skip producer by forcing one lock-wait, in-window.

`qa_collect_skips` reads `data/collect_skips.jsonl` against systemd's own
count of exit-75 cycles, and when neither witness has seen a skip it prints
UNVERIFIED -- correctly: a quiet sidecar is what a dead producer and a
healthy archive both look like. After the sweep stopped holding the lock
across 07:28-07:34Z (last real skip 2026-08-07 07:44Z) that line stood for
26 days with no cycle ever exercising `record_skip()` in production.

This probe exercises it WITHOUT costing a scheduled cycle:

  1. it refuses to run outside the dead band between two `*:0/5` firings
     (a scheduled cycle finishes ~25 s after its tick; the next one starts
     at +300 s, and would WAIT on us, not skip -- but the point is that it
     never has to);
  2. it takes `data/writer.lock` itself, naming itself as holder exactly as
     a real writer does;
  3. it runs the STABLE tree's collector -- the installed unit's own
     interpreter, module and working directory -- with `--once` and a
     `--lock-wait` of a few seconds;
  4. it asserts exit 75 and a NEW sidecar row whose `holder.pid` is this
     process.

Every scheduled cycle after the probe finds the lock free. The row it
leaves is a real row from the real producer, and the next QA reads it as
"1 skipped cycle in 24h (max 3)" -- which is the rate check, i.e. the
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

from hyxlab import lockid

STABLE = Path("/home/devs/workspace/hyxrestration-stable")
LOCK_FILE = Path("data/writer.lock")
SKIP_LOG = Path("data/collect_skips.jsonl")
#: Seconds past a `*:0/5` boundary inside which a probe may START. The
#: scheduled cycle is done by ~+25 s (measured 09-03: total_s 17-23); the
#: probe itself runs one fetch (~15-20 s) plus LOCK_WAIT, so it must be
#: gone well before +300.
WINDOW_S = (40.0, 200.0)
LOCK_WAIT_S = 5.0
SKIP_EXIT = 75


def seconds_past_tick(now: float | None = None) -> float:
    now = time.time() if now is None else now
    return now % 300.0


def in_window(now: float | None = None, window: tuple[float, float] = WINDOW_S) -> bool:
    lo, hi = window
    return lo <= seconds_past_tick(now) <= hi


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
                timeout=120,
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
    ok_row = len(new) == 1 and (new[0].get("holder") or {}).get("pid") == os.getpid()
    print(
        f"probe {datetime.now(UTC).isoformat(timespec='seconds')}: exit={p.returncode} "
        f"(want {SKIP_EXIT}) new_rows={len(new)} holder_pid="
        f"{(new[0].get('holder') or {}).get('pid') if new else None} (want {os.getpid()}) "
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
