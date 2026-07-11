"""Calibration atlas (B6/C3): implied vs realized probability by
(venue-category, price decile, horizon) over settled Kalshi markets.

    python -m simulator.atlas [--db data/hyxlab.duckdb]

Method (proposal §C3): for each settled market and each horizon h in
{1h, 6h, 24h, 72h, 7d} before close, take the LAST clean hourly-candle
mid at or before close−h (crossed candles and empty-book sentinels
excluded — the documented 1.3% defect class). Bucket by (category,
price decile, horizon); per bucket report implied p̄ = mean mid,
realized r = share settled yes, Wilson 95% interval on r, and n.

Buckets with n ≥ 200 where p̄ falls OUTSIDE the Wilson interval are
flagged as candidate inefficiencies (the favorite-longshot signature
appears as realized > implied in the top deciles). A flag is a lead
for a pre-registered strategy, never a verdict by itself.

Output: reports/atlas/<ts>.json + printed markdown table of flags.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

HORIZONS = [("1h", 1), ("6h", 6), ("24h", 24), ("72h", 72), ("7d", 168)]
Z95 = 1.959963985


def wilson(successes: float, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson 95% score interval for a binomial proportion."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


BUCKET_SQL = """
WITH settled AS (
  SELECT m.market_id, m.close_time, m.result,
         coalesce(s.category, '?') AS category
  FROM markets m
  LEFT JOIN series s ON s.venue = m.venue AND s.ticker = m.series
  WHERE m.venue='kalshi' AND m.result IN ('yes','no')
    AND m.close_time IS NOT NULL
), pts AS (
  SELECT st.market_id, st.category, st.result, h.h_label,
         arg_max((c.yes_bid_close + c.yes_ask_close) / 2, c.end_ts) AS mid
  FROM settled st
  CROSS JOIN (VALUES ('1h',1),('6h',6),('24h',24),('72h',72),('7d',168))
       AS h(h_label, h_hours)
  JOIN candles c ON c.venue='kalshi' AND c.market_id = st.market_id
    AND c.end_ts <= st.close_time - INTERVAL 1 HOUR * h.h_hours
    AND c.yes_bid_close IS NOT NULL AND c.yes_ask_close IS NOT NULL
    AND c.yes_bid_close <= c.yes_ask_close             -- crossed-candle gate
    AND NOT (c.yes_ask_close >= 0.995 AND c.yes_bid_close <= 0.005)  -- sentinel
  GROUP BY 1, 2, 3, 4
)
SELECT category, h_label,
       CAST(least(floor(mid * 10), 9) AS INTEGER) AS decile,
       count(*) AS n, avg(mid) AS implied,
       avg(CASE WHEN result = 'yes' THEN 1.0 ELSE 0.0 END) AS realized
FROM pts
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""


def build_atlas(conn) -> dict:
    rows = conn.execute(BUCKET_SQL).fetchall()
    buckets = []
    for category, h_label, decile, n, implied, realized in rows:
        lo, hi = wilson(realized * n, n)
        flagged = n >= 200 and not (lo <= implied <= hi)
        buckets.append(
            {
                "category": category,
                "horizon": h_label,
                "decile": decile,
                "n": n,
                "implied": round(implied, 4),
                "realized": round(realized, 4),
                "wilson_lo": round(lo, 4),
                "wilson_hi": round(hi, 4),
                "flagged": flagged,
            }
        )
    fingerprint = {
        "settled_markets": conn.execute(
            "SELECT count(*) FROM markets WHERE venue='kalshi' AND result IN ('yes','no')"
        ).fetchone()[0],
        "candles": conn.execute("SELECT count(*) FROM candles").fetchone()[0],
    }
    return {
        "generated_at": str(datetime.now(UTC).replace(tzinfo=None, microsecond=0)),
        "data_fingerprint": fingerprint,
        "flag_rule": "n >= 200 and implied outside Wilson 95% of realized",
        "buckets": buckets,
        "flagged": [b for b in buckets if b["flagged"]],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="calibration atlas: implied vs realized")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--out", default="reports/atlas")
    args = ap.parse_args()

    conn = None
    for attempt in range(15):
        try:
            conn = duckdb.connect(args.db, read_only=True)
            break
        except duckdb.IOException:
            if attempt == 14:
                raise
            time.sleep(2)
    atlas = build_atlas(conn)
    conn.close()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(atlas, indent=1) + "\n")

    flags = atlas["flagged"]
    print(f"[atlas] {len(atlas['buckets'])} buckets, {len(flags)} flagged")
    if flags:
        print("| category | horizon | decile | n | implied | realized | wilson |")
        print("|---|---|---|---|---|---|---|")
        for b in sorted(flags, key=lambda b: -b["n"]):
            print(
                f"| {b['category']} | {b['horizon']} | {b['decile']} | {b['n']}"
                f" | {b['implied']} | {b['realized']}"
                f" | [{b['wilson_lo']}, {b['wilson_hi']}] |"
            )
    print(f"[atlas] written to {out}")


if __name__ == "__main__":
    main()
