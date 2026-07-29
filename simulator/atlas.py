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

Correlation caveat, made quantitative (2026-07-19): sibling strikes of
one ladder (same series, same close_time) settle on ONE underlying
outcome, so per-market n overstates the evidence — the 07-18 Financials
cohort showed hundreds of KXDJI/KXINXU markets moving as ~2 day-
outcomes. Each bucket therefore also reports `clusters` = distinct
(series, close_time) groups and a `flagged_robust` tier: the Wilson
interval recomputed with n = clusters (the perfect-within-cluster-
correlation worst case; true confidence lies between the two tiers).
The original `flagged` field is unchanged for cross-report comparability.

Correlation caveat, one level up (2026-07-28): ladders are not independent
of each other either. All same-day Financials ladders resolve off ONE index
path, so a whole settlement day is closer to one draw than to `clusters`
draws — measured on the 07-28 run, Financials 24h d3/d4/d5/d6 ALL flipped
gap sign together when 07-27 landed, and that single day supplied 46-61% of
each bucket's n across only ~16 "clusters". Each bucket therefore also
reports `days` (distinct settlement days), `top_day_share` (largest single
day's share of n — the concentration diagnostic) and a `flagged_day_robust`
tier: Wilson with n = days, the perfect-within-DAY-correlation worst case.
The tiers nest (days <= clusters <= n), so each is strictly more
conservative than the last. The day tier is deliberately too harsh for
categories whose same-day markets have unrelated underlyings (weather across
cities); it is a bound, not an estimate. `top_day_share` is the tier-neutral
read: a bucket carrying most of its evidence on one day is a bet on that
day, not a calibration finding.

Reproducibility (2026-07-29): the last several passes' method is "diff two
atlas reports and chase the drift", which silently assumes that running the
atlas twice on unchanged data gives the same numbers. It did not. `implied`
was `avg(mid)`, and DuckDB accumulates a floating-point average in an order
that depends on how it parallelises the scan — so the low bits move between
identical runs. Measured over 8 back-to-back runs of the same query on the
same connection: 238 of 260 buckets returned a different raw `implied`, and
THREE flipped their reported 4-decimal value (Climate and Weather 1h d2
0.2371<->0.2372, Climate and Weather 7d d3 0.3637<->0.3638, Science and
Technology 72h d2 0.2612<->0.2613) because their exact mean lands on a
rounding boundary. No past conclusion is affected — the phantom is 1e-4 and
the smallest drift ever chased here was 0.033 — but a future pass diffing
reports would have found up to three buckets "drifting" with no new data, and
`flagged` itself was non-deterministic for any bucket whose implied sat on a
Wilson endpoint. `implied` is now summed in exact DECIMAL, which is
order-independent. This is a property of production-scale parallelism and
cannot be reproduced on a unit-test fixture, so the regression test asserts
the mechanism (no floating-point `avg` in the implied projection) plus exact
correctness on a hand-computed fixture.

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
  SELECT m.market_id, m.close_time, m.result, m.series,
         coalesce(s.category, '?') AS category
  FROM markets m
  LEFT JOIN series s ON s.venue = m.venue AND s.ticker = m.series
  WHERE m.venue='kalshi' AND m.result IN ('yes','no')
    AND m.close_time IS NOT NULL
), pts AS (
  SELECT st.market_id, st.category, st.result, h.h_label,
         st.series, st.close_time,
         arg_max((c.yes_bid_close + c.yes_ask_close) / 2, c.end_ts) AS mid
  FROM settled st
  CROSS JOIN (VALUES ('1h',1),('6h',6),('24h',24),('72h',72),('7d',168))
       AS h(h_label, h_hours)
  JOIN candles c ON c.venue='kalshi' AND c.market_id = st.market_id
    AND c.end_ts <= st.close_time - INTERVAL 1 HOUR * h.h_hours
    AND c.yes_bid_close IS NOT NULL AND c.yes_ask_close IS NOT NULL
    AND c.yes_bid_close <= c.yes_ask_close             -- crossed-candle gate
    AND NOT (c.yes_ask_close >= 0.995 AND c.yes_bid_close <= 0.005)  -- sentinel
  GROUP BY 1, 2, 3, 4, 5, 6
), keyed AS (
  SELECT category, h_label, result, series, close_time, mid,
         CAST(least(floor(mid * 10), 9) AS INTEGER) AS decile,
         CAST(close_time AS DATE) AS close_day
  FROM pts
), agg AS (
  SELECT category, h_label, decile,
         count(*) AS n,
         -- NOT avg(mid): see the reproducibility note in the module docstring.
         -- DuckDB accumulates a floating-point avg in a parallelism-dependent
         -- order, so `implied` was not reproducible run-to-run. Exact DECIMAL
         -- addition is order-independent, so this is.
         CAST(sum(CAST(mid AS DECIMAL(18, 6))) AS DOUBLE) / count(*) AS implied,
         -- realized is already reproducible: a sum of exact 1.0/0.0 doubles is
         -- itself exact, so summation order cannot change it. Measured, not
         -- assumed — it never varied across 8 identical production runs.
         avg(CASE WHEN result = 'yes' THEN 1.0 ELSE 0.0 END) AS realized,
         count(DISTINCT (series, close_time)) AS clusters,
         count(DISTINCT close_day) AS days
  FROM keyed
  GROUP BY 1, 2, 3
), per_day AS (
  SELECT category, h_label, decile, close_day, count(*) AS day_n
  FROM keyed
  GROUP BY 1, 2, 3, 4
), top_day AS (
  SELECT category, h_label, decile, max(day_n) AS top_day_n
  FROM per_day
  GROUP BY 1, 2, 3
)
SELECT a.category, a.h_label, a.decile, a.n, a.implied, a.realized,
       a.clusters, a.days, t.top_day_n
