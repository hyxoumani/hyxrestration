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
- TIMING: the distribution of (decrement_ts - print_ts), which sizes the
  ±1ms claim and confirms ABSORB_WINDOW=2s is generous, not tight.

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
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from hyxlab.store import connect_retry

STREAM_DB = "data/hyxstream.duckdb"
EXACT_WINDOW = 0.005  # s: tight pairing for the exact-match rate / timing stats
ABSORB_WINDOW = 2.0  # s: the queuebounds model's own pairing horizon
PRICE_EPS = 1e-9


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

    idx: dict[tuple[str, float], list[tuple[datetime, float]]] = defaultdict(list)
    for ts, side, price, qty in deltas:
        idx[(side, round(price, 4))].append((ts, -qty))
    for bucket in idx.values():
        bucket.sort()

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
        "dt_ms": [],
    }
    for ts, p, q, taker in trades:
        key = predicted_level(taker, p)
        near_exact = [
            (dts, dec) for dts, dec in idx.get(key, []) if abs((dts - ts).total_seconds()) <= EXACT_WINDOW
        ]
        hit = next((dts for dts, dec in near_exact if abs(dec - q) < PRICE_EPS), None)
        if hit is not None:
            res["exact_match"] += 1
            res["absorb_match"] += 1
            res["dt_ms"].append((hit - ts).total_seconds() * 1000)
            continue
        # miss at the tight window: decompose it
        near_absorb = [
            (dts, dec) for dts, dec in idx.get(key, []) if abs((dts - ts).total_seconds()) <= ABSORB_WINDOW
        ]
        if any(abs(dec - q) < PRICE_EPS for _, dec in near_absorb):
            res["absorb_match"] += 1
            res["late_decrement"] += 1
        elif not near_absorb:
            res["no_decrement_at_level"] += 1
        else:
            res["batched_with_cancels"] += 1
        # would the wrong mapping have fit? (must stay ~0)
        naive = naive_level(taker, p)
        if any(
            abs((dts - ts).total_seconds()) <= ABSORB_WINDOW and abs(dec - q) < PRICE_EPS
            for dts, dec in idx.get(naive, [])
        ):
            res["naive_would_match"] += 1
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="verify the trade→book-decrement mapping")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--markets", type=int, default=8, help="top-N book-subscribed by prints")
    ap.add_argument("--stream-db", default=STREAM_DB)
    ap.add_argument("--out", default="reports/priority_check")
    args = ap.parse_args()

    conn = connect_retry(args.stream_db)
    since = conn.execute(
        "SELECT max(recv_ts) - INTERVAL 1 HOUR * CAST(? AS INTEGER) FROM book_events",
        [int(args.hours)],
    ).fetchone()[0]
    markets = [
        r[0]
        for r in conn.execute(
            "SELECT t.market_id FROM stream_trades t WHERE t.venue='kalshi'"
            " AND t.recv_ts > ? AND EXISTS (SELECT 1 FROM book_events b"
            "   WHERE b.market_id = t.market_id AND b.kind='delta')"
            " GROUP BY 1 ORDER BY count(*) DESC LIMIT ?",
            [since, args.markets],
        ).fetchall()
    ]
    print(f"[prioritycheck] window since {since}, {len(markets)} markets")

    per_market = []
    dt_ms: list[float] = []
    for m in markets:
        r = check_market(conn, m, since)
        dt_ms.extend(r.pop("dt_ms"))
        per_market.append(r)
        print(f"  {m}: trades={r['trades']} exact={r['exact_match']} absorb={r['absorb_match']}")
    conn.close()

    tot = sum(r["trades"] for r in per_market)
    agg = {k: sum(r[k] for r in per_market) for k in ("exact_match", "absorb_match", "naive_would_match", "late_decrement", "no_decrement_at_level", "batched_with_cancels")}
    dt_ms.sort()
    timing = {}
    if dt_ms:
        timing = {
            "min_ms": round(dt_ms[0], 3),
            "median_ms": round(statistics.median(dt_ms), 3),
            "p95_ms": round(dt_ms[int(len(dt_ms) * 0.95)], 3),
            "max_ms": round(dt_ms[-1], 3),
            "within_1ms_frac": round(sum(1 for x in dt_ms if abs(x) <= 1) / len(dt_ms), 4),
        }
    report = {
        "generated_at": str(datetime.now().replace(microsecond=0)),
        "window_hours": args.hours,
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
            " not a mapping miss. Verifies WHICH level a trade consumes, not"
            " front-vs-back order within it (that is the pess/opt bracket)."
        ),
        "per_market": per_market,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    for k, v in report.items():
        if k not in ("per_market", "note"):
            print(f"  {k}: {v}")
    print(f"[prioritycheck] written to {out}")


if __name__ == "__main__":
    main()
