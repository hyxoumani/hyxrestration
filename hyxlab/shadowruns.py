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

from datetime import datetime, timedelta


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


def complete_runs(conn) -> list[str]:
    """EVERY finished shadow run that produced fills, oldest first -- the
    same two predicates as `latest_complete_run`, without the LIMIT.

    The newest one is the report's subject; the rest are what that default
    walked past. Measured 2026-11-14: 20261014T030956 (61,812 fills, 9.5
    days) and 20261023T143153 (29,278 fills, 9.4 days) were never reported,
    because each was followed within the day by a second restart (a 23-min
    run, a 6-hour run) that became "the newest finished run" before the
    01:20Z timer came round (mistakes #194).
    """
    return [
        r[0]
        for r in conn.execute(
            "SELECT r.run_id FROM shadow_runs r"
            " WHERE EXISTS (SELECT 1 FROM shadow_runs l WHERE l.started_at > r.started_at)"
            "   AND EXISTS (SELECT 1 FROM shadow_fills f WHERE f.run_id = r.run_id)"
            " ORDER BY r.started_at"
        ).fetchall()
    ]


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


# The blind-seed window (mistakes #196; pre-registered in
# docs/wiki/simulation-honesty.md BEFORE any report was re-run under it).
# From the stream's first per-shard gap row until `shadow_runs.seed_floor`
# existed, a run seeded from the NEWEST gap row -- one connection's
# reconnect -- and most of its books stayed unknown until their own shard
# happened to reconnect. Its early equity is real, but it is the equity
# of a narrower book than the run is named for. The instant is the
# table's own `min(started_at) where shard is not null`, read 2026-11-14;
# twelve hours is the upper end of the range measured on the 11-day run
# (2.7% snapshots per event in its first 6 h, 8-13% after). Neither is
# re-fitted to what the exclusion changes. Here, not in one report: three
# reports read the same ledger, and a rule with three copies is three
# rules the day one constant moves.
PER_SHARD_GAPS_FROM = datetime(2026, 11, 2, 8, 29, 16)
BLIND_SEED_HOURS = 12
BLIND_SEED_STATUSES = ("not_in_population", "partly_blind", "wholly_blind", "unplaceable")


def _run_start_and_floor(conn, run_id: str) -> tuple | None:
    """(started_at, seed_floor) for a run, or None when the ledger cannot
    say: no `shadow_runs` table, no row, or a NULL start."""
    cols = {
        r[0]
        for r in conn.execute(
            "select column_name from information_schema.columns where table_name = 'shadow_runs'"
        ).fetchall()
    }
    if "started_at" not in cols:
        return None
    floor = "seed_floor" if "seed_floor" in cols else "NULL"
    row = conn.execute(
        f"select started_at, {floor} from shadow_runs where run_id = ?", [run_id]
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return row


def blind_seed_until(conn, run_id: str) -> datetime | None:
    """Where this run's blind-seed window ends, or None if it has none.

    A run is in the population when it started in the per-shard era and
    recorded no `seed_floor`. A ledger with no `shadow_runs` row for the
    run (or no such table) cannot place it in the era: None, the run is
    read as it always was -- the rule excludes on evidence, never on a
    missing row."""
    row = _run_start_and_floor(conn, run_id)
    if row is None or row[1] is not None or row[0] < PER_SHARD_GAPS_FROM:
        return None
    return row[0] + timedelta(hours=BLIND_SEED_HOURS)


def blind_seed_block(conn, run_id: str, last_ts: datetime | None) -> dict:
    """The annotation a WHOLE-LIFE number carries about the window.

    The diurnal report can drop blind hours; a whole-life total (equity
    delta, fills observed) cannot, so it says how much of the life it
    sums was blind instead. Four states, because "no window" from a run
    the ledger cannot place is not "no window" from a run known to be
    outside the population: `unplaceable` is the first, and it is never
    folded into `not_in_population`."""
    row = _run_start_and_floor(conn, run_id)
    if row is None:
        return {
            "blind_seed_status": "unplaceable",
            "blind_seed_until": None,
            "blind_seed_life_hours": None,
        }
    until = blind_seed_until(conn, run_id)
    if until is None:
        return {
            "blind_seed_status": "not_in_population",
            "blind_seed_until": None,
            "blind_seed_life_hours": 0.0,
        }
    started = row[0]
    end = last_ts if last_ts is not None else started
    blind_h = max(0.0, (min(end, until) - started).total_seconds() / 3600)
    return {
        "blind_seed_status": "wholly_blind" if end <= until else "partly_blind",
        "blind_seed_until": f"{until:%Y-%m-%dT%H:%M:%SZ}",
        "blind_seed_life_hours": round(blind_h, 2),
    }
