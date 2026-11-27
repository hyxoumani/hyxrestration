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

from hyxlab.store import attach_wait_block, attach_wait_line, held_open, reset_attach_waits

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


#: Two venues' words for one thing, folded to one spelling before scoring.
#: Measured 2026-11-27 on the 11-27 backup slot: Kalshi's "Will the Federal
#: Reserve Cut rates by 25bps at their December 2026 meeting?" against
#: Polymarket's "Will the Fed decrease interest rates by 25 bps after the
#: December 2026 meeting?" -- the same event, the same partition, closing 5 h
#: apart -- scored 0.29 and was never reported, below "Reserve Bank of
#: Australia" (0.36, sharing the word `reserve`). Folded, it scores 0.80.
#: Keep this to words that mean the same on BOTH venues; a fold that merges
#: two events (a bank's name, a month) is worse than a miss.
CANON: dict[str, tuple[str, ...]] = {
    "fed": ("federal", "reserve"),
    "fomc": ("federal", "reserve"),
    "decrease": ("cut",),
    "decreases": ("cut",),
    "cuts": ("cut",),
    "lower": ("cut",),
    "lowers": ("cut",),
    "increase": ("hike",),
    "increases": ("hike",),
    "hikes": ("hike",),
    "raise": ("hike",),
    "raises": ("hike",),
    "rate": ("rates",),
    "bp": ("bps",),
}

#: "25bps" and "25 bps" are one quantity written two ways.
_BPS = re.compile(r"(\d+(?:\.\d+)?)(bps?)\b")


def tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9.]+", _BPS.sub(r"\1 \2", title.lower()))
    out: set[str] = set()
    for w in words:
        for c in CANON.get(w, (w,)):
            if c not in STOP and len(c) > 1:
                out.add(c)
    return frozenset(out)


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


def event_of(kalshi_id: str) -> str:
    """A Kalshi market id minus its strike leg: `KXFED-26DEC-T4.25` ->
    `KXFED-26DEC`. An id with no leg is its own event."""
    return kalshi_id.rsplit("-", 1)[0] if "-" in kalshi_id else kalshi_id


def by_event(rows: list[dict]) -> list[dict]:
    """One row per Kalshi EVENT: its best pair and how many pairs stand
    behind it. A strike ladder matched against a ladder is n x m rows of one
    lead -- on 2026-11-27 one series (KXIPO) was 499 of 1,677 rows and the
    ranked top 100 named 5 of the 55 series that matched."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(event_of(r["kalshi_id"]), []).append(r)
    out = []
    for ev, rs in groups.items():
        best = max(rs, key=lambda r: r["score"])
        out.append(
            {
                "event": ev,
                "score": best["score"],
                "pairs": len(rs),
                "kalshi_markets": len({r["kalshi_id"] for r in rs}),
                "poly_markets": len({r["poly_id"] for r in rs}),
                "best": best,
            }
        )
    out.sort(key=lambda e: (-e["score"], e["event"]))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="cross-venue pair candidates (leads only)")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--min-score", type=float, default=0.4)
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--out", default="reports/pairs")
    args = ap.parse_args(argv)

    reset_attach_waits()
    now = datetime.now(UTC).replace(tzinfo=None)
    # `held_open`: this is the 5-minute collector's file, so even two
    # title scans exclude a capture cycle for their length -- measured and
    # printed after the release (#91, #96).
    with held_open(args.db, read_only=True) as store:
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
    # After the release: `held_s` is charged by the close.
    print(attach_wait_line(attach_wait_block(db=Path(args.db).name), prefix="[pairs]"))

    scored = candidates(kalshi, poly, args.min_score)
    events = by_event(scored)
    rows = scored[: args.top]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    (out_dir / f"{stamp}.json").write_text(json.dumps(rows, indent=1, default=str) + "\n")

    md = [
        f"# Cross-venue pair candidates — {stamp}",
        "",
        f"{len(kalshi)} open Kalshi × {len(poly)} active Polymarket; "
        f"{len(scored)} candidate pair(s) at score ≥ {args.min_score} over"
        f" {len(events)} Kalshi event(s); the top {len(rows)} pair(s) are listed.",
        "",
        "**Leads only.** A pair activates in `watchlist.json` ONLY after the",
        "user verifies both venues' resolution rules coincide (source,",
        "cutoff, rounding).",
        "",
        "## By Kalshi event (best pair of each)",
        "",
        "| score | pairs | K mkts | P mkts | Δclose (d) | event | best kalshi | best polymarket |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in events:
        b = e["best"]
        md.append(
            f"| {e['score']} | {e['pairs']} | {e['kalshi_markets']} | {e['poly_markets']}"
            f" | {b['close_delta_days']} | `{e['event']}` | {b['kalshi_title']}"
            f" | {b['poly_title']} (`{b['poly_id'][:16]}…`) |"
        )
    md += [
        "",
        "## Pairs, ranked",
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
    print(
        f"[pairs] {len(scored)} candidate pair(s) over {len(events)} Kalshi event(s)"
        f" -> {out_dir / f'{stamp}.md'}"
    )
    for e in events[:10]:
        b = e["best"]
        print(
            f"  {e['score']:.2f}  x{e['pairs']:<4d} {e['event'][:24]:24s}"
            f" {b['kalshi_title'][:40]:40s} ~ {b['poly_title'][:40]}"
        )


if __name__ == "__main__":
    main()
