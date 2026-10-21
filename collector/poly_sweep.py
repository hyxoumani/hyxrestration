"""Polymarket archival sweep: metadata, volume series, price history,
trade tails — everything the venue still serves (probed 2026-07-07).

    python -m collector.poly_sweep [--db ...] [--min-volume 10000] [--limit N]

Retention facts driving the design:
- prices-history: ~60-day ROLLING window; closed markets purge ~1-2
  months after close → capture incrementally (per-token watermark),
  daily, before it rolls off.
- data-api trades: hard cap of the LAST 3,000 prints per market — a
  tail sample, never a tape. The forward tape is the WS stream; these
  tails fill in low-volume markets completely and busy ones partially.
- Gamma metadata + volumeNum/liquidityNum: full history, cheap — swept
  volume-desc down to --min-volume, plus recently-closed markets for
  settlement results.

Plays nice with the 5-min collector: REST fetching holds no lock; DB is
touched in short flock-guarded bursts (same pattern as trades_backfill).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import requests

from collector.venues import polymarket as poly
from hyxlab.lockid import instance_lock_or_reason, note_holder
from hyxlab.store import (
    attach_wait_block,
    attach_wait_line,
    charge_hold,
    open_retry,
    reset_attach_waits,
)

FLUSH_MARKETS = 25
LOCK_FILE = "data/writer.lock"
REQUEST_PAUSE_S = 0.25  # CLOB/data-api pacing

#: DuckDB worker threads for this sweep's writes. The default (one per core,
#: 24 here) is what sized the 2026-10-11 anon spikes: a 25-market trades flush
#: dedups against every poly print (the market_id scope's BETWEEN spans the
#: whole random-hex range, so it never prunes), and each scan thread holds its
#: own buffers OUTSIDE `memory_limit`. Measured on the 10-10 backup, one real
#: 43,966-print batch: uncapped 7.7 GiB / 8.9s; capped at 2G (limit 1 GiB) at 24
#: threads, OOM-killed; at 4 threads 855 MiB under a 1G cap in 6.4-6.8s. 2
#: threads is 725 MiB but 9.1s. So 4 is the fastest setting as well as a small one.
DUCK_THREADS = 4


def _open(db: str):
    """`open_retry` + the thread pin, closing on any escape (#97: a wrapper
    that owns the connection between the open and its caller leaks the
    writer lock if the tuning raises)."""
    store = open_retry(db)  # readers (QA/doctor/backtest) don't take the flock
    try:
        store.conn.execute(f"SET threads = {DUCK_THREADS}")
    except BaseException:
        store.close()
        raise
    return store


def _flush(db: str, batch: dict) -> None:
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        note_holder(LOCK_FILE)
        store = _open(db)
        try:
            # `charge_hold`: `_open` is a wrapper (open + thread pin) that
            # hands the connection here, so THIS body is the hold -- on the
            # 5-minute collector's file, one burst per batch across a 15-17h
            # run, summed on the `[poly] attach_wait:` line after `done:`.
            # It closes the store in its own `finally` (close, THEN clock).
            with charge_hold(store.close):
                if batch["infos"]:
                    store.upsert_markets(batch["infos"])
                if batch["stats"]:
                    store.insert_poly_stats(batch["stats"])
                if batch["prices"]:
                    batch["n_prices"] += store.insert_poly_prices(batch["prices"])
                if batch["trades"]:
                    batch["n_trades"] += store.insert_trades(batch["trades"])
                # Carried in the batch, not drained from the client here: a
                # failed flush holds the batch and retries it, and a stop
                # drained inside the failing burst would be the one row the
                # retry no longer has -- archiving a short tail with no record
                # that it was short, which is precisely the blindness this
                # table removes.
                if batch["tail_stops"]:
                    batch["n_tail_stops"] += store.insert_poly_tail_stops(batch["tail_stops"])
                if batch.get("swept"):
                    store.insert_poly_sweep_log(batch["swept"])
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    batch["infos"], batch["stats"], batch["prices"], batch["trades"] = [], [], [], []
    batch["tail_stops"], batch["swept"] = [], []


#: Markets between progress lines; also the window the trailing rate is read over.
PROGRESS_EVERY = 200
#: `poly_sweep_log` rows this far back are the previous daily run (04:15Z,
#: ~17h) and nothing older; a market's LATEST wall in the window is its prior.
ETA_PRIOR_LOOKBACK_H = 30
#: A `poly_sweep_log` wall above this is a FETCH (at least one HTTP round
#: trip); below it, the skip branch (median 6e-6 s on the 2026-10-20 backup).
#: The log does not record which branch ran, so this is how a prior run's
#: first-seen-closed fetches are told apart inside today's skip set.
SKIP_WALL_MAX_S = 0.01


def prior_market_wall(store, now: datetime | None = None) -> dict[str, float]:
    """`market_id -> wall seconds` from the previous run's `poly_sweep_log`.

    Advisory: a wrong prior costs an ETA, nothing else, so a store with no
    archive behind it or any read failure returns {} and the line falls back
    to the mean rate, tagged.
    """
    conn = getattr(store, "conn", None)
    if conn is None:
        return {}
    now = now or datetime.now(UTC).replace(tzinfo=None)
    since = now - timedelta(hours=ETA_PRIOR_LOOKBACK_H)
    try:
        rows = conn.execute(
            "SELECT market_id, arg_max(wall_s, swept_at) FROM poly_sweep_log"
            " WHERE swept_at >= ? GROUP BY market_id",
            [since],
        ).fetchall()
    except duckdb.Error:
        return {}
    return {m: float(w) for m, w in rows}


def eta_weighted(
    done: list[str],
    remaining: list[str],
    elapsed_s: float,
    prior: dict[str, float],
    skip: set[str] | frozenset[str] = frozenset(),
    closed: set[str] | frozenset[str] = frozenset(),
) -> tuple[float, str]:
    """ETA in minutes for `remaining` plus a tag naming the estimate it is.

    With a prior: each remaining market costs its previous-run wall, scaled
    by today's pace -- elapsed over the prior cost of the markets already
    done, which absorbs the flushes and the day's rate limit (a flat x1.37
    across the 2026-10-20 run: the per-market wall is 73% of the loop).
    Without one: the mean rate since the start, tagged `mean rate` so a
    reader knows it carries the opening (2026-10-17: `~404` at 11,600 for
    328 actual) and, past the trade-heavy mid-rank, the other way
    (2026-10-18: `~509` at 10,000 for ~425).

    A market WITHOUT a prior is new to the enumeration, so no prior run set
    its result and today's run fetches it. It costs the median prior of the
    markets today FETCHES -- `skip`, the markets this run skips, is excluded
    from that population, because a skip's prior is the microseconds the
    skip branch took (median 1.8e-5 s over 18,458 priors on the 2026-10-20
    backup, 12,475 of them skips) and prices 1,151 new markets at nothing:
    ~40 min of a 478-min line (median fetched prior 1.56 s, x1.37).

    A market IN `skip` costs 0.0 whatever its prior says, on both sides of
    the scale. A market that resolved between the two runs was FETCHED by
    the prior run and is skipped by this one, so its prior is a fetch wall
    (2026-10-20: 1,671 such markets, median 5.3 s, 132 min unscaled);
    charged at that prior the line read `~125 min left` at 11,600 for 75
    actual, and on the done side the same priors inflated the pace
    denominator and walked the scale 1.44 -> 1.24 through the closed
    segment. The skip legs' flush time (15-19 s per 200) rides in the
    scale, so a skip-heavy remainder reads a few minutes low, not an hour
    high.

    An unpriced market in `closed` is a first-seen-closed fetch -- prices
    to the close plus the whole tail from offset 0 -- and costs the median
    prior of the markets that WERE that yesterday: today's skip members
    whose prior is a fetch wall (above `SKIP_WALL_MAX_S`). 2026-10-20:
    1,056 such markets ran 88.8 min, 5.0 s each, against a fetched median
    of 1.56 s dominated by the open segment; that pool's median was 5.27 s.
    With no such pool (the first run with a skip set) it falls back to the
    fetched median.
    """
    if not prior or not done or elapsed_s <= 0:
        mean = len(remaining) * elapsed_s / len(done) / 60 if done and elapsed_s > 0 else 0.0
        return mean, "mean rate"
    fetched = sorted(w for m, w in prior.items() if m not in skip) or sorted(prior.values())
    med = fetched[len(fetched) // 2]
    closed_fetched = sorted(w for m, w in prior.items() if m in skip and w > SKIP_WALL_MAX_S)
    closed_med = closed_fetched[len(closed_fetched) // 2] if closed_fetched else med

    def cost(m: str) -> float:
        if m in skip:
            return 0.0
        if m in prior:
            return prior[m]
        return closed_med if m in closed else med

    prior_done = sum(cost(m) for m in done)
    scale = elapsed_s / prior_done if prior_done > 0 else 1.0
    unpriced = sum(1 for m in remaining if m not in prior and m not in skip)
    eta = sum(cost(m) for m in remaining) * scale / 60
    return eta, f"prior-run weighted x{scale:.2f}, {unpriced} unpriced"


def eta_minutes(done: int, total: int, elapsed_s: float, window_s: float) -> tuple[float, float]:
    """`(mean-rate ETA, window-rate ETA)` in minutes for the markets left.

    The mean rate since the start carries the run's slow opening to its
    last line: on 2026-10-17 it read `~404 min left` at 11,600/18,497 for
    328 actual and `~60` at 17,400 for 48, and three passes pre-registered
    a budget FAIL off it. The trailing rate -- the last `PROGRESS_EVERY`
    markets over the seconds since the previous line -- is the other
    estimate; the line prints both and names them, so a reader projecting
    a done time knows which one is the bias. Both are advisory.
    """
    left = max(total - done, 0)
    mean = left * elapsed_s / done / 60 if done > 0 and elapsed_s > 0 else 0.0
    window = left * window_s / PROGRESS_EVERY / 60 if window_s > 0 else mean
    return mean, window


def _fetch_market(sess, m, cond, pair, watermarks, now, batch) -> None:
    """The two paced fetches for one market: each token's price history from
    its watermark (chunked; the API rejects spans over ~30d) and the trade
    tail from offset 0, with the tail stops the client recorded."""
    for token, outcome in zip(pair, ("yes", "no"), strict=True):
        wm = watermarks.get(token)
        # First capture reaches the full ~60d retention window; later
        # sweeps resume from the watermark.
        start = (
            int(wm.replace(tzinfo=UTC).timestamp()) + 1
            if wm
            else int((now - timedelta(days=62)).timestamp())
        )
        hist = poly.prices_history_range(token, start, session=sess)
        batch["prices"].extend(poly.price_rows(token, cond, outcome, hist))
        time.sleep(REQUEST_PAUSE_S)
    batch["trades"].extend(poly.poly_trade_row(t) for t in poly.trades_tail(cond, session=sess))
    batch["tail_stops"].extend(poly.take_tail_stops())
    time.sleep(REQUEST_PAUSE_S)


def resolved_complete(store) -> set[str]:
    """`Store.poly_resolved_complete`, or the empty set with a line when the
    archive cannot answer: the fallback is the old behaviour (fetch every
    market), which is slow and lossless -- a skip set from a failed read
    would be neither."""
    try:
        return store.poly_resolved_complete()
    except (duckdb.Error, AttributeError) as exc:
        print(f"[poly] resolved set unreadable, fetching every market: {exc}", flush=True)
        return set()


def sweep(
    db: str,
    min_volume: float,
    limit: int | None = None,
    include_closed_days: int = 7,
) -> dict:
    sess = requests.Session()
    sess.headers["User-Agent"] = "hyxlab-research"
    # Module-level counters span every run in the process otherwise -- the
    # bug #80 found in kalshi's ledger. One run, one reading.
    poly.reset_retry_counts()

    now = datetime.now(UTC)
    print("[poly] enumerating markets via Gamma (volume-desc)...", flush=True)
    markets = poly.iter_markets_by_volume(min_volume, closed=False, session=sess)
    n_open = len(markets)
    closed_floor = (now - timedelta(days=include_closed_days)).strftime("%Y-%m-%d")
    markets += poly.iter_markets_by_volume(
        min_volume, closed=True, session=sess, end_date_min=closed_floor
    )
    if limit:
        markets = markets[:limit]
        n_open = min(n_open, limit)
    # The order IS the pace profile: every open market first, volume-desc,
    # then the closed ones -- so the resolved skip cannot land before index
    # `n_open`. The 2026-10-19 reading of the first skip run compared leg
    # walls at index 2,000-5,000 against the prior day and found ~13 min,
    # because all 6,105 of those were open; name the boundary.
    print(
        f"[poly] {len(markets)} markets >= ${min_volume:g} volume"
        f" ({n_open} open, {len(markets) - n_open} closed)",
        flush=True,
    )

    with open(LOCK_FILE, "a") as lock:  # ensure schema + read watermarks
        fcntl.flock(lock, fcntl.LOCK_EX)
        note_holder(LOCK_FILE)
        store = _open(db)
        try:
            with charge_hold(store.close):  # the watermark read is a hold too
                watermarks = store.poly_price_watermarks()
                prior = prior_market_wall(store)
                resolved = resolved_complete(store)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

    batch: dict = {
        "infos": [],
        "stats": [],
        "prices": [],
        "trades": [],
        "tail_stops": [],
        "swept": [],
        "n_prices": 0,
        "n_trades": 0,
        "n_tail_stops": 0,
    }
    totals: dict = {"markets": 0, "errors": 0, "skipped_resolved": 0}
    # The three populations the walk is made of, each with the rows it
    # FETCHED (pre-dedup, counted at the fetch; `n_prices`/`n_trades` are
    # the archive's post-dedup insert counts) and its loop wall. On the
    # first skip run (2026-10-19) the closed-fetched 1,572 markets carried
    # ~718k of the run's 810k trades -- the final prints before a close --
    # and 6.7 s/market against 2.7 for the open ones; readable until now
    # only by hand-differencing the progress lines across the open/closed
    # boundary. `flush_min` is the rest of the loop, so the four walls sum
    # to `elapsed_min` less the enumeration.
    segments: dict[str, dict] = {
        k: {"markets": 0, "prices": 0, "trades": 0, "wall_s": 0.0}
        for k in ("open", "closed_fetched", "closed_skipped")
    }
    flush_s = 0.0
    # `errors` alone cannot say WHAT failed: 23 days of journal carry 107
    # HTTPError and 4 ConnectionError under one integer. Keyed by class, a
    # rising total is actionable without a journal scrape.
    error_classes: dict[str, int] = {}
    t0 = time.monotonic()
    t_line = t0
    ids = [m.get("conditionId") or "" for m in markets]
    # The markets this run skips (the loop's `skip`, decided up front): the
    # ETA's fallback price for a market with no prior is the median of the
    # OTHER population, the markets it fetches.
    closed_ids = {c for m, c in zip(markets, ids, strict=True) if m.get("closed")}
    skipped_ids = {c for c in closed_ids if c in resolved}

    for i, m in enumerate(markets):
        cond = m.get("conditionId")
        pair = poly.token_pair(m)
        if not cond or pair is None:
            continue
        t_m = time.monotonic()
        # A market a PRIOR run archived as resolved, owing no tail, gets its
        # Gamma row (info + stats, free with the enumeration) and neither
        # fetch: nothing trades on a closed market, and the run that first
        # saw it closed already fetched to that instant. Gamma's flag alone
        # is not enough -- the final prints land in the run that sees the
        # close (see `Store.poly_resolved_complete`). 12,458 of 18,496
        # markets were resolved on 2026-10-17 and cost 74% of the run's
        # ~93k requests (plus their pacing), for 0 new rows.
        skip = bool(m.get("closed")) and cond in resolved
        seg = segments["open" if i < n_open else "closed_skipped" if skip else "closed_fetched"]
        n_p, n_t = len(batch["prices"]), len(batch["trades"])
        n_i, n_s = len(batch["infos"]), len(batch["stats"])
        try:
            batch["infos"].append(poly.gamma_market_info(m))
            batch["stats"].append(
                (cond, now, float(m.get("volumeNum") or 0), float(m.get("liquidityNum") or 0))
            )
            if skip:
                totals["skipped_resolved"] += 1
            else:
                _fetch_market(sess, m, cond, pair, watermarks, now, batch)
        except Exception as exc:
            totals["errors"] += 1
            name = type(exc).__name__
            error_classes[name] = error_classes.get(name, 0) + 1
            print(f"[poly] {name} at {cond[:16]}: {str(exc)[:80]}", flush=True)
            # A market whose fetch raised is NOT one this run fetched, so it
            # keeps neither row that says so. Its Gamma row sets
            # `markets.result` for a closed market, and the stats row is
            # the tail-absorption oracle's "a run fetched after the stop";
            # with both archived, tomorrow's `poly_resolved_complete` skips
            # the market's two fetches for good -- prices from a ~60-day
            # rolling window, tail capped at 3,000 -- a permanent hole for
            # every market FIRST seen closed on an incident day. Measured
            # 2026-10-21: 87 CLOB `500 Server Error` on prices-history from
            # 09:04Z, still landing as the loop crossed the open/closed
            # boundary at 09:17Z (`prices_history` has no 5xx retry). The
            # prices/trades it put in the batch before raising stay (the
            # watermark is max(ts), so a partial token is re-read tomorrow).
            del batch["infos"][n_i:]
            del batch["stats"][n_s:]
            time.sleep(2)
        totals["markets"] += 1
        # The market's own wall, before the flush it may trigger: tomorrow's
        # prior for this market. The flush cost lands in the pace scale.
        wall_m = time.monotonic() - t_m
        batch["swept"].append((cond, datetime.now(UTC), wall_m))
        # Counted after the fetch, so a market that raised mid-way still
        # owns the rows it put in the batch (they are flushed with it).
        seg["markets"] += 1
        seg["prices"] += len(batch["prices"]) - n_p
        seg["trades"] += len(batch["trades"]) - n_t
        seg["wall_s"] += wall_m
        if len(batch["infos"]) >= FLUSH_MARKETS or i == len(markets) - 1:
            t_f = time.monotonic()
            try:
                _flush(db, batch)
            except Exception as exc:  # batch is cleared only on success
                totals["errors"] += 1
                error_classes["flush"] = error_classes.get("flush", 0) + 1
                print(f"[poly] flush failed, batch held: {str(exc)[:80]}", flush=True)
            flush_s += time.monotonic() - t_f
        if (i + 1) % PROGRESS_EVERY == 0:
            now_m = time.monotonic()
            _, eta_w = eta_minutes(i + 1, len(markets), now_m - t0, now_m - t_line)
            eta, tag = eta_weighted(
                ids[: i + 1], ids[i + 1 :], now_m - t0, prior, skipped_ids, closed_ids
            )
            t_line = now_m
            print(
                f"[poly] {i + 1}/{len(markets)} | {batch['n_prices']} prices,"
                f" {batch['n_trades']} trades, {totals['skipped_resolved']} skipped"
                f" | ~{eta:.0f} min left ({tag};"
                f" ~{eta_w:.0f} at the last {PROGRESS_EVERY}'s rate)",
                flush=True,
            )
    t_f = time.monotonic()
    _flush(db, batch)
    flush_s += time.monotonic() - t_f
    totals["n_prices"], totals["n_trades"] = batch["n_prices"], batch["n_trades"]
    totals["segments"] = {
        k: {**{f: v for f, v in d.items() if f != "wall_s"}, "wall_min": round(d["wall_s"] / 60, 1)}
        for k, d in segments.items()
    }
    totals["flush_min"] = round(flush_s / 60, 1)
    totals["n_tail_stops"] = batch["n_tail_stops"]
    totals["elapsed_min"] = round((time.monotonic() - t0) / 60, 1)
    totals["error_classes"] = error_classes
    # The per-class retry/fault ledger this run spent. `tail_truncated` is
    # the population the `errors` total was structurally blind to -- ~80 per
    # run, absorbed by the next sweep. The COUNT alone could not re-check
    # that absorber (the claim is per market and per depth); `n_tail_stops`
    # counts the same faults archived as rows, which can, and does in
    # `qa.qa_poly_tail_absorbed`.
    totals["http_retries"] = poly.retry_counts()
    totals["keyset_budget_frac"] = round(poly.keyset_budget_frac(), 3)
    return totals


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab polymarket archival sweep")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--min-volume", type=float, default=10000.0)
    ap.add_argument("--limit", type=int, default=None, help="max markets (smoke tests)")
    ap.add_argument("--closed-days", type=int, default=7, help="recently-closed lookback")
    args = ap.parse_args()
    Path(LOCK_FILE).parent.mkdir(exist_ok=True)
    # ~7h of paced HTTP against per-token watermarks: a second instance
    # re-walks the same worklist, doubles writer-lock contention against
    # the 5-min collector, and spends the venue rate budget twice.
    lock, why = instance_lock_or_reason("poly_sweep")
    if lock is None:
        print(f"[poly] {why}; aborting", flush=True)
        sys.exit(75)  # EX_TEMPFAIL — the next timer firing resumes
    reset_attach_waits()
    try:
        totals = sweep(args.db, args.min_volume, args.limit, args.closed_days)
    finally:
        lock.close()
    print(f"[poly] done: {json.dumps(totals)}", flush=True)
    # The run's whole exclusion of the collector's file -- every `_flush`
    # burst -- through the one renderer (#95), db-scoped.
    print(attach_wait_line(attach_wait_block(db=Path(args.db).name), prefix="[poly]"), flush=True)


if __name__ == "__main__":
    main()
