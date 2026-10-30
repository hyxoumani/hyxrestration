"""Empirical verification of the trade→book-decrement mapping that the
maker queue-position bounds are built on (simulator/queuebounds.py).

    python -m simulator.prioritycheck [--hours 24] [--markets 8]

The queue accounting rests on one claim about how Kalshi's tape and L2
relate (queuebounds.consuming_print, first probed 2026-07-11 on a single
market, 269/270): a trade print recorded at the YES price `p` with
`taker_side` consumes exactly ONE resting book level —

    no-taker  → resting YES bids at price p        (yes = p)
    yes-taker → resting NO  bids at price 1-p      (no  = 1-p)

and the venue's own displayed-size decrement for that print lands at the
SAME level within a few ms (the model's ABSORB_WINDOW = 2s pairing
horizon). This probe tests both halves against the whole stream archive:

- MAPPING: for each print, is there an exact-size decrement at the
  PREDICTED (side, price) inside the pairing window? And crucially, is
  the naive same-side mapping (taker=yes → yes@p) ever the better fit?
  If the complement mapping were coincidence, same-side would match too.
- TIMING: the distribution of (decrement_ts - print_ts) over every
  absorb-window match, which sizes the ±1ms claim and is the only thing
  here that can show ABSORB_WINDOW=2s is generous rather than tight.
  Sampling only the exact-window matches cannot: that sample is bounded
  by ±5ms by construction, so its max is the window edge, not the tail
  (readings before 2026-09-20 published exactly that, and the 159 late
  decrements of the 07-13 run -- the whole 5ms-to-2s tail -- were the
  ones it dropped). The conditioned sub-distribution is kept as
  `timing.exact_window` so those readings stay comparable to something.

Residual no-matches are decomposed (decrement present but late; no
decrement at the level at all = a coverage gap; batched with same-instant
cancels) so the miss rate is attributed to archive coverage, not to a
wrong mapping. Note what this does and does NOT establish: it verifies
WHICH level a trade consumes (the mechanical foundation of the bracket),
not the front-vs-back consumption ORDER within a level — that ordering
ambiguity is exactly what the pess/opt bracket already brackets rather
than assumes. Ledger-only; nothing is traded.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hyxlab.memcap import rss_anon_mb
from hyxlab.store import attach_wait_block, attach_wait_line, held_attach, reset_attach_waits
from simulator.queuescore import cost_block, cost_line, iter_market_rows, select_markets

STREAM_DB = "data/hyxstream.duckdb"
EXACT_WINDOW = 0.005  # s: tight pairing for the exact-match rate / timing stats
ABSORB_WINDOW = 2.0  # s: the queuebounds model's own pairing horizon
PRICE_EPS = 1e-9


def _p95(ordered: list[float]) -> float:
    """Nearest-rank 95th percentile: the smallest value >= 95% of the sample.

    `ordered[int(len(ordered) * 0.95)]` is the (floor(0.95n)+1)-th order
    statistic, whose rank is >= 95% by construction and EQUALS the maximum
    whenever 0.95n is an integer -- n=20 publishes max_ms twice under two
    names. Nearest rank is ceil(0.95n)-1.
    """
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _timing(ordered: list[float]) -> dict:
    """Timing stats for an ALREADY-SORTED pairing-delay sample (ms)."""
    return {
        "n": len(ordered),
        "min_ms": round(ordered[0], 3),
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(_p95(ordered), 3),
        "max_ms": round(ordered[-1], 3),
        "within_1ms_frac": round(sum(1 for x in ordered if abs(x) <= 1) / len(ordered), 4),
        # census of the tail, so "ABSORB_WINDOW=2s is generous" is a
        # checkable claim in the artifact rather than prose: how many
        # pairings needed more than 5ms, 50ms, 500ms of window.
        "over_ms": {str(b): sum(1 for x in ordered if abs(x) > b) for b in (5, 50, 500)},
    }


def predicted_level(taker_side: str, print_price: float) -> tuple[str, float] | None:
    """The (side, price) a print consumes under the complement mapping."""
    if taker_side == "no":
        return ("yes", round(print_price, 4))
    if taker_side == "yes":
        return ("no", round(1.0 - print_price, 4))
    return None


def naive_level(taker_side: str, print_price: float) -> tuple[str, float]:
    """The wrong same-side mapping, kept only to prove it does NOT fit."""
    return (taker_side, round(print_price, 4))


def check_market(conn, market_id: str, since: datetime) -> dict:
    """The one-market read: two `WHERE market_id=?` scans, then `check_rows`.

    This is the REFERENCE path the fixture tests pair against, not what
    `main` runs. A `WHERE market_id=?` read over a columnar table pays the
    whole window's row volume whatever its selectivity, so N markets through
    this function held `hyxstream.duckdb` -- the file `collector.streamd`
    flushes every 15s -- for 2N window scans (#147, the same shape fixed in
    `queuescore` the same morning). `main` streams one scan per table
    through `queuescore.iter_market_rows` and feeds `check_rows` the same
    rows in the same order (`tests/test_prioritycheck.py` asserts the two
    paths agree across chunk boundaries).
    """
    trades = conn.execute(
        "SELECT recv_ts, price, qty, taker_side FROM stream_trades"
        " WHERE venue='kalshi' AND market_id=? AND recv_ts > ? ORDER BY recv_ts",
        [market_id, since],
    ).fetchall()
    deltas = conn.execute(
        "SELECT recv_ts, side, price, qty FROM book_events"
        " WHERE venue='kalshi' AND market_id=? AND kind='delta' AND qty < 0"
        " AND recv_ts > ? ORDER BY recv_ts",
        [market_id, since],
    ).fetchall()
    return check_rows(market_id, trades, deltas)


def negative_deltas(events: list) -> list[tuple[datetime, str, float, float]]:
    """`(recv_ts, side, price, qty)` for the `kind='delta' AND qty < 0` rows of
    an `iter_market_rows` event group (columns: venue, market_id, recv_ts,
    src_ts, sid, seq, kind, side, price, qty), in the group's order."""
    return [(e[2], e[7], e[8], e[9]) for e in events if e[6] == "delta" and e[9] < 0]


