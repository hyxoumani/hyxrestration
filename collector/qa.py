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
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

ARCHIVE = "data/hyxlab.duckdb"
STREAM = "data/hyxstream.duckdb"

# Per-section completion record, so a skip can be BOUNDED. Without it a
# locked archive skips silently forever and the journal still reads green.
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
#:                     08-02, a 24x step over the day before. 10.5 is that
#:                     measurement plus ~0.4h. Whether it is the one-time
#:                     crypto backlog or the new steady state is UNKNOWN until
#:                     a second full pass completes; `qa_batch_run_budget` is
#:                     what will say so out loud.
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
    "hyxlab-sweep.timer": 10.5,
    "hyxlab-tradepass.timer": 4.0,
}
#: Journald on this box holds ~16M / ~2 days, so this is a ceiling on the
#: lookback, not a promise of one. Unreachable days read UNMEASURED.
BATCH_RUN_LOOKBACK_DAYS = 7

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
_passes = 0
_lock_holder: str | None = None  # set by _connect_ro when a live writer holds the file


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    line = f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else "")
    print(line, flush=True)
    if ok:
        _passes += 1
    else:
        _failures.append(name)


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
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
            return duckdb.connect(path, read_only=True)
        except duckdb.Error as exc:
            m = re.search(r"Conflicting lock is held in (\S+) \(PID (\d+)\)", str(exc))
            if m and Path(f"/proc/{m.group(2)}").exists():
                _lock_holder = f"{m.group(1)} pid {m.group(2)}"
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
        (m, t0, t1)
        for m, t0, t1 in holes
        if not any(g0 <= t1 and g1 >= t0 for g0, g1 in gap_spans)
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
        + (
            "; UNKNOWN " + ", ".join(f"{t}x{n}" for t, n in unknown)
            if unknown
            else "; all known"
        ),
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

    p99 = conn.execute(
        "SELECT quantile_cont(epoch(recv_ts - src_ts), 0.99) FROM stream_trades"
        " WHERE src_ts IS NOT NULL AND recv_ts > ? - INTERVAL 1 HOUR * CAST(? AS INTEGER)",
        [now, int(hours)],
    ).fetchone()[0]
    # 25s allows for the known ~20s box-clock skew until NTP lands.
    check(
        "trade latency p99 sane",
        p99 is not None and -2.0 < p99 < 25.0,
        f"p99 {p99 if p99 is None else round(p99, 2)}s (incl. known clock skew)",
    )

    size_gb = Path(path).stat().st_size / 1e9
    check("stream disk under 20 GB", size_gb < 20.0, f"{size_gb:.2f} GB")
    conn.close()
    _record_ok("stream", now)


def qa_archive(hours: float, path: str = ARCHIVE) -> None:
    conn = _connect_ro(path)
    now = datetime.now(UTC).replace(tzinfo=None)
    if not _reachable(conn, "main archive reachable", "archive", now):
        return

    age = conn.execute("SELECT epoch(? - max(ts)) FROM snapshots", [now]).fetchone()[0]
    check(
        "collector fresh (snapshots < 20 min old)",
        age is not None and age < 1200,
        f"age {age:.0f}s" if age is not None else "no snapshots",
    )

    ok_sweeps = conn.execute(
        "SELECT count(*) FROM sweep_log WHERE status='ok' AND swept_at > ? - INTERVAL 36 HOUR",
        [now],
    ).fetchone()[0]
    check("sweep ran in last 36h", ok_sweeps > 0, f"{ok_sweeps} ok entries")

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
        # and was caught by a lucky dead probe, not by QA. Compare the
        # last completed day's distinct swept markets against the prior
        # week's peak; a sharp drop means upstream pagination broke.
        yday, prior = conn.execute(
            """
            WITH daily AS (
              SELECT date_trunc('day', ts) AS d, count(DISTINCT market_id) AS cnt
              FROM poly_prices WHERE ts > ? - INTERVAL 9 DAY GROUP BY 1
            )
            SELECT
              (SELECT cnt FROM daily WHERE d = date_trunc('day', ? - INTERVAL 1 DAY)),
              (SELECT max(cnt) FROM daily WHERE d < date_trunc('day', ? - INTERVAL 1 DAY))
            """,
            [now, now, now],
        ).fetchone()
        # 0.5: the swept universe declines organically ~5%/day as markets
        # resolve (0.66 vs peak observed 2026-07-11, benign); the failure
        # class is a step-function halving, not a drift.
        if prior and prior > 500:
            check(
                "poly swept universe not shrinking",
                yday is not None and yday >= 0.5 * prior,
                f"yesterday {yday or 0} distinct markets vs prior-week peak {prior}",
            )

    # Signal feeds (B4): once a feed has ever pulled, its cadence must
    # hold. Guarded on non-empty so pre-first-pull archives stay green.
    n_vint = conn.execute("SELECT count(*) FROM econ_vintages").fetchone()[0]
    if n_vint:
        age_d = conn.execute(
            "SELECT epoch(? - max(knowable_at)) / 86400 FROM econ_vintages", [now]
        ).fetchone()[0]
        check("econ vintages fresh (< 8 days)", age_d < 8, f"age {age_d:.1f}d")
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
                "journalctl", "--user", "-u", unit,
                "--since", f"{hours:g} hours ago",
                "--grep", r"Main process exited",
                "-o", "cat", "--output-fields=MESSAGE",
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