FROM agg a
JOIN top_day t USING (category, h_label, decile)
ORDER BY 1, 2, 3
"""


SETTLED_BY_CATEGORY_SQL = """
SELECT coalesce(s.category, '?') AS category, count(*) AS n
FROM markets m
LEFT JOIN series s ON s.venue = m.venue AND s.ticker = m.series
WHERE m.venue='kalshi' AND m.result IN ('yes','no') AND m.close_time IS NOT NULL
GROUP BY 1
ORDER BY 1
"""


def _observations_by_category_horizon(buckets: list[dict]) -> dict[str, int]:
    """Per (category, horizon) observation counts, keyed 'category|horizon'."""
    out: dict[str, int] = {}
    for b in buckets:
        key = f"{b['category']}|{b['horizon']}"
        out[key] = out.get(key, 0) + b["n"]
    return out


def build_atlas(conn) -> dict:
    rows = conn.execute(BUCKET_SQL).fetchall()
    buckets = []
    for row in rows:
        (category, h_label, decile, n, implied, realized, clusters, days, top_day_n) = row
        lo, hi = wilson(realized * n, n)
        flagged = n >= 200 and not (lo <= implied <= hi)
        # worst case: every market in a (series, close_time) ladder settles
        # on one shared outcome, so at most `clusters` independent draws
        rlo, rhi = wilson(realized * clusters, clusters)
        flagged_robust = flagged and not (rlo <= implied <= rhi)
        # one level up: every ladder closing on the same day can share one
        # underlying path (index ladders do), so at most `days` draws
        dlo, dhi = wilson(realized * days, days)
        buckets.append(
            {
                "category": category,
                "horizon": h_label,
                "decile": decile,
                "n": n,
                "clusters": clusters,
                "days": days,
                "top_day_share": round(top_day_n / n, 4) if n else 0.0,
                "implied": round(implied, 4),
                "realized": round(realized, 4),
                "wilson_lo": round(lo, 4),
                "wilson_hi": round(hi, 4),
                "flagged": flagged,
                "wilson_robust_lo": round(rlo, 4),
                "wilson_robust_hi": round(rhi, 4),
                "flagged_robust": flagged_robust,
                "wilson_day_lo": round(dlo, 4),
                "wilson_day_hi": round(dhi, 4),
                "flagged_day_robust": flagged_robust and not (dlo <= implied <= dhi),
            }
        )
    fingerprint = {
        "settled_markets": conn.execute(
            "SELECT count(*) FROM markets WHERE venue='kalshi' AND result IN ('yes','no')"
        ).fetchone()[0],
        "candles": conn.execute("SELECT count(*) FROM candles").fetchone()[0],
        # per-category counts: a bucket reading is only an INDEPENDENT
        # confirmation if its category actually gained settled markets since
        # the prior run. Index-ladder categories (Financials) gain nothing
        # over a weekend, so consecutive "flat" readings there can be the
        # same data re-measured rather than new evidence.
        "settled_by_category": dict(
            conn.execute(SETTLED_BY_CATEGORY_SQL).fetchall()
        ),
        # one level finer, and the granularity that actually matters: the
        # bucket key is (category, HORIZON, decile), and a market only enters
        # the horizon-h bucket if it carries a candle h before its close.
        # Same-day index ladders (Financials 24h) therefore gain nothing from
        # an increment that adds thousands of settled Financials markets, so
        # settled_by_category can read "+1113 new evidence" over buckets that
        # are bit-identical. Summed from `buckets` rather than re-queried, so
        # it describes exactly the population the buckets are built from.
        "observations_by_category_horizon": _observations_by_category_horizon(
            buckets
        ),
    }
    return {
        "generated_at": str(datetime.now(UTC).replace(tzinfo=None, microsecond=0)),
        "data_fingerprint": fingerprint,
        "flag_rule": "n >= 200 and implied outside Wilson 95% of realized",
        "flag_rule_robust": (
            "flagged AND implied outside Wilson 95% with n = clusters "
            "(distinct (series, close_time) ladders; perfect-correlation worst case)"
        ),
        "flag_rule_day_robust": (
            "flagged_robust AND implied outside Wilson 95% with n = days "
            "(distinct settlement days; same-day ladders can share one "
            "underlying path — perfect-within-day-correlation worst case)"
        ),
        "buckets": buckets,
        "flagged": [b for b in buckets if b["flagged"]],
        "flagged_robust": [b for b in buckets if b["flagged_robust"]],
        "flagged_day_robust": [b for b in buckets if b["flagged_day_robust"]],
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
    print(
        f"[atlas] {len(atlas['buckets'])} buckets, {len(flags)} flagged"
        f" ({len(atlas['flagged_robust'])} cluster-robust)"
    )
    if flags:
        print("| category | horizon | decile | n | clusters | implied | realized | wilson | robust |")
        print("|---|---|---|---|---|---|---|---|---|")
        for b in sorted(flags, key=lambda b: -b["n"]):
            print(
                f"| {b['category']} | {b['horizon']} | {b['decile']} | {b['n']}"
                f" | {b['clusters']} | {b['implied']} | {b['realized']}"
                f" | [{b['wilson_lo']}, {b['wilson_hi']}]"
                f" | {'YES' if b['flagged_robust'] else 'no'} |"
            )
    print(f"[atlas] written to {out}")


if __name__ == "__main__":
    main()
