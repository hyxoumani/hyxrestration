"""Cross-venue pair candidates report (last B3.5 checkbox).

    python -m simulator.pair_candidates [--min-score 0.4] [--top 100]

Matches OPEN Kalshi markets against ACTIVE Polymarket markets by
normalized-title token overlap (Jaccard) with close-time proximity,
and emits a ranked candidate list for HUMAN verification.

Candidates are leads only: a pair enters `watchlist.json`
(`polymarket_pairs`) exclusively after the USER hand-verifies that the
two venues' resolution rules actually coincide — subtle mismatches
(source, cutoff time, rounding) are precisely where cross-venue "arb"
loses money. This tool never activates anything.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hyxlab.store import open_retry

STOP = {
    "will",
    "the",
    "a",
    "an",
    "in",
    "on",
    "at",
    "by",
    "of",
    "to",
    "be",
    "for",
    "or",
    "and",
    "is",
    "was",
    "before",
    "after",
    "than",
    "what",
    "who",
    "how",
    "many",
    "much",
    "win",
    "wins",
    "reach",
    "hit",
    "above",
    "below",
    "between",
    "more",
    "less",
    "over",
    "under",
    "up",
    "down",
}


def tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9.]+", title.lower())
    return frozenset(w for w in words if w not in STOP and len(w) > 1)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def candidates(kalshi: list[tuple], poly: list[tuple], min_score: float) -> list[dict]:
    """Rows: (market_id, title, close_time). Returns scored matches."""
    p_tokens = [(mid, t, ct, tokens(t)) for mid, t, ct in poly]
    out = []
    for k_mid, k_title, k_close in kalshi:
        kt = tokens(k_title)
        for p_mid, p_title, p_close, pt in p_tokens:
            s = jaccard(kt, pt)
            if s < min_score:
                continue
            dt_days = (
                abs((k_close - p_close).total_seconds()) / 86400 if k_close and p_close else None
            )
            if dt_days is not None and dt_days > 3:
                continue
            out.append(
                {
                    "score": round(s, 3),
                    "kalshi_id": k_mid,
                    "kalshi_title": k_title,
                    "poly_id": p_mid,
                    "poly_title": p_title,
                    "close_delta_days": None if dt_days is None else round(dt_days, 2),
                }
            )
    out.sort(key=lambda r: -r["score"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="cross-venue pair candidates (leads only)")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--min-score", type=float, default=0.4)
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--out", default="reports/pairs")
    args = ap.parse_args()

    store = open_retry(args.db, read_only=True)
    now = datetime.now(UTC).replace(tzinfo=None)
    kalshi = store.conn.execute(
        "SELECT market_id, title, close_time FROM markets WHERE venue='kalshi'"
        " AND result='' AND title IS NOT NULL AND close_time > ?",
        [now],
    ).fetchall()
    poly = store.conn.execute(
        "SELECT market_id, title, close_time FROM markets WHERE venue='polymarket'"
        " AND result='' AND title IS NOT NULL"
        " AND (close_time IS NULL OR close_time > ?)",
        [now - timedelta(days=1)],
    ).fetchall()
    store.close()

    rows = candidates(kalshi, poly, args.min_score)[: args.top]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    (out_dir / f"{stamp}.json").write_text(json.dumps(rows, indent=1, default=str) + "\n")

    md = [
        f"# Cross-venue pair candidates — {stamp}",
        "",
        f"{len(kalshi)} open Kalshi × {len(poly)} active Polymarket; "
        f"{len(rows)} candidates at score ≥ {args.min_score}.",
        "",
        "**Leads only.** A pair activates in `watchlist.json` ONLY after the",
        "user verifies both venues' resolution rules coincide (source,",
        "cutoff, rounding).",
        "",
        "| score | Δclose (d) | kalshi | polymarket |",
        "|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| {r['score']} | {r['close_delta_days']} | {r['kalshi_title']}"
            f" (`{r['kalshi_id']}`) | {r['poly_title']} (`{r['poly_id'][:16]}…`) |"
        )
    (out_dir / f"{stamp}.md").write_text("\n".join(md) + "\n")
    print(f"[pairs] {len(rows)} candidates -> {out_dir / f'{stamp}.md'}")
    for r in rows[:10]:
        print(f"  {r['score']:.2f}  {r['kalshi_title'][:48]:48s} ~ {r['poly_title'][:48]}")


if __name__ == "__main__":
    main()