def qa_collect_skips(
    hours: float = 24.0,
    path: str = COLLECT_SKIP_LOG,
    journal_skips: int | None | object = _QUERY_JOURNAL,
) -> None:
    """Fail when 5-min capture cycles are being dropped for the writer lock.

    Each skipped cycle is an unrecoverable hole in the snapshot tape. Over
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

    witness = (
        journal_skip_exits(hours)
        if journal_skips is _QUERY_JOURNAL
        else journal_skips
    )
    if witness is not None and witness > recent:
        check(
            name,
            False,
            f"PRODUCER INERT: systemd journalled {witness} exit-{COLLECT_SKIP_EXIT} "
            f"(skipped) cycle(s) of {COLLECT_UNIT} in {hours:g}h but {path} holds "
            f"{recent}" + ("" if p.exists() else " and does not exist")
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
        _skipped.append("collect-skips")
        print(f"SKIP  {name} — {detail}", flush=True)
        return
    check(
        name,
        recent <= COLLECT_SKIP_MAX_24H,
        f"{recent} skipped cycles in {hours:g}h (max {COLLECT_SKIP_MAX_24H})"
        + (f", producer proven alive against {witness} journalled exit-"
           f"{COLLECT_SKIP_EXIT} cycle(s)" if witness is not None else "")
        + tail,
    )
    if recent <= COLLECT_SKIP_MAX_24H:
        _record_ok("collect-skips", now)


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
                "journalctl", "--user", "-u", unit,
                "--since", since.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "--until", until.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "-o", "short-iso", "--no-pager",
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
_CONSUMED_RE = re.compile(
    r"^(\S+)\s.*?:\s*(\S+\.service): Consumed .*? over (.+?) wall clock time"
)


def parse_systemd_duration(text: str) -> float | None:
    """Seconds in a systemd duration string, or None if it holds no token."""
    total = None
    for value, unit in _DUR_TOKEN_RE.findall(text):
        total = (total or 0.0) + float(value) * _DUR_UNIT_S[unit]
    return total


@dataclass(frozen=True)
class BatchRun:
    """One COMPLETED run of a batch unit, as the journal recorded it."""

    unit: str  # timer name, so it keys BATCH_RUN_BUDGET_H
    end: datetime  # UTC instant the cgroup was released
    wall_h: float

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
        for line in text.splitlines():
            m = _CONSUMED_RE.match(line)
            if not m or m.group(2) != service:
                continue
            try:
                end = datetime.fromisoformat(m.group(1)).astimezone(UTC)
            except ValueError:
                continue
            secs = parse_systemd_duration(m.group(3))
            if secs is None:
                continue
            runs.append(BatchRun(timer, end, secs / 3600.0))
        out[timer] = runs
    return out


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

    over = [
        r
        for rs in measured.values()
        for r in rs
        if r.wall_h > BATCH_RUN_BUDGET_H[r.unit]
    ]
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
    fresh_overlap = sorted(keys - reported)
    entry["reported"] = sorted(reported | keys)
    entry.setdefault("first_seen", now.isoformat())
    _save_state(state)

    worst = {
        u: max(r.wall_h for r in rs) for u, rs in measured.items()
    }
    detail = (
        f"{sum(len(r) for r in measured.values())} completed run(s) over "
        f"{BATCH_RUN_LOOKBACK_DAYS}d; worst "
        + ", ".join(
            f"{u} {worst[u]:.2f}h/{BATCH_RUN_BUDGET_H[u]:g}h" for u in sorted(worst)
        )
    )
    unmeasured = sorted(u for u, r in runs.items() if not r)
    if unmeasured:
        detail += f"; UNMEASURED: {', '.join(unmeasured)}"

    if over or fresh_overlap:
        reasons = [
            f"{r.unit} ran {r.wall_h:.2f}h (budget {BATCH_RUN_BUDGET_H[r.unit]:g}h), "
            f"{r.start:%m-%d %H:%M}Z -> {r.end:%m-%d %H:%M}Z"
            for r in over
        ] + [
            f"{r.unit} spent {h:.2f}h inside the {FADE_WINDOW_START_H}:00Z fade window "
            f"({r.start:%m-%d %H:%M}Z -> {r.end:%m-%d %H:%M}Z)"
            for r, h in overlaps
            if f"{r.unit}@{r.end:%Y-%m-%dT%H:%M}" in set(fresh_overlap)
        ]
        check(name, False, detail + "; " + "; ".join(reasons))
        return
    if overlaps:
        print(
            f"WATCH {name} — {detail}; "
            + "; ".join(
                f"{r.unit} overlapped the fade window by {h:.2f}h "
                f"ending {r.end:%m-%d %H:%M}Z"
                for r, h in overlaps
            )
            + " (already reported)",
            flush=True,
        )
        return
    check(name, True, detail)
    _record_ok("batch-run-budget", now)


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab daily data-quality checks")
    ap.add_argument("--hours", type=float, default=26.0, help="recency window")
    args = ap.parse_args()

    print(f"[qa] {datetime.now(UTC):%Y-%m-%d %H:%M} window={args.hours}h", flush=True)
    qa_stream(args.hours)
    qa_archive(args.hours)
    qa_collect_skips()  # sidecar journal; never gated by the archive lock
    qa_fade_window_capture()  # journal-only, for the same reason
    qa_batch_run_budget()  # journal-only, for the same reason
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
