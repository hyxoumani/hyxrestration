"""Exchange-wide archival sweep (proposal C8, corrected per data_contracts.md).

Enumerates Kalshi series by **category allowlist**, captures settled
markets + hourly candles since each series' watermark, and logs progress
to sweep_log. Idempotent: watermarks + anti-join inserts make re-runs and
crashes safe; a re-run resumes where the last one stopped.

    python -m collector.sweep --days 60            # initial retention capture
    python -m collector.sweep --days 2             # daily incremental
    python -m collector.sweep --days 2 --limit 20  # smoke test
    python -m collector.sweep --series KXETH \
        --refetch-from 2026-08-02T02:00 --refetch-to 2026-08-02T09:00Z
        # targeted BELOW-watermark hole repair (EXP-1287); watermark untouched

Rationale: Kalshi purges market data ~60-90 days after settlement
(verified 2026-07-06); anything not swept is gone. The allowlist excludes
the ~8,200 sports/entertainment/politics series that dominate settle
volume but are outside our strategy domains — one line to revisit.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import requests

from collector.venues import kalshi
from hyxlab.lockid import instance_lock_or_reason, note_holder
from hyxlab.store import (
    Store,
    attach_budget_s,
    attach_wait_block,
    attach_wait_line,
    held_duck,
    held_open,
    reset_attach_waits,
)

LOCK_FILE = "data/writer.lock"

DEFAULT_CATEGORIES = [
    "Economics",
    "Financials",
    "Climate and Weather",
    "Companies",
    "Commodities",
    "Science and Technology",
    "Health",
    "World",
    # Widened 2026-08-02 (breadth audit, owner infrastructure-first
    # directive): Crypto alone is ~29% of exchange notional ($7.7M/wk, 77
    # series) with ZERO archive coverage — uncalibratable forever without
    # settlement+candle capture. Collection is measurement, not trading:
    # CLAUDE.md still marks these families UNCALIBRATED = UNTRADEABLE for
    # the live agent. Sports/Entertainment/Politics stay excluded — that
    # is a standing USER-CONFIRMED decision (2026-07-08), not a default.
    "Crypto",
    "Exotics",
    "Mentions",
]

MARKETS_PAUSE_S = 0.2  # empirical safe pacing (data_contracts.md)
CANDLES_PAUSE_S = 0.35

# Per-series, per-run market budget. NOT a coverage cap: get_markets_ascending
# returns a contiguous oldest-first prefix, so stopping here advances the
# watermark honestly and the next run resumes exactly where this one stopped.
# What it bounds is TIME. Measured (EXP-931, 2026-08-02): sweep_series costs
# ~0.5s per market (candles + trade tape + CANDLES_PAUSE_S), and the widened
# category list put three sub-hourly BNB series near the front of the
# alphabetical target order. Each burned ~1.5h, so a 3.5h run reached
# KXBNB15M and died there — every KXBTC*/KXETH*/KXSOL* series and all ten
# Exotics series were never requested at all, and the resulting archive
# ("crypto == BNB only") looked like a finding rather than a starved run.
# 2,000 caps one series at ~17min so no single series can eat the run.
MAX_MARKETS_PER_SERIES = 2000

# Per-series budget raises for the series that settle more markets/day than
# the flat budget reaches (EXP-1275, 2026-08-12; densities measured from
# sweep_log covered-range 08-03..08-12). Post-294a5ae these series resume
# honestly, but resume cannot outrun density: KXETH/KXETHD/KXSOLD/KXSOLE
# settle ~6.7-7.8k/day against ~3.4k delivered per run (the flat budget stops
# at the first accepted window past 2,000), so their frontier recedes
# ~12-13h/day until it pins at the purge horizon (~110 days out), after which
# ~half of every day is lost permanently. KXBTC/KXBTCD sit at ~4.4-4.6k/day
# vs ~4.4k delivered — borderline, so they are included too. These series ARE
# consumed (hylshi's decided_tail_scan and price_path_signal_scan read the
# archive's crypto candles directly; cross-family-fade-replication-2026-08-03
# names the daily crypto sweep a prerequisite). 8,000 clears the densest
# measured day (7.8k) with catch-up slack and bounds one series at ~75 min;
# in steady state a caught-up series only fetches what settled since its
# watermark (~its density), so the raise costs catch-up transients, not a
# permanent 4x spend. Applies only under the DEFAULT budget — an explicit
# --max-markets (smoke tests) wins uniformly.
SERIES_MAX_MARKETS: dict[str, int] = {
    "KXBTC": 8000,
    "KXBTCD": 8000,
    "KXETH": 8000,
    "KXETHD": 8000,
    "KXSOLD": 8000,
    "KXSOLE": 8000,
}

# Families deliberately NOT swept, even though their category is allowlisted
# (EXP-1275, 2026-08-12). KXMVE* (multivariate parlays, Exotics/Mentions):
# measured close-time density ~85k-7M markets/day against ~3.4k/run — full
# keep-up would cost 20-35 HOURS of sweep per day, so coverage was <3% of
# close-time and pinned at the purge horizon regardless of budget. NOTHING
# consumes the family: zero references in strategies/, simulator/ or hyxlab/,
# and hylshi's archive_reconcile_job already excludes KXMVE% outright
# (99.97% of the raw deficit, no traded family). The 3 chronic truncators
# ate 12,939 markets = 21% of the 08-11 sweep (61,566 markets, 9.4h) for an
# archive slice nobody reads. Freeing that pays for the crypto raises above
# with room to spare. A targeted `--series` repair pass bypasses this —
# it is a default, not a ban.
EXCLUDED_SERIES_PREFIXES: tuple[str, ...] = ("KXMVE",)

# Consecutive-failure circuit breaker. Measured 2026-08-06: Kalshi's
# /markets endpoint degraded mid-run (503s, hour-long 429 storm) and the
# sweep fail-fasted through 2,569 CONSECUTIVE series errors — ~10k
# useless requests against a venue already refusing service, sharing the
# rate budget with capture daemons — then reported success to systemd.
# Organic series errors are rare (recent runs: 0) and never contiguous;
# a long unbroken error run is a venue-side outage. Aborting leaves
# every unfinished watermark untouched, so the next timer firing resumes
# exactly where this one stopped: a delayed sweep, never a lost one.
ABORT_CONSEC_ERRORS = 25
# Lock contention is not venue degradation and gets its own, much shorter
# fuse: every lock skip has ALREADY burned the full BURST_OPEN_RETRIES *
# BURST_OPEN_DELAY_S budget (300s) waiting, so 5 unbroken skips is 25
# minutes of solid contention -- a stuck reader or a broken file, not the
# seconds-long overlap the budget exists to absorb. Reusing the venue's 25
# would burn two hours before saying so.
ABORT_CONSEC_LOCK_SKIPS = 5

# Mid-series flush threshold, in buffered rows (candles + trades).
# "All writes for the series land in a single writer_burst" assumed the
# burst is short; measured 2026-08-07, KXBTC's recovery backlog buffered
# ~3.85M trade rows and that single burst held the writer lock for ~21
# min — four consecutive collect cycles skipped (a 20-min capture gap,
# QA FAIL). Every insert in the burst is idempotent (trade_id/candle-key
# dedup, OR REPLACE marks) and the watermark still advances ONLY in the
# final burst, so flushing mid-series is crash-safe: a crash between
# bursts re-fetches and dedups exactly as before. 250k rows keeps each
# burst well under collect's 300s open budget (measured ~3k rows/s
# against the 165M-row trades table).
FLUSH_ROWS = 250_000

# Resume floor for series with a watermark. Kalshi purges settled-market
# data ~60-90 days out (module docstring), so resuming from a watermark
# older than this buys nothing — the range is gone. It bounds the cost of
# a long-dormant series to a handful of empty adaptive-window probes.
# 60 matches the initial `--days 60` retention-capture depth.
PURGE_HORIZON_DAYS = 60

# Per-burst open budget, deliberately far above open_retry's 60s default.
# Releasing between series has a cost the old whole-run hold did not: a
# READER can now get in, and DuckDB excludes a writer while any reader is
# attached. A long atlas/backtest/simui read (minutes over a 4.5GB
# archive) would otherwise kill the sweep at its next flush. 5 minutes
# outlasts every standing report measured to date; past that the sweep
# exits 75 and the next timer firing resumes from the watermark, so the
# failure mode is a delayed sweep, never a lost one.
BURST_OPEN_RETRIES = 150
BURST_OPEN_DELAY_S = 2.0


# The `~N min left` line is an ETA over SERIES, and the run's cost is per
# MARKET: the 06-10 crypto ladders (KXSOLD/KXSOLE/KXETH/KXETHD ~7.2k markets
# each at a flat ~0.45 s) sit past index 3000 of ~3950, so a series-rate
# ETA read `~152 min left` at 3100/3958 on 2026-10-16 and the run took 190;
# at 400 it said 330 against 703. Three passes in a row projected a done
# time off that line and missed by 1-2h. The previous run's per-series wall
# IS recorded -- `sweep_log` has one `swept_at` per series, in the same
# sorted order every day, so consecutive differences are each series' wall
# -- and weighting the remainder by it, scaled by today's pace over the
# series already done, replayed the 10-16 run within 5-14% from index 400 on
# (at 3100: 164 vs 190 actual; at 1600: 379 vs 378). Rows this far back
# cover the previous daily run and nothing older; the largest run in the
# window is taken so a hand `--series` repair cannot stand in for the day.
# Measured 2026-10-23 (mistakes #135): a 30h window after an ABORTED day
# holds only the abort. The 10-22 run died at 07:14Z with 715 of 4,031
# series swept and the 10-21 complete run ended 17:47Z, 36h before the
# 10-23 start, so the prior was the 715-series stub: the listings line read
# `+3328 new`, the tag `3328 unpriced`, and the median those were priced at
# was the median of the alphabetically FIRST 715 series -- the cheap ones --
# so the line said `~113 min left` at 100/4043 and `~80 min left` at 3100
# with 3h+ really left, on a 16h+ catch-up run. The window is now three
# days, and the pick is the LATEST run that is at least
# `ETA_PRIOR_COMPLETE_FRAC` of the largest one in it (an abort, a hand
# repair and a tradepass are all far smaller than a day; two complete days
# differ by ~2%), falling back to the largest when no run qualifies.
ETA_PRIOR_LOOKBACK_H = 78
ETA_PRIOR_COMPLETE_FRAC = 0.8
# A gap this long between consecutive `sweep_log` rows is a run boundary; a
# single series never takes this long (the slowest, KXBTC15M, is ~50 min).
ETA_RUN_GAP_S = 2 * 3600.0
# Progress line cadence, in series.
PROGRESS_EVERY = 100


def _prior_run(store, now: datetime | None = None) -> list[tuple[str, datetime]]:
    """`(series, swept_at)` rows of the previous COMPLETE run: the latest run
    of `sweep_log` rows in the lookback (split at `ETA_RUN_GAP_S`) that is at
    least `ETA_PRIOR_COMPLETE_FRAC` of the largest run there, else the
    largest. Advisory: any read failure returns [], and the readers below
    treat [] as "no prior" rather than as a run that swept nothing."""
    conn = getattr(store, "conn", None)
    if conn is None:  # a store-like stub with no archive behind it
        return []
    now = now or datetime.now(UTC).replace(tzinfo=None)
    since = now - timedelta(hours=ETA_PRIOR_LOOKBACK_H)
    try:
        rows = conn.execute(
            "SELECT series, swept_at FROM sweep_log WHERE swept_at >= ? ORDER BY swept_at",
            [since],
        ).fetchall()
    except duckdb.Error:
        return []
    runs: list[list[tuple[str, datetime]]] = []
    cur: list[tuple[str, datetime]] = []
    for series, at in rows:
        if cur and (at - cur[-1][1]).total_seconds() > ETA_RUN_GAP_S:
            runs.append(cur)
            cur = []
        cur.append((series, at))
    if cur:
        runs.append(cur)
    if not runs:
        return []
    floor = ETA_PRIOR_COMPLETE_FRAC * max(len(r) for r in runs)
    complete = [r for r in runs if len(r) >= floor]
    return complete[-1] if complete else max(runs, key=len)


def prior_series_wall(
    store, now: datetime | None = None, run: list[tuple[str, datetime]] | None = None
) -> dict[str, float]:
    """`ticker -> wall seconds` from the previous run's `sweep_log` rows.

    The first series of a run has no wall (nothing marks the run's start),
    so it is absent and falls to the median like a new series. Advisory: a
    wrong prior costs an ETA, nothing else, so any read failure returns {}.
    `run`: the `_prior_run` rows when the caller already holds them.
    """
    run = _prior_run(store, now) if run is None else run
    return {s1: (t1 - t0).total_seconds() for (_, t0), (s1, t1) in zip(run, run[1:], strict=False)}


def listings_delta(targets: list[str], prior_run: list[tuple[str, datetime]]) -> dict | None:
    """What this run's enumeration added to and dropped from the prior run's.

    `None` without a prior run (a first run, or a `sweep_log` that could
    not be read) -- never a delta of "everything is new". The prior is the
    run's SERIES SET, not the wall dict: a run's first series has no wall
    and would otherwise read as a new listing every day.

    Measured 2026-10-21: 4,031 targets against 3,963 swept on 10-20 -- 84
    series the prior run never saw (54 of them nine international index
    families x six close/high/low/period variants, 14 MVE exotics, 13 ski-
    resort openings/snowfall, KXBRENT15M) -- a fact the progress line
    carried only as `68 unpriced` and that otherwise cost a contended 8s
    read-only attach on the live archive to recover, mid-run.
    """
    if not prior_run:
        return None
    prior = {s for s, _ in prior_run}
    want = set(targets)
    return {
        "targets": len(targets),
        "prior": len(prior),
        # When the prior run's last series was swept: the reader's one
        # chance to see that "last run" is yesterday's abort, or three days
        # old, rather than the complete run before this one (mistakes #135).
        "prior_end": max(at for _, at in prior_run),
        "new": sorted(want - prior),
        "dropped": sorted(prior - want),
    }


#: How many new/dropped tickers the listings line names before `...`.
LISTINGS_NAMED = 12


def listings_line(delta: dict | None) -> str:
    """ONE journal line at the start of a run: the enumeration against the
    prior run's. Every field of the delta perturbs it (mistakes #95)."""
    if delta is None:
        return "[sweep] listings: no prior run in the lookback"

    def names(ts: list[str]) -> str:
        if not ts:
            return ""
        shown = ", ".join(ts[:LISTINGS_NAMED])
        more = f", ... +{len(ts) - LISTINGS_NAMED}" if len(ts) > LISTINGS_NAMED else ""
        return f" ({shown}{more})"

    return (
        f"[sweep] listings: {delta['targets']} targets vs {delta['prior']} swept last run"
        f" (ended {delta['prior_end']:%m-%d %H:%M}Z)"
        f" | +{len(delta['new'])} new{names(delta['new'])}"
        f" | -{len(delta['dropped'])} dropped{names(delta['dropped'])}"
    )


def eta_minutes(
    done: list[str], remaining: list[str], elapsed_s: float, prior: dict[str, float]
) -> tuple[float, str]:
    """ETA for `remaining` plus a tag naming the estimate it is.

    With a prior: each remaining series costs its previous-run wall (the
    median prior for a series without one), scaled by today's pace --
    elapsed over the prior cost of the series already done. Without one:
    the series-rate estimate, tagged so a reader knows it is the weak one.
    """
    if not prior or not done or elapsed_s <= 0:
        rate = len(done) / elapsed_s if done and elapsed_s > 0 else 0.0
        eta = len(remaining) / rate / 60 if rate > 0 else 0.0
        return eta, "series-rate"
    med = sorted(prior.values())[len(prior) // 2]
    prior_done = sum(prior.get(t, med) for t in done)
    scale = elapsed_s / prior_done if prior_done > 0 else 1.0
    unpriced = sum(1 for t in remaining if t not in prior)
    eta = sum(prior.get(t, med) for t in remaining) * scale / 60
    return eta, f"prior-run weighted x{scale:.2f}, {unpriced} unpriced"


# Trade tapes the candles prove empty, skipped without a request; reset by
# run_sweep and published in its totals. Measured 2026-10-08: from 10-06 the
# same ~95 markets/min that drew ~45 tape 429s per run drew ~16-25k, so
# Kalshi's read limit fell below the sweep's rate. 82% of fetched tapes were
# empty, and the candles fetched first already say so for nearly all of them.
_TAPES_SKIPPED = 0


def tape_provably_empty(candles: list[dict], close_ts: int) -> bool:
    """True when the hourly candles prove a market never traded.

    Needs every candle to carry an explicit zero `volume_fp` AND the last
    candle to end at or after the close. The coverage clause is the one that
    matters: hourly candles can stop short of the close, and trades in that
    final partial hour show in no candle. Measured on the 10-07 backup over
    markets swept since 09-20: 943,224 markets were zero-volume with covering
    candles and NONE had a trade; all 186 zero-volume markets that did trade
    (556 prints) had every print after the last candle, which ended before
    the close. A missing field is not a zero: `candle_row` reads an absent
    `volume_fp` as 0.0, so an API rename must make every tape fetch again,
    not skip every tape.
    """
    if not candles:
        return False
    for c in candles:
        v = c.get("volume_fp")
        if v in (None, "") or float(v) != 0.0:
            return False
    return max(int(c["end_period_ts"]) for c in candles) >= close_ts


@contextmanager
def writer_burst(db: str, lock_file: str | None = None):
    """Hold the writer lock + DB connection for ONE short write, then release.

    2026-08-02: this sweep used to run under a unit-level
    `flock data/writer.lock python -m collector.sweep`, holding both the
    advisory lock and the DuckDB file lock for its ENTIRE multi-hour run
    while interleaving REST fetches with inserts. `hyxlab-collect` runs
    `flock -n`, so every 5-min capture cycle that landed in that window
    was DROPPED, not delayed — 421 of 3,706 cycles over the 14 days to
    08-02 (11.4%), clustered in the daily 06:10 sweep window. A dropped
    cycle is an unrecoverable hole in the 5-min tape: unlike a sweep,
    the collector cannot backfill a snapshot it never took.

    This is fix direction (a) of H1 in docs/reviews/2026-07-11-deep-review.md
    ("one rule for everybody: all writers touch the DB only in
    open -> write -> close bursts"), which poly_sweep, trades_backfill and
    signals already follow and this module never did. HTTP happens
    OUTSIDE the lock; the DB is touched once per series for ~ms.

    `lock_file` resolves at CALL time, not as a default argument: a
    default binds the module constant at definition and silently ignores
    a monkeypatched LOCK_FILE, which made the tests block on the live
    production lock.
    """
    lock_file = lock_file or LOCK_FILE
    with open(lock_file, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        note_holder(lock_file)  # name the holder BEFORE open_retry can spin for 300s
        # readers (QA/doctor/backtest) don't take the flock, so the open
        # can still lose to one — hence the widened budget above.
        #
        # `held_open`, not `open_retry`: the whole POINT of this function is
        # that the hold is short, and until 2026-09-26 nothing measured it.
        # The ledger recorded what each burst's open COST this sweep and
        # never what the burst cost the 5-minute collector — the exact
        # blindness that let a 45-minute divergence attach publish a
        # flawless `attach_wait` while starving `streamd` (mistakes #91).
        # This is the widest ladder in the repo (150 x 2.0s) over ~2 bursts
        # x ~3,709 series, so `held_s_total` in this run's `attach_wait`
        # block is the sweep's total exclusion of the archive — the number
        # the 08-02 fix claimed to shrink and never printed.
        with held_open(db, retries=BURST_OPEN_RETRIES, delay=BURST_OPEN_DELAY_S) as store:
            try:
                yield store
            finally:
                # Inside `held_open`, so the flock is released only after the
                # DuckDB handle is: unlocking first would let the next writer
                # take the advisory lock and then block on the file lock this
                # frame still holds.
                fcntl.flock(lock, fcntl.LOCK_UN)


def refresh_series(
    store: Store, session: requests.Session, series: list[dict] | None = None
) -> list[dict]:
    """Persist series metadata, return the full set.

    `series` lets the caller fetch OUTSIDE the writer burst; when omitted
    the fetch happens here, which is the convenient shape for ad-hoc use
    but holds the lock across a REST call.
    """
    if series is None:
        series = kalshi.get_series_list(session)
    store.upsert_series(
        [
            (
                "kalshi",
                s["ticker"],
                s.get("title", ""),
                s.get("category", ""),
                s.get("fee_type", ""),
                s.get("fee_multiplier"),
                s.get("frequency", ""),
            )
            for s in series
        ]
    )
    return series


def sweep_series(
    db: str,
    series_ticker: str,
    days: int,
    session: requests.Session,
    max_markets: int | None = MAX_MARKETS_PER_SERIES,
    refetch_from: datetime | None = None,
    refetch_to: datetime | None = None,
) -> tuple[int, int, bool]:
    """Capture settled markets + candles for one series since its watermark.

    Every REST call happens with NO lock and NO open connection; writes
    land in `writer_burst`s — one final burst for a normal series, plus
    intermediate bursts every FLUSH_ROWS buffered rows for giant ones
    (the single-burst shape held the lock ~21 min on KXBTC's 3.85M-row
    recovery backlog, 2026-08-07). Crash-safety: the watermark advances
    only in the final burst, and every intermediate write is idempotent,
    so a crash mid-series re-runs exactly — flushed rows dedup away.

    `refetch_from` / `refetch_to` (EXP-1287): a targeted BELOW-watermark
    repair window. The normal resume floor is `wm + 1s`, which by design
    can never see a hole BEHIND the watermark — and the pre-294a5ae
    `--days` clamp burned exactly such holes into every chronically
    truncated series (measured 08-02..08-12: ~29h each on KXBTC/KXBTCD,
    ~117-120h each on KXETH/KXETHD/KXSOLD/KXSOLE). A bare watermark
    reset is NOT a substitute for the dense series: it forces re-walking
    the interleaved already-covered ranges too (~2x the requests) and
    drags the live frontier weeks stale while the walk catches back up.
    In repair mode the watermark is READ nothing and WRITTEN never — the
    nightly incremental sweep's frontier is untouched; every insert is
    idempotent, and sweep_log records the repaired [floor, max_close]
    interval so recovery is verifiable. Bounded below by the purge
    horizon like everything else.
    """
    global _TAPES_SKIPPED
    now = datetime.now(UTC)
    repair = refetch_from is not None
    floor_ts = now - timedelta(days=days)
    ceiling_ts = now
    wm = None
    if repair:
        floor_ts = max(refetch_from, now - timedelta(days=PURGE_HORIZON_DAYS))
        if refetch_to is not None:
            ceiling_ts = min(refetch_to, now)
    else:
        with writer_burst(db) as store:
            wm = store.watermark(series_ticker)
    if wm is not None:
        # Resume from the watermark, NEVER clamped forward by the --days
        # window. The old `max(now - days, wm + 1s)` silently jumped the
        # floor past any watermark more than `days` old, so a truncated
        # dense series (daily timer: --days 2) lost the wm -> now-2d range
        # every single day while its sweep_log note promised "resume from
        # <max_close>" — measured 08-03..08-12: 0.8h-24h of close-time
        # coverage lost per day on each of the 9 chronically-truncated
        # crypto/MVE series (EXP-1271; the EXP-931 range-loss shape).
        # The only forward bound is the venue purge horizon: below that
        # the data no longer exists to fetch.
        floor_ts = max(
            wm.replace(tzinfo=UTC) + timedelta(seconds=1),
            now - timedelta(days=PURGE_HORIZON_DAYS),
        )

    markets, markets_truncated = kalshi.get_markets_ascending(
        series_ticker=series_ticker,
        status="settled",
        min_close_ts=int(floor_ts.timestamp()),
        max_close_ts=int(ceiling_ts.timestamp()),
        max_markets=max_markets,
        session=session,
        pause_s=MARKETS_PAUSE_S,
    )
    time.sleep(MARKETS_PAUSE_S)
    if not markets:
        with writer_burst(db) as store:
            store.log_sweep(series_ticker, floor_ts, None, 0, 0, "ok", "no settled markets")
        return 0, 0, False

    infos = [kalshi.to_market_info(m) for m in markets]
    candle_rows: list[tuple] = []
    trade_rows: list[tuple] = []
    swept: list[tuple[str, int, str]] = []
    max_close = floor_ts
    n_candles = 0

    def flush_buffers(store: Store) -> None:
        nonlocal n_candles
        n_candles += store.insert_candles(candle_rows)
        store.insert_trades(trade_rows)
        for ticker, n_trades, status in swept:
            store.mark_trades_swept(ticker, n_trades, status)
        candle_rows.clear()
        trade_rows.clear()
        swept.clear()

    for m in markets:
        open_ts = _ts(m.get("open_time"))
        close_ts = _ts(m.get("close_time"))
        if open_ts is None or close_ts is None:
            # The watermark may advance past this market via its
            # siblings' closes — without a log line that's a permanent
            # invisible hole in a system built on marking what it missed.
            print(f"[sweep] {m.get('ticker', '?')} skipped: missing open/close time", flush=True)
            continue
        try:
            candles = kalshi.get_candlesticks(
                series_ticker,
                m["ticker"],
                open_ts,
                close_ts,
                60,
                session=session,
                pause_s=CANDLES_PAUSE_S,
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                retry_after = e.response.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 5.0)
                candles = kalshi.get_candlesticks(
                    series_ticker,
                    m["ticker"],
                    open_ts,
                    close_ts,
                    60,
                    session=session,
                    pause_s=CANDLES_PAUSE_S,
                )
            else:
                raise
        candle_rows.extend(kalshi.candle_row(series_ticker, m, c, 3600) for c in candles)
        if tape_provably_empty(candles, close_ts):
            # No request: the candles already prove the tape is empty.
            # Recorded under its own status so the skip stays auditable.
            _TAPES_SKIPPED += 1
            swept.append((m["ticker"], 0, "candle_empty"))
        else:
            # Trade tape rides along (B3.5): prints purge on the same
            # retention clock as candles, so capture them at first sight.
            try:
                raw, truncated = kalshi.get_trades(
                    m["ticker"], session=session, page_pause_s=CANDLES_PAUSE_S
                )
                rows = [kalshi.trade_row(t) for t in raw]
                trade_rows.extend(rows)
                status = "truncated" if truncated else ("ok" if rows else "empty")
                swept.append((m["ticker"], len(rows), status))
            except requests.HTTPError as e:
                # Stays unmarked here (watermark advances past it regardless,
                # so a later sweep won't retry) — hyxlab-tradepass.timer's daily
                # retro-pass is what actually catches it.
                code = e.response.status_code if e.response is not None else "?"
                print(f"[sweep] {m.get('ticker', '?')} trade tape fetch HTTP {code}", flush=True)
        close_dt = datetime.fromtimestamp(close_ts, tz=UTC)
        max_close = max(max_close, close_dt)
        if len(candle_rows) + len(trade_rows) >= FLUSH_ROWS:
            print(
                f"[sweep] {series_ticker} mid-series flush at "
                f"{len(candle_rows)} candles + {len(trade_rows)} trades",
                flush=True,
            )
            with writer_burst(db) as store:
                flush_buffers(store)
        time.sleep(CANDLES_PAUSE_S)

    with writer_burst(db) as store:
        store.upsert_markets(infos)
        flush_buffers(store)
        if not repair:
            # A repair pass must NEVER move the watermark: forward would
            # skip the un-repaired remainder for the nightly sweep;
            # backward would make the nightly re-walk already-covered
            # range. Its coverage lives in sweep_log, not the watermark.
            store.set_watermark(series_ticker, max_close)
        # A truncated sweep is NOT 'ok'. It used to be — the only trace was a
        # print, which is how "crypto == BNB only" survived into an
        # experiment's premises (EXP-931). status='truncated' puts it in
        # sweep_log where doctor(), QA and any later analyst must trip over it.
        note = ""
        if markets_truncated:
            note = f"budget {max_markets} markets reached; resume from {max_close:%Y-%m-%dT%H:%M}Z"
        if repair:
            resume = (
                f"; re-run with --refetch-from {max_close:%Y-%m-%dT%H:%M:%S}Z"
                if markets_truncated
                else ""
            )
            note = f"repair pass (watermark untouched){resume}"
        store.log_sweep(
            series_ticker,
            floor_ts,
            max_close,
            len(markets),
            n_candles,
            "truncated" if markets_truncated else "ok",
            note,
        )
    return len(markets), n_candles, markets_truncated


def _ts(v: str | None) -> int | None:
    if not v:
        return None
    return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())


def _utc_arg(v: str) -> datetime:
    """Parse a CLI ISO timestamp; naive values are UTC (all archive
    timestamps — sweep_log, watermarks, close_time — are UTC)."""
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def run_sweep(
    db: str,
    days: int,
    categories: list[str],
    session: requests.Session | None = None,
    limit: int | None = None,
    only_series: list[str] | None = None,
    max_markets: int | None = MAX_MARKETS_PER_SERIES,
    refetch_from: datetime | None = None,
    refetch_to: datetime | None = None,
) -> dict:
    if refetch_from is not None and not only_series:
        # A below-watermark refetch is a scalpel: it re-requests a range
        # the watermark says is done, so pointing it at whole categories
        # would re-walk the archive. Explicit series only.
        raise ValueError("--refetch-from requires --series (targeted repair only)")
    sess = session or requests.Session()
    series = kalshi.get_series_list(sess)  # HTTP outside the lock
    with writer_burst(db) as store:
        all_series = refresh_series(store, sess, series=series)
        prior_run = _prior_run(store)
        eta_prior = prior_series_wall(store, run=prior_run)
    targets = [s["ticker"] for s in all_series if s.get("category") in categories]
    if only_series:
        # Targeted repair pass: the alphabetical whole-category order is what
        # starved KXBTC*/KXETH*/KXSOL* (EXP-931), so recovering a specific
        # family must not have to wait its turn behind 200 unrelated series.
        # Deliberately NOT filtered by EXCLUDED_SERIES_PREFIXES: naming a
        # series explicitly is the bypass.
        wanted = set(only_series)
        targets = [t for t in targets if t in wanted]
    else:
        targets = [t for t in targets if not t.startswith(EXCLUDED_SERIES_PREFIXES)]
    targets.sort()
    if limit:
        targets = targets[:limit]
    listings = listings_delta(targets, prior_run)
    print(listings_line(listings), flush=True)
    global _TAPES_SKIPPED
    _TAPES_SKIPPED = 0
    totals = {
        "series": len(targets),
        # The enumeration against the prior run's series set, or None on a
        # first run: the listings term of the (23) by-class model, legible
        # on the done line instead of behind an attach on the live file.
        "new_series": None if listings is None else len(listings["new"]),
        "dropped_series": None if listings is None else len(listings["dropped"]),
        "markets": 0,
        "candles": 0,
        "errors": 0,
        "lock_skips": 0,
        # Tickers behind `lock_skips`, popped by main and written to
        # sweep_log in the CLOSING burst. A skip cannot log itself -- the
        # burst it needs is the one that just failed -- but by the end of
        # the run the reader that caused it is almost always gone, so the
        # record is merely DEFERRED, not impossible (which is why this
        # needs no `collect_skips.jsonl`-style sidecar of its own).
        # Without it a partial sweep would report green with a count that
        # lives only in the journal: mistake #46, a measurement no
        # instrument reads. `doctor` already prints sweep_log by status.
        "lock_skipped": [],
        "truncated": 0,
        "aborted": False,
    }
    t0 = time.monotonic()
    consec_errors = 0
    consec_lock_skips = 0
    # ticker -> (wall seconds, markets). The run's budget (`qa.BATCH_RUN_BUDGET_H`,
    # 12.5h) is spent per SERIES and nothing recorded which: the journal has one
    # line per 100 series and `sweep_log` a timestamp per series, so a breach
    # could be attributed only by hand-differencing 3,900 `swept_at` values. On
    # 2026-10-16 that hand read found the 10-15 run's 655 min to be 61% crypto
    # ladders at a flat ~0.45 s/market -- and ONE series, KXBTC15M, at 47 min
    # for 91 markets (31 s/market: ~3.5M trade prints a day), 7% of the wall
    # for 0.15% of the markets, every day for a week. The breach the budget
    # check exists to print needs that table on the same journal, so the loop
    # keeps it and `wall_attribution_line` renders it.
    series_wall: dict[str, tuple[float, int]] = {}
    for i, ticker in enumerate(targets):
        s0 = time.monotonic()
        n_m = 0
        # Per-series raises ride the DEFAULT budget only; an explicit
        # --max-markets (or 0 = unbounded) wins uniformly, so a --limit
        # smoke test never burns an hour draining 8,000 KXETH markets.
        budget = max_markets
        if max_markets == MAX_MARKETS_PER_SERIES:
            budget = SERIES_MAX_MARKETS.get(ticker, max_markets)
        try:
            repair_kw = (
                {"refetch_from": refetch_from, "refetch_to": refetch_to}
                if refetch_from is not None
                else {}
            )
            n_m, n_c, truncated = sweep_series(db, ticker, days, sess, budget, **repair_kw)
            totals["markets"] += n_m
            totals["candles"] += n_c
            totals["truncated"] += int(truncated)
            consec_errors = 0
            consec_lock_skips = 0
            if truncated:
                print(
                    f"[sweep] {ticker} TRUNCATED at {n_m} markets"
                    f" (per-series budget); logged non-ok, resumes next run",
                    flush=True,
                )
        except duckdb.Error as e:
            # A lost writer open is the ONE failure this loop used to let
            # kill the whole run: `writer_burst` raised straight through
            # run_sweep and main, and the 09-10 06:10Z sweep died at series
            # ~600 of 3,656 because a reader held data/hyxlab.duckdb past
            # the 300s budget. Nothing was corrupt and nothing was lost --
            # the watermark advances only in the final burst, so the series
            # simply re-runs -- yet 3,000 untouched series waited a day for
            # a contention that had already cleared. `hyxlab-collect` hits
            # the identical condition and treats it as a SKIP; this is the
            # same rule for the other writer. Deliberately no sweep_log row:
            # writing one needs the burst that just failed.
            totals["lock_skips"] += 1
            totals["lock_skipped"].append((ticker, str(e)[:200]))
            consec_lock_skips += 1
            print(
                f"[sweep] {ticker} SKIPPED: archive busy"
                f" ({type(e).__name__}: {str(e)[:120]}) —"
                f" watermark unmoved, next run resumes",
                flush=True,
            )
            if consec_lock_skips >= ABORT_CONSEC_LOCK_SKIPS:
                totals["aborted"] = True
                print(
                    f"[sweep] ABORT after {consec_lock_skips} consecutive"
                    f" archive-busy skips at {ticker} — the DB is held, not"
                    f" contended; watermarks intact, next run resumes",
                    flush=True,
                )
                break
        except requests.RequestException as e:
            totals["errors"] += 1
            consec_errors += 1
            try:
                with writer_burst(db) as store:
                    store.log_sweep(ticker, None, None, 0, 0, "error", str(e)[:200])
            except duckdb.Error:
                # The breaker must not be disarmed by the archive being
                # busy: losing the audit row is worse than not counting
                # this failure, but letting the open kill the run is worst.
                totals["lock_skips"] += 1
            if consec_errors >= ABORT_CONSEC_ERRORS:
                totals["aborted"] = True
                print(
                    f"[sweep] ABORT after {consec_errors} consecutive series"
                    f" errors at {ticker} ({str(e)[:120]}) — venue degraded;"
                    f" watermarks intact, next run resumes",
                    flush=True,
                )
                break
        series_wall[ticker] = (time.monotonic() - s0, n_m)
        if (i + 1) % PROGRESS_EVERY == 0:
            eta_min, eta_tag = eta_minutes(
                targets[: i + 1], targets[i + 1 :], time.monotonic() - t0, eta_prior
            )
            print(
                f"[sweep] {i + 1}/{len(targets)} series | "
                f"{totals['markets']} markets, {totals['candles']} candles, "
                f"{totals['errors']} errors, {totals['truncated']} truncated"
                f" | ~{eta_min:.0f} min left ({eta_tag})"
                f" | {class_terms_tag(wall_by_class(series_wall))}"
            )
    totals["tapes_skipped"] = _TAPES_SKIPPED
    totals["elapsed_min"] = round((time.monotonic() - t0) / 60, 1)
    totals["slowest"] = slowest_series(series_wall)
    totals["by_class"] = wall_by_class(series_wall)
    print(wall_attribution_line(series_wall, time.monotonic() - t0), flush=True)
    print(wall_class_line(totals["by_class"], time.monotonic() - t0), flush=True)
    return totals


#: How many series the attribution line names. Five covered 31% of the 10-15
#: run's wall (KXBTC15M, KXETHD, KXSOLD, KXSOLE, KXETH) and every one of the
#: series whose per-market cost was not the flat ~0.45s.
ATTRIBUTION_TOP_N = 5


def slowest_series(series_wall: dict[str, tuple[float, int]]) -> list[tuple[str, float, int]]:
    """`(ticker, wall_s, markets)` for the `ATTRIBUTION_TOP_N` longest series,
    longest first -- the done dict's copy of the line below, so an analyst
    reading `totals` is not sent back to the journal (mistakes #46)."""
    ranked = sorted(series_wall.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(t, round(w, 1), n) for t, (w, n) in ranked[:ATTRIBUTION_TOP_N]]


def wall_attribution_line(series_wall: dict[str, tuple[float, int]], elapsed_s: float) -> str:
    """ONE journal line: where the run's wall clock went, by series.

    Each entry prints minutes, markets and seconds per market, because the
    per-market rate is the number that separates "many markets" (a listing
    count, which only a category decision changes) from "expensive markets"
    (a per-market cost, which a tape or candle rule changes): the 10-15 run's
    top five were 0.43-0.45 s/market except the one at 30.9. The share is of
    the whole run's elapsed, not of the series sum, so the line is honest
    about overhead outside the loop (the listing refresh, the closing burst).
    """
    if not series_wall:
        return "[sweep] wall attribution: no series ran"
    top = slowest_series(series_wall)
    top_s = sum(w for _, w, _ in top)
    share = 100.0 * top_s / elapsed_s if elapsed_s > 0 else 0.0
    parts = ", ".join(
        f"{t} {w / 60:.1f}min/{n} mkts ({w / max(n, 1):.1f} s/mkt)" for t, w, n in top
    )
    return (
        f"[sweep] wall attribution: top {len(top)} of {len(series_wall)} series"
        f" = {top_s / 60:.1f} of {elapsed_s / 60:.1f} min ({share:.0f}%): {parts}"
    )


#: A series with at least this many markets is a LADDER: its wall is the
#: listing count times a flat ~0.45 s/market (10-13..10-17: 28-33 ladders,
#: 0.456-0.469 s/mkt), so it grows with listings and only a category
#: decision changes it.
LADDER_MIN_MARKETS = 200
#: A sub-ladder series at this per-market cost is DENSE: its wall is the
#: trade tape, not the listing, and only a per-market rule changes it.
#: Measured 10-13..10-17 over the ~100-130 sub-ladder series with >= 10
#: markets: KXBTC15M at 27.8-32.0 s/mkt, the next at 4.7-6.1 (KXGOLD15M,
#: KXETH15M), everything else below -- a gap of 20 s, so 10.0 names the class
#: on every run. The class is a RATE, not a wall: KXSOL15M swept 0 markets on
#: 10-16 and 192 on 10-17 (606 s at 3.2 s/mkt), and a wall threshold would
#: have classed that catch-up as dense for one day.
DENSE_S_PER_MKT = 10.0
#: Below this many markets a series' per-market rate is one retried request
#: away from any value, so it is small whatever it cost.
DENSE_MIN_MARKETS = 10


def wall_by_class(series_wall: dict[str, tuple[float, int]]) -> dict[str, dict]:
    """The run's wall in the three terms the budget model is written in.

    `qa.BATCH_RUN_BUDGET_H` headroom is `budget - small - dense` divided
    by the ladder rate (data-pipeline.md), and until 2026-10-17 those three
    numbers came from hand-differencing `sweep_log` -- the 10-15 linear fit
    `97 + 8.39 x markets/1k` lumped the ~150-170 min flat block into its
    slope and under-predicted every run with fewer listings (three positive
    residuals, +22/+29/+32 min, all of them the intercept). Each class:
    `n` series, `wall_s`, `markets`, and `tickers` for the dense class only
    (it is one or two names and the line should say which).
    """
    out = {k: {"n": 0, "wall_s": 0.0, "markets": 0} for k in ("ladder", "dense", "small")}
    out["dense"]["tickers"] = []
    for t, (w, n) in series_wall.items():
        if n >= LADDER_MIN_MARKETS:
            k = "ladder"
        elif n >= DENSE_MIN_MARKETS and w / n >= DENSE_S_PER_MKT:
            k = "dense"
            out["dense"]["tickers"].append(t)
        else:
            k = "small"
        out[k]["n"] += 1
        out[k]["wall_s"] += w
        out[k]["markets"] += n
    for c in out.values():
        c["wall_s"] = round(c["wall_s"], 1)
    out["dense"]["tickers"].sort()
    return out


def wall_class_line(by_class: dict[str, dict], elapsed_s: float) -> str:
    """ONE journal line for `wall_by_class`: ladders and dense at s/mkt (the
    rate a listing change or a per-market rule moves), small at s/series
    (its markets are ~1 each, so the per-series cost is the flat block)."""
    lad, den, sm = by_class["ladder"], by_class["dense"], by_class["small"]
    loop_s = lad["wall_s"] + den["wall_s"] + sm["wall_s"]
    names = f" [{', '.join(den['tickers'])}]" if den["tickers"] else ""
    return (
        f"[sweep] wall by class: ladders (>={LADDER_MIN_MARKETS} mkts) {lad['n']} series"
        f" {lad['wall_s'] / 60:.1f} min/{lad['markets']} mkts"
        f" ({lad['wall_s'] / max(lad['markets'], 1):.3f} s/mkt);"
        f" dense (>={DENSE_S_PER_MKT:.0f} s/mkt) {den['n']} series {den['wall_s'] / 60:.1f} min/"
        f"{den['markets']} mkts ({den['wall_s'] / max(den['markets'], 1):.1f} s/mkt){names};"
        f" small {sm['n']} series {sm['wall_s'] / 60:.1f} min/{sm['markets']} mkts"
        f" ({sm['wall_s'] / max(sm['n'], 1):.2f} s/series);"
        f" loop {loop_s / 60:.1f} of {elapsed_s / 60:.1f} min"
    )


def class_terms_tag(by_class: dict[str, dict]) -> str:
    """The by-class terms of the series DONE so far, for the progress line.

    The `~N min left` ETA prices the remainder at yesterday's walls scaled
    by today's pace, so a day with more LISTINGS reads as a slower day
    (2026-10-20: x1.19 at 3100 while the tail ran 10-19's pace to the
    minute; mistakes #130). The scale cannot say which; this can, read
    against yesterday's line at the same index: ladder markets up at the
    same s/mkt is listings, the same markets at a higher s/mkt is pace.
    """
    lad, den, sm = by_class["ladder"], by_class["dense"], by_class["small"]
    return (
        f"done: ladders {lad['n']}/{lad['markets']} mkts"
        f" {lad['wall_s'] / max(lad['markets'], 1):.3f} s/mkt,"
        f" small {sm['wall_s'] / 60:.0f} min, dense {den['wall_s'] / 60:.0f} min"
    )


def doctor(store: Store) -> None:
    """Archive health at a glance."""
    print(json.dumps(store.counts(), indent=1))
    mv = store.mirror_violations()
    print(f"kalshi mirror violations: {mv}" + (" <-- PIPELINE CORRUPTION" if mv else ""))
    stream_db = Path("data/hyxstream.duckdb")
    if stream_db.exists():
        size_mb = stream_db.stat().st_size / 1e6
        try:
            with held_duck(str(stream_db), read_only=True) as sconn:
                counts = {
                    t: sconn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                    for t in ("book_events", "stream_trades", "stream_gaps")
                }
            print(f"stream archive: {counts} ({size_mb:.0f} MB)")
        except Exception as exc:  # daemon mid-flush holds the writer lock
            print(f"stream archive: busy ({type(exc).__name__}) ({size_mb:.0f} MB)")
    rows = store.conn.execute(
        "SELECT status, count(*) FROM sweep_log"
        " WHERE swept_at > now() - INTERVAL 2 DAY GROUP BY status"
    ).fetchall()
    print("sweep_log (48h):", dict(rows))
    rows = store.conn.execute(
        "SELECT s.category, count(DISTINCT m.market_id) AS markets"
        " FROM markets m JOIN series s ON s.ticker = m.series AND s.venue = m.venue"
        " GROUP BY s.category ORDER BY markets DESC"
    ).fetchall()
    print("archived markets by category:")
    for cat, n in rows:
        print(f"  {cat or '?'}: {n}")


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab exchange-wide archival sweep")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--categories", nargs="*", default=DEFAULT_CATEGORIES)
    ap.add_argument("--limit", type=int, default=None, help="max series (smoke tests)")
    ap.add_argument(
        "--series", nargs="*", default=None, help="only these series tickers (repair pass)"
    )
    ap.add_argument(
        "--max-markets",
        type=int,
        default=MAX_MARKETS_PER_SERIES,
        help="per-series, per-run market budget (0 = unbounded)",
    )
    ap.add_argument(
        "--refetch-from",
        type=_utc_arg,
        default=None,
        help="ISO timestamp: repair floor BELOW the watermark (requires --series;"
        " watermark is neither consulted nor advanced)",
    )
    ap.add_argument(
        "--refetch-to",
        type=_utc_arg,
        default=None,
        help="ISO timestamp: repair ceiling (default now); bounds the hole"
        " so a repair never re-walks covered range above it",
    )
    ap.add_argument("--doctor", action="store_true", help="print archive health and exit")
    args = ap.parse_args()

    if args.doctor:
        # `held_open` in place of the hand-rolled 5 x 2s loop this branch
        # carried: same ladder (a writer's flush burst lasts ~seconds), but
        # it is now a LEDGERED attach whose hold is measured. --doctor
        # reads the whole archive -- the markets GROUP BY alone is 486k
        # rows -- while the 5-minute collector wants to write it, and that
        # hold was charged to nobody.
        opened = False
        try:
            with held_open(args.db, read_only=True, retries=5) as store:
                # Set INSIDE, so the handler below can tell "could not get
                # in" from "got in and the read failed". Wrapping the whole
                # body in one `except duckdb.Error` would report a
                # corrupt-archive read error as a busy writer and retry
                # forever on a timer.
                opened = True
                doctor(store)
        except duckdb.Error:
            if opened:
                raise
            # Nonzero so systemd records a failed run instead of a silent
            # no-op success only QA would notice 36h later.
            print("archive busy (writer active); try again in a few seconds")
            sys.exit(75)  # EX_TEMPFAIL
        return

    # The sweep itself never holds a connection between bursts, so there
    # is nothing to open here — `writer_burst` opens and closes per write.
    # Single-INSTANCE guard (`hyxlab.lockid`), job-scoped and NOT the
    # writer lock: it is held for the whole multi-hour run and blocks no
    # collector. Was `<db>.lock`, which only stayed correct while the
    # sweep was the one job that took it.
    lock, why = instance_lock_or_reason("sweep")
    if lock is None:
        print(f"[sweep] {why}; aborting")
        sys.exit(75)
    # Scope the attach ledger to THIS run: `writer_burst` spends the 300s
    # `BURST_OPEN_RETRIES` ladder once per burst, ~2 bursts per series over
    # ~3,709 series, and until now the only thing it ever published about
    # that budget was `lock_skips` -- a counter that increments ONLY when
    # the budget is exhausted (mistakes #72). Thirty days of `lock_skips: 0`
    # therefore says nothing about whether the ladder is spending 8ms or
    # 290s, and the one run that went over (09-10 06:10Z, dead at series
    # ~600 of 3,656) had no warning in front of it by construction.
    reset_attach_waits()
    # Same scoping, same reason, for the HTTP ladders -- and this one had no
    # reader at all. `collector.breadth`, a 3.5-second oneshot, publishes four
    # retry fields for a population of 19 retried 429s per 48h. This run
    # drives every multi-request loop in the Kalshi client across ~3,700
    # series for ~10.5 hours and published NOTHING about any of them, while
    # its journal carried ~42 trade-tape 429s per run, every run, for the 17
    # days anyone has looked (2026-09-07..23, 714 of them). The ladder that
    # fires most is again the one with no instrument on it (#71, #79).
    kalshi.reset_retry_counts()
    try:
        totals = run_sweep(
            args.db,
            args.days,
            args.categories,
            limit=args.limit,
            only_series=args.series,
            max_markets=args.max_markets or None,
            refetch_from=args.refetch_from,
            refetch_to=args.refetch_to,
        )
        skipped = totals.pop("lock_skipped", [])
        # `rows=False`: the per-attach sample is thousands of rows here and
        # the margin is the whole message. Printed on its own line, before
        # the closing census -- which takes a burst of its own and would
        # otherwise be the last word on a run it did not measure.
        wait = attach_wait_block(rows=False)
        print(f"[sweep] done: {totals}")
        if wait is not None:
            # `attach_budget_s`, never `RETRIES * DELAY`: 150 attempts is
            # 149 sleeps, so the hand-multiplied 300 is 2s wider than the
            # ladder the fraction is a share of -- printing it beside
            # `budget_frac_max` would make the two disagree (#71's atlas
            # literal, one module over).
            budget = attach_budget_s(BURST_OPEN_RETRIES, BURST_OPEN_DELAY_S)
            # SLEPT, not elapsed, and the two printed apart (mistakes #78).
            # The first production line, 09-22 11:52Z, read "96.3s spent
            # waiting in total" over 7,483 attaches. A successful open of the
            # 20GB archive costs 7.2ms median (measured 2026-09-23, 30
            # read-only opens, first excluded; the read-write path here also
            # runs `_SCHEMA` and costs MORE), so >= 53.8s of that 96.3s --
            # at least 56%, plausibly all of it -- was this sweep opening its
            # own database, not waiting for anyone. `contended_n` is the
            # count a max and a total cannot give: one 96s block and 48 x 2s
            # are the same two numbers, and only one of them is a reason to
            # touch the budget.
            # `attach_wait_line`, not a format string here: this line IS
            # the sweep's artifact (no JSON is written), so a field the
            # block gains and the line does not is a measurement taken and
            # thrown away -- which is exactly what happened to the hold
            # half on 09-27 06:10Z.
            print(attach_wait_line(wait, prefix="[sweep]", budget_s=budget), flush=True)
        # WORST BUDGET, NOT THE MEAN, and a fraction beside every count: this
        # run holds thousands of per-request transport budgets, so
        # `retries / TRANSPORT_TRIES` -- breadth's `walk_budget_frac`, correct
        # there because `fetch_universe` walks once -- would be a sum over
        # budgets divided by one budget's denominator. `unretried` is printed
        # apart from `total` because it is not a retry: it is the trade-tape
        # 429 class, which has no ladder and is recovered by
        # `hyxlab-tradepass` instead (670 of 714 re-fetched as of 2026-09-23,
        # the rest closed within 3 days). A rise in it is a reason to look at
        # tradepass's backlog, not at this ladder.
        counts = kalshi.retry_counts()
        print(
            f"[sweep] http_retries: {counts['total']} spent"
            f" (transport {counts['transport']}, gateway {counts['gateway']},"
            f" rate_limit {counts['rate_limit']}), worst walk"
            f" {kalshi.transport_budget_frac():.3f} of its transport budget,"
            f" worst request {kalshi.rate_limit_budget_frac():.3f} of its 429"
            f" budget, {counts['rate_limit_unretried']} unretried 429s"
            f" (trade tape, no ladder)",
            flush=True,
        )
        try:
            with writer_burst(args.db) as store:
                for ticker, why in skipped:
                    store.log_sweep(ticker, None, None, 0, 0, "busy", why)
                print(f"[sweep] db={store.counts()}")
        except duckdb.Error as e:
            # A closing-census open that loses to a reader must not retitle
            # a completed run as a failed one — every series is already
            # persisted at this point and the census is a journal nicety.
            print(f"[sweep] db=busy ({type(e).__name__}: {str(e)[:120]})", flush=True)
        if totals.get("aborted"):
            # Nonzero so systemd records a failed run — today's outage run
            # said "Finished" with an 82%-error pass only the journal knew
            # about. EX_TEMPFAIL matches the lock/reader-contention exits:
            # the next timer firing resumes from the watermarks.
            sys.exit(75)
    finally:
        lock.close()


if __name__ == "__main__":
    main()