def check_rows(market_id: str, trades: list, deltas: list) -> dict:
    """Pair one market's prints with its negative book deltas.

    `trades` rows are `(recv_ts, price, qty, taker_side)` and `deltas` rows
    `(recv_ts, side, price, qty)` with `qty < 0`, both ascending in time.
    """
    # Per (side, price) level: the decrements' timestamps and sizes in time
    # order, so a print's window is a `bisect` slice rather than a scan of
    # the level. A comprehension over the whole level per print cost
    # 206.7s of a 224.8s hold on streamd's file for eight KXBTC15M markets
    # (2026-10-29: 540k prints against levels of 10^4-10^5 decrements).
    pairs: dict[tuple[str, float], list[tuple[datetime, float]]] = defaultdict(list)
    for ts, side, price, qty in deltas:
        pairs[(side, round(price, 4))].append((ts, -qty))
    idx: dict[tuple[str, float], tuple[list[datetime], list[float]]] = {}
    for key, bucket in pairs.items():
        bucket.sort()
        idx[key] = ([ts for ts, _ in bucket], [dec for _, dec in bucket])

    def within(key, ts, window_s: float) -> list[tuple[datetime, float]]:
        """The level's decrements with |dts - ts| <= window, in time order."""
        bucket = idx.get(key)
        if bucket is None:
            return []
        tss, decs = bucket
        lo = bisect.bisect_left(tss, ts - timedelta(seconds=window_s))
        hi = bisect.bisect_right(tss, ts + timedelta(seconds=window_s))
        return list(zip(tss[lo:hi], decs[lo:hi], strict=True))

    res = {
        "market_id": market_id,
        "trades": len(trades),
        "neg_deltas": len(deltas),
        "exact_match": 0,  # exact-size decrement at predicted level, ±EXACT_WINDOW
        "absorb_match": 0,  # exact-size decrement at predicted level, ±ABSORB_WINDOW
        "naive_would_match": 0,  # no-match where same-side mapping WOULD match
        "late_decrement": 0,  # predicted decrement present but outside EXACT_WINDOW
        "no_decrement_at_level": 0,  # nothing at predicted level within ABSORB_WINDOW (gap)
        "batched_with_cancels": 0,  # decrement present but size != trade (merged)
        # pairing delay for EVERY absorb-window match, exact and late alike.
        # Conditioning this sample on EXACT_WINDOW would make the timing
        # block unfalsifiable: it could not report a delay wider than the
        # window it is quoted to justify.
        "dt_ms": [],
    }
    for ts, p, q, taker in trades:
        key = predicted_level(taker, p)
        near_exact = within(key, ts, EXACT_WINDOW)
        hit = next((dts for dts, dec in near_exact if abs(dec - q) < PRICE_EPS), None)
        if hit is not None:
            res["exact_match"] += 1
            res["absorb_match"] += 1
            res["dt_ms"].append((hit - ts).total_seconds() * 1000)
            continue
        # miss at the tight window: decompose it
        near_absorb = within(key, ts, ABSORB_WINDOW)
        late = next((dts for dts, dec in near_absorb if abs(dec - q) < PRICE_EPS), None)
        if late is not None:
            res["absorb_match"] += 1
            res["late_decrement"] += 1
            res["dt_ms"].append((late - ts).total_seconds() * 1000)
        elif not near_absorb:
            res["no_decrement_at_level"] += 1
        else:
            res["batched_with_cancels"] += 1
        # would the wrong mapping have fit? (must stay ~0)
        if any(
            abs(dec - q) < PRICE_EPS for _, dec in within(naive_level(taker, p), ts, ABSORB_WINDOW)
        ):
            res["naive_would_match"] += 1
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="verify the trade→book-decrement mapping")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--markets", type=int, default=8, help="top-N book-subscribed by prints")
    ap.add_argument(
        "--series",
        default=None,
        help="comma-separated Kalshi series (the whole first dash-token, e.g. KXHIGHNY,KXHIGHCHI);"
        " default = all, which since the 10-23 coverage expansion is KXBTC15M-dominated"
        " -- the 2026-09-20 reading was weather, so a drift read names the series",
    )
    ap.add_argument("--stream-db", default=STREAM_DB)
    ap.add_argument("--out", default="reports/priority_check")
    args = ap.parse_args()
    series = [x.strip() for x in args.series.split(",") if x.strip()] if args.series else None

    reset_attach_waits()
    # `held_attach`: every market is checked through a cursor on
    # `hyxstream.duckdb` -- the file `collector.streamd` OWNS -- so the hold
    # is the whole loop, measured and published (#91, #102). The selector is
    # `queuescore.select_markets`, not a copy: this module carried the SAME
    # unbounded correlated EXISTS that killed the weather bracket (#124),
    # one file over from the fix. The window aggregate stays bare on purpose
    # (statistics-folded; a venue bound makes it a 1.2e9-row scan).
    with held_attach(args.stream_db) as conn:
        since = conn.execute(
            "SELECT max(recv_ts) - INTERVAL 1 HOUR * CAST(? AS INTEGER) FROM book_events",
            [int(args.hours)],
        ).fetchone()[0]
        markets = select_markets(conn, since, args.markets, series)
        print(
            f"[prioritycheck] window since {since}, {len(markets)} markets"
            + (f", series={series}" if series else "")
        )

        # Inside the hold: ONE scan per table, streamed by market, each
        # market paired as its group completes -- `check_market`'s two
        # `WHERE market_id=?` reads per market were 2N window scans (#147).
        # `score_s` is the pairing's share of the hold; the rest is the scans.
        per_market = []
        dt_ms: list[float] = []
        score_s = 0.0
        rows_n: dict[str, tuple[int, int]] = {}
        # `rss_peak_mb` is two terms, measured 2026-10-30 on the live archive
        # (24h window, 8 KXBTC15M/NFL markets, 6.9M book rows): DuckDB's
        # buffer pool caching the scanned blocks -- `duckdb_memory()` tag
        # BASE_TABLE, 2.8-3.2 GB after `select_markets` alone, the same under
        # an EXISTS or a GROUP BY form, bounded only by `memory_limit` (half
        # the cgroup cap in a unit via `store.cgroup_memory_limit`; 80% of
        # host RAM ad hoc) -- and the largest market's Python rows, ~0.5 KB
        # per row (734k rows = 370 MB, 81 MB as negative deltas). Neither is
        # the pairing. So the peak is the cap's to bound, not the window's:
        # a unit that runs this needs `MemoryMax`, and an ad-hoc run on an
        # uncapped box lets DuckDB cache up to its host default.
        rss_peak = rss_anon_mb()
        for m, events, trades in iter_market_rows(conn, markets, since):
            deltas = negative_deltas(events)
            rows_n[m] = (len(events), len(trades))
            del events
            t_pair = time.monotonic()
            r = check_rows(m, trades, deltas)
            score_s += time.monotonic() - t_pair
            dt_ms.extend(r.pop("dt_ms"))
            per_market.append(r)
            print(
                f"  {m}: trades={r['trades']} exact={r['exact_match']} absorb={r['absorb_match']}"
            )
            rss = rss_anon_mb()
            if rss is not None and (rss_peak is None or rss > rss_peak):
                rss_peak = rss
    # After the RELEASE: `held_s` is charged by the close.
    wait = attach_wait_block(db=Path(args.stream_db).name)
    cost = cost_block(wait, score_s=score_s, rows=rows_n, rss_peak_mb=rss_peak)
    print(f"[prioritycheck] cost: {cost_line(cost, compute='pairing')}")

    tot = sum(r["trades"] for r in per_market)
    agg = {
        k: sum(r[k] for r in per_market)
        for k in (
            "exact_match",
            "absorb_match",
            "naive_would_match",
            "late_decrement",
            "no_decrement_at_level",
            "batched_with_cancels",
        )
    }
    dt_ms.sort()
    timing = {}
    if dt_ms:
        # the population is every absorb-window match; the exact-window
        # sub-block is the same statistics conditioned on |dt| <= 5ms,
        # which is what readings before 2026-09-20 published as `timing`.
        timing = {"population": "absorb_match", **_timing(dt_ms)}
        tight = [x for x in dt_ms if abs(x) <= EXACT_WINDOW * 1000]
        if tight:
            timing["exact_window"] = _timing(tight)
    report = {
        "generated_at": str(datetime.now(UTC).replace(tzinfo=None, microsecond=0)),
        "window_hours": args.hours,
        "series": series,
        "markets": len(markets),
        "trades": tot,
        "exact_match_frac": round(agg["exact_match"] / tot, 4) if tot else None,
        "absorb_match_frac": round(agg["absorb_match"] / tot, 4) if tot else None,
        **agg,
        "timing": timing,
        "note": (
            "absorb_match_frac = prints with an exact-size decrement at the"
            " PREDICTED complement level within the model's 2s window."
            " naive_would_match must stay ~0: it is the same-side mapping the"
            " complement rule replaces — a nonzero value would mean the"
            " mapping is ambiguous. no_decrement_at_level is coverage (gap),"
            " not a mapping miss. `timing` is the pairing delay over EVERY"
            " absorb-window match, late ones included, so it can report a"
            " delay wider than the window it sizes; `timing.exact_window`"
            " is the same statistics conditioned on |dt| <= 5ms, which is"
            " what readings before 2026-09-20 published as `timing`."
            " Verifies WHICH level a trade consumes, not"
            " front-vs-back order within it (that is the pess/opt bracket)."
        ),
        "per_market": per_market,
        "attach_wait": wait,
        "cost": cost,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    for k, v in report.items():
        if k not in ("per_market", "note", "attach_wait", "cost"):
            print(f"  {k}: {v}")
    print(attach_wait_line(wait, prefix="[prioritycheck]"))
    print(f"[prioritycheck] written to {out}")


if __name__ == "__main__":
    main()
