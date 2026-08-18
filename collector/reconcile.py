"""Archive reconciliation pass — REPAIR the holes, oldest-first (EXP-934).

WHAT THIS IS FOR
----------------
`collector/sweep.py` walks a CATEGORY ALLOWLIST of series and asks
`/markets?status=settled` per series. Everything it never asks for, or asks
for and loses (429 runs, truncated pages, skipped runs), is simply absent
from `markets`/`candles` and nothing repairs it. The exchange-wide stream
firehose (`hyxstream.duckdb::stream_trades`) sees EVERY market that printed
a trade, allowlist or not, so the set difference

    {market_id seen trading on the wire}  -  {market_id in the archive}

is a direct, venue-independent census of what the polled archive is missing.
This module computes that difference and force-repairs it market by market.

MEASURED 2026-08-03 (27 days of stream coverage, 2026-07-07 .. now):
7,622,520 traded markets in the sweep's allowlist categories are absent from
the archive. 7,516,962 of those are `KXMVE*` Exotics parlay legs (the family
`collector/breadth.py` measured at >200k OPEN markets); EXCLUDING them the
deficit is **105,558 markets**, of which 94,651 are older than the sweep's
own 2-day grace. Zero are past the purge horizon TODAY — see below — so
today the split is 100% repairable / 0% lost. That is a deadline, not a
reprieve.

THE ORDERING, AND WHY IT IS THE WHOLE POINT
-------------------------------------------
Kalshi purges settled market data on a retention clock, so every night a
slice of the deficit stops being repairable FOREVER. A reconciliation that
orders its work by anything uncorrelated with that clock (the sweep ordered
series ALPHABETICALLY, and three noisy KXBNB series consumed an entire
3.5-hour run — every KXBTC*/KXETH*/KXSOL* series and all of Exotics were
never requested at all) spends its budget uniformly at random with respect
to expiry: under a budget shortfall it loses data with 3 days of life left
at exactly the rate it loses data with 60.

So work is ordered by TIME-TO-PURGE ASCENDING, most endangered first. Under
a shortfall this loses only the items with the MOST remaining life — which
is by construction the subset a later run can still recover.

The horizon is MEASURED, not assumed. Probed live 2026-08-03 against
KXHIGHNY with `get_markets_ascending` over one close-day at a time:

    close 2026-05-24 (age 71d) -> 0 markets returned   PURGED
    close 2026-05-25 (age 70d) -> 6 markets returned   present
    close 2026-05-26 (age 69d) -> 6                    present
    close 2026-05-27 (age 68d) -> 6                    present
    ... through age 31d        -> 6                    present
    close 2026-05-13 (age 81d) -> 0                    PURGED
    close 2026-05-03 (age 91d) -> 0                    PURGED

i.e. the cliff sits between 70 and 71 days, inside the "~60-90 days"
docs/wiki/data-pipeline.md records and consistent with the "prints purge
~64 days after close" note on `kalshi.get_trades`. Re-derive it with
`--probe-horizon` (10 requests) rather than trusting this constant.

Time-to-purge needs a close time we do not have (the market is missing from
the archive — that is the point), so it is estimated from the LAST TRADE the
firehose saw. A market's last print is at or BEFORE its close, so this
estimate can only make an item look OLDER, i.e. more endangered, than it is.
That is the safe direction for an ordering: it can misprioritise work
earlier, never later. It is explicitly NOT used to declare anything lost —
absence is established by ASKING (see below), never by arithmetic.

WHAT IT CANNOT REPAIR, IT WRITES DOWN
-------------------------------------
`kalshi.get_markets_by_tickers` returns (found, absent, undetermined).
`absent` = requested and not returned = purged, and every one of those is
recorded into the hylshi completeness ledger
(`/home/devs/workspace/hylshi/data/completeness_ledger.json`) through that
repo's OWN `tools/completeness_ledger.acknowledge`/`save_state`, so the
schema cannot drift from the reader's. `undetermined` (a batch whose cursor
was still live) is NEITHER repaired NOR recorded as lost: it stays in the
remainder for the next run, because writing "this is gone forever" for data
that was one page away is EXP-931's error with the sign flipped.

HONESTY GUARANTEES (each one is a regression test)
--------------------------------------------------
* Never advances a watermark. A watermark is a claim of coverage; this pass
  repairs holes BEHIND watermarks and moving one would re-create the exact
  EXP-931 loss it exists to clean up.
* Never writes `sweep_log`. `archive_completeness` reads that table and
  treats any non-`ok` status as an error-day; injecting reconciliation runs
  would manufacture error runs in a detector this pass is meant to serve.
* Exits 2 whenever ANY work remains (budget exhausted, per-series cap,
  undetermined batch). A partial repair can never be reported as a complete
  one; exit 0 means the scanned deficit reached zero.
* Per-ITEM budgets, not just a global one (`--max-per-series`): the
  starvation bug above was a global budget with no per-item cap.

DISCIPLINE
----------
HTTP happens outside `data/writer.lock`; the DB is touched in short
`writer_burst`s, one per batch (a long-held lock dropped 11.4% of 5-min
collect cycles over the 14 days to 2026-08-02 — unrecoverable). Every
request goes through `kalshi._get_with_429_retry`. Pacing defaults to the
repo's empirical `sweep.MARKETS_PAUSE_S`/`CANDLES_PAUSE_S`.

DO NOT RUN A HEAVY REPAIR DURING 23:00-08:00Z: the live trading loop shares
Kalshi's rate budget and that is its fade window.

Run:
    python -u -m collector.reconcile --dry-run                # census + cost
    python -u -m collector.reconcile --probe-horizon          # 10 requests
    python -u -m collector.reconcile --exclude-series 'KXMVE%' --max-markets 5000
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import requests

from collector.sweep import CANDLES_PAUSE_S, DEFAULT_CATEGORIES, MARKETS_PAUSE_S, writer_burst
from collector.venues import kalshi
from hyxlab.store import connect_retry

__all__ = [
    "GRACE_DAYS",
    "MAX_MARKETS_PER_BATCH",
    "PURGE_HORIZON_DAYS",
    "Missing",
    "bounded_batches",
    "candle_request_cost",
    "diff_missing",
    "ledger_findings",
    "plan_cost",
    "probe_horizon",
    "reconcile",
    "record_holes",
    "series_of",
    "time_to_purge_days",
    "work_order",
]

#: Days after close at which Kalshi stops serving a settled market. MEASURED
#: live 2026-08-03 (see module docstring: present at 70d, gone at 71d).
#: Used for ORDERING and reporting ONLY — never to declare data lost.
PURGE_HORIZON_DAYS = 70

#: A market whose last print is younger than this is not a hole yet: the
#: daily sweep runs `--days 2` at 06:10 UTC, so anything that closed inside
#: the last two days is simply not due. Counting it would make the deficit
#: look ~11% bigger than it is (measured: 105,558 -> 94,651 at 2 days).
GRACE_DAYS = 2

#: Per-SERIES market cap for ONE run. The sweep's equivalent
#: (MAX_MARKETS_PER_SERIES) exists because one dense family ate a whole run;
#: here the ordering is by expiry rather than by name, so the cap is the
#: second line of defence: a family with 6M missing legs of the SAME age
#: cannot monopolise a budget-limited run just because it is uniformly old.
MAX_PER_SERIES = 2000

#: How many of the most-endangered missing markets to MATERIALISE per run.
#: The census COUNT is always exact and always reported; this only bounds
#: how much of the tail is pulled into memory (the full deficit is 7.6M rows
#: and 96% of it is one parlay family).
SCAN_LIMIT = 200_000

#: Cap on markets repaired per BATCH in the full (non-metadata) path. Budgets
#: are checked BETWEEN batches, and a URL-length metadata batch is ~190
#: tickers — so before this cap, one batch could spend the run's whole time
#: budget and then some. Measured (EXP-1307, 2026-08-13..15): the same
#: 240-market / 360-request work order went 162s -> 516s -> >1020s as the
#: missing mix shifted from short-lived Crypto hourlies to long-lived
#: Financials/Economics monthlies (59,639 candles inserted on 08-13 vs 1,390
#: on 08-11; request rate 2.85 -> 0.93 Hz), and the invoker's outer timeout
#: killed the process mid-batch two days running because `--max-minutes`
#: never got a boundary to fire at. 25 markets bounds the overshoot at
#: ~2 minutes even at the worst measured per-market cost (~4.3 s/market).
#: Metadata-only runs keep the URL-length batches: they make one request per
#: batch, so splitting them would multiply requests for no bound gained.
MAX_MARKETS_PER_BATCH = 25

DEFAULT_DB = "data/hyxlab.duckdb"
DEFAULT_STREAM_DB = "data/hyxstream.duckdb"
SUMMARY_PATH = "data/reconcile_last.json"

#: The hylshi ledger and the module that owns its schema. Loaded BY PATH:
#: hyxrestration must not grow an import edge into the trading repo, and the
#: ledger's writer must be the reader's own code so the schema cannot drift.
LEDGER_TOOL = "/home/devs/workspace/hylshi/tools/completeness_ledger.py"
LEDGER_STATE = "/home/devs/workspace/hylshi/data/completeness_ledger.json"


class Missing(NamedTuple):
    market_id: str
    series: str
    category: str
    last_trade_ts: datetime


def series_of(market_id: str) -> str:
    """Series ticker = everything before the first '-' ('KXHIGHNY-26JUL02-T99')."""
    return market_id.split("-")[0]


def time_to_purge_days(
    last_trade_ts: datetime, now: datetime, horizon_days: int = PURGE_HORIZON_DAYS
) -> float:
    """Days of life left, from the LAST-TRADE lower bound on close time.

    Negative means the estimate says it is already gone. The estimate is
    deliberately pessimistic (last trade <= close), so this is a LOWER bound
    on remaining life: it can order an item earlier than strictly needed and
    can never order an endangered item late.
    """
    last = last_trade_ts if last_trade_ts.tzinfo else last_trade_ts.replace(tzinfo=UTC)
    age_days = (now - last).total_seconds() / 86400.0
    return horizon_days - age_days


# ---------------------------------------------------------------------------
# the diff
# ---------------------------------------------------------------------------


def _allowed_series(db: str, categories: list[str] | None) -> dict[str, str]:
    """series ticker -> category, from ONE short read burst on the archive."""
    con = connect_retry(db, read_only=True)
    try:
        rows = con.execute(
            "SELECT ticker, category FROM series WHERE venue='kalshi'"
        ).fetchall()
    finally:
        con.close()
    cats = None if categories is None else set(categories)
    return {t: (c or "") for t, c in rows if cats is None or (c or "") in cats}


def _export_archived_ids(db: str, out_path: str) -> int:
    """Dump archived kalshi market_ids to parquet in one short read burst.

    Parquet rather than a Python set passed into the stream query: the
    anti-join then happens inside DuckDB, and the archive's file lock is
    held for milliseconds instead of for the length of a 7.9M-row scan of
    the OTHER database. Readers exclude the 5-min collector's writer.
    """
    con = connect_retry(db, read_only=True)
    try:
        con.execute(
            f"COPY (SELECT market_id FROM markets WHERE venue='kalshi')"
            f" TO '{out_path}' (FORMAT PARQUET)"
        )
        return con.execute(
            "SELECT count(*) FROM markets WHERE venue='kalshi'"
        ).fetchone()[0]
    finally:
        con.close()


def diff_missing(
    db: str = DEFAULT_DB,
    stream_db: str = DEFAULT_STREAM_DB,
    categories: list[str] | None = None,
    exclude_series_like: str | None = None,
    now: datetime | None = None,
    grace_days: int = GRACE_DAYS,
    scan_limit: int = SCAN_LIMIT,
) -> tuple[list[Missing], dict]:
    """(most-endangered `scan_limit` missing markets, census).

    The census counts are EXACT over the whole deficit even when the
    returned list is truncated to `scan_limit` — a pass that reports only
    what it looked at is how a 12% hole stays invisible.
    """
    now = now or datetime.now(UTC)
    allowed = _allowed_series(db, categories)
    cutoff = (now - timedelta(days=grace_days)).replace(tzinfo=None)

    with tempfile.TemporaryDirectory() as tmp:
        ids_path = str(Path(tmp) / "archived_ids.parquet")
        n_archived = _export_archived_ids(db, ids_path)
        con = connect_retry(stream_db, read_only=True)
        try:
            con.execute("CREATE TEMP TABLE _allowed(series VARCHAR, category VARCHAR)")
            if allowed:
                con.executemany(
                    "INSERT INTO _allowed VALUES (?, ?)", list(allowed.items())
                )
            excl = ""
            params: list = [cutoff]
            if exclude_series_like:
                excl = " AND s.series NOT LIKE ?"
                params.append(exclude_series_like)
            base = f"""
                WITH t AS (
                    SELECT market_id, max(src_ts) AS last_ts
                    FROM stream_trades WHERE venue='kalshi' GROUP BY 1
                ),
                s AS (
                    SELECT t.market_id, split_part(t.market_id,'-',1) AS series,
                           t.last_ts
                    FROM t
                    LEFT JOIN read_parquet('{ids_path}') a USING (market_id)
                    WHERE a.market_id IS NULL
                )
                SELECT {{cols}}
                FROM s JOIN _allowed al ON al.series = s.series
                WHERE s.last_ts <= ?{excl}
            """
            total = con.execute(
                base.format(cols="count(*)"), params
            ).fetchone()[0]
            by_cat = con.execute(
                base.format(cols="al.category, count(*)") + " GROUP BY 1 ORDER BY 2 DESC",
                params,
            ).fetchall()
            rows = con.execute(
                base.format(cols="s.market_id, s.series, al.category, s.last_ts")
                + " ORDER BY s.last_ts ASC, s.series, s.market_id LIMIT ?",
                [*params, int(scan_limit)],
            ).fetchall()
        finally:
            con.close()

    missing = [
        Missing(mid, ser, cat or "", ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts)
        for mid, ser, cat, ts in rows
    ]
    census = {
        "archived_markets": n_archived,
        "allowed_series": len(allowed),
        "missing_total": total,
        "missing_scanned": len(missing),
        "missing_by_category": {c or "?": n for c, n in by_cat},
        "grace_days": grace_days,
        "scan_limit": scan_limit,
        "as_of": now.isoformat(timespec="seconds"),
    }
    return missing, census


# ---------------------------------------------------------------------------
# the work order
# ---------------------------------------------------------------------------


def work_order(
    missing: list[Missing],
    now: datetime | None = None,
    horizon_days: int = PURGE_HORIZON_DAYS,
    max_per_series: int | None = MAX_PER_SERIES,
) -> tuple[list[Missing], list[Missing]]:
    """(ordered work, deferred) — most endangered first.

    Sort key is time-to-purge ASCENDING (equivalently last-trade ascending),
    with `(series, market_id)` as a deterministic tie-break so two runs over
    the same deficit do the same work in the same order.

    `max_per_series` is the PER-ITEM budget. Deferred items are NOT holes and
    are NOT recorded as lost — they are simply work this run did not reach,
    and they keep the run's exit status non-zero.
    """
    now = now or datetime.now(UTC)
    ordered = sorted(
        missing,
        key=lambda m: (time_to_purge_days(m.last_trade_ts, now, horizon_days),
                       m.series, m.market_id),
    )
    if not max_per_series:
        return ordered, []
    seen: dict[str, int] = {}
    keep, deferred = [], []
    for m in ordered:
        n = seen.get(m.series, 0)
        if n >= max_per_series:
            deferred.append(m)
        else:
            seen[m.series] = n + 1
            keep.append(m)
    return keep, deferred


def plan_cost(
    order: list[Missing],
    metadata_only: bool = False,
    pause_s: float = MARKETS_PAUSE_S,
    candles_pause_s: float = CANDLES_PAUSE_S,
    per_market_s: float = 0.5,
) -> dict:
    """Requests and wall-clock a full repair of `order` would cost.

    Metadata resolves ~250 tickers per request (URL-length bounded), so it
    is nearly free; candles+trades are 2 requests and ~`per_market_s` each
    and dominate everything. `per_market_s` is the sweep's measured
    ~0.5 s/market (EXP-931 note in collector/sweep.py).
    """
    n = len(order)
    batches = len(kalshi.batch_tickers([m.market_id for m in order]))
    reqs = batches + (0 if metadata_only else 2 * n)
    secs = batches * (pause_s * 2) + (0 if metadata_only else n * (per_market_s + candles_pause_s))
    return {
        "markets": n,
        "metadata_batches": batches,
        "requests": reqs,
        "est_minutes": round(secs / 60.0, 1),
        "est_request_rate_hz": round(reqs / secs, 2) if secs else 0.0,
    }


# ---------------------------------------------------------------------------
# the ledger — permanent holes are RECORDED, never skipped
# ---------------------------------------------------------------------------


def ledger_findings(purged: list[Missing], now: datetime | None = None) -> dict[str, dict]:
    """Keyed defects for `tools.completeness_ledger`, one per series-day.

    Per-MARKET keys would put tens of thousands of entries in a file a human
    is supposed to read; per-SERIES keys would hide a hole that grows into a
    new day. Series-day is the same granularity the ledger's existing
    `absent_day:` class uses, so the two read alike.

    `magnitude` is the market COUNT, which makes the ledger's growth
    discriminator work correctly: the same series-day found with more lost
    markets tomorrow escalates, the same one at the same extent stays quiet.
    """
    buckets: dict[str, list[Missing]] = {}
    for m in purged:
        day = m.last_trade_ts.date().isoformat()
        buckets.setdefault(f"purged_market:{m.series}@{day}", []).append(m)
    out: dict[str, dict] = {}
    for key in sorted(buckets):
        ms = buckets[key]
        sample = ", ".join(sorted(x.market_id for x in ms)[:3])
        out[key] = {
            "kind": "purged_market",
            "magnitude": float(len(ms)),
            "unit": "markets",
            "detail": (
                f"{len(ms)} traded market(s) absent from the archive and no "
                f"longer served by Kalshi (past the ~{PURGE_HORIZON_DAYS}d "
                f"purge horizon); permanently unrepairable. e.g. {sample}"
            ),
        }
    return out


def _load_ledger_tool(tool_path: str = LEDGER_TOOL):
    """Import hylshi's ledger module BY PATH (no import edge into that repo)."""
    p = Path(tool_path)
    if not p.exists():
        raise FileNotFoundError(f"completeness ledger tool not found: {tool_path}")
    spec = importlib.util.spec_from_file_location("_hylshi_completeness_ledger", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def record_holes(
    findings: dict[str, dict],
    note: str = "",
    state_path: str = LEDGER_STATE,
    tool_path: str = LEDGER_TOOL,
) -> int:
    """Write permanent holes into the ledger using ITS OWN writer.

    Returns the number of keys recorded. No findings -> no write at all, so
    a clean run never touches the trading repo's state.
    """
    if not findings:
        return 0
    mod = _load_ledger_tool(tool_path)
    state = mod.load_state(state_path)
    state = mod.acknowledge(findings, state, note=note or "recorded by collector.reconcile (EXP-934)")
    mod.save_state(state, state_path)
    return len(findings)


# ---------------------------------------------------------------------------
# the repair
# ---------------------------------------------------------------------------


def probe_horizon(
    series_ticker: str = "KXHIGHNY",
    ages: tuple[int, ...] = (30, 45, 55, 60, 65, 68, 70, 71, 75, 85),
    session: requests.Session | None = None,
    now: datetime | None = None,
    pause_s: float = MARKETS_PAUSE_S,
) -> list[tuple[int, int]]:
    """Re-derive PURGE_HORIZON_DAYS: [(age_days, markets_returned), ...].

    One request per age. The horizon is wherever the count falls to 0 and
    stays there; the constant in this module is a MEASUREMENT and any run
    that finds it wrong should say so rather than quietly keep ordering by a
    stale number.
    """
    sess = session or requests.Session()
    now = now or datetime.now(UTC)
    out = []
    for age in ages:
        close = (now - timedelta(days=age)).replace(hour=4, minute=59, second=0, microsecond=0)
        markets, _ = kalshi.get_markets_ascending(
            series_ticker,
            status="settled",
            min_close_ts=int((close - timedelta(hours=2)).timestamp()),
            max_close_ts=int((close + timedelta(hours=2)).timestamp()),
            session=sess,
        )
        out.append((age, len(markets)))
        time.sleep(pause_s)
    return out


def _ts(v: str | None) -> int | None:
    if not v:
        return None
    return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())


