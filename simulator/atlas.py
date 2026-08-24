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

Correlation caveat ACROSS buckets (2026-07-29): the three tiers above all
bound correlation inside one bucket. Nothing bounded it between buckets, and
the horizon dimension duplicates evidence by construction — a market enters up
to five horizon buckets and its `result` is the SAME in all of them (89,371
settled markets currently produce 205,602 bucket observations, 2.3x reuse). So
a tier's survivor COUNT is a count of buckets, not of findings: measured on the
07-29 archive, Climate and Weather 1h d0 and 6h d0 share 98.8% of the smaller
bucket's markets. Every report therefore carries `cross_bucket_overlap`, which
unions survivors sharing >= 30% of the smaller bucket's markets and reports
`groups`. On the 07-29 data: 91 flagged -> 28 groups, 67 cluster-robust -> 28,
16 day-robust -> 11. Share is measured against the SMALLER bucket because a
small bucket wholly contained in a large one is fully redundant however small a
fraction of the large one it is. `groups` is a LOWER bound and `buckets` an
UPPER bound on distinct findings — union-find is transitive, so a chain of
adjacent deciles linked pairwise collapses into one group even where the ends
share nothing (the Commodities d0-d4 group). Same standing as the day tier: a
bound, not an estimate.

Day weighting (2026-07-30): the day tier above is internally inconsistent. It
takes its sample size from days (`wilson(realized * days, days)`) but its point
estimate from markets — `realized` and `implied` are both market-weighted. So a
day carrying 106 markets outvotes a day carrying 1 market 106:1 in the mean
while both count as a single draw in n, which is exactly the correlation the
tier exists to bound. Measured on the 07-30 archive over the 13 day-robust
survivors: re-weighting both sides so each day contributes once shrinks
Financials 24h d8 from +0.1289 to +0.0208 (6.2x) and Economics 1h d6 from
+0.1453 to +0.0444 (3.3x); no survivor flips sign, and the effect is NOT
uniformly conservative — it inflates a gap wherever the largest days happen to
agree with the signature. Every bucket therefore also reports
`implied_day_weighted` / `realized_day_weighted` and a `flagged_day_weighted`
tier, which is the day tier with the same unit on both sides. The existing
`flagged_day_robust` and its market-weighted `implied`/`realized` are unchanged
for cross-report comparability, per the divergence-matcher / day-tier /
overlap-tier / bracket-concentration precedent — unlike the QA seq headline,
this one is a coarser valid measure rather than an artifact, so it is kept.

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

Tier stability (2026-08-01): the pass method above — diff two atlas reports
and chase the drift — reads a tier as a COUNT, and a count cannot tell a
stable set of survivors from a set of equal size whose members swap every
reading. Measured over the archive, the difference is real and it corrects
this log's own narrative: five buckets have LEFT the day-robust tier and
RETURNED, all five Financials mid/high deciles, and the 07-31 reading of
"both day-robust demotions are again HIGH deciles" — taken then as the
signature narrowing to fading longshots — was both of them dropping out for
a single reading. `flagged_day_weighted` over the same span has zero
re-entries and identical membership across three readings. Reports therefore
carry `tier_stability`: per tier the churn against the last distinct data
state, and per surviving bucket `persistence` and `reentered`.

Three units of counting decide whether that number means anything. A reading
is a distinct DATA state, not a report file — the archive holds three
duplicate-`data_fingerprint` pairs, each a re-run minutes after shipping a
tier, and each would contribute a guaranteed-zero churn step. Dedup keeps the
LAST report per state, since that re-run is exactly how a new tier first
appears. A tier's denominator counts only readings whose report CARRIES that
tier, or `flagged_day_weighted` reads 2/21 for a tier that has never lost a
member. And a bucket's denominator counts only readings where it was
ELIGIBLE (present in `buckets`), because a bucket below n>=200 is absent from
the data, not absent from the tier. A stable tier is still only a stable
lead; pre-registration decides.

Quoted books (2026-08-02): every tier above bounds CORRELATION, and none of
them bounds whether `implied` was a price. `mid` is (bid + ask) / 2, so a
book quoted 0.01 / 0.96 contributes mid = 0.485 — indistinguishable from a
market the world genuinely thinks is a coin flip. The crossed-candle gate and
the 0.995/0.005 sentinel were both meant to exclude empty books and both test
a CORNER rather than a WIDTH, so that book passes them.

