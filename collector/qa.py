"""Daily data-quality checks over both archives (scheduled: hyxlab-qa.timer).

    python -m collector.qa [--hours 26]

Read-only. Every check prints PASS/FAIL; any FAIL makes the exit code 1
so the failure lands loudly in the journal. Promoted from the one-off
2026-07-07 stream audit — the archives are now big enough that silent
rot (a wedged daemon, a schema drift, a purge racing ahead of the tape)
is the main operational threat, and each of these checks watches a
failure mode that has either happened or provably can.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from collector.backup import DBS
from collector.venues import alfred
from hyxlab.reportdir import shared_reports
from hyxlab.shadowruns import latest_complete_run, run_completed_at
from hyxlab.store import SCHEMA_VERSION, duck_connect, lock_holder

ARCHIVE = "data/hyxlab.duckdb"
STREAM = "data/hyxstream.duckdb"
SHADOW = "data/hyxshadow.duckdb"
# Shared with `hyxlab-divergence`, which WRITES what this check READS from
# the other worktree -- hence rooted, not cwd-relative (hyxlab.reportdir).
DIVERGENCE_REPORTS = shared_reports("shadow_divergence")

# Per-section completion record, so a skip can be BOUNDED. Without it a
# locked archive skips silently forever and the journal still reads green.
# Deliberately cwd-RELATIVE, unlike DIVERGENCE_REPORTS above: this is a log
# of what THIS checkout did, not an artifact derived from the shared
# archive, and the dev tree must not inherit the stable tree's record (or
# vice versa). See CLAUDE.md and tests/test_health_digest.py.
STATE = Path("reports/qa/sections.json")
SKIP_MAX_AGE_H = 36.0  # matches the "sweep ran in last 36h" tolerance

# Lock-wait budget. Measured 2026-08-02: `hyxlab-collect` is OnCalendar
# `*:0/5` and `hyxlab-qa` is `07:00:00 UTC` — a 5-minute boundary — so the
# two start in the SAME SECOND every day, by construction. The collector
# holds the archive write lock for ~11s per cycle (02:00:00 → 02:00:11 on
# 08-02). The old budget was 5 attempts x 2s ≈ 10s, so QA gave up ~1s
# before the release and skipped the whole archive half on 10 of the 14
# runs Jul 20 – Aug 02. 60s clears the collector cycle ~5x over. It does
# NOT clear the poly sweep (13.7–15.8h wall clock, measured over 8 runs) —
# no budget could, which is why the skip must also be bounded by STATE.
LOCK_WAIT_S = 60.0
RETRY_SLEEP_S = 2.0

# Skipped-collection journal (written by collector.collect when it cannot
# get the writer lock within its budget). 288 cycles/day, so 3 tolerates
# occasional contention and trips on a pattern — the measured starvation
# was 12-38 skips/day. Deliberately NOT read from the archive: a cycle
# skips precisely because the archive was unopenable, so this is the one
# check that cannot be silenced by the failure it watches.
COLLECT_SKIP_LOG = "data/collect_skips.jsonl"
COLLECT_SKIP_MAX_24H = 3

# EXP-943 — the INDEPENDENT witness that makes an absent sidecar decidable.
# `collector.collect.main()` exits 75/EX_TEMPFAIL on exactly the path that
# calls `record_skip()`, and systemd journals that exit whether or not python
# ever got far enough to write the file. So the two disagree in precisely one
# case: the producer is not running. Verified live 2026-08-03 — three exit-75
# events at 12:54/12:59/13:04Z and three matching sidecar rows.
COLLECT_UNIT = "hyxlab-collect.service"
COLLECT_SKIP_EXIT = 75

# EXP-1360 — econ-vintage ingest, split into the two questions the old
# `econ vintages fresh (< 8 days)` was adding together and losing both.
#
# (a) THE NUISANCE TERM. `knowable_at` is not an ingest time: ALFRED
# vintages are date-granular, so `alfred.pessimistic_knowable_at` stamps
# the vintage date's 23:59 US/Eastern (= vintage_date+1 03:59 UTC), a
# deliberately LATE stamp so no backtest can see a print before a live
# trader could. It therefore leads the fetch by up to ~28h, and
# `now - max(knowable_at)` is (ingest staleness − pessimism margin). On
# 2026-08-24 that check printed "age -0.6d" and PASSED. A freshness
# measure that can go negative is not measuring freshness. Recovering the
# vintage DATE from the stamp cancels the nuisance exactly, because the
# stamp is a deterministic function of it.
KNOWABLE_AT_STAMP_OFFSET_H = 4  # 23:59 ET -> next-day 03:59 UTC; see alfred.py
# (b) POOLING. A max over seven series whose print cadences run from daily
# to monthly is set by the fastest one, always. The daily Fed-target pair
# refreshes it every day, so CPIAUCSL/CPILFESL/PAYEMS/UNRATE could stop
# arriving forever and this check would stay green — and on 2026-08-24 it
# WAS green while four of the seven series sat 10.2d, 10.2d, 15.2d and
# 15.2d stale, i.e. past the check's own 8-day budget. Pooling is right for
# exactly one question ("did the pull bring anything home"), so that is the
# only question this check now asks; per-series coverage moves to
# `qa_signals_fetch`, which reads a witness that can tell "not published
# yet" apart from "not fetched".
#
# Budget measured over the 44 distinct vintage dates ingested 2026-07-11 ..
# 2026-08-24 (45 calendar days): the pull lands daily and the ONLY gap
# above one day in that history is a single 2-day gap at 07-13. 4 days =
# 2x the worst observed gap.
ECON_PULL_GAP_BUDGET_D = 4

# EXP-1360 — per-series ALFRED fetch outcomes (`collector.signals.record_fetch`).
# The pull is daily, so a series not SUCCESSFULLY fetched within the same
# budget as the pull itself has been dropped, whatever its print cadence.
SIGNALS_FETCH_LOG = "data/signals_fetch.jsonl"
_SIGNALS_FETCH_SECTION = "signals-fetch"

# The one section that is SKIPPED on a HEALTHY box, and the only one whose skip
# is deliberately not aged by `_skip_age_h` (see `qa_collect_skips`): it reports
# UNVERIFIED whenever no collector cycle happened to wait out the archive lock
# in the window, and weeks without lock contention is a healthy system, not a
# defect. Every OTHER skip name means something could not be MEASURED --
# `fade-window` and `batch-run-budget` need a readable journal, `poly-universe`
# and `signals-fetch` escalate to FAIL after SKIP_MAX_AGE_H -- so their presence
# is news and this one's is not.
#
# Named as a set, and exported, because a READER of qa's record cannot make that
# distinction from the record alone: `collector.health` printed "NOT a full pass"
# every night forever on a green box, and a genuinely new skip changed only the
# contents of a list nobody could see change. One definition, so a rename cannot
# desync the producer from its reader.
COLLECT_SKIP_SECTION = "collect-skips"
STANDING_SKIPS: frozenset[str] = frozenset({COLLECT_SKIP_SECTION})

# The RECOVERY half of the same hole. A skipped cycle stopped being an
# unrecoverable one on 2026-09-10: the rows are fetched before the lock is
# reached for, so `collector.spool` writes them beside the archive and the
# next successful cycle drains them. `qa_collect_skips` above still counts
# the contention (that is a real signal about the box); this check asks the
# different question of whether the recovery actually happened.
COLLECT_SPOOL_LOG = "data/collect_spool.jsonl"
COLLECT_SPOOL_DIR = "data/collect_spool"
# A spooled cycle drains on the NEXT successful cycle, i.e. within ~5 min.
# 2h is 24 consecutive failures to drain -- by then the spool is at its cap
# and dropping data, so a budget larger than the cap's own horizon would
# announce the loss after it happened.
COLLECT_SPOOL_STALE_H = 2.0
# THE INERT-PRODUCER WITNESS, and why it is a date. Every skip row and every
# `spooled` event are written by the same branch of `collect.main`, one line
# apart, so a skip in the window with no spool event beside it means the
# spool is not running (or its write failed, which prints "cycle LOST" and
# is the same finding). The 17 skip rows already on disk predate the spool,
# so the comparison starts here. The constant is load-bearing for exactly
# one window's width and then INERT BY CONSTRUCTION -- once it falls out of
# the `hours` window it can no longer exclude anything -- which is the only
# form of hardcoded date this file will take.
COLLECT_SPOOL_ARMED_AT = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)

# EXP-1381 — the poly enumeration tripwire's own liveness. Its guard reads a
# WINDOWED slice of poly_market_stats, so it stops being emitted at all on
# exactly the input a dead stats writer produces (see qa_archive). The floor
# below is what makes a shrink RATIO meaningful, and it is the sharper half of
# the same defect: a universe that collapsed under it would silence the check
# written to catch a collapse.
_POLY_UNIVERSE_SECTION = "poly-universe"
POLY_UNIVERSE_MIN_PRIOR = 500

# EXP-960 — the fade window, in UTC hours. Live trading reads the snapshot
# tape 23:00-04:00Z (research/lowt-window-structure: KXLOWT candidates appear
# almost exclusively then), so a capture hole inside it is worth strictly more
# than the same hole at noon. `qa_collect_skips` above is DAY-WIDE and blind to
# that: its budget of 3 skips/24h passes cleanly on a night that loses three
# consecutive fade-window cycles.
FADE_WINDOW_START_H = 23
FADE_WINDOW_END_H = 4
# One lost cycle in a 60-cycle window is a transient; the measured harm event
# (2026-07-29) was 4. Every other measured night 07-27..08-02 lost ZERO, so the
# budget sits one above the observed-clean floor rather than being fitted to
# the breach.
FADE_WINDOW_MAX_HOLES = 1
FADE_WINDOW_NIGHTS = 7
SWEEP_UNIT = "hyxlab-poly-sweep.service"

#: Worst COMPLETED wall clock measured from the systemd journal, per Kalshi-
#: facing batch unit. Keyed by TIMER because its first consumer is
#: `tests/test_systemd_units.py`, which reads the timer's OnCalendar and
#: asserts start + budget lands clear of FADE_WINDOW_START_H (EXP-959); the
#: second consumer is `qa_batch_run_budget` below, which measures the same
#: units against the same numbers in production. The test alone was not
#: enough — see EXP-961 in that function's docstring.
#:
#: These are measured intervals (Starting -> `Consumed ... over N`), never a
#: duration inferred from a running process's age.
#:
#:   hyxlab-sweep      41m51s / 1h12m / 1h10m / 57m48s / 1h26m / 54m55s /
#:                     25m15s over 2026-07-27..08-02, then 2026-08-03:
#:                     11:10:00Z -> 21:16:38Z, **10h06m38s** — the first full
#:                     pass after Crypto entered sweep.DEFAULT_CATEGORIES on
#:                     08-02, a 24x step over the day before. 10.5 was that
#:                     measurement plus ~0.4h; the open question then (one-time
#:                     backlog or steady state?) was ANSWERED by 08-06..08-12:
#:                     steady state settled at 7h52m-9h26m (one 11h29m outlier
#:                     on 08-07), comfortably inside 10.5. Then EXP-1275
#:                     (2026-08-12) deliberately raised the crypto per-series
#:                     candle budget to 8k, and the first pass under it
#:                     (2026-08-13, 06:10Z -> 18:18:51Z) measured **12h08m51s**
#:                     — the same shape as the 08-03 step: a capacity change
#:                     moved the worst case, not drift. 12.5 is that
#:                     measurement plus ~0.35h. Whether 12h09m is the 8k-budget
#:                     backlog or its steady state is UNKNOWN until subsequent
#:                     passes complete; `qa_batch_run_budget` is what will say
#:                     so out loud, exactly as it did last time.
#:   hyxlab-tradepass  up to 2h51m (see QA_CLEARANCE_H in the unit tests);
#:                     4.0h allowed.
#:
#: DELIBERATELY ABSENT: hyxlab-poly-sweep. It is measured at 13h41m-17h11m
#: wall clock, and 2026-07-29 ran 1d 0h21m -- fully spanning that night's
#: fade window. It is exempt because it talks to POLYMARKET, so it spends no
#: Kalshi quota; its rivalry with the collector is `data/writer.lock` only,
#: and collector/poly_sweep.py already touches the DB in short bursts. It is
#: also unfixable by scheduling: a 14-24h job on a 24h cadence has a 57-100%
#: duty cycle, so no start time clears a 5h window. Its lever is wall clock,
#: not OnCalendar. Do NOT "fix" this by adding it here and moving its timer.
BATCH_RUN_BUDGET_H = {
    "hyxlab-sweep.timer": 12.5,
    "hyxlab-tradepass.timer": 4.0,
}
#: Journald on this box holds ~16M / ~2 days, so this is a ceiling on the
#: lookback, not a promise of one. Unreachable days read UNMEASURED.
BATCH_RUN_LOOKBACK_DAYS = 7

# EXP-1359 — the freshness checks are INSTANTANEOUS, so an outage that heals
# before the next daily QA run is structurally invisible to them. The
# 2026-08-20 box outage (21:33:37Z -> 2026-08-21 01:52:56Z, 4h19m, all three
# writers down together) fell entirely between two 10:00Z runs and entered the
# wiki as an "unexplained shadow silence" rather than an alarm. This bounds the
# collector's cadence RETROSPECTIVELY over the window instead.
#
# Measured over 21 days / 6,040 collector cycles on the live archive:
# p50 300.0s (the 5-min timer, exactly), p99 314.0s, p99.9 600.0s (one skipped
# cycle). The largest gap in those 21 days that was NOT the 08-20 outage is
# 25.0 min; the outage is 264.8 min, i.e. 10.6x the worst benign gap. 60 min is
# that benign worst case plus 2.4x headroom, and still 4.4x under the event it
# exists to catch. A gap here is (real downtime + writer-lock skips) — the skips
# are NOT subtracted, because subtracting them is how a lock-starved collector
# would go quiet forever and still read green (mistakes #25-27).
COLLECTION_GAP_BUDGET_S = 3600.0

# The instantaneous half of the pair: "is this writer running NOW". Shared by
# every 5-min writer of the archive for the same reason _check_continuity is
# (2026-09-04) — three writers now cycle on `*:0/5` (collect -> snapshots,
# breadth -> breadth_snapshots, and the NWS pull inside the collect cycle ->
# nws_forecasts), and a per-writer literal is how the fourth ships with a
# quietly different tolerance than the first three.
CYCLE_FRESH_S = 1200.0

# EXP-962 — a draining backfill is not rot. 2026-08-04: the tape-coverage
# check reported "3 traded markets unswept" while collector.trades_backfill
# was live and landing 1.4k-9.3k markets/hour; the count fell 3 -> 2 during
# the very QA pass that read it. The check had no notion of "a sweeper is
# currently draining this", so it rendered a healthy tail exactly as it
# renders rot. The discriminator is per-market PERSISTENCE, judged from the
# archive (what LANDED, never what is presumed to be running): tradepass is
# daily, so a genuinely queued market clears within one cycle. Grace is that
# cycle plus slack, measured from when QA first OBSERVED the market unswept.
TAPE_DRAIN_GRACE_H = 30.0
# If NOTHING has landed in trades_swept for this long, the draining story has
# no evidence at all — the sweeper itself is dead, and that is rot regardless
# of how young the uncovered set is. 26h clears the daily tradepass with the
# same tolerance the freshness checks use.
TAPE_SWEEP_STALL_H = 26.0

_failures: list[str] = []
_skipped: list[str] = []
#: Every check name this run actually EXECUTED, pass or fail. A SKIPPED section
#: never calls `check()` at all, so its names appear in neither `_failures` nor
#: here -- and that absence is the only evidence in the process that separates
#: "went green" from "was not looked at". `qa_prior_run` is the reader; see the
#: healed-set derivation there for what went wrong without it.
_ran: list[str] = []
_passes = 0
_lock_holder: str | None = None  # set by _connect_ro when a live writer holds the file


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    line = f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else "")
    print(line, flush=True)
    _ran.append(name)  # executed, whatever the verdict — see `_ran`
    if ok:
        _passes += 1
    else:
        _failures.append(name)


def _load_state(path: Path | None = None) -> dict:
    """The record, or {} if it is absent or unreadable. `path` is a parameter
    because STATE is RELATIVE to the working directory and the dev tree and the
    stable worktree therefore keep SEPARATE records (measured 2026-09-06: the
    dev copy read 09-05T20:25Z from a hand run while production's read
    09-06T10:00Z). A reader outside this process must be able to name the one
    the timer actually writes; see `collector.health`."""
    # Resolved at CALL time, never as a default argument: STATE is monkeypatched
    # by the suite and a default would bind the module-load value forever.
    try:
        return json.loads((STATE if path is None else path).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1, sort_keys=True))
    except OSError:
        pass  # QA is read-only by contract; losing the record must not fail the run


def _note_seen(section: str, now: datetime) -> None:
    """Start a section's clock the first time QA ever observes it — on a
    SKIP as much as on a success. Otherwise a section locked from the very
    first run has no reference point and can never go stale."""
    state = _load_state()
    entry = state.setdefault(section, {})
    entry.setdefault("first_seen", now.isoformat())
    _save_state(state)


def _record_ok(section: str, now: datetime) -> None:
    state = _load_state()
    entry = state.setdefault(section, {})
    entry.setdefault("first_seen", now.isoformat())
    entry["last_ok"] = now.isoformat()
    _save_state(state)


def _last_ok(section: str) -> datetime | None:
    """When this section last COMPLETED (a real measurement), or None. Unlike
    `_skip_age_h` it does NOT fall back to `first_seen`: "first observed" is
    not "last measured", and conflating them is how an untested producer
    would borrow a date it never earned."""
    ref = (_load_state().get(section) or {}).get("last_ok")
    if not ref:
        return None
    try:
        return datetime.fromisoformat(ref)
    except ValueError:
        return None


def _skip_age_h(section: str, now: datetime) -> float | None:
    """Hours since this section last COMPLETED, falling back to when it was
    first observed if it never has."""
    entry = _load_state().get(section) or {}
    ref = entry.get("last_ok") or entry.get("first_seen")
    if not ref:
        return None
    try:
        return (now - datetime.fromisoformat(ref)).total_seconds() / 3600.0
    except ValueError:
        return None


def _connect_ro(path: str, wait_s: float = LOCK_WAIT_S) -> duckdb.DuckDBPyConnection | None:
    """read-only connect with retry. Distinguishes a live writer holding
    the lock (normal: poly sweep holds it for hours) from a genuinely
    unreachable file — alarm fatigue trains people to ignore QA."""
    global _lock_holder
    _lock_holder = None
    attempts = max(1, int(wait_s / RETRY_SLEEP_S))
    for attempt in range(attempts):
        try:
            return duck_connect(path, read_only=True)
        except duckdb.Error as exc:
            _lock_holder = lock_holder(exc) or _lock_holder
            if attempt == attempts - 1:
                return None
            time.sleep(RETRY_SLEEP_S)  # writer burst (collector/tradepass flush)
    return None


def _reachable(conn, name: str, section: str, now: datetime) -> bool:
    """Emit the reachability line. A lock held by a live writer SKIPS the
    section — it is not a data defect and must not alarm — but a skip is
    NOT a pass: it is reported as SKIP, tracked separately, and bounded by
    how long the section has gone without actually completing."""
    if conn is not None:
        return True
    if _lock_holder:
        print(f"SKIP  {name} — live writer holds lock ({_lock_holder})", flush=True)
        _skipped.append(section)
        _note_seen(section, now)
        age = _skip_age_h(section, now)
        check(
            f"{section} checks completed within {SKIP_MAX_AGE_H:.0f}h",
            age is not None and age <= SKIP_MAX_AGE_H,
            f"last completed {age:.1f}h ago" if age is not None else "no completion on record",
        )
    else:
        check(name, False, "unreachable and no live writer holds the lock")
    return False


def qa_stream(hours: float, path: str = STREAM) -> None:
    conn = _connect_ro(path)
    now = datetime.now(UTC).replace(tzinfo=None)
    if not _reachable(conn, "stream archive reachable", "stream", now):
        return

    age = conn.execute("SELECT epoch(? - max(recv_ts)) FROM stream_trades", [now]).fetchone()[0]
    check(
        "stream fresh (trades < 5 min old)",
        age is not None and age < 300,
        f"age {age:.0f}s" if age is not None else "no trades",
    )

    bad = conn.execute(
        "SELECT count(*) FROM stream_trades WHERE recv_ts > ? - INTERVAL 1 HOUR *"
        " CAST(? AS INTEGER) AND (price <= 0 OR price >= 1 OR qty <= 0)",
        [now, int(hours)],
    ).fetchone()[0]
    check("trade price/qty domains", bad == 0, f"{bad} bad rows in window")

    # Seq continuity. seq is CONNECTION-scoped and restarts at 1 on every
    # reconnect, while Kalshi hands out the same sid (1) to the first
    # subscription of each new connection — so grouping by sid alone welds
    # every connection in the window into one min..max range and reports the
    # interleaving as holes (measured 2026-07-29: 8 reconnects in a 26h
    # window, a garbage count that swung 0 <-> 1.4M on where the window cut).
    # Segment into connection runs first: ordered by time, a seq that goes
    # BACKWARDS starts a new run. Holes are then measured strictly inside a
    # run. A hole is then excused only by a gap row overlapping THE HOLE's own
    # interval — not the run's. Run-scoped excusal (2026-07-29 → 07-30) was
    # still vacuous: every completed run ends in a logged reconnect, whose gap
    # row touches the run's endpoint and so excused every hole in it however
    # far away (measured: 22 holes at 17:55 excused by a 21:29 reconnect). Only
    # the currently-OPEN run, which has no terminating gap yet, could ever
    # fail — which is exactly the false alarm the 07-30 07:00 run emitted and
    # that self-cleared an hour later. Gap rows are also scoped to the channel
    # that owns these runs: a polymarket or kalshi-trades reconnect says
    # nothing about the kalshi books connection.
    holes = conn.execute(
        """
        WITH ev AS (
          SELECT DISTINCT sid, seq, recv_ts FROM book_events
          WHERE recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)
            AND sid IS NOT NULL AND seq IS NOT NULL
        ), marked AS (
          SELECT sid, seq, recv_ts, CASE WHEN seq < lag(seq)
                   OVER (PARTITION BY sid ORDER BY recv_ts, seq)
                 THEN 1 ELSE 0 END AS reset
          FROM ev
        ), runs AS (
          SELECT sid, seq, recv_ts, sum(reset) OVER (
                   PARTITION BY sid ORDER BY recv_ts, seq
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run
          FROM marked
        ), uniq AS (
          SELECT sid, run, seq, min(recv_ts) AS recv_ts
          FROM runs GROUP BY sid, run, seq
        )
        SELECT seq - lag(seq) OVER w - 1 AS missing,
               lag(recv_ts) OVER w AS t0, recv_ts AS t1
        FROM uniq WINDOW w AS (PARTITION BY sid, run ORDER BY seq)
        QUALIFY missing > 0
        """,
        [now, int(hours)],
    ).fetchall()
    gap_spans = conn.execute(
        "SELECT started_at, ended_at FROM stream_gaps WHERE ended_at > ?"
        " - INTERVAL 1 HOUR * CAST(? AS INTEGER)"
        " AND ((venue = 'kalshi' AND channel = 'books') OR venue = '*')",
        [now, int(hours)],
    ).fetchall()
    unexcused = [
        (m, t0, t1) for m, t0, t1 in holes if not any(g0 <= t1 and g1 >= t0 for g0, g1 in gap_spans)
    ]
    check(
        "book seq contiguous or gap-marked",
        not unexcused,
        f"{sum(m for m, _, _ in holes)} missing seq in {len(holes)} hole events, "
        f"{len(gap_spans)} own-channel gap rows, "
        f"{len(unexcused)} unexcused ({sum(m for m, _, _ in unexcused)} seq)",
    )

    # Void-frame attribution. The seq check above is now BLIND to a Kalshi
    # schema change by construction: a frame type this parser does not
    # recognise archives no book level, and since 2026-07-30 it writes a
    # kind='void' row that CLOSES the seq hole it would otherwise leave. So
    # the very event the seq check used to catch (an unrecognised frame
    # thinning book capture) now reads green there. Nothing read the void
    # rows until this check, which is why that fix silently traded a
    # detectable failure for an invisible one. Alarm on any void frame type
    # outside the known-benign set: an empty-ladder orderbook_snapshot
    # (markets with no resting book — measured 2026-07-30, the only mid-run
    # producer) plus the sequenced control acks. Legacy void rows carry ''
    # in `side` (written before the frame type was recorded) and are
    # unattributable, so they are counted but cannot trip the check; they
    # roll out of the window on their own.
    voids = conn.execute(
        "SELECT side, count(*) FROM book_events"
        " WHERE venue = 'kalshi' AND kind = 'void'"
        " AND recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)"
        " GROUP BY side ORDER BY 2 DESC",
        [now, int(hours)],
    ).fetchall()
    benign = {"orderbook_snapshot", "subscribed", "ok", "error", "heartbeat", "pong"}
    unknown = [(t, n) for t, n in voids if t and t not in benign]
    legacy = sum(n for t, n in voids if not t)
    check(
        "void frames are known types",
        not unknown,
        f"{sum(n for _, n in voids)} void frames"
        + (f" ({legacy} legacy unattributed)" if legacy else "")
        + ("; UNKNOWN " + ", ".join(f"{t}x{n}" for t, n in unknown) if unknown else "; all known"),
    )

    # Reconstruct each Kalshi book from its time-latest snapshot image
    # (an image is one WS frame, so its rows share recv_ts) plus the
    # signed deltas received after it. seq is NOT usable as an ordering
    # key here: it is subscription-scoped and resets on every reconnect.
    # Pairs whose delta window a coverage gap intersects are skipped —
    # the book is legitimately unknown until the next snapshot re-seeds.
    # (Polymarket deltas carry absolute sizes, never negative; excluded.)
    neg = conn.execute(
        """
        WITH pair AS (
          -- One snapshot frame carries BOTH sides, so the baseline is
          -- keyed per market: a side legitimately empty in the newest
          -- image must not stay anchored to an older image's rows.
          SELECT market_id,
                 max(recv_ts) FILTER (kind='snap') AS snap_ts,
                 max(recv_ts) AS last_ts
          FROM book_events WHERE venue='kalshi' GROUP BY market_id
        ), eligible AS (
          SELECT market_id, snap_ts FROM pair
          WHERE snap_ts IS NOT NULL
            AND NOT EXISTS (
              SELECT 1 FROM stream_gaps g
              WHERE g.venue IN ('kalshi', '*') AND g.channel IN ('books', '*')
                AND g.started_at > pair.snap_ts AND g.started_at <= pair.last_ts)
        ), levels AS (
          SELECT e.market_id, e.side, e.price,
                 sum(CASE WHEN e.kind='snap' AND e.recv_ts = el.snap_ts THEN e.qty
                          WHEN e.kind='delta' AND e.recv_ts > el.snap_ts THEN e.qty
                          ELSE 0 END) AS qty
          FROM book_events e
          JOIN eligible el ON el.market_id = e.market_id
          GROUP BY e.market_id, e.side, e.price
        )
        SELECT count(*) FROM levels WHERE qty < -1e-9
        """
    ).fetchone()[0]
    check("reconstructed book levels non-negative", neg == 0, f"{neg} negative levels")

    # `recv_ts - src_ts` is NOT latency: it is (box clock offset + transport
    # latency), and the offset dominates by two orders of magnitude. Measured
    # 2026-08-23 over 12.6M kalshi trades in 24h, p01 25.55s / p50 25.71s /
    # p99 25.89s — the WHOLE distribution is a 0.34s band sitting at +25.7s.
    # The old single check `-2 < p99 < 25` therefore watched the clock, not
    # the stream, and went permanently red when the offset drifted past its
    # own ceiling. A threshold cannot absorb an unbounded drift (mistakes
    # #25-27), so the two quantities are separated and each gets the bound
    # its own failure mode earns.
    lat = conn.execute(
        "SELECT quantile_cont(epoch(recv_ts - src_ts), 0.50),"
        "       quantile_cont(epoch(recv_ts - src_ts), 0.99)"
        " FROM stream_trades"
        " WHERE src_ts IS NOT NULL AND recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)",
        [now, int(hours)],
    ).fetchone()
    p50, p99 = lat if lat else (None, None)

    # (a) Transport latency, offset-invariant: a constant clock error cancels
    # in a difference of two quantiles of the same window, so this is the only
    # part that actually watches the stream (backpressure, reconnect storms).
    # Measured 0.03-0.18s; 5s is ~30x the observed spread.
    disp = None if p50 is None or p99 is None else p99 - p50
    check(
        "trade latency dispersion sane",
        disp is not None and disp < 5.0,
        f"p99-p50 {disp if disp is None else round(disp, 3)}s"
        + ("" if p99 is None else f" (p99 {round(p99, 2)}s raw)"),
    )

    # (b) The offset itself, named for what it is. The bound is ASYMMETRIC
    # because the two directions cost different things. A FAST box clock is
    # conservative: `sim._maker_check_and_expire` drops any snapshot with
    # `snap.ts >= close_time`, so a fast clock only discards data near the
    # close — and it currently discards none, measured 2026-08-23 over 7 days
    # of kalshi snapshots: ZERO of 1,141,594 pre-close snapshots land in the
    # final 26s, and only 1,061 in the final 5 min. That is the cost of the
    # present +25.7s offset to the sim: zero. A SLOW clock is the dangerous
    # side — it stamps post-close snapshots as pre-close and feeds the sim
    # genuine lookahead — hence the tight floor. The 60s ceiling is a drift
    # alarm well above today's offset and far below the ~300s where the
    # discard cost first becomes measurable. NTP is the real fix (user-gated).
    check(
        "box clock offset within tolerance",
        p50 is not None and -2.0 < p50 < 60.0,
        f"median recv-src {p50 if p50 is None else round(p50, 2)}s"
        " (box clock vs venue; NTP pending)",
    )

    conn.close()
    _record_ok("stream", now)


# ---------------------------------------------------------------------------
# The shadow-vs-replay calibration haircut, given a consumer.
#
# `simulator.divergence` is the report that decides whether Tier-3 shadow and
# Tier-2 replay are still ONE semantics -- the number every backtest verdict
# rests on. Until 2026-09-10 it was written to a JSON file that NOTHING read.
# Mistake #46 is what that costs: the report defaulted to a run that had ended
# three weeks earlier, was launched, printed its subject on line one, and the
# line scrolled past unread. Fixing the default fixed the SELECT; it did not
# give the measurement a reader, and an unread measurement is not one.
#
# This is the reader. It cannot re-run the report (30 min, 4G peak on the
# 08-29 run -- `hyxlab-divergence.timer` runs it daily), so it asserts the
# only two things a reader can: that the newest measurable run HAS been
# measured, and that what the measurement says is still inside the bounds the
# equivalence claim needs.
#
# It cannot pin itself to an unrepairable past, which is the property that
# distinguishes it from the checks retired as mistakes #29 and #45. Its
# subject is `latest_complete_run`, which always advances to the NEWEST
# finished run: a run that becomes permanently unmeasurable (its stream window
# aged out of the archive) stops being the subject the moment the shadow
# daemon restarts, so the check goes green on the next run rather than staying
# red on the lost one.

#: Hours a finished shadow run may go unmeasured. `hyxlab-divergence.timer` is
#: daily, so consecutive attempts sit 24h apart and one missed slot is 48h; 36h
#: trips on a single miss with 12h of slack, the same arithmetic (and the same
#: number) as QA_RUN_GAP_BUDGET_H and the sweep's 36h tolerance. The clock
#: starts when the run became MEASURABLE -- the successor's start -- not when
#: it last wrote, so a daemon that stays down owes nothing.
DIVERGENCE_GAP_BUDGET_H = 36.0

#: Floor on the exact-tier fill match rate, both directions. MEASURED against
#: the whole post-matcher-v2 record (07-13 onward): 0.9982, 0.9984, 0.9927,
#: 0.9992 and eight runs at 1.0 exactly. The worst benign shortfall is 0.0073,
#: so a 0.01 budget carries ~1.4x headroom over the worst window the machinery
#: has actually produced -- and the failure it exists to catch is not a nudge
#: past that, it is the pre-fix 2026-07-09 regime at 0.6943/0.9337, which this
#: clears by 45x. A rate in [0, 1] is not a monotone series, so a constant is
#: the bound this failure mode earns (the #29 test).
DIVERGENCE_MATCH_FLOOR = 0.99

#: Ceiling on mean |price delta| over MATCHED fills, in dollars. Kalshi prices
#: in whole cents, so this is 0.1% of fills disagreeing by a full tick -- a
#: systematic fill-model mispricing, not a stray. Observed maximum over the
#: entire record, broken era included, is 7e-6: 140x under. Deliberately the
#: coarse arm; the match rate above carries the sensitivity, because a fill the
#: replay never produced has no price delta to contribute at all.
DIVERGENCE_PRICE_DELTA_MAX = 1e-3

DIVERGENCE_FRESH_CHECK = "shadow-vs-replay divergence measured on the newest complete run"


def _divergence_report(run_id: str, reports: Path) -> dict | None:
    """`run_id`'s report, or None. Unparseable reads as absent, so the day a
    truncated write lands is a "not measured" (repairable by re-running) and
    not a crash inside QA."""
    path = reports / f"{run_id}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def qa_divergence(
    now: datetime | None = None, path: str = SHADOW, reports: Path = DIVERGENCE_REPORTS
) -> None:
    conn = _connect_ro(path)
    now = now or datetime.now(UTC).replace(tzinfo=None)
    if not _reachable(conn, "shadow ledger reachable", "divergence", now):
        return

    run_id = latest_complete_run(conn)
    if run_id is None:
        # One live run, or none. Nothing is measurable yet, and a free pass
        # here is how the section would read green on an empty ledger
        # forever (mistakes #28) -- say so instead of banking it.
        print(
            "WATCH " + DIVERGENCE_FRESH_CHECK + " — no finished shadow run with fills to measure",
            flush=True,
        )
        conn.close()
        return
    completed = run_completed_at(conn, run_id)
    conn.close()

    report = _divergence_report(run_id, reports)
    if report is None:
        have = sorted(p.stem for p in reports.glob("*.json")) if reports.is_dir() else []
        # run_ids are UTC stamps, so lexical order IS chronological.
        newest = have[-1] if have else "none"
        age_h = (now - completed).total_seconds() / 3600.0 if completed else 0.0
        check(
            DIVERGENCE_FRESH_CHECK,
            age_h <= DIVERGENCE_GAP_BUDGET_H,
            f"run {run_id} finished {age_h:.1f}h ago, unmeasured (budget"
            f" {DIVERGENCE_GAP_BUDGET_H:.0f}h); newest report on file is {newest}."
            " Repair: `python -m simulator.divergence` (~30 min), or read why"
            " hyxlab-divergence.service last failed",
        )
        _record_ok("divergence", now)
        return

    check(
        DIVERGENCE_FRESH_CHECK,
        True,
        f"run {run_id} measured {report.get('generated_at')}",
    )

    rates = [report.get("match_rate_vs_shadow"), report.get("match_rate_vs_replay")]
    check(
        "shadow-vs-replay fill match rate",
        all(r is not None and r >= DIVERGENCE_MATCH_FLOOR for r in rates),
        f"{rates[0]} vs shadow, {rates[1]} vs replay (floor {DIVERGENCE_MATCH_FLOOR}),"
        f" unexplained {report.get('unmatched_shadow_by_cause', {}).get('unexplained')}"
        f"/{report.get('unmatched_replay_by_cause', {}).get('unexplained')}"
        f" of {report.get('shadow_fills')}/{report.get('replay_fills')} fills",
    )
    delta = report.get("price_delta_abs_mean")
    check(
        "shadow-vs-replay fill price agreement",
        delta is not None and abs(delta) <= DIVERGENCE_PRICE_DELTA_MAX,
        f"mean |price delta| {delta} (max {DIVERGENCE_PRICE_DELTA_MAX:g})",
    )
    _record_ok("divergence", now)


# ---------------------------------------------------------------------------
# Disk headroom. The predecessor was `stream disk under 20 GB` — a CONSTANT
# ceiling on a monotonically increasing quantity, which is mistake #29's exact
# defect sitting three lines below the comment that named it. It crossed on
# 2026-09-09 at 20.31 GB and could never go green again, and a check that can
# only be red is not a signal: it masked the breadth failure that cleared in
# the same run, because the digest says `hyxlab-qa FAILED` either way.
#
# It was also watching the wrong 8% of the thing it was named for. The stream
# file is 20.3 GB of a 262 GB `data/`, because `collector.backup` keeps a
# SEVEN-slot rotation of every archive on the same filesystem: each byte the
# tape gains is eventually paid for eight times. Measured from the rotation's
# own slots, 2026-09-02 → 09-08: stream +353 MB/day, hyxlab +249 MB/day,
# shadow +0.2 MB/day — ~0.6 GB/day live, ~4.8 GB/day of disk. Against 1.216 TB
# free that is ~250 days, so 20 GB was never the number and free space was
# never in question.
#
# And it was INSIDE `qa_stream`, which returns early when the stream archive
# is unreachable — so the disk signal vanished exactly when the box was in
# trouble. This section touches no database on purpose.
BACKUP_DIR = "data/backups"
# `collector.backup` names each slot `<stem>.<%a>.duckdb`, so the rotation is
# exactly one week deep. Read off that format, not chosen here.
ROTATION_SLOTS = 7
HEADROOM_MIN_DAYS = 90.0


def _rotation_growth(live: list[Path], backup_dir: Path) -> tuple[float | None, float]:
    """Bytes/day the LIVE archives are gaining, measured from the backup
    rotation's own slot series, with the span it was measured over.

    The rotation is the only growth history that already exists on disk, so
    this costs no new state. Returns `(None, span)` when ANY live archive
    lacks a two-slot series spanning a day: a partial sum would UNDERSTATE
    the burn rate, and understating it is the optimistic direction — the one
    that turns a headroom check into a reassurance.
    """
    total = 0.0
    span = 0.0
    for db in live:
        slots = []
        for p in backup_dir.glob(f"{db.stem}.*.duckdb"):
            try:
                st = p.stat()
            except OSError:
                continue
            slots.append((st.st_mtime, st.st_size))
        slots.sort()
        days = 0.0 if len(slots) < 2 else (slots[-1][0] - slots[0][0]) / 86400.0
        if days < 1.0:
            return None, span
        total += (slots[-1][1] - slots[0][1]) / days
        span = max(span, days)
    return total, span


def qa_disk_headroom(
    dbs: list[str] | None = None,
    backup_dir: str | Path | None = None,
    min_days: float = HEADROOM_MIN_DAYS,
) -> None:
    """Will the filesystem holding the archives still be there in `min_days`?

    Bounded as a HORIZON rather than a level, so it tracks the failure it is
    named for and can go green again. A rate also catches the shape a constant
    cannot: the tape's growth follows market activity, so a quiet 250-day
    horizon can become a 30-day one without any file size looking unusual.

    Every branch below assigns `(ok, detail)` and the check is emitted ONCE, at
    the end. A verdict computed in four places and printed in four places is
    four chances for one of them to stop printing on the input it exists to
    notice (`test_qa_silent_guards`); printed in one place it cannot.
    """
    live = [Path(p) for p in (DBS if dbs is None else dbs)]
    live = [p for p in live if p.exists()]
    if backup_dir is None:
        backup_dir = os.environ.get("HYXLAB_BACKUP_DIR", BACKUP_DIR)
    backup_dir = Path(backup_dir)

    if not live:
        ok, detail = False, "no archive present to measure"
    else:
        root = live[0].parent
        free = shutil.disk_usage(root).free
        footprint = sum(p.stat().st_size for p in live)

        # The rotation multiplies the burn ONLY while it shares the filesystem
        # it would exhaust. Off-box -- the standing user item -- it costs this
        # disk nothing, so the same archive has two different honest horizons
        # and the check must not assume the pessimistic one is always right.
        try:
            same_fs = backup_dir.stat().st_dev == root.stat().st_dev
        except OSError:
            same_fs = False
        mult = 1 + ROTATION_SLOTS if same_fs else 1
        where = "backups share this filesystem" if same_fs else "backups off-box"

        rate, span = _rotation_growth(live, backup_dir)
        burn = None if rate is None else rate * mult
        if burn is None:
            # No history, so no projection. Fall back to the one property that
            # needs none and is still not a constant: can this disk hold one
            # more full copy of what it is already carrying? Labelled
            # UNPROJECTED so it is never read as the horizon it is not.
            ok = free > footprint
            detail = (
                f"UNPROJECTED (no 2-slot rotation series in {backup_dir}) —"
                f" free {free / 1e9:.0f} GB vs {footprint / 1e9:.0f} GB live"
            )
        elif burn <= 0:
            ok = True
            detail = f"free {free / 1e9:.0f} GB; archives not growing over {span:.1f}d"
        else:
            days = free / burn
            ok = days > min_days
            detail = (
                f"{days:.0f} d to full (floor {min_days:.0f}) —"
                f" free {free / 1e9:.0f} GB, burn {burn / 1e9:.2f} GB/day ="
                f" {rate / 1e9:.2f} live x{mult} ({where}), measured over {span:.1f}d"
            )
    check("archive disk headroom", ok, detail)


def _largest_gap(conn, table: str, col: str, lo: datetime) -> tuple | None:
    """Widest interval between consecutive cycles of `table`.`col` since `lo`, as
    (resumed_at, seconds), or None when the window holds fewer than two
    cycles.

    Anchors on the newest cycle at or BEFORE the window, so an outage
    straddling the left edge is measured rather than lost with its
    predecessor. Without it the first in-window cycle has no lag at all.
    """
    return conn.execute(
        f"""
        WITH cyc AS (
            SELECT DISTINCT {col} AS ts FROM {table} WHERE {col} >= ?
            UNION ALL
            SELECT max({col}) FROM {table} WHERE {col} < ? AND {col} IS NOT NULL
        ),
        lagged AS (SELECT ts, ts - lag(ts) OVER (ORDER BY ts) AS d FROM cyc)
        SELECT ts, epoch(d) FROM lagged WHERE d IS NOT NULL ORDER BY d DESC LIMIT 1
        """,
        [lo, lo],
    ).fetchone()


def _check_freshness(conn, name: str, table: str, col: str, now: datetime, noun: str) -> None:
    """ "Is this writer running NOW", for one 5-min writer of the archive.

    The instantaneous half of the pair `_check_continuity` completes: this
    one cannot see an outage that HEALED before QA looked, and that one
    cannot see a writer that stopped inside the last 24h window and stayed
    stopped for less than the gap budget. Neither is sufficient alone.
    """
    age = conn.execute(f"SELECT epoch(? - max({col})) FROM {table}", [now]).fetchone()[0]
    check(
        name,
        age is not None and age < CYCLE_FRESH_S,
        f"age {age:.0f}s" if age is not None else f"no {noun}",
    )


def _check_continuity(conn, name: str, table: str, col: str, now: datetime, noun: str) -> None:
    """Retrospective cadence check over the last 24h. Shared by every 5-min
    writer of the archive, so a second one cannot ship with a subtly
    different window, anchor or budget than the first."""
    gap = _largest_gap(conn, table, col, now - timedelta(hours=24))
    if gap is None:
        # One cycle cannot exhibit a gap. That is UNMEASURED, not healthy —
        # say so out loud rather than banking a free pass (mistakes #28).
        print(f"WATCH {name} — fewer than 2 {noun} cycles in window", flush=True)
        return
    resumed, gap_s = gap
    check(
        name,
        gap_s <= COLLECTION_GAP_BUDGET_S,
        f"largest gap {gap_s / 60.0:.1f} min (resumed {resumed:%Y-%m-%d %H:%M}Z),"
        f" budget {COLLECTION_GAP_BUDGET_S / 60.0:.0f} min",
    )


BREADTH_TRUNCATION_CHECK = "breadth universe enumerated exhaustively over last 24h"


def _check_breadth_truncation(conn, now: datetime) -> None:
    """Did the breadth walk actually SEE the universe it claims to rank?

    THE CHECK THE 2026-09-06 EVENT NEEDED AND NOBODY HAD. Between 23:32Z
    on 09-06 and 14:00Z on 09-07 `breadth_snapshots` fell from ~990 to as
    low as 3 rows per cycle — a ~97% loss of the archive's only
    exchange-wide quote history — and BOTH existing breadth checks stayed
    green the whole time, necessarily: cycles kept landing every 5 minutes
    exactly on schedule, so freshness passed and 24h-continuity passed.
    They measure CADENCE. Nothing measured CONTENT. The one failure that
    did fire was an accident of arithmetic — a 65-minute stretch where the
    count was exactly zero tripped the gap budget — and had the flood been
    5% smaller it would never have fired at all.

    Row COUNT is deliberately not the signal here. A genuinely quiet hour
    has few volume-bearing markets, so a count threshold is a guess that
    either misses the event or cries wolf on a Sunday — the alarm fatigue
    `STANDING_SKIPS` exists to prevent. Truncation is not a guess: the
    cursor was live when the page budget ran out, which is a fact about the
    walk, and it makes every ranking claim in the window unsound whatever
    the counts look like.

    Reported per cycle rather than as a rate, because ONE truncated cycle
    is already a ranking that cannot be trusted.
    """
    # QA connects READ-ONLY, so it cannot create the table it reads: the
    # schema lands only when a WRITER next opens the archive. Between
    # promoting this code and breadth's next 5-min cycle the table does not
    # exist, and an unguarded query raises CatalogException and takes the
    # whole QA run down with it — a new check that can abort every OTHER
    # check is a worse fault than the one it was added to catch.
    exists = conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'breadth_cycles'"
    ).fetchone()[0]
    if not exists:
        print(
            f"WATCH {BREADTH_TRUNCATION_CHECK} — no breadth_cycles table in this archive",
            flush=True,
        )
        return

    row = conn.execute(
        "SELECT count(*), sum(CASE WHEN truncated THEN 1 ELSE 0 END), max(universe)"
        " FROM breadth_cycles WHERE ts >= ?",
        [now - timedelta(hours=24)],
    ).fetchone()
    cycles, n_trunc, widest = row[0], row[1] or 0, row[2]
    if not cycles:
        # `breadth_cycles` landed 2026-09-07, after breadth_snapshots. An
        # archive with breadth rows but no cycle records predates it and has
        # NOT been measured — say so rather than banking a free pass
        # (mistakes #28, the idiom _check_continuity uses).
        print(f"WATCH {BREADTH_TRUNCATION_CHECK} — no breadth cycle records in window", flush=True)
        return
    check(
        BREADTH_TRUNCATION_CHECK,
        n_trunc == 0,
        f"{n_trunc}/{cycles} cycles truncated, widest universe {widest}",
    )


def qa_archive(hours: float, path: str = ARCHIVE) -> int | None:
    """Returns the econ pull's age in days (see `qa_econ_pull_live`), or
    None when the archive was unreachable or has never been pulled — the
    witness `qa_signals_fetch` needs, and None means "cannot decide"."""
    conn = _connect_ro(path)
    now = datetime.now(UTC).replace(tzinfo=None)
    if not _reachable(conn, "main archive reachable", "archive", now):
        return None

    _check_freshness(
        conn, "collector fresh (snapshots < 20 min old)", "snapshots", "ts", now, "snapshots"
    )

    # The freshness check above answers "is it collecting NOW". This answers
    # "has it been collecting ALL DAY" — the question no instantaneous check
    # can reach, because QA runs once and an outage that healed is over by the
    # time it looks. See COLLECTION_GAP_BUDGET_S.
    _check_continuity(
        conn, "collection continuous over last 24h", "snapshots", "ts", now, "collector"
    )

    # EXP-928 breadth is the FOURTH writer of this archive (measured
    # 2026-09-03) and the only exchange-wide quote history: the 5-min collect
    # cycle snapshots the 23-series watchlist, so a series family we have
    # never studied can be evaluated retrospectively ONLY out of this table.
    # Nothing watched it until 2026-09-04 — its death would have stayed
    # invisible until someone tried to study a family and found the hole,
    # which is the one moment the data can no longer be recovered.
    #
    # Guarded on non-empty, the poly_prices/news_items idiom below: breadth is
    # DEFAULT DISABLED and installing the timer IS the enabling act, so an
    # archive that never ran it must stay green rather than alarm about a
    # deliberate choice.
    #
    # It reuses the collector's budgets, and that reuse is MEASURED rather
    # than assumed: over 32 days / 9,150 cycles on the live archive breadth
    # runs p50 300.0s (its 5-min timer, exactly) and p99 314.7s, and its worst
    # benign gap is 20.0 min. Its ONLY gap past 30 min is 264.8 min — the same
    # 2026-08-20 box outage COLLECTION_GAP_BUDGET_S was cut against, all
    # writers down together. So 60 min sits 3x above breadth's benign worst
    # and 4.4x under the event it exists to catch, exactly as for snapshots.
    n_breadth = conn.execute("SELECT count(*) FROM breadth_snapshots").fetchone()[0]
    if n_breadth:
        _check_freshness(
            conn,
            "breadth fresh (snapshots < 20 min old)",
            "breadth_snapshots",
            "ts",
            now,
            "breadth snapshots",
        )
        _check_continuity(
            conn, "breadth continuous over last 24h", "breadth_snapshots", "ts", now, "breadth"
        )
        _check_breadth_truncation(conn, now)

    # THE SECOND UNWATCHED LIVE WRITER, found 2026-09-04 by the derived
    # coverage test (tests/test_qa_table_coverage.py) rather than by hand —
    # which is the whole point of deriving it. `nws_forecasts` had 580,270
    # rows and was being written in the same 5-min cycle as `snapshots`, and
    # qa.py did not name it ONCE. It is the ground truth `strategies.WeatherNWS`
    # trades against, and forecasts are unrecoverable after the fact: NWS
    # publishes the CURRENT forecast, so a pull missed at 14:15Z is gone.
    #
    # `snapshots` freshness is NOT a witness for it, and that is the reason
    # this needs its own check rather than riding the collector's. The pull
    # sits inside the same cycle but under a per-station try/except
    # (`collect.py`: `print(f"[collect] nws {station}: ...")`), so an NWS
    # outage, a DNS failure or a station rename drops forecasts silently while
    # snapshots keep flowing and every existing archive check reads green.
    #
    # Budgets are the collector's, and the reuse is MEASURED, not assumed:
    # over 32 days on the live archive the minute-bucketed cycle gap runs p50
    # 300.0s and p99 300.0s (the `*:0/5` timer, exactly), the worst benign gap
    # is 25.0 min, and the ONLY gap past 30 min is 265.0 min — the same
    # 2026-08-20 box outage COLLECTION_GAP_BUDGET_S was cut against, all
    # writers down together. So 60 min sits 2.4x above the benign worst and
    # 4.4x under the event, exactly as it does for snapshots and breadth.
    #
    # Guarded on non-empty, the breadth/poly_prices/news_items idiom: the
    # station list is watchlist-driven (`nws_stations`), so a deployment that
    # pulls no weather must be told NEITHER that it is broken nor that it is
    # fine.
    n_nws = conn.execute("SELECT count(*) FROM nws_forecasts").fetchone()[0]
    if n_nws:
        _check_freshness(
            conn,
            "nws forecasts fresh (< 20 min old)",
            "nws_forecasts",
            "fetched_at",
            now,
            "nws forecasts",
        )
        _check_continuity(
            conn,
            "nws forecasts continuous over last 24h",
            "nws_forecasts",
            "fetched_at",
            now,
            "nws pull",
        )

    ok_sweeps = conn.execute(
        "SELECT count(*) FROM sweep_log WHERE status='ok' AND swept_at > ? - INTERVAL 36 HOUR",
        [now],
    ).fetchone()[0]
    check("sweep ran in last 36h", ok_sweeps > 0, f"{ok_sweeps} ok entries")

    # `candles` is the ONE archive table with no ingest stamp — end_ts is the
    # candle's period end, and the sweep walks settled history, so max(end_ts)
    # legitimately moves BACKWARDS and cannot answer "is candle ingest alive".
    # Found 2026-09-04 by tests/test_qa_staleness_coverage.py, which derives
    # exactly that question per table; `candles` was the only live-written
    # table with neither its own age check nor a witness.
    #
    # THE FAILURE THIS EXISTS FOR, and why "sweep ran in last 36h" is not it:
    # sweep.py logs status='ok' on the row COUNT of markets, not of candles,
    # so a candlestick endpoint that starts returning `{"candlesticks": []}`
    # under HTTP 200 (a renamed field, a dropped period_interval) inserts
    # nothing while the trade tape riding in the same loop keeps landing.
    # sweep_log fills with ok entries, trades_swept stays fresh, and every
    # existing check reads green while the archive stops gaining candles —
    # the nws shape exactly: two payloads in one loop, one silent failure.
    #
    # The writer's own log carries the stamp the table lacks, so ask it, over
    # the same 36h window as the sweep check above. > 0 is deliberately a
    # DEATH threshold, not a degradation one, and the measurement says it can
    # be no tighter: over 30 days of hourly-stepped 36h windows on the live
    # archive the sum runs median 312,296 and min 5,726 (the 2026-08-20 box
    # outage), a 55x benign spread. Any percentile budget across that would
    # fire on a quiet day; ingest going to zero is unambiguous.
    #
    # Nested under ok_sweeps rather than guarded on non-empty: with no sweep
    # in the window there is no candle to expect, and the failure is already
    # named once above. Two red lines for one cause is noise, not coverage.
    if ok_sweeps:
        landed = conn.execute(
            "SELECT coalesce(sum(n_candles), 0) FROM sweep_log WHERE swept_at > ? - INTERVAL"
            " 36 HOUR",
            [now],
        ).fetchone()[0]
        check(
            "candle ingest landing (36h)",
            landed > 0,
            f"{landed} candles inserted by {ok_sweeps} ok sweep(s) in the window",
        )

    # promote.sh installs code; it does not migrate the archive, and nothing
    # asserts the version at open — so a shipped migration can sit unapplied
    # indefinitely while every read silently uses the pre-migration
    # convention. Make that visible instead (2026-08-23, migration_2).
    sv = conn.execute("SELECT max(version) FROM schema_meta").fetchone()[0] or 0
    check(
        "archive schema at current version",
        sv >= SCHEMA_VERSION,
        f"archive at v{sv}, code expects v{SCHEMA_VERSION}"
        " — run `python -m hyxlab.migrate` when no writer holds the lock",
    )

    mv = conn.execute(
        "SELECT count(*) FROM snapshots WHERE venue='kalshi' AND ("
        " (no_ask IS NOT NULL AND yes_bid IS NOT NULL AND abs(no_ask - (1-yes_bid)) > 0.005)"
        " OR (no_bid IS NOT NULL AND yes_ask IS NOT NULL AND abs(no_bid - (1-yes_ask)) > 0.005))"
    ).fetchone()[0]
    check("kalshi mirror invariant", mv == 0, f"{mv} violations")

    # Polymarket price capture: once the poly sweep has ever run, its
    # daily cadence must hold (retention rolls off at ~60d).
    n_poly = conn.execute("SELECT count(*) FROM poly_prices").fetchone()[0]
    if n_poly:
        page = conn.execute("SELECT epoch(? - max(ts)) / 3600 FROM poly_prices", [now]).fetchone()[
            0
        ]
        check("poly prices fresh (< 30h old)", page < 30, f"age {page:.1f}h")

        # Enumeration-shrink tripwire: the 2026-07-08 Gamma offset cap
        # silently dropped the swept universe from ~4600 to ~2000 markets
        # and was caught by a lucky dead probe, not by QA.
        #
        # It reads poly_market_stats, NOT poly_prices. poly_prices.ts is a
        # CLOB print time, so "distinct markets on day D" counts markets that
        # TRADED that day and keeps growing for days afterwards as later
        # sweeps backfill history — the newest complete day is always the
        # least-filled one. That made the ratio decay to ~0.57 of the
        # prior-week peak by construction (measured 2026-08-23: 10,984 nine
        # days back against 6,302 yesterday, with nothing wrong), i.e. a
        # check sitting just above its own floor for a structural reason and
        # blind to the drop it was written for. poly_market_stats instead
        # holds one row per market per sweep RUN stamped with that run's
        # start, so a run is a `ts` group and the count is the enumeration.
        # Grouping by the exact instant also needs no day alignment.
        #
        # RUN_SETTLE_H: the walk takes up to ~15h, so the newest group is
        # normally still in flight and its partial count is not a datum.
        # 20h clears it while leaving yesterday's run (~30h old at the 10:00Z
        # QA) well inside.
        RUN_SETTLE_H = 20
        runs = conn.execute(
            """
            SELECT ts, count(DISTINCT market_id) AS cnt
            FROM poly_market_stats
            WHERE ts > ? - INTERVAL 10 DAY AND ts <= ?
            GROUP BY ts ORDER BY ts DESC
            """,
            [now, now - timedelta(hours=RUN_SETTLE_H)],
        ).fetchall()
        # 0.75, not the old 0.5: on a clean enumeration signal the nine runs
        # to 2026-08-22 spanned 16,391-16,952, a 3.4% band, so 0.5 could only
        # ever catch a halving. 0.75 catches a quarter of the universe going
        # missing and still sits far outside the observed spread.
        #
        # THE ELSE BRANCH IS THE POINT (EXP-1381). Until 2026-09-05 this guard
        # had none, and it is the one guard in qa.py computed from a WINDOWED
        # read: `n_poly`, `n_breadth`, `n_nws`, `n_news` gate on a whole-table
        # count that only ever goes up (nothing in this codebase deletes from
        # an archive table), so they can be false only on a deployment that
        # never enabled the writer. `runs` is the last 10 days, so it empties
        # on precisely the input this check exists to notice — and then the
        # tripwire disappeared from the output entirely, with no line to miss.
        # Two ways in, both silent before today:
        #   (a) the stats half of the walk goes inert (a renamed column, a
        #       raised insert) while the CLOB prices half keeps landing, so
        #       `poly prices fresh` stays green and `runs` empties;
        #   (b) the universe collapses BELOW the floor and stays there ten
        #       days, so the guard that exists to make the ratio meaningful
        #       consumes the collapse it was protecting against.
        # `poly prices fresh (< 30h old)` above is the independent witness
        # that decides the skip, the qa_signals_fetch shape exactly: the same
        # sweep writes both tables, so prices fresh + no settled runs = the
        # stats writer is inert (FAIL, once a cycle has had time to run);
        # prices stale = the sweep is stopped, already named once above, and a
        # second red line for one cause is noise (SKIP).
        #
        # 36h of grace is SKIP_MAX_AGE_H, and it is loose on the measurement:
        # over the 30 days to 2026-09-05 the trailing-10d window held median 9
        # and minimum 9 settled runs at every one of 721 hourly steps — it was
        # never once below the 2 this guard needs — and the widest gap between
        # consecutive run starts across all 64 runs is 26.4h.
        prior = max((c for _, c in runs[1:]), default=0)
        if len(runs) >= 2 and prior > POLY_UNIVERSE_MIN_PRIOR:
            last_ts, last = runs[0]
            check(
                "poly swept universe not shrinking",
                last >= 0.75 * prior,
                f"last completed sweep {last_ts:%Y-%m-%d %H:%MZ} enumerated {last}"
                f" markets vs prior-10d peak {prior}",
            )
            _record_ok(_POLY_UNIVERSE_SECTION, now)
        else:
            name = "poly swept universe not shrinking"
            why = (
                f"only {len(runs)} settled sweep run(s) in the last 10 days"
                f" (need 2, {RUN_SETTLE_H}h settle)"
                if len(runs) < 2
                else f"the prior-10d peak is {prior} markets, under the"
                f" {POLY_UNIVERSE_MIN_PRIOR} floor a shrink ratio needs"
            )
            waited = _skip_age_h(_POLY_UNIVERSE_SECTION, now)
            if page < 30 and waited is not None and waited >= SKIP_MAX_AGE_H:
                check(
                    name,
                    False,
                    f"TRIPWIRE INERT: {why}, while poly_prices is {page:.1f}h old so the"
                    f" sweep IS running — poly_market_stats is not being written, and the"
                    f" enumeration-shrink tripwire has measured nothing for {waited:.1f}h"
                    f" (max {SKIP_MAX_AGE_H:g}h)",
                )
            else:
                _note_seen(_POLY_UNIVERSE_SECTION, now)
                print(
                    f"SKIP  {name} — UNVERIFIED: {why}"
                    + (
                        f"; poly_prices is {page:.1f}h old so the sweep looks stopped too and"
                        " that is already reported above"
                        if page >= 30
                        else f"; the sweep IS running (poly_prices {page:.1f}h old), so this"
                        f" escalates to FAIL if no run settles within {SKIP_MAX_AGE_H:g}h"
                        + (f" (waiting {waited:.1f}h)" if waited is not None else "")
                    ),
                    flush=True,
                )
                _skipped.append(_POLY_UNIVERSE_SECTION)

    # Signal feeds (B4): once a feed has ever pulled, its cadence must
    # hold. Guarded on non-empty so pre-first-pull archives stay green.
    pull_age_d = qa_econ_pull_live(conn, now)
    n_news = conn.execute("SELECT count(*) FROM news_items WHERE source='gdelt'").fetchone()[0]
    if n_news:
        age_h = conn.execute(
            "SELECT epoch(? - max(knowable_at)) / 3600 FROM news_items WHERE source='gdelt'",
            [now],
        ).fetchone()[0]
        check("gdelt news fresh (< 30h)", age_h < 30, f"age {age_h:.1f}h")

    qa_tape_coverage(conn, now)
    conn.close()
    _record_ok("archive", now)
    return pull_age_d


def read_signals_fetch(path: str) -> tuple[dict[str, datetime], datetime | None, int]:
    """Read the per-series ALFRED fetch sidecar: (last SUCCESSFUL fetch per
    series, newest run's stamp, malformed row count). Timestamps come back
    tz-aware; a naive one in the file is read as UTC, which is what
    `record_fetch` writes.

    Shared by `qa_signals_fetch` and `qa_econ_pull_live` on purpose — the two
    decide opposite questions against this one file (is a series being
    dropped / is the ingest landing at all), and a second parser is a second
    opinion about what the file says.
    """
    last_ok: dict[str, datetime] = {}
    newest_run: datetime | None = None
    malformed = 0
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                at = datetime.fromisoformat(row["at"])
                outcomes = row["series"]
                if not isinstance(outcomes, dict):
                    raise ValueError("series")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                malformed += 1
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            newest_run = at if newest_run is None else max(newest_run, at)
            for sid, outcome in outcomes.items():
                if isinstance(outcome, dict) and outcome.get("ok"):
                    prev = last_ok.get(sid)
                    if prev is None or at > prev:
                        last_ok[sid] = at
    return last_ok, newest_run, malformed


def qa_econ_pull_live(conn, now: datetime, fetch_log: str = SIGNALS_FETCH_LOG) -> int | None:
    """Is the ALFRED pull still bringing vintages home at all?

    Returns the age in whole days of the newest ingested VINTAGE DATE, or
    None if nothing has ever been ingested — `qa_signals_fetch` uses it as
    the independent witness that decides an absent sidecar.

    Two deliberate choices, both explained at ECON_PULL_GAP_BUDGET_D:
    the age is measured on the vintage date recovered from `knowable_at`
    rather than on `knowable_at` itself (the stamp is pessimistically in
    the FUTURE, which is what made the old check print a negative age and
    pass), and it pools all series ON PURPOSE, because "did anything
    arrive" is the one question pooling answers honestly. It cannot see a
    single series being dropped; nothing archive-side can, which is why
    the sidecar exists.
    """
    name = "econ pull live (any series, last vintage date)"
    if not conn.execute("SELECT count(*) FROM econ_vintages").fetchone()[0]:
        # An empty table is MONOTONE (nothing in this repo deletes archive
        # rows — tests/test_qa_silent_guards.py derives that), so on a
        # deployment that never enabled the pull this early return is silent
        # on purpose: "was this feed ever turned on" is a deliberate choice.
        #
        # It is NOT silent when the pull is demonstrably running. The sidecar
        # is written by `record_fetch`, which `signals.main` calls BEFORE the
        # locked DuckDB write, so a fetch that succeeds and a write that never
        # lands leaves exactly this state — ok series in the sidecar, nothing
        # in the table — and `diff_vintages` cannot explain it, because on an
        # empty table every fetched observation is new. Then the silence would
        # hide the pull failing from the day it was installed, with no line to
        # miss: the archive-age witness is not available on a young deployment
        # and every econ check downstream reads "cannot decide" (EXP-1382).
        last_ok, newest_run, _ = read_signals_fetch(fetch_log)
        aware = now if now.tzinfo else now.replace(tzinfo=UTC)
        run_age_d = None if newest_run is None else (aware - newest_run).total_seconds() / 86400.0
        if last_ok and run_age_d is not None and run_age_d <= ECON_PULL_GAP_BUDGET_D:
            check(
                name,
                False,
                f"INGEST NEVER LANDED: econ_vintages is empty, but {fetch_log} records"
                f" {len(last_ok)} series fetched OK {run_age_d:.1f}d ago — the fetch half of"
                " the pull works and the archive write does not, so nothing econ-side has"
                " ever had anything to measure",
            )
        return None  # pre-first-pull archive: nothing to hold to a cadence
    # INTERVAL takes no placeholder in DuckDB; the offset is an int constant.
    last_vd = conn.execute(
        "SELECT max(cast(knowable_at - INTERVAL "
        f"{KNOWABLE_AT_STAMP_OFFSET_H:d} HOUR AS DATE)) FROM econ_vintages"
    ).fetchone()[0]
    age_d = (now.date() - last_vd).days
    check(
        name,
        age_d <= ECON_PULL_GAP_BUDGET_D,
        f"newest vintage date {last_vd:%Y-%m-%d}, {age_d}d ago, budget {ECON_PULL_GAP_BUDGET_D}d",
    )
    return age_d


def qa_signals_fetch(
    pull_age_d: int | None,
    path: str = SIGNALS_FETCH_LOG,
    series: list[str] | None = None,
    now: datetime | None = None,
) -> None:
    """Fail when an individual econ series has stopped being fetched.

    This is the question `econ pull live` structurally cannot reach.
    `collector.signals.fetch_alfred` retries three times and then moves on
    with the series simply absent from its result, so a series ALFRED has
    dropped (or whose CSV header changed, which makes `parse_vintage_csv`
    raise) produces no rows and no archive-visible trace. And no
    archive-side rule can recover it, because `econ_vintages` only gains a
    row when a value CHANGES: a monthly series that died and a monthly
    series that has not printed yet are the same table for a month.

    So the witness is the pull's own per-series outcome record, and an
    absent record is DECIDED rather than trusted — the same shape as
    `qa_collect_skips`, against a different witness:

      sidecar absent/stale, archive says the pull IS running  -> FAIL
                                                                (producer inert)
      sidecar absent/stale, archive says the pull is NOT
      running (or is unreadable)                              -> UNVERIFIED
      sidecar current                                         -> the per-series
                                                                 age check
    """
    now = now or datetime.now(UTC)
    naive_now = now.replace(tzinfo=None)
    name = "econ series all fetched, not just the fast ones"
    expected = series if series is not None else list(alfred.SERIES)
    # last successful fetch per series, over every run the sidecar holds
    last_ok, newest_run, malformed = read_signals_fetch(path)
    p = Path(path)
    tail = f", {malformed} malformed rows" if malformed else ""

    run_age_d = None if newest_run is None else (now - newest_run).total_seconds() / 86400.0
    producer_quiet = run_age_d is None or run_age_d > ECON_PULL_GAP_BUDGET_D
    if producer_quiet:
        # The archive is the independent witness: it is written by the same
        # run, through a different path (the DuckDB insert), so the two
        # disagree in exactly one case — the recorder is not running.
        if pull_age_d is not None and pull_age_d <= ECON_PULL_GAP_BUDGET_D:
            # A sidecar that has NEVER held a run is genuinely undecidable on
            # sight: "record_fetch is dead" and "record_fetch shipped an hour
            # ago and the 04:40Z pull has not fired yet" are the same file. So
            # the never-produced case gets the bounded-SKIP treatment (clock
            # starts at first observation, escalates once a pull cycle has
            # provably had time to run) rather than a red that is guaranteed
            # for the first day of its own life. A sidecar that HAS produced
            # and then went quiet needs no such grace.
            grace_h = _skip_age_h(_SIGNALS_FETCH_SECTION, naive_now)
            if run_age_d is None and (grace_h is None or grace_h < SKIP_MAX_AGE_H):
                _note_seen(_SIGNALS_FETCH_SECTION, naive_now)
                print(
                    f"SKIP  {name} — UNVERIFIED: {path} holds no run"
                    + ("" if p.exists() else " and does not exist")
                    + f"; the pull IS running (archive vintage {pull_age_d}d old), so this"
                    f" escalates to FAIL if no run is recorded within {SKIP_MAX_AGE_H:g}h"
                    + (f" (waiting {grace_h:.1f}h)" if grace_h is not None else "")
                    + tail,
                    flush=True,
                )
                _skipped.append(_SIGNALS_FETCH_SECTION)
                return
            check(
                name,
                False,
                f"PRODUCER INERT: the archive holds a vintage from {pull_age_d}d ago so the"
                f" pull IS running, but {path} holds"
                + (" no run" if run_age_d is None else f" nothing newer than {run_age_d:.1f}d")
                + ("" if p.exists() else " and does not exist")
                + f". record_fetch() is not running, so this check reads a file nothing"
                f" writes{tail}",
            )
            return
        print(
            f"SKIP  {name} — UNVERIFIED: {path} holds"
            + (" no run" if run_age_d is None else f" nothing newer than {run_age_d:.1f}d")
            + ("" if p.exists() else " (file absent)")
            + (
                " and the archive shows no econ vintages either, so the pull is neither"
                " proven alive nor proven dead"
                if pull_age_d is None
                else f" and the archive's newest vintage is {pull_age_d}d old, so the pull"
                " looks stopped too — nothing here is measured"
            )
            + tail,
            flush=True,
        )
        _skipped.append(_SIGNALS_FETCH_SECTION)
        return

    stale = sorted(
        (
            (sid, None if sid not in last_ok else (now - last_ok[sid]).total_seconds() / 86400.0)
            for sid in expected
        ),
        key=lambda t: (t[1] is not None, t[1]),
    )
    _record_ok(_SIGNALS_FETCH_SECTION, naive_now)
    bad = [(sid, a) for sid, a in stale if a is None or a > ECON_PULL_GAP_BUDGET_D]
    worst = "never" if stale[0][1] is None else f"{stale[0][1]:.1f}d"
    check(
        name,
        not bad,
        (
            f"{len(expected) - len(bad)}/{len(expected)} series fetched within"
            f" {ECON_PULL_GAP_BUDGET_D}d; oldest {stale[0][0]} {worst}"
            + (
                "; STALE "
                + ", ".join(f"{s}={'never' if a is None else f'{a:.1f}d'}" for s, a in bad)
                if bad
                else ""
            )
            + tail
        ),
    )


def qa_tape_coverage(conn, now: datetime) -> None:
    """Settled+traded markets inside the retention window (~64d, use 55 to
    stay ahead of the boundary) must have a tape sweep — but a tail the
    sweeper is actively draining is WATCH, not FAIL (EXP-962). Three
    renderings, kept distinct:

      nothing unswept                             -> PASS
      unswept, sweeps landing, all inside grace   -> WATCH (draining tail)
      no sweep landed for > 26h, or any market
      unswept past its 30h grace                  -> FAIL (rot / stuck)

    Both FAILs are repairable (run the backfill; unstick the market), so
    unlike a capture hole they keep failing until repaired.
    """
    name = "trade tape covers retention window"
    uncovered = sorted(
        r[0]
        for r in conn.execute(
            """
            SELECT m.market_id FROM markets m
            WHERE m.venue='kalshi' AND m.result != ''
              AND m.close_time > ? - INTERVAL 55 DAY AND m.close_time < ? - INTERVAL 1 DAY
              AND EXISTS (SELECT 1 FROM candles c WHERE c.market_id = m.market_id AND c.volume > 0)
              AND NOT EXISTS (SELECT 1 FROM trades_swept s WHERE s.market_id = m.market_id)
            """,
            [now, now],
        ).fetchall()
    )

    # First-seen ledger. Age runs from when QA first OBSERVED the market
    # unswept, not from close_time — close_time would start the clock while
    # the market was still legitimately queued behind older work.
    state = _load_state()
    entry = state.setdefault("tape-coverage", {})
    seen = {m: t for m, t in (entry.get("first_seen") or {}).items() if m in set(uncovered)}
    for m in uncovered:
        seen.setdefault(m, now.isoformat())
    entry["first_seen"] = seen
    _save_state(state)

    if not uncovered:
        check(name, True, "0 traded markets unswept")
        return

    landed_age_h = conn.execute(
        "SELECT epoch(? - max(swept_at)) / 3600 FROM trades_swept", [now]
    ).fetchone()[0]

    def age_h(m: str) -> float:
        t = datetime.fromisoformat(seen[m])
        if t.tzinfo is not None:  # qa_archive's clock is naive UTC
            t = t.astimezone(UTC).replace(tzinfo=None)
        return (now - t).total_seconds() / 3600

    stuck = [m for m in uncovered if age_h(m) >= TAPE_DRAIN_GRACE_H]
    if landed_age_h is None or landed_age_h > TAPE_SWEEP_STALL_H:
        since = "never" if landed_age_h is None else f"{landed_age_h:.1f}h ago"
        check(
            name,
            False,
            f"{len(uncovered)} traded market(s) unswept and the last tape sweep landed "
            f"{since} (> {TAPE_SWEEP_STALL_H:g}h): the sweeper is not draining anything",
        )
    elif stuck:
        check(
            name,
            False,
            f"{len(stuck)} of {len(uncovered)} unswept market(s) sat past the "
            f"{TAPE_DRAIN_GRACE_H:g}h drain grace despite sweeps landing "
            f"{landed_age_h:.1f}h ago — stuck, not draining: " + ", ".join(stuck[:5]),
        )
    else:
        oldest = max(age_h(m) for m in uncovered)
        print(
            f"WATCH {name} — {len(uncovered)} unswept but sweeps landed "
            f"{landed_age_h:.1f}h ago and the oldest has waited {oldest:.1f}h "
            f"(grace {TAPE_DRAIN_GRACE_H:g}h); draining tail, not rot",
            flush=True,
        )


def journal_skip_exits(hours: float = 24.0, unit: str = COLLECT_UNIT) -> int | None:
    """How many cycles systemd saw exit 75 in the window; None if unreadable.

    None and 0 are DIFFERENT answers and the caller must keep them apart: an
    unreadable journal cannot testify that nothing skipped.
    """
    try:
        p = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                unit,
                "--since",
                f"{hours:g} hours ago",
                "--grep",
                r"Main process exited",
                "-o",
                "cat",
                "--output-fields=MESSAGE",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # journalctl exits 1 with empty output when the filter matched nothing;
    # that is a real zero. Any other nonzero is an unreadable journal.
    if p.returncode not in (0, 1) or (p.returncode == 1 and p.stdout.strip()):
        return None
    return len(re.findall(rf"status={COLLECT_SKIP_EXIT}\b", p.stdout))


#: Sentinel: `journal_skips=None` must be able to MEAN "the journal was
#: unreadable", so it cannot double as "not supplied".
_QUERY_JOURNAL = object()


def qa_collect_spool(
    hours: float = 24.0,
    log_path: str = COLLECT_SPOOL_LOG,
    spool_dir: str = COLLECT_SPOOL_DIR,
    skip_path: str = COLLECT_SKIP_LOG,
    now: datetime | None = None,
) -> None:
    """Fail when a skipped cycle's rows were NOT recovered.

    `qa_collect_skips` counts the contention and always will. This counts
    the DATA, and the two answers differ: on 2026-09-10 seven cycles
    skipped between 07:08 and 07:36Z while the daily sweep held the
    archive, and all seven discarded a complete, correctly stamped fetch
    (426 Kalshi snapshots + 5,649 infos + 35 forecasts each) that a drain
    would have written minutes later.

    Four ways the recovery can be broken, and each is a FAILURE rather
    than a warning because each one means rows are gone:

      inert    a skip in the window with no `spooled` event beside it --
               `collector.spool` is not running, or its write failed and
               the cycle was lost at the source
      stuck    the oldest spooled cycle is older than
               COLLECT_SPOOL_STALE_H -- nothing is draining, and the cap
               will start dropping cycles
      dropped  the spool hit MAX_FILES and threw the oldest away
      bad      a payload could not be decoded against the live models and
               was quarantined (see collector/spool.py on why a partial
               apply is not offered)

    A quiet box passes cleanly and says so: an empty spool with no skips
    is the correct state, and unlike `qa_collect_skips` this check needs
    no UNVERIFIED rung, because the inert arm above decides the producer
    against a witness that a dead producer cannot write.
    """
    now = now or datetime.now(UTC)
    name = "skipped collector cycles are recovered from the spool"
    cutoff = now - timedelta(hours=hours)

    events = []
    malformed = 0
    lp = Path(log_path)
    if lp.exists():
        for line in lp.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                at = datetime.fromisoformat(rec["at"])
            except (ValueError, KeyError, json.JSONDecodeError):
                malformed += 1
                continue
            if at >= cutoff:
                events.append((at, rec.get("event"), rec))
    tail = f", {malformed} malformed rows" if malformed else ""

    n = {e: sum(1 for _, ev, _ in events if ev == e) for e in ("spooled", "drained", "dropped")}

    # Skips are counted only from the moment the spool shipped; see
    # COLLECT_SPOOL_ARMED_AT for why that bound expires on its own.
    armed_cutoff = max(cutoff, COLLECT_SPOOL_ARMED_AT)
    skips = 0
    sp = Path(skip_path)
    if sp.exists():
        for line in sp.read_text().splitlines():
            if not line.strip():
                continue
            try:
                at = datetime.fromisoformat(json.loads(line)["at"])
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
            if at >= armed_cutoff:
                skips += 1

    d = Path(spool_dir)
    pending = sorted(d.glob("cycle-*.json")) if d.is_dir() else []
    quarantined = sorted(d.glob("*.bad")) if d.is_dir() else []
    oldest_age_h = None
    if pending:
        try:
            oldest_age_h = (now.timestamp() - pending[0].stat().st_mtime) / 3600
        except OSError:
            oldest_age_h = None

    problems = []
    if skips > n["spooled"]:
        problems.append(
            f"PRODUCER INERT: {skips} skipped cycle(s) recorded in {skip_path} since "
            f"the spool shipped but only {n['spooled']} spooled event(s) in {log_path}"
            + ("" if lp.exists() else f" ({log_path} does not exist)")
        )
    if n["dropped"]:
        problems.append(f"{n['dropped']} cycle(s) DROPPED at the spool cap -- rows lost")
    if quarantined:
        problems.append(
            f"{len(quarantined)} unreadable payload(s) quarantined: "
            f"{', '.join(f.name for f in quarantined[:3])}"
        )
    if oldest_age_h is not None and oldest_age_h > COLLECT_SPOOL_STALE_H:
        problems.append(
            f"oldest spooled cycle is {oldest_age_h:.1f}h old (budget "
            f"{COLLECT_SPOOL_STALE_H:g}h) -- nothing is draining"
        )

    detail = (
        "; ".join(problems)
        if problems
        else (
            f"{n['spooled']} spooled / {n['drained']} drained in {hours:g}h, {len(pending)} pending"
        )
    )
    check(name, not problems, detail + tail)


def qa_collect_skips(
    hours: float = 24.0,
    path: str = COLLECT_SKIP_LOG,
    journal_skips: int | None | object = _QUERY_JOURNAL,
) -> None:
    """Fail when 5-min capture cycles are being dropped for the writer lock.

    Each skipped cycle is a hole in the snapshot tape AT THE TIME IT FELL --
    since 2026-09-10 a recoverable one (`collector.spool` buffers the fetched
    rows and the next cycle drains them), which changes nothing about this
    check: contention is a fact about the box worth failing on whether or not
    the rows were later replayed. Whether they WERE is `qa_collect_spool`. Over
    the 14 days to 2026-08-02 the collector's `flock -n` wrapper dropped
    421 of 3,706 cycles (11.4%) while the daily sweep held the lock across
    its whole multi-hour run, and NOTHING recorded it: the wrapper failed
    before python started, so no archive-reading instrument could see it.

    EXP-943 — AND THAT IS ALSO WHY THIS CHECK COULD NOT FIRE. The version
    written on 2026-08-02 treated an absent sidecar as a PASS ("no skips
    recorded"), which is the same rendering a genuinely quiet day gets. Those
    are not the same fact: for 14 days the file was absent because
    `record_skip()` never ran, and on 2026-08-03 it was absent because no
    cycle had needed to wait. An alarm whose producer is dead is strictly
    WORSE than no alarm, because the green line is then read as health.

    So absence is now DECIDED against an independent witness — systemd's own
    count of exit-75 cycles, which is journalled whether or not python ever
    reached `record_skip()`:

      journal says N>0 skipped, sidecar has fewer  -> FAIL: producer inert
      journal says 0, sidecar empty/absent         -> UNVERIFIED (a SKIP, not
                                                      a pass: neither witness
                                                      saw anything, so nothing
                                                      was measured)
      journal unreadable, sidecar empty/absent     -> UNVERIFIED
      sidecar has rows                             -> the RATE check, as before

    Pass `journal_skips` to inject the witness (tests); the default queries
    journalctl, and `None` means "could not read it", never "zero".
    """
    now = datetime.now(UTC)
    name = "collector cycles are not skipped for the lock"
    p = Path(path)
    recent = 0
    malformed = 0
    if p.exists():
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                at = datetime.fromisoformat(json.loads(line)["at"])
            except (ValueError, KeyError, json.JSONDecodeError):
                malformed += 1
                continue
            if (now - at).total_seconds() <= hours * 3600:
                recent += 1
    tail = f", {malformed} malformed rows" if malformed else ""

    witness = journal_skip_exits(hours) if journal_skips is _QUERY_JOURNAL else journal_skips
    if witness is not None and witness > recent:
        check(
            name,
            False,
            f"PRODUCER INERT: systemd journalled {witness} exit-{COLLECT_SKIP_EXIT} "
            f"(skipped) cycle(s) of {COLLECT_UNIT} in {hours:g}h but {path} holds "
            f"{recent}"
            + ("" if p.exists() else " and does not exist")
            + f". record_skip() is not running, so this check reads a file nothing "
            f"writes{tail}",
        )
        return
    if recent == 0:
        # Nothing was measured. Reporting that as a pass is the defect above.
        detail = (
            f"UNVERIFIED: no skip recorded in {path}"
            + ("" if p.exists() else " (file absent)")
            + (
                f" and the {COLLECT_UNIT} journal is unreadable, so the sidecar's "
                "producer is neither proven alive nor proven dead"
                if witness is None
                else f" and systemd journalled 0 exit-{COLLECT_SKIP_EXIT} cycles — "
                "consistent, but no cycle needed to wait out the lock, so "
                "production is untested"
            )
            + tail
        )
        # Deliberately NOT bounded by `_skip_age_h`: a system with no lock
        # contention for weeks is healthy, so ageing this SKIP into a failure
        # would manufacture the alarm fatigue the check exists to avoid. The
        # journal witness above is what catches a dead producer, and it does so
        # on the first cycle that actually skips.
        #
        # What the reader DOES get is the last time production was measured
        # (the rate check ran on a real row), so "untested" carries a date:
        # 26 days of this line after 08-07 could not say whether the producer
        # had been proven once or never. `scripts/probe_collect_skip.py`
        # forces one in-window lock-wait to refresh it.
        last = _last_ok(COLLECT_SKIP_SECTION)
        detail += (
            f"; production last measured {(now - last).total_seconds() / 3600:.0f}h ago "
            f"({last.isoformat(timespec='minutes')})"
            if last
            else "; production has never been measured on this host"
        )
        _skipped.append(COLLECT_SKIP_SECTION)
        print(f"SKIP  {name} — {detail}", flush=True)
        return
    check(
        name,
        recent <= COLLECT_SKIP_MAX_24H,
        f"{recent} skipped cycles in {hours:g}h (max {COLLECT_SKIP_MAX_24H})"
        + (
            f", producer proven alive against {witness} journalled exit-"
            f"{COLLECT_SKIP_EXIT} cycle(s)"
            if witness is not None
            else ""
        )
        + tail,
    )
    if recent <= COLLECT_SKIP_MAX_24H:
        _record_ok(COLLECT_SKIP_SECTION, now)


@dataclass
class NightCapture:
    """One 23:00-04:00Z fade window's capture record.

    `starts`/`completions` are None when the journal could not testify about
    that night at all. None is NOT zero: a night nobody watched must not be
    rendered as a clean night, which is the same defect EXP-943 closed for the
    skip sidecar.
    """

    date: str  # date of the 23:00Z edge, UTC
    starts: int | None
    completions: int | None
    sweep_in_window: bool | None = None  # was the poly sweep still running?

    @property
    def measured(self) -> bool:
        return self.starts is not None and self.completions is not None and self.starts > 0

    @property
    def holes(self) -> int:
        if not self.measured:
            return 0
        return max(0, int(self.starts) - int(self.completions))


def _journal(unit: str, since: datetime, until: datetime) -> str | None:
    """Raw journal text for `unit` in [since, until), or None if unreadable."""
    try:
        p = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                unit,
                "--since",
                since.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "--until",
                until.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "-o",
                "short-iso",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode not in (0, 1):
        return None
    return p.stdout


def read_fade_windows(nights: int = FADE_WINDOW_NIGHTS, now: datetime | None = None) -> list:
    """Measure the last `nights` fade windows from systemd's journal.

    Counted from the journal rather than the archive on purpose: a cycle that
    loses the writer lock writes NOTHING, so the archive cannot report its own
    holes, and the archive is exactly what the sweep has locked.

    A cycle counts as CAPTURED only when its python emitted the `[collect]
    <iso> {...}` payload line. That marker predates and survives the `flock -n`
    wrapper removal, so a window is comparable across both eras; counting exit
    codes instead would silently stop working when the failure mode changes.
    """
    now = now or datetime.now(UTC)
    out = []
    for i in range(nights, 0, -1):
        start = (now - timedelta(days=i)).replace(
            hour=FADE_WINDOW_START_H, minute=0, second=0, microsecond=0
        )
        end = (start + timedelta(days=1)).replace(hour=FADE_WINDOW_END_H)
        if end > now:
            continue
        text = _journal(COLLECT_UNIT, start, end)
        if text is None:
            out.append(NightCapture(f"{start:%Y-%m-%d}", None, None, None))
            continue
        starts = len(re.findall(r"Starting hyxlab 5-min collector", text))
        done = len(re.findall(r"\[collect\] \d{4}-\d\d-\d\dT", text))
        sweep = _journal(SWEEP_UNIT, start, end)
        out.append(
            NightCapture(
                f"{start:%Y-%m-%d}",
                starts,
                done,
                None if sweep is None else bool(sweep.strip()),
            )
        )
    return out


def qa_fade_window_capture(
    nights: int = FADE_WINDOW_NIGHTS,
    records: list | None = None,
    now: datetime | None = None,
) -> None:
    """Fail when the 23:00-04:00Z snapshot tape loses cycles.

    EXP-960. `hyxlab-poly-sweep` is a ~14h oneshot on a 24h cadence: over the
    14 runs journalled 07-20..08-02 the eleven clean ones took 13.685-15.771h
    (median 14.44h, sd 0.64h) from 05:00Z, ending 18:42-20:47Z — 2.2 to 4.3h
    clear of the fade window. But the right tail is FAT, not drifting (OLS over
    the eleven: -0.055 h/run, r=-0.27 — if anything shortening): on 2026-07-29
    a run of Polymarket API stalls (measured gaps between progress lines of
    9,314s / 6,440s / 6,062s with the price counter frozen) stretched it to
    24h21m, ending 05:22Z — straight through the whole window.

    MEASURED CONSEQUENCE, and it is why the alarm is here and not on the
    duration: the sweep does NOT hold the lock while it runs. Sampled from
    /proc/locks (no flock taken, so the sampler cannot starve what it
    measures), `hyxlab-poly-sweep` takes the writer lock in 6.75-11.72s bursts
    at ~11% duty. So an overrun does not stop capture; it makes capture
    LOSE A DIE ROLL more often. On the 07-29 breach 4 of 60 cycles were lost
    (23:10, 23:45, 02:05, 02:10Z); on the six other nights 07-27..08-02, 0 of
    60 each. A duration alarm would therefore be the wrong instrument twice
    over — it fires on runs that cost nothing, and it cannot fire on a hole
    caused by anything other than the sweep.

    So: alarm on the HOLES, over a multi-night window, and report the overrun
    only as attribution. Three renderings, kept distinct:

      no night measurable                -> UNVERIFIED (a SKIP, never a pass)
      holes > budget on a NEW night      -> FAIL
      holes > budget, already reported   -> WATCH, non-failing

    The last of those is deliberate. A capture hole is unrecoverable — it
    cannot be fixed, only noticed — so a permanent FAIL for a night already
    on the record is pure noise, and noise is what trains an operator to stop
    reading QA. Escalation is on CHANGE: a night not previously reported.
    """
    now = now or datetime.now(UTC)
    name = f"fade window ({FADE_WINDOW_START_H:02d}:00-{FADE_WINDOW_END_H:02d}:00Z) capture"
    recs = read_fade_windows(nights, now) if records is None else records
    measured = [r for r in recs if r.measured]

    if not measured:
        _skipped.append("fade-window")
        print(
            f"SKIP  {name} — UNVERIFIED: none of the last {nights} fade windows could be "
            f"counted from the {COLLECT_UNIT} journal ({len(recs)} window(s) examined), so "
            "capture in the window is neither proven whole nor proven holed",
            flush=True,
        )
        return

    holed = [r for r in measured if r.holes > FADE_WINDOW_MAX_HOLES]
    state = _load_state()
    entry = state.setdefault("fade-window", {})
    reported = set(entry.get("reported") or [])
    fresh = [r for r in holed if r.date not in reported]
    entry["reported"] = sorted({r.date for r in holed} | (reported & {r.date for r in measured}))
    entry.setdefault("first_seen", now.isoformat())
    _save_state(state)

    detail = (
        f"{sum(r.holes for r in measured)} lost cycle(s) over {len(measured)} measured "
        f"window(s) of {len(recs)} (budget {FADE_WINDOW_MAX_HOLES}/window)"
    )
    if holed:
        detail += "; " + ", ".join(
            f"{r.date} lost {r.holes}/{r.starts}"
            + (" while the poly sweep was still running" if r.sweep_in_window else "")
            for r in holed
        )
    if len(measured) < len(recs):
        detail += f"; {len(recs) - len(measured)} window(s) UNMEASURED"

    if fresh:
        check(name, False, detail)
        return
    if holed:
        # Known and already journalled once. Still say it out loud, but do not
        # keep failing the run for a hole nobody can now repair.
        print(f"WATCH {name} — {detail} (already reported)", flush=True)
        return
    overrun = [r for r in measured if r.sweep_in_window]
    if overrun:
        print(
            f"WATCH {name} — {SWEEP_UNIT} was still running inside "
            f"{len(overrun)} window(s) ({', '.join(r.date for r in overrun)}) but cost no "
            "cycles; leading indicator only",
            flush=True,
        )
    check(name, True, detail)
    _record_ok("fade-window", now)


#: systemd renders durations as "1d 0h21min", "10h 6min 38.768s", "25min
#: 14.784s", "1.5s", "704ms". Tokenised rather than pattern-matched whole, so
#: an unseen combination degrades to a partial sum instead of a None.
_DUR_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(d|h|min|ms|us|s)\b")
_DUR_UNIT_S = {"d": 86400.0, "h": 3600.0, "min": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6}

#: The line systemd writes when a oneshot's cgroup is released. Anchored on
#: " over " because the SAME line carries CPU time first, and CPU time is the
#: smaller and wronger number: the 2026-08-03 sweep reads "Consumed 1h 12min
#: CPU time over 10h 6min wall clock time".
_CONSUMED_RE = re.compile(r"^(\S+)\s.*?:\s*(\S+\.service): Consumed .*? over (.+?) wall clock time")
#: systemd's two ways of saying a run did not finish its work. The `status=`
#: form is matched with an explicit non-zero test because `status=0/SUCCESS`
#: rides the same sentence on a clean stop.
_FAILED_RE = re.compile(r"Failed with result|Main process exited, code=\w+, status=(?!0/)")


def parse_systemd_duration(text: str) -> float | None:
    """Seconds in a systemd duration string, or None if it holds no token."""
    total = None
    for value, unit in _DUR_TOKEN_RE.findall(text):
        total = (total or 0.0) + float(value) * _DUR_UNIT_S[unit]
    return total


@dataclass(frozen=True)
class BatchRun:
    """One run of a batch unit that TERMINATED, as the journal recorded it.

    `ok` is the difference between a unit that finished its work and one that
    stopped early, and it is not decoration: systemd emits the `Consumed ...
    over ...` line for both, so wall clock alone cannot separate them, and the
    confusion is asymmetric. An abort TRUNCATES wall clock, so a unit that dies
    reads as further INSIDE its budget than one that does the work (EXP-1351).
    """

    unit: str  # timer name, so it keys BATCH_RUN_BUDGET_H
    end: datetime  # UTC instant the cgroup was released
    wall_h: float
    ok: bool = True  # exited 0; False for any of systemd's failure results

    @property
    def start(self) -> datetime:
        return self.end - timedelta(hours=self.wall_h)


def _fade_overlap_h(start: datetime, end: datetime) -> float:
    """Hours of [start, end) spent inside any 23:00-04:00Z fade window."""
    total = 0.0
    span_h = (24 - FADE_WINDOW_START_H) + FADE_WINDOW_END_H
    day = (start - timedelta(days=1)).date()
    while day <= end.date():
        ws = datetime(day.year, day.month, day.day, FADE_WINDOW_START_H, tzinfo=UTC)
        we = ws + timedelta(hours=span_h)
        total += max(0.0, (min(end, we) - max(start, ws)).total_seconds())
        day += timedelta(days=1)
    return total / 3600.0


def read_batch_runs(
    days: int = BATCH_RUN_LOOKBACK_DAYS, now: datetime | None = None
) -> dict[str, list[BatchRun] | None]:
    """Completed runs per budgeted unit, or None for a unit journald cannot reach.

    None is not an empty list: "the journal did not go back that far" and "the
    unit never ran" are different facts and only one of them is a problem.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    out: dict[str, list[BatchRun] | None] = {}
    for timer in sorted(BATCH_RUN_BUDGET_H):
        service = timer.removesuffix(".timer") + ".service"
        text = _journal(service, since, now)
        if text is None:
            out[timer] = None
            continue
        runs = []
        failed = False
        for line in text.splitlines():
            # Failure is announced BEFORE the cgroup accounting, so the flag is
            # always set by the time its own `Consumed` line arrives, and is
            # cleared there so it cannot leak onto the next run in the read.
            if _FAILED_RE.search(line) and f"{service}:" in line:
                failed = True
            m = _CONSUMED_RE.match(line)
            if not m or m.group(2) != service:
                continue
            ok, failed = not failed, False
            try:
                end = datetime.fromisoformat(m.group(1)).astimezone(UTC)
            except ValueError:
                continue
            secs = parse_systemd_duration(m.group(3))
            if secs is None:
                continue
            runs.append(BatchRun(timer, end, secs / 3600.0, ok=ok))
        out[timer] = runs
    return out


def _catch_up_clause(run: BatchRun, siblings: list[BatchRun]) -> str:
    """Name the abort a breach is catching up from, when there is one.

    A run that follows an aborted one carries the work the abort left behind,
    so it is long for a reason that re-measuring the budget would not fix —
    it would bake a one-off backlog in and blind the check to real drift.
    Live case: 08-20 aborted 1h21m in at 600/3458 series, and 08-21 then ran
    14.64h against a 12.5h budget with double the usual CPU time.
    """
    prior = [r for r in siblings if r.end <= run.start]
    if not prior or max(prior, key=lambda r: r.end).ok:
        return ""
    last = max(prior, key=lambda r: r.end)
    return f" — catch-up after the {last.end:%m-%d %H:%M}Z abort, not a stale budget"


def qa_batch_run_budget(
    runs: dict[str, list[BatchRun] | None] | None = None,
    now: datetime | None = None,
) -> None:
    """Fail when a Kalshi batch unit outruns the budget its schedule assumes.

    EXP-961. `test_kalshi_batch_units_finish_before_the_live_fade_window`
    asserts `OnCalendar + BATCH_RUN_BUDGET_H <= 23:00Z`. That is an invariant
    over two CONSTANTS: it is green for as long as the budget is written down,
    whether or not the budget is true. On 2026-08-03 it was not — the sweep's
    first full crypto pass ran **10h06m**, 2.1h past the 8.0h the constant
    claimed, and every check in the repo stayed green through it. The suite
    cannot see this; only the journal can. So this is the other half of the
    same invariant, and the halves fail differently on purpose:

      budget breach   -> the CONSTANT is stale. FAIL: it is repairable, either
                         by re-measuring or by making the unit faster, and it
                         must keep failing until someone does one of them.
      fade overlap    -> the unit actually spent Kalshi quota inside the live
                         agent's window (EXP-958). FAIL on a NEW date, WATCH
                         on one already reported — a past overlap cannot be
                         un-spent, and a permanent FAIL is the noise that
                         trains an operator to stop reading QA.

    Overlap is computed from the run's own measured interval, never from
    OnCalendar + budget. The 08-03 run is exactly why: it started 11:10Z under
    the pre-EXP-950 local-time schedule, not the 06:10Z its timer now reads, so
    judging it by the current spec put its finish at 16:17Z and "7.6h clear"
    when it truly ended 21:16:38Z with **1h43m** of margin. A schedule
    describes future runs; only the journal describes the ones that happened.
    """
    now = now or datetime.now(UTC)
    name = "batch units within measured run budget"
    runs = read_batch_runs(now=now) if runs is None else runs

    measured = {u: r for u, r in runs.items() if r}
    if not measured:
        _skipped.append("batch-run-budget")
        unreachable = sorted(u for u, r in runs.items() if r is None)
        print(
            f"SKIP  {name} — UNVERIFIED: no COMPLETED run of any budgeted unit "
            f"({', '.join(sorted(runs))}) could be read from the journal over the last "
            f"{BATCH_RUN_LOOKBACK_DAYS}d"
            + (f"; unreadable: {', '.join(unreachable)}" if unreachable else "")
            + ", so the budgets are neither confirmed nor refuted",
            flush=True,
        )
        return

    # Only a run that finished measures how long the work takes. An aborted
    # run's wall clock says when it died, and folding that into the budget can
    # only ever understate it (EXP-1351).
    healthy = {u: [r for r in rs if r.ok] for u, rs in measured.items()}
    aborted = [r for rs in measured.values() for r in rs if not r.ok]

    over = [r for rs in healthy.values() for r in rs if r.wall_h > BATCH_RUN_BUDGET_H[r.unit]]
    # Overlap, unlike the budget, is about quota actually spent — a run that
    # died inside the fade window still spent it, so this reads every run.
    overlaps = [
        (r, _fade_overlap_h(r.start, r.end))
        for rs in measured.values()
        for r in rs
        if _fade_overlap_h(r.start, r.end) > 0
    ]

    state = _load_state()
    entry = state.setdefault("batch-run-budget", {})
    reported = set(entry.get("reported") or [])
    keys = {f"{r.unit}@{r.end:%Y-%m-%dT%H:%M}" for r, _ in overlaps}
    keys |= {f"abort:{r.unit}@{r.end:%Y-%m-%dT%H:%M}" for r in aborted}
    fresh = keys - reported
    entry["reported"] = sorted(reported | keys)
    entry.setdefault("first_seen", now.isoformat())
    _save_state(state)

    fresh_overlap = {k for k in fresh if not k.startswith("abort:")}
    fresh_abort = [r for r in aborted if f"abort:{r.unit}@{r.end:%Y-%m-%dT%H:%M}" in fresh]

    worst = {u: max(r.wall_h for r in rs) for u, rs in healthy.items() if rs}
    detail = (
        f"{sum(len(rs) for rs in healthy.values())} completed run(s) over "
        f"{BATCH_RUN_LOOKBACK_DAYS}d"
    )
    if worst:
        detail += "; worst " + ", ".join(
            f"{u} {worst[u]:.2f}h/{BATCH_RUN_BUDGET_H[u]:g}h" for u in sorted(worst)
        )
    if aborted:
        detail += (
            f"; {len(aborted)} aborted ("
            + ", ".join(
                f"{r.unit} {r.end:%m-%d %H:%M}Z after {r.wall_h:.2f}h"
                for r in sorted(aborted, key=lambda r: r.end)
            )
            + ")"
        )
    starved = sorted(u for u, rs in healthy.items() if not rs)
    if starved:
        detail += (
            f"; UNVERIFIED, every run aborted so the budget is unmeasured: {', '.join(starved)}"
        )
    unmeasured = sorted(u for u, r in runs.items() if not r)
    if unmeasured:
        detail += f"; UNMEASURED: {', '.join(unmeasured)}"

    if over or fresh_overlap or fresh_abort:
        reasons = (
            [
                f"{r.unit} ran {r.wall_h:.2f}h (budget {BATCH_RUN_BUDGET_H[r.unit]:g}h), "
                f"{r.start:%m-%d %H:%M}Z -> {r.end:%m-%d %H:%M}Z"
                + _catch_up_clause(r, measured[r.unit])
                for r in over
            ]
            + [
                f"{r.unit} spent {h:.2f}h inside the {FADE_WINDOW_START_H}:00Z fade window "
                f"({r.start:%m-%d %H:%M}Z -> {r.end:%m-%d %H:%M}Z)"
                for r, h in overlaps
                if f"{r.unit}@{r.end:%Y-%m-%dT%H:%M}" in fresh_overlap
            ]
            + [
                f"{r.unit} ABORTED {r.wall_h:.2f}h in at {r.end:%m-%d %H:%M}Z — its wall "
                f"clock measures the abort, not the work"
                for r in sorted(fresh_abort, key=lambda r: r.end)
            ]
        )
        check(name, False, detail + "; " + "; ".join(reasons))
        return
    if overlaps or aborted:
        past = [
            f"{r.unit} overlapped the fade window by {h:.2f}h ending {r.end:%m-%d %H:%M}Z"
            for r, h in overlaps
        ] + [
            f"{r.unit} aborted {r.wall_h:.2f}h in at {r.end:%m-%d %H:%M}Z"
            for r in sorted(aborted, key=lambda r: r.end)
        ]
        print(
            f"WATCH {name} — {detail}; " + "; ".join(past) + " (already reported)",
            flush=True,
        )
        return
    check(name, True, detail)
    _record_ok("batch-run-budget", now)


# EXP-1383 — WHAT READS QA'S OWN VERDICT. Nothing did.
# `hyxlab-qa.service` is `Type=oneshot` with no `OnFailure=`, no
# `ExecStopPost=` and no notifier, and the repo contains no `systemctl
# is-failed`, no `--failed` and no other reader of a unit's state (checked
# repo-wide 2026-09-05). So `sys.exit(1)` set the unit to `failed` where
# nothing queried it, and the `NOT a full pass` line exits 0 — for that one
# even systemd's own state reads clean. The journal holds both, and the only
# journal reader in this project is qa.py itself (`_journal`, and
# `read_batch_runs` over the two units in BATCH_RUN_BUDGET_H, which does not
# include this one). That makes the answer structural rather than a matter of
# taste: the only consumer available to QA is the NEXT QA run, so it is made
# one here.
#
# WHAT THIS CATCHES that nothing else can. The freshness checks are
# INSTANTANEOUS (EXP-1359), so a defect that repairs itself between two 10:00Z
# runs leaves no trace anywhere: yesterday's FAIL scrolled past in a journal
# nobody reads and today's run is green. Recording the verdict makes the heal
# reportable. The same record makes a MISSED run reportable, which is the
# other half — a disabled timer, a box that stayed down, or a qa.py that dies
# before its first line all look identical from inside a run that happens.
#
# WHAT IT CANNOT DO, stated so the next pass does not assume otherwise: report
# its own non-execution. No in-process check can, which is exactly why the gap
# arm reads the record's AGE instead of trusting that a run occurred — a
# crash-looping QA is named by the first run that completes, and until one
# does, no reader inside this process exists to name it.
QA_RUN_SECTION = "run"
QA_RUN_CHECK = "prior QA run was read"
# The timer is daily at 10:00Z with `Persistent=true`, so consecutive records
# sit 24h apart and ONE missed slot is 48h. 36h is the midpoint: it trips on a
# single missed run while leaving 12h of slack for a late start or a boot
# catch-up, and it is the tolerance SKIP_MAX_AGE_H already uses for the same
# daily cadence.
QA_RUN_GAP_BUDGET_H = 36.0


def _own_findings() -> list[str]:
    """This run's failures MINUS its own prior-run report. Including that
    report would let one unread failure re-arm the report of itself, every
    day, forever — the check would then be describing nothing but its own
    echo."""
    return [f for f in _failures if f != QA_RUN_CHECK]


def _record_run(failures: list[str], skipped: list[str], now: datetime, unread: str = "") -> None:
    """Persist WHAT THIS RUN FOUND, for the next run to read back.

    The two lists are stored rather than a single verdict word because the
    next run's question is not "did it pass" but "did anything it reported go
    unread" — and that is answered per NAME, against what is true today.

    `unread` is THIS run's own `qa_prior_run` reason, and it is a THIRD field
    rather than a sixth name in `failures` on purpose. The record has two
    readers asking different questions, and one list cannot answer both:

      next qa run  -> "did a name I reported go quiet?"  MUST NOT see this
                      one. `_own_findings` explains why: an unread failure
                      would re-arm the report of itself, every day, forever.
      health digest -> "what did the last run find?"     MUST see it. It is
                      the whole finding, and it is invisible in `failures` by
                      the construction directly above.

    Keeping it out of `failures` preserves the first answer exactly; putting
    it on the record at all is what gives the second one a path. Nothing
    compares against this field, so it cannot echo.
    """
    state = _load_state()
    state[QA_RUN_SECTION] = {
        "last_run": now.isoformat(),
        "failures": sorted(failures),
        "skipped": sorted(skipped),
        "unread": unread,
    }
    _save_state(state)


@dataclass(frozen=True)
class PriorRun:
    """The last recorded run, as the one parser of the record reports it."""

    at: datetime
    failures: tuple[str, ...]
    skipped: tuple[str, ...]
    #: The run's own `qa_prior_run` reason, "" if it passed. Absent on any
    #: record written before this field existed, which reads the same as a
    #: pass -- the conservative direction: a digest that stays silent about
    #: one historical run, never one that invents a finding for it.
    unread: str = ""


def _prior_run(state: dict | None = None) -> PriorRun | None:
    """The last recorded run, or None if there is none — one parser for the
    record, so nothing can hold a second opinion about what it says. `state`
    lets a reader outside this process pass a record loaded from another
    tree's path without reimplementing the parse.

    A record written by an older qa.py, a truncated file or a hand-edited
    timestamp all read as "no prior run": the alternative is failing on the
    day the check is installed, which is how a check gets switched off. Naive
    timestamps are read as UTC — sections.json has carried both forms since
    before this record shared it.
    """
    entry = (_load_state() if state is None else state).get(QA_RUN_SECTION)
    if not isinstance(entry, dict):
        return None
    try:
        at = datetime.fromisoformat(entry["last_run"])
    except (KeyError, TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)

    def _names(key: str) -> tuple[str, ...]:
        raw = entry.get(key)
        return tuple(str(x) for x in raw) if isinstance(raw, list) else ()

    raw_unread = entry.get("unread")
    return PriorRun(
        at,
        _names("failures"),
        _names("skipped"),
        raw_unread if isinstance(raw_unread, str) else "",
    )


def qa_prior_run(
    now: datetime | None = None,
    failures: list[str] | None = None,
    skipped: list[str] | None = None,
    ran: list[str] | None = None,
) -> str:
    """Read back what the previous run reported, because nothing else does.

    Returns the reason it FAILED on, or "" — so that `main` can put this
    check's own finding on the record for the digest, which `_own_findings`
    keeps out of `failures` and which therefore reached no reader at all.
    See `_record_run`.

    Runs LAST, and is given this run's own findings, because both of its arms
    are comparisons against today:

      record too old  -> the run did not HAPPEN. The archive went unwatched
                         across the gap, whatever the last run said.
      unread + healed -> a name the prior run FAILED or SKIPPED that is green
                         today AND was actually re-run today (`ran`). That is the state nothing else in the project
                         can see: the checks are instantaneous (EXP-1359), so
                         a defect that repairs itself between two 10:00Z runs
                         leaves yesterday's line in a journal nobody reads and
                         today's run green.

    ONLY THE HEALED SET, and the production steady state is why. `collect-skips`
    reports UNVERIFIED — hence SKIPPED — on every run of a healthy box, so
    "the prior run was partial" is the NORMAL verdict here and an arm that
    fired on it would fail every night forever. A name that is still failing
    or still skipped today is reported by today's own line and bounded by its
    own clock (SKIP_MAX_AGE_H); repeating it here would be a second opinion
    about a live state, which is the noise that trains an operator to stop
    reading QA. What is NOT repeated anywhere is the name that went quiet.

    That is also what keeps the FAIL from re-arming itself: the record written
    after this check carries the OTHER sections' findings, so a healed failure
    is named exactly once and its own report is not a finding to report.
    """
    now = now or datetime.now(UTC)
    today_failed, today_skipped = set(failures or ()), set(skipped or ())
    # A name that FAILED today ran today, by construction — folded in so the
    # execution evidence cannot disagree with the findings it is filtering.
    today_ran = set(_ran if ran is None else ran) | today_failed
    prior = _prior_run()
    if prior is None:
        check(QA_RUN_CHECK, True, "no prior run on record — nothing to read back")
        return ""
    age_h = (now - prior.at).total_seconds() / 3600.0
    reasons = []
    if age_h > QA_RUN_GAP_BUDGET_H:
        reasons.append(
            f"QA DID NOT RUN — last run {prior.at:%Y-%m-%d %H:%M}Z, {age_h:.1f}h ago "
            f"(budget {QA_RUN_GAP_BUDGET_H:.0f}h)"
        )
    # HEALED IS A CLAIM ABOUT TODAY, SO ONLY TODAY'S EXECUTED CHECKS CAN MAKE IT.
    # A name absent from `today_failed` does not mean it passed — it means no
    # FAIL was recorded for it, and a check that never reached a verdict records
    # none. Both no-verdict paths were live on the 09-11 10:00Z run, and this
    # arm announced BOTH as "green today":
    #
    #   SKIP  — `collector cycles are not skipped for the lock` FAILED on 09-10
    #           and printed SKIP/UNVERIFIED on 09-11 (no cycle waited out the
    #           lock), in the same file whose exit path already says a skipped
    #           section is NOT a passed one.
    #   WATCH — `batch units within measured run budget` FAILED on 09-10 naming
    #           the 09-10 07:45Z sweep abort, and on 09-11 printed
    #           `WATCH ... (already reported)` and RETURNED. The abort had not
    #           gone away; the report of it had been acknowledged.
    #
    # The section namespace could not catch either: the record's `skipped` list
    # holds SECTION names ("collect-skips") and its `failures` list holds CHECK
    # names, so the check name crossed into an arm that could not see the skip
    # — and no section is skipped at all in the WATCH case. `_ran` is fed from
    # `check()`, the one place a verdict is reached, so it covers both paths and
    # whatever third one is written next without being told about it.
    unwatched = sorted(set(prior.failures) - today_ran)
    healed_f = sorted((set(prior.failures) & today_ran) - today_failed)
    healed_s = sorted(set(prior.skipped) - today_skipped)
    if healed_f:
        reasons.append(f"prior run FAILED {healed_f} and is green today — nothing read it")
    if healed_s:
        reasons.append(f"prior run SKIPPED {healed_s} and ran today — nothing read it")
    # Reported as CONTEXT on whatever verdict the arms above reach, never as a
    # reason of its own. A prior failure whose section skips again today is the
    # normal shape of a chronic skip (`collect-skips` is UNVERIFIED on every run
    # of a healthy box), so failing on it would fire every night forever — the
    # same trap the healed-only rule above exists to avoid. How long a section
    # may go unrun is already owned, and bounded, by its own
    # `<section> checks completed within 36h` line.
    tail = (
        f"; {unwatched} not re-checked today — the section did not run, so "
        "neither healed nor still-failing can be claimed"
        if unwatched
        else ""
    )
    if reasons:
        reason = "; ".join(reasons) + tail
        check(QA_RUN_CHECK, False, reason)
        return reason
    still = sorted((set(prior.failures) | set(prior.skipped)) - set(unwatched))
    check(
        QA_RUN_CHECK,
        True,
        f"prior run {prior.at:%m-%d %H:%M}Z was {age_h:.1f}h ago"
        + (f"; {still} still open today, reported by their own lines" if still else ", clean")
        + tail,
    )
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab daily data-quality checks")
    ap.add_argument("--hours", type=float, default=26.0, help="recency window")
    args = ap.parse_args()

    now = datetime.now(UTC)
    print(f"[qa] {now:%Y-%m-%d %H:%M} window={args.hours}h", flush=True)
    qa_disk_headroom()  # filesystem-only; must not be gated by an archive lock
    qa_stream(args.hours)
    qa_divergence()  # sidecar ledger + report dir; never gated by the archive lock
    pull_age_d = qa_archive(args.hours)
    qa_signals_fetch(pull_age_d)  # sidecar witness; the archive cannot see a dropped series
    qa_collect_skips()  # sidecar journal; never gated by the archive lock
    qa_collect_spool()  # the recovery half of the same hole; also sidecar-only
    qa_fade_window_capture()  # journal-only, for the same reason
    qa_batch_run_budget()  # journal-only, for the same reason
    # `_ran` is read here for the same reason the findings are: it must be the
    # LIVE set at the moment of the comparison, not one captured earlier.
    unread = qa_prior_run(now, _own_findings(), _skipped, _ran)  # the only reader of the last run
    # Re-read AFTER the check rather than reusing the list above: the
    # exclusion is then a live filter, not an artifact of statement order.
    _record_run(_own_findings(), _skipped, now, unread)  # BEFORE either exit path below
    if _failures:
        print(f"[qa] {len(_failures)} FAILURES: {_failures}", flush=True)
        sys.exit(1)
    # A skipped section is NOT a passed one. Saying "all checks pass" while
    # half the checks never ran is how the archive half went unwatched on 10
    # of 14 runs (Jul 20 – Aug 02 2026) with every journal line reading green.
    if _skipped:
        print(
            f"[qa] {_passes} checks pass, {len(_skipped)} SECTION(S) SKIPPED "
            f"({', '.join(_skipped)}) — NOT a full pass",
            flush=True,
        )
        return
    print("[qa] all checks pass", flush=True)


if __name__ == "__main__":
    main()