def bounded_batches(batches: list[list[str]], cap: int | None) -> list[list[str]]:
    """Re-split URL-length batches so no batch exceeds `cap` markets,
    PRESERVING ORDER (the caller's order is a priority order).

    This is what makes `reconcile`'s between-batch budget checks mean
    something in the full path: the checks can only fire at batch
    boundaries, so batch size IS the overshoot bound (EXP-1307).
    """
    if not cap:
        return batches
    return [b[i:i + cap] for b in batches for i in range(0, len(b), cap)]


def candle_request_cost(start_ts: int, end_ts: int, period_interval: int = 60) -> int:
    """How many HTTP requests `kalshi.get_candlesticks` will actually spend.

    Spans over MAX_CANDLES_PER_REQUEST periods are chunked (EXP-1274), and
    before EXP-1307 each candles call was counted as ONE request no matter
    how many chunks it made — a 298-day monthly market is 2 requests, so
    `--max-requests` under-counted exactly when the work order was slowest.
    """
    span_s = max(0, int(end_ts) - int(start_ts))
    max_span_s = kalshi.MAX_CANDLES_PER_REQUEST * period_interval * 60
    return max(1, -(-span_s // max_span_s))  # ceil division


def reconcile(
    order: list[Missing],
    db: str = DEFAULT_DB,
    session: requests.Session | None = None,
    metadata_only: bool = False,
    max_markets: int | None = None,
    max_requests: int | None = None,
    max_minutes: float | None = None,
    pause_s: float = MARKETS_PAUSE_S,
    candles_pause_s: float = CANDLES_PAUSE_S,
    lock_file: str | None = None,
    ledger_state: str | None = LEDGER_STATE,
    ledger_tool: str = LEDGER_TOOL,
    now: datetime | None = None,
    max_markets_per_batch: int | None = MAX_MARKETS_PER_BATCH,
) -> dict:
    """Repair `order` (already prioritised) and report what is left.

    Every batch is: HTTP (no lock) -> one short `writer_burst`. Budgets are
    checked BETWEEN batches so a stop is always at a clean boundary and the
    unreached tail is reported as `remaining`, never as done. In the full
    path, batches are capped at `max_markets_per_batch` markets so those
    checks actually get boundaries to fire at (EXP-1307).
    """
    sess = session or requests.Session()
    t0 = time.monotonic()
    now_ts = int((now or datetime.now(UTC)).timestamp())
    requests_made = 0
    repaired: list[str] = []
    purged: list[Missing] = []
    undetermined: list[Missing] = []
    n_candles = 0
    n_trades = 0
    by_id = {m.market_id: m for m in order}
    processed = 0
    stop_reason = ""

    batches = kalshi.batch_tickers([m.market_id for m in order])
    if not metadata_only:
        # Metadata-only runs are one request per batch, so re-splitting them
        # would multiply requests for no bound gained; the full path is
        # 1 + 2/market and MUST be re-split or the between-batch budget
        # checks below are decorative (EXP-1307).
        batches = bounded_batches(batches, max_markets_per_batch)
    for i, batch in enumerate(batches):
        if max_markets is not None and processed >= max_markets:
            stop_reason = "max-markets"
            break
        if max_requests is not None and requests_made >= max_requests:
            stop_reason = "max-requests"
            break
        if max_minutes is not None and (time.monotonic() - t0) / 60.0 >= max_minutes:
            stop_reason = "max-minutes"
            break
        if i and pause_s:
            time.sleep(pause_s)

        found, absent, undet = kalshi.get_markets_by_tickers(batch, session=sess, pause_s=pause_s)
        requests_made += 1
        processed += len(batch)
        purged.extend(by_id[t] for t in absent if t in by_id)
        undetermined.extend(by_id[t] for t in undet if t in by_id)

        infos = [kalshi.to_market_info(m) for m in found.values()]
        candle_rows: list[tuple] = []
        trade_rows: list[tuple] = []
        swept: list[tuple[str, int, str]] = []
        if not metadata_only:
            for ticker, m in found.items():
                open_ts, close_ts = _ts(m.get("open_time")), _ts(m.get("close_time"))
                series = series_of(ticker)
                if open_ts is None or close_ts is None:
                    print(f"[reconcile] {ticker} skipped: missing open/close time", flush=True)
                    continue
                # Candles cannot exist in the future, but the census surfaces
                # OPEN far-dated markets (KXFEDFUNDSYEAR closes 2030-2037) and
                # `get_candlesticks` walks 5000-candle chunks all the way to
                # close_time — measured on the 2026-08-16/17 counting runs,
                # 745 of 924 candle requests (81%) covered time past `now`
                # and were guaranteed empty (EXP-1314). Clamp the fetch to
                # now; the not-yet-existing tail is captured by the settled
                # sweep once the market settles, exactly as it always was.
                candle_end_ts = max(open_ts, min(close_ts, now_ts))
                try:
                    candles = kalshi.get_candlesticks(
                        series, ticker, open_ts, candle_end_ts, 60, session=sess
                    )
                    # Chunked long-span markets spend several requests; count
                    # what was actually spent, not one (EXP-1307).
                    requests_made += candle_request_cost(open_ts, candle_end_ts, 60)
                    candle_rows.extend(kalshi.candle_row(series, m, c, 3600) for c in candles)
                except requests.HTTPError as e:
                    code = e.response.status_code if e.response is not None else "?"
                    print(f"[reconcile] {ticker} candles HTTP {code}", flush=True)
                try:
                    raw, truncated = kalshi.get_trades(ticker, session=sess)
                    requests_made += 1
                    rows = [kalshi.trade_row(t) for t in raw]
                    trade_rows.extend(rows)
                    # A truncated tape is NOT 'ok' (EXP-931): trades_swept is
                    # what the retro-pass reads to decide what to revisit.
                    swept.append(
                        (ticker, len(rows), "truncated" if truncated else ("ok" if rows else "empty"))
                    )
                except requests.HTTPError as e:
                    code = e.response.status_code if e.response is not None else "?"
                    print(f"[reconcile] {ticker} trade tape HTTP {code}", flush=True)
                time.sleep(candles_pause_s)

        # --- lock held from here; no HTTP below this line -------------------
        # A batch that resolved to nothing (all purged) takes no lock at all:
        # the 5-min collector uses `flock -n`, so every needless acquisition
        # is a chance to DROP a capture cycle.
        if infos or candle_rows or trade_rows or swept:
            with writer_burst(db, lock_file=lock_file) as store:
                if infos:
                    store.upsert_markets(infos)
                n_candles += store.insert_candles(candle_rows)
                n_trades += store.insert_trades(trade_rows)
                for ticker, n, status in swept:
                    store.mark_trades_swept(ticker, n, status)
        # NOTE: no set_watermark and no log_sweep, deliberately. See module
        # docstring — a watermark is a coverage claim, and sweep_log rows
        # would manufacture error-days in archive_completeness.
        # --------------------------------------------------------------------
        repaired.extend(found)

    elapsed_s = time.monotonic() - t0
    reached = {m.market_id for m in order[:processed]}
    remaining = [m for m in order if m.market_id not in reached]
    findings = ledger_findings(purged, now=now)
    recorded = 0
    if findings and ledger_state:
        recorded = record_holes(findings, state_path=ledger_state, tool_path=ledger_tool)

    complete = not remaining and not undetermined
    return {
        "in_order": len(order),
        "processed": processed,
        "repaired": len(repaired),
        "purged": len(purged),
        "purged_keys": sorted(findings),
        "ledger_recorded": recorded,
        "undetermined": len(undetermined),
        "remaining": len(remaining),
        "candles_inserted": n_candles,
        "trades_inserted": n_trades,
        "requests": requests_made,
        "elapsed_s": round(elapsed_s, 1),
        "request_rate_hz": round(requests_made / elapsed_s, 2) if elapsed_s > 0 else 0.0,
        "stop_reason": stop_reason,
        "complete": complete,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_order(order: list[Missing], now: datetime, horizon: int, n: int = 10) -> None:
    print(f"  work order (most endangered first, {len(order)} items):")
    for m in order[:n]:
        ttp = time_to_purge_days(m.last_trade_ts, now, horizon)
        print(f"    {ttp:6.1f}d left  {m.series:<28} {m.market_id}")
    if len(order) > n:
        print(f"    ... and {len(order) - n} more")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="hyxlab archive reconciliation (EXP-934)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--stream-db", default=DEFAULT_STREAM_DB)
    ap.add_argument("--categories", nargs="*", default=DEFAULT_CATEGORIES,
                    help="series categories in scope (default: the sweep allowlist)")
    ap.add_argument("--all-categories", action="store_true",
                    help="ignore the allowlist (includes Sports/Politics/Entertainment, "
                         "which are a standing USER-CONFIRMED exclusion)")
    ap.add_argument("--exclude-series", default=None,
                    help="SQL LIKE pattern of series to skip, e.g. 'KXMVE%%'")
    ap.add_argument("--grace-days", type=int, default=GRACE_DAYS)
    ap.add_argument("--horizon-days", type=int, default=PURGE_HORIZON_DAYS)
    ap.add_argument("--scan-limit", type=int, default=SCAN_LIMIT)
    ap.add_argument("--max-per-series", type=int, default=MAX_PER_SERIES,
                    help="per-ITEM budget; 0 = unbounded")
    ap.add_argument("--max-markets", type=int, default=None)
    ap.add_argument("--max-requests", type=int, default=None)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--metadata-only", action="store_true",
                    help="settlement metadata only, no candles/trades (~1/500 the requests)")
    ap.add_argument("--dry-run", action="store_true", help="census + cost, no HTTP, no writes")
    ap.add_argument("--probe-horizon", action="store_true",
                    help="re-derive the purge horizon live (10 requests) and exit")
    ap.add_argument("--ledger-state", default=LEDGER_STATE)
    ap.add_argument("--summary", default=SUMMARY_PATH)
    args = ap.parse_args(argv)

    now = datetime.now(UTC)

    if args.probe_horizon:
        print(f"purge-horizon probe ({datetime.now(UTC):%Y-%m-%dT%H:%MZ}), "
              f"module constant = {PURGE_HORIZON_DAYS}d")
        for age, n in probe_horizon(now=now):
            print(f"  close age {age:3d}d -> {n} markets returned"
                  f"{'   PURGED' if n == 0 else ''}")
        return 0

    cats = None if args.all_categories else args.categories
    missing, census = diff_missing(
        db=args.db, stream_db=args.stream_db, categories=cats,
        exclude_series_like=args.exclude_series, now=now,
        grace_days=args.grace_days, scan_limit=args.scan_limit,
    )
    order, deferred = work_order(
        missing, now=now, horizon_days=args.horizon_days,
        max_per_series=args.max_per_series or None,
    )
    # `--max-markets` cuts the TAIL off the work order. That tail is unreached
    # work exactly like the per-series deferrals, and it must be counted as
    # such — otherwise a `--max-markets 5` smoke run over a fully-scanned,
    # uncapped deficit would report `complete` after repairing five markets.
    dropped = 0
    if args.max_markets is not None and len(order) > args.max_markets:
        dropped = len(order) - args.max_markets
        order = order[: args.max_markets]

    print(f"=== archive reconciliation (EXP-934) === {now.isoformat(timespec='seconds')}")
    print(f"  archive: {census['archived_markets']} kalshi markets, "
          f"{census['allowed_series']} series in scope")
    print(f"  MISSING (traded on the wire, absent from the archive): "
          f"{census['missing_total']}")
    for cat, n in census["missing_by_category"].items():
        print(f"    {cat:<26} {n}")
    print(f"  scanned {census['missing_scanned']} most-endangered; "
          f"deferred by per-series cap: {len(deferred)}")
    _print_order(order, now, args.horizon_days)
    cost = plan_cost(order, metadata_only=args.metadata_only)
    print(f"  cost of this order: {cost}")

    if args.dry_run:
        print("  DRY RUN — no requests made, nothing written.")
        return 2 if census["missing_total"] else 0

    summary = reconcile(
        order, db=args.db, metadata_only=args.metadata_only,
        max_markets=args.max_markets, max_requests=args.max_requests,
        max_minutes=args.max_minutes, ledger_state=args.ledger_state, now=now,
    )
    summary["census"] = census
    summary["deferred_by_series_cap"] = len(deferred)
    summary["dropped_by_max_markets"] = dropped
    # Every kind of unreached work counts: per-series deferrals, the
    # --max-markets tail, and a deficit bigger than the scan window.
    summary["complete"] = (
        bool(summary["complete"])
        and not deferred
        and not dropped
        and census["missing_total"] <= census["missing_scanned"]
    )
    print(f"[reconcile] {summary}")
    try:
        Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    except OSError as e:
        print(f"[reconcile] could not write summary: {e}", flush=True)
    if not summary["complete"]:
        print("[reconcile] INCOMPLETE — work remains; rerun. (exit 2)")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