The consequence is not hypothetical and it runs the wrong way: measured over
the archive on 2026-08-02, the share of observations with spread > 0.5 RISES
with tier strictness — 4.6% flagged, 5.3% cluster-robust, 18.1% day-robust,
**34.2% day-weighted** (71.9% at spread > 0.2). The strictest tier is the
most contaminated one, because the tiers select on gap SIZE and an empty
book manufactures the largest gaps. Statistical strictness cannot fix this:
the artifact is systematic, not noisy, so an empty book is stably empty and
more days of it TIGHTEN the Wilson interval and make the bucket MORE
robust. It is also why `tier_stability` reads zero oscillation there —
stability is what an artifact looks like.

`flagged_quoted` therefore re-runs the strictest test on the subsample whose
books were actually two-sided (spread <= MAX_QUOTED_SPREAD), requiring the
same MIN_N and the same sign. Buckets keep their original decile: the
question is whether THIS bucket's flag is carried by quoted books, not what a
re-binned population would say. Every bucket also reports `median_spread`,
`mean_spread` and `wide_share` unconditionally, so the contamination is
readable even where the tier is not.

A boolean over three outcomes (2026-08-24): `flagged_quoted` is one bit, and
a bucket can fail it three ways that mean opposite things. Its gap can REVERSE
on two-sided books (evidence AGAINST the signature), collapse inside its own
interval (weak evidence against), or never reach MIN_N quoted observations at
all (no evidence either way). The 2026-08-02 pass separated these by hand, in
prose, over six survivors — and then the tier printed `0` for five consecutive
readings while settled markets grew 165,814 -> 1,592,941 (9.6x) and the
day-weighted tier grew 6 -> 22, with nobody redoing the decomposition. Read on
the 08-24 archive, the zero is mostly silence: of 22 survivors, **19 are
SILENT** (quoted_n 30-191 against the 200 bar) and only **3 were tested**, all
three failing on the interval after the gap shrank 3.1-4.1x
(Financials 1h d2 +0.1454 -> +0.0463, 6h d4 +0.1178 -> +0.0286,
6h d8 -0.0932 -> -0.0457). So the honest statement is unchanged in direction
and much weaker in strength than "zero survivors" sounds: no bucket with
quoted evidence supports the signature, and 19 of 22 have no quoted evidence.
Six of those 19 REVERSE sign on their quoted point estimate — which is
evidence against, not silence, and was invisible under the boolean.

Every bucket therefore carries `quoted_status` (`confirmed` /
`not_significant` / `refuted_sign` / `silent` / `not_applicable`) and the
report carries `quoted_verdict`, whose counts sum to the day-weighted tier
size by construction. `wilson_quoted_lo/hi` are None when the test did not
run, rather than the (0.0, 1.0) that printed as a test that ran and passed.
MIN_N is UNCHANGED at 200 on the quoted subsample: lowering it to reach a
verdict would be fitting the threshold to the answer, and the point here is
to report the silence, not to abolish it. Same standing as every tier above —
`flagged_quoted` keeps its exact meaning for cross-report comparability.

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


# The per-observation base: one row per (market, horizon) carrying a clean
# candle h before its close. Shared verbatim by BUCKET_SQL and OVERLAP_SQL so
# the two can never disagree about which observations exist — same rationale as
# summing `observations_by_category_horizon` from `buckets` instead of
# re-querying it. The decile expression is a constant rather than duplicated
# text for the same reason.
_DECILE_EXPR = "CAST(least(floor(mid * 10), 9) AS INTEGER)"

# A book wider than this makes `mid` uninformative about probability, so the
# bucket's `implied` stops being a price at all.
#
# The crossed-candle gate and the 0.995/0.005 sentinel gate below were both
# meant to keep empty books out, but each tests a CORNER, not a WIDTH: a book
# quoted bid 0.01 / ask 0.96 passes both and contributes mid = 0.485. An empty
# book therefore lands in the middle deciles carrying a manufactured
# implied-of-0.5 against whatever the ladder's true base rate is, which is
# exactly the shape of a large implied-minus-realized gap.
#
# 0.20 is the width at which the measurement error swamps the effect: a 0.20
# spread puts +/-0.10 of ambiguity on `implied`, and every day-weighted gap
# this report has ever flagged sits in 0.08-0.19. Wider than the thing being
# measured is not a bound worth reporting through.
MAX_QUOTED_SPREAD = 0.20

