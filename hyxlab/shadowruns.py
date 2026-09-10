"""Which shadow run a report should measure, and when it became measurable.

Kernel, not `simulator/`, because it now has TWO callers on opposite
sides of the import boundary: `simulator.divergence` picks the run it
replays, and `collector.qa` asks whether that run has been measured yet.
A collector cannot import a simulator (tests/test_boundaries.py), and a
second hand-written copy of this SELECT is how the report and its
consumer would come to disagree about which run is the subject --
leaving the consumer green while the thing it audits sat unmeasured,
which is the exact failure (#46) the consumer exists to catch.
"""

from __future__ import annotations

from datetime import datetime


def latest_complete_run(conn) -> str | None:
    """The newest shadow run that is finished and produced fills.

    The default used to be `ORDER BY count(*) DESC` -- the run with the
    MOST fills -- which makes re-running the report a no-op by
    construction: an argmax over a growing record only moves when a
    bigger run appears, and bigger runs get rarer as the record grows.
    Measured 2026-09-09: the report had defaulted to 20260810T081931
    (54,007 fills, ended 08-20, already reported 1.0/1.0) for three
    weeks while eight later runs went unmeasured, including
    20260829T191841 -- 38,143 fills over 8.8 days, the second-largest in
    the record and never reported. The point of the report is
    calibration drift over time; its default pointed at the past.

    "Finished" is read off the table rather than a heartbeat or a new
    column: exactly one shadow daemon can hold the archive's owner lock,
    so the live run -- if any -- is always `max(started_at)`. A strictly
    later run existing therefore proves the daemon restarted past this
    one. That also conservatively skips the newest run when the daemon
    is stopped for good; `--run` overrides, and the next restart makes
    it selectable. The asymmetry is deliberate: skipping a measurable
    run costs a flag, whereas replaying a LIVE run races a moving `end`
    against a stream archive being written at that same boundary.

    Runs with no fills are skipped -- a fill comparison over zero fills
    is not a zero divergence, it is no measurement at all.
    """
    row = conn.execute(
        "SELECT r.run_id FROM shadow_runs r"
        " WHERE EXISTS (SELECT 1 FROM shadow_runs l WHERE l.started_at > r.started_at)"
        "   AND EXISTS (SELECT 1 FROM shadow_fills f WHERE f.run_id = r.run_id)"
        " ORDER BY r.started_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def run_completed_at(conn, run_id: str) -> datetime | None:
    """The instant `run_id` became MEASURABLE, i.e. stopped being live.

    That is the start of the next run, not `max(shadow_equity.ts)` --
    the same fact `latest_complete_run` reads completion from, so the
    two cannot disagree about when the clock on a measurement starts.
    The ledger's last equity row is when the daemon last WROTE, which
    for a daemon killed mid-flush is earlier, and for a clean stop still
    says nothing about when a successor appeared.

    None when no later run exists -- `run_id` is live, and nothing is
    yet owed on it.
    """
    row = conn.execute(
        "SELECT min(started_at) FROM shadow_runs WHERE started_at >"
        " (SELECT started_at FROM shadow_runs WHERE run_id = ?)",
        [run_id],
    ).fetchone()
    return row[0] if row else None