# Minimum observations for a flag, shared by the full sample and the quoted
# subsample so the quoted tier is held to the same bar the base tier is.
MIN_N = 200

_OBSERVATIONS_CTE = """
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
         arg_max((c.yes_bid_close + c.yes_ask_close) / 2, c.end_ts) AS mid,
         -- from the SAME candle as `mid` (arg_max on the same key), so the
         -- width always describes the book the midpoint was taken from
         arg_max(c.yes_ask_close - c.yes_bid_close, c.end_ts) AS spread
  FROM settled st
  CROSS JOIN (VALUES ('1h',1),('6h',6),('24h',24),('72h',72),('7d',168))
       AS h(h_label, h_hours)
  JOIN candles c ON c.venue='kalshi' AND c.market_id = st.market_id
    AND c.end_ts <= st.close_time - INTERVAL 1 HOUR * h.h_hours
    AND c.yes_bid_close IS NOT NULL AND c.yes_ask_close IS NOT NULL
    AND c.yes_bid_close <= c.yes_ask_close             -- crossed-candle gate
    AND NOT (c.yes_ask_close >= 0.995 AND c.yes_bid_close <= 0.005)  -- sentinel
  GROUP BY 1, 2, 3, 4, 5, 6
)
"""


BUCKET_SQL = _OBSERVATIONS_CTE + f"""
, keyed AS (
  SELECT category, h_label, result, series, close_time, mid, spread,
         {_DECILE_EXPR} AS decile,
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
  SELECT category, h_label, decile, close_day, count(*) AS day_n,
         -- exact DECIMAL for the same reproducibility reason as `implied`
         CAST(sum(CAST(mid AS DECIMAL(18, 6))) AS DOUBLE) / count(*) AS day_implied,
         avg(CASE WHEN result = 'yes' THEN 1.0 ELSE 0.0 END) AS day_realized
  FROM keyed
  GROUP BY 1, 2, 3, 4
), top_day AS (
  SELECT category, h_label, decile, max(day_n) AS top_day_n,
         -- EQUALLY weighted across days: one day is one draw, so a 106-market
         -- day and a 1-market day count the same here. See the day-weighting
         -- note in the module docstring.
         avg(day_implied) AS implied_dw,
         avg(day_realized) AS realized_dw
  FROM per_day
  GROUP BY 1, 2, 3
), spreads AS (
  SELECT category, h_label, decile,
         avg(spread) AS mean_spread,
         median(spread) AS median_spread,
         sum(CASE WHEN spread > {MAX_QUOTED_SPREAD} THEN 1 ELSE 0 END) AS wide_n
  FROM keyed
  GROUP BY 1, 2, 3
-- The quoted subsample re-runs the WHOLE ladder of estimates over the same
-- bucket, restricted to observations whose book was actually quoted. The
-- decile assignment is deliberately NOT recomputed: the question is whether
-- this bucket's flag survives on its quoted members, not what a differently
-- binned population would say.
), quoted_per_day AS (
  SELECT category, h_label, decile, close_day,
         CAST(sum(CAST(mid AS DECIMAL(18, 6))) AS DOUBLE) / count(*) AS qday_implied,
         avg(CASE WHEN result = 'yes' THEN 1.0 ELSE 0.0 END) AS qday_realized
  FROM keyed
  WHERE spread <= {MAX_QUOTED_SPREAD}
  GROUP BY 1, 2, 3, 4
), quoted AS (
  SELECT category, h_label, decile,
         count(*) AS quoted_n,
         count(DISTINCT close_day) AS quoted_days,
         CAST(sum(CAST(mid AS DECIMAL(18, 6))) AS DOUBLE) / count(*) AS quoted_implied,
         avg(CASE WHEN result = 'yes' THEN 1.0 ELSE 0.0 END) AS quoted_realized
  FROM keyed
  WHERE spread <= {MAX_QUOTED_SPREAD}
  GROUP BY 1, 2, 3
), quoted_dw AS (
  SELECT category, h_label, decile,
         avg(qday_implied) AS quoted_implied_dw,
         avg(qday_realized) AS quoted_realized_dw
  FROM quoted_per_day
  GROUP BY 1, 2, 3
)
SELECT a.category, a.h_label, a.decile, a.n, a.implied, a.realized,
       a.clusters, a.days, t.top_day_n, t.implied_dw, t.realized_dw,
       s.mean_spread, s.median_spread, s.wide_n,
       -- LEFT JOIN: a bucket can have zero quoted observations (the whole
       -- Crypto|24h|d4 bucket does), and that is a reading, not a missing row
       coalesce(q.quoted_n, 0) AS quoted_n,
       coalesce(q.quoted_days, 0) AS quoted_days,
       q.quoted_implied, q.quoted_realized,
       d.quoted_implied_dw, d.quoted_realized_dw
FROM agg a
JOIN top_day t USING (category, h_label, decile)
JOIN spreads s USING (category, h_label, decile)
LEFT JOIN quoted q USING (category, h_label, decile)
LEFT JOIN quoted_dw d USING (category, h_label, decile)
ORDER BY 1, 2, 3
"""


# Shared markets between every pair of buckets. A market contributes one
# observation per horizon it reaches, and its `result` is the SAME at every
# horizon — so two buckets sharing markets are re-counting identical outcomes,
# and the report's flag COUNTS overstate how many distinct findings exist.
OVERLAP_SQL = _OBSERVATIONS_CTE + f"""
, keyed AS (
  SELECT market_id, category, h_label, {_DECILE_EXPR} AS decile FROM pts
)
SELECT a.category, a.h_label, a.decile,
       b.category, b.h_label, b.decile,
       count(*) AS shared
FROM keyed a
JOIN keyed b ON a.market_id = b.market_id
-- one row per unordered pair; a market is unique within a bucket (pts is
-- keyed by (market_id, h_label)) so self-pairs cannot arise
WHERE a.category || '|' || a.h_label || '|' || CAST(a.decile AS VARCHAR)
    < b.category || '|' || b.h_label || '|' || CAST(b.decile AS VARCHAR)
GROUP BY 1, 2, 3, 4, 5, 6
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


TIERS = (
    "flagged",
    "flagged_robust",
    "flagged_day_robust",
    "flagged_day_weighted",
    "flagged_quoted",
)


def _key(b: dict) -> tuple[str, str, int]:
    return (b["category"], b["horizon"], b["decile"])


def _distinct_readings(out_dir: Path, exclude_fp: str | None = None) -> tuple[list[dict], int]:
    """Prior reports collapsed to distinct DATA states, oldest first.

    Two reports sharing a `data_fingerprint` are one measurement, not two:
    the archive holds three such pairs (07-28, 07-29, 07-30), each a re-run
    minutes after shipping a new tier. Counting report FILES makes every
    such pair contribute a guaranteed-zero churn step — the same data must
    give the same membership — and biases every stability estimate toward
    stable. Same unit-of-counting class as `new_share_vs_all` and
    `underlying_sign_p`.

    Dedup keeps the LAST report per data state, because a re-run on
    identical data is exactly how a new tier first appears: keeping the
    first would discard the only reading that carries it.

    `exclude_fp` drops the CURRENT run's own data state. That is not
    hypothetical — re-running after shipping a tier is precisely what
    produced the three duplicate pairs in the archive, and without it the
    re-run compares against itself: churn reads 0 and every survivor gains
    a free reading of persistence.
    """
    by_fp: dict[str, dict] = {}
    n_files = 0
    for path in sorted(out_dir.glob("*.json")):
        try:
            rep = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        n_files += 1
        fp = json.dumps(rep.get("data_fingerprint"), sort_keys=True)
        if exclude_fp is not None and fp == exclude_fp:
            continue
        by_fp[fp] = rep
    return list(by_fp.values()), n_files


def tier_stability(out_dir: Path, current: dict) -> dict:
    """Is a tier's survivor count a finding, or is it churn?

    A tier reported only as a COUNT cannot distinguish a stable set of
    survivors from a set of equal size whose members swap every reading.
    Measured over the archive (2026-08-01) the difference is real and it
    changes a standing narrative: five buckets have left the day-robust
    tier and returned, ALL of them Financials mid/high deciles, and the
    07-31 reading of "both day-robust demotions are HIGH deciles" — read
    then as the signature narrowing to fading longshots — was both of them
    dropping out for one reading. `flagged_day_weighted` over the same
    span has zero re-entries.

    Per bucket, `persistence` is the share of readings it held the tier,
    over the readings in which it was ELIGIBLE (present in `buckets` at
    all). A bucket that has not accumulated 200 markets yet is absent from
    the data, not absent from the tier, and scoring it as the latter makes
    every genuinely new survivor read as churn.

    Per tier, the denominator counts only readings whose report CARRIES
    that tier — `flagged_day_weighted` shipped 20 runs into the archive,
    so scoring it against every report would print 3/23 for a tier that
    has never lost a member.

    Never a verdict: a stable tier is a stable lead, and pre-registration
    still decides.
    """
    priors, n_files = _distinct_readings(
        out_dir, json.dumps(current.get("data_fingerprint"), sort_keys=True)
    )
    out: dict = {}
    for tier in TIERS:
        readings = [p for p in priors if tier in p]
        cur_members = {_key(b) for b in current.get(tier, [])}
        # membership + eligibility per reading, oldest first
        seq = [({_key(b) for b in r[tier]}, {_key(b) for b in r.get("buckets", [])}) for r in readings]

        buckets = []
        for k in sorted(cur_members):
            hist = [k in members for members, elig in seq if k in elig]
            n_elig = len(hist)
            # a re-entry needs in -> out -> in; the current reading is the
            # final `in`, so a trailing gap in the priors is a re-entry too
            full = hist + [True]
            transitions = sum(1 for i in range(1, len(full)) if full[i] != full[i - 1])
            buckets.append(
                {
                    "bucket": list(k),
                    "eligible_readings": n_elig,
                    "persistence": round(sum(hist) / n_elig, 4) if n_elig else None,
                    "reentered": transitions >= 2,
                }
            )

        prior_members = seq[-1][0] if seq else None
        out[tier] = {
            "reports_read": n_files,
            "readings": len(seq),
            "size": len(cur_members),
            "prior_size": len(prior_members) if prior_members is not None else None,
            "churn_vs_prior": (
                len(cur_members ^ prior_members) if prior_members is not None else None
            ),
            "gained": (
                [list(k) for k in sorted(cur_members - prior_members)]
                if prior_members is not None
                else None
            ),
            "lost": (
                [list(k) for k in sorted(prior_members - cur_members)]
                if prior_members is not None
                else None
            ),
            "oscillators": [b["bucket"] for b in buckets if b["reentered"]],
            "buckets": buckets,
        }
    return out


OVERLAP_THRESHOLD = 0.3


def _bucket_label(key: tuple[str, str, int]) -> str:
    return f"{key[0]}|{key[1]}|d{key[2]}"


def _cross_bucket_groups(
    buckets: list[dict],
    overlaps: list[tuple],
    tier: str,
    threshold: float = OVERLAP_THRESHOLD,
) -> dict:
    """Collapse a tier's surviving buckets into groups that share markets.

    The three existing tiers all bound correlation WITHIN a bucket. Nothing
    bounded it ACROSS buckets, and the horizon dimension duplicates outcomes by
    construction: one market enters up to 5 horizon buckets and settles the
    same way in all of them. So a tier's survivor COUNT — the "16 day-robust,
    zero counter-signature" headline — is a count of buckets, not of distinct
    findings. Buckets linked by sharing at least `threshold` of the smaller
    one's markets are unioned into one group; `groups` is the honest sample
    size. Share is measured against the SMALLER bucket on purpose: a 250-market
    bucket entirely contained in a 3,000-market one is fully redundant even
    though it is only 8% of the larger.
    """
    members = {
        (b["category"], b["horizon"], b["decile"]): b for b in buckets if b[tier]
    }
    parent = {k: k for k in members}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    pairs, max_share = [], 0.0
    for ca, ha, da, cb, hb, db, shared in overlaps:
        ka, kb = (ca, ha, da), (cb, hb, db)
        if ka not in members or kb not in members:
            continue
        share = shared / min(members[ka]["n"], members[kb]["n"])
        max_share = max(max_share, share)
        if share >= threshold:
            pairs.append(
                {
                    "a": _bucket_label(ka),
                    "b": _bucket_label(kb),
                    "shared_markets": shared,
                    "share_of_smaller": round(share, 4),
                }
            )
            ra, rb = find(ka), find(kb)
            if ra != rb:
                parent[ra] = rb
    grouped: dict[tuple, list[str]] = {}
    for k in members:
        grouped.setdefault(find(k), []).append(_bucket_label(k))
    return {
        "buckets": len(members),
        "groups": len(grouped),
        "shared_groups": sorted(
            (sorted(g) for g in grouped.values() if len(g) > 1), key=len, reverse=True
        ),
        "max_share_of_smaller": round(max_share, 4),
        "linked_pairs": sorted(pairs, key=lambda p: -p["share_of_smaller"]),
    }


def build_atlas(conn) -> dict:
    rows = conn.execute(BUCKET_SQL).fetchall()
    buckets = []
    for row in rows:
        (
            category, h_label, decile, n, implied, realized, clusters, days,
            top_day_n, implied_dw, realized_dw,
            mean_spread, median_spread, wide_n,
            quoted_n, quoted_days, quoted_implied, quoted_realized,
            quoted_implied_dw, quoted_realized_dw,
        ) = row
        lo, hi = wilson(realized * n, n)
        flagged = n >= MIN_N and not (lo <= implied <= hi)
        # worst case: every market in a (series, close_time) ladder settles
        # on one shared outcome, so at most `clusters` independent draws
        rlo, rhi = wilson(realized * clusters, clusters)
        flagged_robust = flagged and not (rlo <= implied <= rhi)
        # one level up: every ladder closing on the same day can share one
        # underlying path (index ladders do), so at most `days` draws
        dlo, dhi = wilson(realized * days, days)
        flagged_day_robust = flagged_robust and not (dlo <= implied <= dhi)
        # ...and the same unit on BOTH sides of the comparison: the tier above
        # takes its sample size from days but its point estimate from markets,
        # so an unequally-sized day set lets one big day dominate the mean
        # while counting as a single draw. Re-weight implied and realized so
        # each day contributes once, then apply the same n = days Wilson.
        dwlo, dwhi = wilson(realized_dw * days, days)
        flagged_day_weighted = (
            flagged_day_robust and not (dwlo <= implied_dw <= dwhi)
        )
        # ...and the tier the four Wilson tiers above cannot reach, because
        # the artifact they admit is SYSTEMATIC rather than noisy: an empty
        # book is stably empty, so more days of it TIGHTEN the interval and
        # make the bucket more robust, not less. Re-run the strictest test on
        # the quoted subsample only. Surviving means the gap is carried by
        # books that were actually two-sided, and the sign must agree — a
        # quoted subsample that flips direction is not a confirmation.
        qwlo, qwhi = (None, None)
        flagged_quoted = False
        gap_dw = implied_dw - realized_dw
        quoted_gap_dw = (
            quoted_implied_dw - quoted_realized_dw
            if quoted_implied_dw is not None and quoted_realized_dw is not None
            else None
        )
        # THE STATUS, NOT THE BOOLEAN (2026-08-24). `flagged_quoted` is one
        # bit over three outcomes that mean opposite things, so the tier's
        # headline zero cannot say which one it saw. See `quoted_status`.
        if not flagged_day_weighted:
            quoted_status = "not_applicable"
        elif quoted_n < MIN_N or not quoted_days or quoted_implied_dw is None:
            quoted_status = "silent"
        else:
            qwlo, qwhi = wilson(quoted_realized_dw * quoted_days, quoted_days)
            if quoted_gap_dw * gap_dw <= 0:
                quoted_status = "refuted_sign"
            elif qwlo <= quoted_implied_dw <= qwhi:
                quoted_status = "not_significant"
            else:
                quoted_status = "confirmed"
            flagged_quoted = quoted_status == "confirmed"
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
                "flagged_day_robust": flagged_day_robust,
                "implied_day_weighted": round(implied_dw, 4),
                "realized_day_weighted": round(realized_dw, 4),
                "wilson_day_weighted_lo": round(dwlo, 4),
                "wilson_day_weighted_hi": round(dwhi, 4),
                "flagged_day_weighted": flagged_day_weighted,
                "mean_spread": round(mean_spread, 4),
                "median_spread": round(median_spread, 4),
                "wide_share": round(wide_n / n, 4) if n else 0.0,
                "quoted_n": quoted_n,
                "quoted_days": quoted_days,
                "quoted_implied": (
                    round(quoted_implied, 4) if quoted_implied is not None else None
                ),
                "quoted_realized": (
                    round(quoted_realized, 4) if quoted_realized is not None else None
                ),
                "quoted_implied_day_weighted": (
                    round(quoted_implied_dw, 4)
                    if quoted_implied_dw is not None
                    else None
                ),
                "quoted_realized_day_weighted": (
                    round(quoted_realized_dw, 4)
                    if quoted_realized_dw is not None
                    else None
                ),
                # None, not (0.0, 1.0), when the test did not run: a
                # fabricated interval spanning the whole unit line prints as
                # a test that ran and found the implied inside it, which is
                # the one reading the data cannot support.
                "wilson_quoted_lo": round(qwlo, 4) if qwlo is not None else None,
                "wilson_quoted_hi": round(qwhi, 4) if qwhi is not None else None,
                "flagged_quoted": flagged_quoted,
                "quoted_status": quoted_status,
                # tier-neutral and readable even where the tier is silent:
                # how much of the day-weighted gap survives on two-sided
                # books. > 1 means the gap GREW; a negative value means it
                # reversed. This is a diagnostic, not a test.
                "quoted_gap_dw": (
                    round(quoted_gap_dw, 4) if quoted_gap_dw is not None else None
                ),
                "quoted_gap_retained": (
                    round(quoted_gap_dw / gap_dw, 4)
                    if quoted_gap_dw is not None and gap_dw
                    else None
                ),
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
    overlaps = conn.execute(OVERLAP_SQL).fetchall()
    return {
        "generated_at": str(datetime.now(UTC).replace(tzinfo=None, microsecond=0)),
        "data_fingerprint": fingerprint,
        # how many DISTINCT findings each tier's survivor count represents: the
        # three Wilson tiers bound correlation within a bucket, but one market
        # reaches up to 5 horizon buckets and settles the same way in all of
        # them, so survivors that share markets are re-counting one outcome set.
        "cross_bucket_overlap": {
            "rule": (
                f"survivors sharing >= {OVERLAP_THRESHOLD:.0%} of the smaller "
                "bucket's markets are unioned into one group; `groups` is the "
                "honest count of distinct findings in the tier"
            ),
            "threshold_share_of_smaller": OVERLAP_THRESHOLD,
            "tiers": {
                tier: _cross_bucket_groups(buckets, overlaps, tier)
                for tier in TIERS
            },
        },
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
        "flag_rule_day_weighted": (
            "flagged_day_robust AND day-weighted implied outside Wilson 95% of "
            "day-weighted realized with n = days — the day tier with the SAME "
            "unit on both sides, so unequal day sizes cannot let one large day "
            "set the mean while counting as one draw"
        ),
        "flag_rule_quoted": (
            f"flagged_day_weighted AND >= {MIN_N} observations whose book was "
            f"quoted within {MAX_QUOTED_SPREAD} AND the day-weighted flag "
            "survives on that subsample with the SAME sign — the Wilson tiers "
            "above cannot reach this, because an empty book is stably empty "
            "and more days of it tighten the interval rather than widen it"
        ),
        "quoted_rule": f"spread = ask - bid <= {MAX_QUOTED_SPREAD} on the mid's own candle",
        "buckets": buckets,
        "flagged": [b for b in buckets if b["flagged"]],
        "flagged_robust": [b for b in buckets if b["flagged_robust"]],
        "flagged_day_robust": [b for b in buckets if b["flagged_day_robust"]],
        "flagged_day_weighted": [b for b in buckets if b["flagged_day_weighted"]],
        "flagged_quoted": [b for b in buckets if b["flagged_quoted"]],
        # THE DECOMPOSITION OF THE ZERO (2026-08-24). `flagged_quoted` has
        # read 0 for five consecutive readings across a 9.6x growth in
        # settled markets, and that single number cannot distinguish the
        # three things it has been standing for. Every day-weighted survivor
        # lands in exactly one status, so these counts sum to the
        # day-weighted tier size — the arithmetic is the guard against
        # reading a zero as a measurement again.
        "quoted_verdict": _quoted_verdict(buckets),
    }


# The statuses a day-weighted survivor can hold under the quoted test,
# ordered from "the gap survived two-sided books" to "the test never ran".
QUOTED_STATUSES = ("confirmed", "not_significant", "refuted_sign", "silent")


def _quoted_verdict(buckets: list[dict]) -> dict:
    """Break the quoted tier's headline count into the statuses it pools.

    A bare `flagged_quoted: 0` reads identically for a bucket whose gap
    REVERSED on two-sided books (evidence against the signature), one whose
    gap collapsed inside its own interval (weak evidence against), and one
    that never had `MIN_N` quoted observations to test (no evidence at all).
    The 2026-08-02 pass separated these by hand, in prose, on six survivors;
    the population is larger now and nobody redid it. Encoded here so the
    separation is a property of the report rather than of whoever read it.
    """
    survivors = [b for b in buckets if b["flagged_day_weighted"]]
    by_status = {
        s: [b for b in survivors if b["quoted_status"] == s] for s in QUOTED_STATUSES
    }
    retained = sorted(
        b["quoted_gap_retained"]
        for b in survivors
        if b["quoted_gap_retained"] is not None
    )
    return {
        "rule": (
            "every flagged_day_weighted bucket holds exactly one status, so "
            "these counts sum to the day-weighted tier size; `silent` is the "
            "test not running, which is not the same reading as `refuted_sign` "
            "or `not_significant` and must never be pooled with them"
        ),
        "day_weighted_survivors": len(survivors),
        "counts": {s: len(v) for s, v in by_status.items()},
        "buckets": {
            s: [f"{b['category']}|{b['horizon']}|d{b['decile']}" for b in v]
            for s, v in by_status.items()
        },
        # the share of the pooled gap that survives on two-sided books,
        # over every survivor where a quoted gap exists at all (INCLUDING
        # the silent ones — the point estimate is readable where the test
        # is not, and it is the diagnostic the 08-02 pass actually used).
        "gap_retained_measurable": len(retained),
        "gap_retained_median": (
            round(retained[len(retained) // 2], 4) if retained else None
        ),
        "gap_retained_min": round(retained[0], 4) if retained else None,
        "gap_retained_max": round(retained[-1], 4) if retained else None,
        "gap_reversed_on_quoted_books": sum(1 for r in retained if r < 0),
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
    # computed against the archived priors, so it must run before this
    # report is written -- the current run is the comparison's subject,
    # not one of its priors.
    atlas["tier_stability"] = tier_stability(out_dir, atlas)
    out = out_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(atlas, indent=1) + "\n")

    flags = atlas["flagged"]
    print(
        f"[atlas] {len(atlas['buckets'])} buckets, {len(flags)} flagged"
        f" ({len(atlas['flagged_robust'])} cluster-robust)"
    )
    # printed per TIER rather than only for the survivors: the whole point is
    # that wide-book contamination RISES with tier strictness, and that is
    # invisible from any single tier's rows.
    print("| tier | buckets | n | wide share | quoted-tier survivors |")
    print("|---|---|---|---|---|")
    for tier in TIERS:
        rows = atlas[tier]
        tot = sum(b["n"] for b in rows)
        wide = sum(b["wide_share"] * b["n"] for b in rows)
        share = f"{wide / tot:.2%}" if tot else "-"
        survivors = sum(1 for b in rows if b["flagged_quoted"])
        print(f"| {tier} | {len(rows)} | {tot} | {share} | {survivors} |")
    # the quoted tier's headline is a COUNT OF SURVIVORS, and a zero there
    # has been read five times as "no bucket survives two-sided books" when
    # most survivors were never tested at all. Print the decomposition next
    # to the count so the two can never again be read as the same statement.
    qv = atlas["quoted_verdict"]
    c = qv["counts"]
    print(
        f"[atlas] quoted tier over {qv['day_weighted_survivors']} day-weighted"
        f" survivors: {c['confirmed']} confirmed, {c['not_significant']}"
        f" not-significant, {c['refuted_sign']} refuted-on-sign,"
        f" {c['silent']} SILENT (< {MIN_N} quoted obs -- untested, not rejected)"
    )
    if qv["gap_retained_measurable"]:
        print(
            f"[atlas] gap retained on quoted books over"
            f" {qv['gap_retained_measurable']} survivors: median"
            f" {qv['gap_retained_median']}, range [{qv['gap_retained_min']},"
            f" {qv['gap_retained_max']}], {qv['gap_reversed_on_quoted_books']}"
            f" reversed sign"
        )
    if flags:
        print("| category | horizon | decile | n | clusters | implied | realized | wilson | robust | med spr | quoted |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for b in sorted(flags, key=lambda b: -b["n"]):
            print(
                f"| {b['category']} | {b['horizon']} | {b['decile']} | {b['n']}"
                f" | {b['clusters']} | {b['implied']} | {b['realized']}"
                f" | [{b['wilson_lo']}, {b['wilson_hi']}]"
                f" | {'YES' if b['flagged_robust'] else 'no'}"
                f" | {b['median_spread']}"
                f" | {b['quoted_status']} |"
            )
    print(f"[atlas] written to {out}")


if __name__ == "__main__":
    main()
