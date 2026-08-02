"""Calibration atlas: Wilson interval against known values, bucket
construction on a hand-computed fixture, crossed/sentinel exclusion."""

from datetime import UTC, datetime, timedelta

import pytest

from hyxlab.models import MarketInfo
from hyxlab.store import Store
from simulator.atlas import (
    BUCKET_SQL,
    MAX_QUOTED_SPREAD,
    MIN_N,
    build_atlas,
    wilson,
)

CLOSE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def test_wilson_against_known_values():
    lo, hi = wilson(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3)
    assert hi == pytest.approx(0.5962, abs=1e-3)
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    assert wilson(0, 0) == (0.0, 1.0)


def _candle(mid, market_id, end_ts, spread=0.02):
    return (
        "kalshi",
        market_id,
        end_ts,
        3600,
        None,
        None,
        None,
        mid,
        mid - spread / 2,
        mid + spread / 2,  # bid/ask closes around mid
        None,
        None,
        10.0,
        5.0,
    )


def test_atlas_buckets_hand_computed(tmp_path):
    store = Store(tmp_path / "a.duckdb")
    # two markets in the same (category-less, 24h, decile-4) bucket:
    # mids 0.40 and 0.48, one settles yes, one no
    store.upsert_markets(
        [
            MarketInfo(venue="kalshi", market_id="A", result="yes", close_time=CLOSE),
            MarketInfo(venue="kalshi", market_id="B", result="no", close_time=CLOSE),
        ]
    )
    t = (CLOSE - timedelta(hours=25)).replace(tzinfo=None)
    later = (CLOSE - timedelta(hours=24)).replace(tzinfo=None)
    store.insert_candles(
        [
            _candle(0.30, "A", t),  # superseded by the later clean candle
            _candle(0.40, "A", later),
            _candle(0.48, "B", later),
            # crossed candle at a fresher ts must be EXCLUDED, not win arg_max
            ("kalshi", "B", later, 3600, None, None, None, 0.9, 0.95, 0.85, None, None, 1.0, 1.0),
        ]
    )
    atlas = build_atlas(store.conn)
    b24 = [b for b in atlas["buckets"] if b["horizon"] == "24h"]
    assert len(b24) == 1
    b = b24[0]
    assert b["decile"] == 4 and b["n"] == 2
    assert b["implied"] == pytest.approx(0.44)
    assert b["realized"] == pytest.approx(0.5)
    assert not b["flagged"]  # n < 200 never flags
    # the 1h horizon also exists (candles at close-24h qualify for 1h too)
    assert any(x["horizon"] == "1h" for x in atlas["buckets"])
    store.close()


def test_atlas_flags_large_miscalibrated_bucket(tmp_path):
    store = Store(tmp_path / "a.duckdb")
    infos, candles = [], []
    t = (CLOSE - timedelta(hours=2)).replace(tzinfo=None)
    # 250 markets implied ~0.90 that ALL settle yes: realized 1.0,
    # Wilson lo ~0.985 > implied -> flag (favorite-longshot signature)
    for i in range(250):
        mid = 0.90 + (i % 5) * 0.001
        infos.append(MarketInfo(venue="kalshi", market_id=f"F{i}", result="yes", close_time=CLOSE))
        candles.append(_candle(mid, f"F{i}", t))
    store.upsert_markets(infos)
    store.insert_candles(candles)
    atlas = build_atlas(store.conn)
    flagged = [b for b in atlas["flagged"] if b["horizon"] == "1h"]
    assert len(flagged) == 1
    assert flagged[0]["decile"] == 9 and flagged[0]["n"] == 250
    assert flagged[0]["realized"] == 1.0
    store.close()


def _bulk_atlas(tmp_path, series_for, close_for=lambda i: CLOSE):
    """250 miscalibrated markets (implied ~.90, all settle yes); the
    per-market flag always fires — series_for(i) controls the clustering
    and close_for(i) controls how many settlement days they span."""
    store = Store(tmp_path / "a.duckdb")
    infos, candles = [], []
    for i in range(250):
        mid = 0.90 + (i % 5) * 0.001
        close = close_for(i)
        infos.append(
            MarketInfo(
                venue="kalshi",
                market_id=f"F{i}",
                series=series_for(i),
                result="yes",
                close_time=close,
            )
        )
        candles.append(_candle(mid, f"F{i}", (close - timedelta(hours=2)).replace(tzinfo=None)))
    store.upsert_markets(infos)
    store.insert_candles(candles)
    atlas = build_atlas(store.conn)
    store.close()
    return [b for b in atlas["flagged"] if b["horizon"] == "1h"][0]


def test_single_ladder_collapses_robust_flag(tmp_path):
    # all 250 markets are sibling strikes of ONE ladder (same series,
    # same close): one underlying outcome, so clusters=1 and the
    # cluster-robust Wilson interval must swallow the flag
    b = _bulk_atlas(tmp_path, lambda i: "KXDJI")
    assert b["flagged"] and b["clusters"] == 1
    assert not b["flagged_robust"]


def test_independent_markets_keep_robust_flag(tmp_path):
    # 250 markets in 250 distinct series: genuinely independent
    # evidence, the robust tier must agree with the per-market flag
    b = _bulk_atlas(tmp_path, lambda i: f"S{i}")
    assert b["flagged"] and b["clusters"] == 250
    assert b["flagged_robust"]


def test_single_settlement_day_collapses_day_robust_flag(tmp_path):
    # 250 DISTINCT series -> 250 clusters, so the cluster tier sees
    # fully independent evidence and keeps the flag. But every market
    # settles on the SAME day, and same-day ladders can resolve off one
    # underlying path (Financials 24h d3-d6 all flipped gap sign together
    # when 07-27 landed). The day tier must swallow the flag, and
    # top_day_share must expose the concentration.
    b = _bulk_atlas(tmp_path, lambda i: f"S{i}")
    assert b["flagged_robust"] and b["clusters"] == 250
    assert b["days"] == 1 and b["top_day_share"] == 1.0
    assert not b["flagged_day_robust"]


def test_evidence_spread_across_days_keeps_day_robust_flag(tmp_path):
    # same 250 independent series, but one market per settlement day:
    # no single day can carry the finding, so the day tier agrees
    b = _bulk_atlas(
        tmp_path,
        lambda i: f"S{i}",
        close_for=lambda i: CLOSE - timedelta(days=i),
    )
    assert b["flagged_robust"] and b["days"] == 250
    assert b["top_day_share"] == pytest.approx(1 / 250)
    assert b["flagged_day_robust"]


def test_implied_is_not_a_floating_point_average():
    # `implied` must not be a float avg(). DuckDB accumulates one in a
    # parallelism-dependent order, so the value moved between identical runs
    # on production data: 238/260 buckets differed in their raw implied and 3
    # flipped their reported 4th decimal, which the report-diff drift method
    # would read as real drift. Asserted on the SQL because the nondeterminism
    # only appears at production scan sizes and cannot be provoked by a
    # fixture — same reasoning as the tree-wide datetime.now() tz invariant.
    # `--` comments are stripped first: the fix's own comment names avg(mid),
    # and a check that a comment can satisfy is not a check.
    code = "\n".join(line.split("--")[0] for line in BUCKET_SQL.splitlines())
    assert "avg(mid)" not in code
    assert "sum(CAST(mid AS DECIMAL(18, 6)))" in code


def test_implied_is_the_exact_mean_not_the_float_mean(tmp_path):
    # the value half of the same fix: exact DECIMAL accumulation must also be
    # RIGHT. Mids 0.200/0.205/0.290 all sit in decile 2 and were chosen because
    # their float mean (0.2316666666666667) and their exact mean
    # (0.23166666666666666) are different doubles — so this fails with avg()
    # and passes with the exact sum.
    store = Store(tmp_path / "a.duckdb")
    infos, candles = [], []
    t = (CLOSE - timedelta(hours=2)).replace(tzinfo=None)
    for i, mid in enumerate((0.200, 0.205, 0.290)):
        infos.append(MarketInfo(venue="kalshi", market_id=f"M{i}", result="yes", close_time=CLOSE))
        candles.append(_candle(mid, f"M{i}", t))
    store.upsert_markets(infos)
    store.insert_candles(candles)
    row = [r for r in store.conn.execute(BUCKET_SQL).fetchall() if r[1] == "1h"]
    assert len(row) == 1 and row[0][2] == 2 and row[0][3] == 3
    assert row[0][4] == 0.23166666666666666
    store.close()


def _day_weighted_atlas(tmp_path, day_specs):
    """Build a 1h bucket from `day_specs` = [(n_markets, n_yes), ...], one
    entry per settlement day, all at mid 0.60 and each market in its own
    series (so clusters == n and the cluster tier never interferes)."""
    store = Store(tmp_path / "a.duckdb")
    infos, candles, i = [], [], 0
    for day, (n_markets, n_yes) in enumerate(day_specs):
        close = CLOSE - timedelta(days=day)
        for k in range(n_markets):
            mid, mid_ = 0.60, f"F{i}"
            infos.append(
                MarketInfo(
                    venue="kalshi",
                    market_id=mid_,
                    series=f"S{i}",
                    result="yes" if k < n_yes else "no",
                    close_time=close,
                )
            )
            candles.append(
                _candle(mid, mid_, (close - timedelta(hours=2)).replace(tzinfo=None))
            )
            i += 1
    store.upsert_markets(infos)
    store.insert_candles(candles)
    atlas = build_atlas(store.conn)
    store.close()
    return [b for b in atlas["buckets"] if b["horizon"] == "1h" and b["decile"] == 6][0]


# one 300-market day that settles all-yes, plus 40 five-market days that
# settle at exactly the implied 0.60. Market-weighted, the big day drags
# realized to 0.84 against implied 0.60 — a +0.24 gap the day tier calls
# robust off n = 41. But that is 41 draws' worth of variance applied to a
# mean 60% supplied by ONE draw, which is the correlation the tier exists
# to bound. Weight each day once and the gap is +0.01.
_ONE_BIG_DAY = [(300, 300)] + [(5, 3)] * 40
_EVEN_DAYS = [(10, 10)] * 42 + [(10, 0)] * 8  # same 0.84 realized, evenly spread


def test_day_tier_mean_is_dominated_by_one_large_day(tmp_path):
    b = _day_weighted_atlas(tmp_path, _ONE_BIG_DAY)
    assert b["flagged_robust"] and b["days"] == 41
    # the market-weighted day tier is satisfied and reports a large gap...
    assert b["flagged_day_robust"]
    assert b["realized"] - b["implied"] == pytest.approx(0.24, abs=1e-3)
    # ...but that gap is one day's outcome; re-weighted it all but vanishes
    assert b["realized_day_weighted"] - b["implied_day_weighted"] == pytest.approx(
        0.0098, abs=1e-3
    )
    assert not b["flagged_day_weighted"]


def test_day_weighted_tier_keeps_evenly_spread_evidence(tmp_path):
    # the discrimination control: the SAME 0.84 realized against the same
    # implied 0.60, but no day larger than any other. Nothing is being
    # carried by one draw, so the day-weighted tier must agree — otherwise
    # the tier is merely always-stricter rather than measuring day balance.
    b = _day_weighted_atlas(tmp_path, _EVEN_DAYS)
    assert b["flagged_day_robust"] and b["days"] == 50
    assert b["realized"] == pytest.approx(b["realized_day_weighted"], abs=1e-9)
    assert b["flagged_day_weighted"]


def test_day_weighted_fields_weight_each_day_equally(tmp_path):
    # the fields themselves, independent of the tier: implied/realized
    # day-weighted must be the mean OVER DAYS, not the market mean.
    b = _day_weighted_atlas(tmp_path, _ONE_BIG_DAY)
    assert b["realized"] == pytest.approx(420 / 500, abs=1e-4)  # market-weighted
    assert b["realized_day_weighted"] == pytest.approx(
        (1.0 + 40 * 0.6) / 41, abs=1e-4
    )  # (one all-yes day + 40 days at 0.6) / 41 days
    assert b["implied_day_weighted"] == pytest.approx(0.60, abs=1e-6)


def test_day_weighted_tier_nests_inside_day_robust(tmp_path):
    # tiers must nest (day_weighted <= day_robust <= robust <= flagged) so
    # the report cannot claim a survivor a looser tier already rejected
    b = _day_weighted_atlas(tmp_path, _EVEN_DAYS)
    assert b["flagged"] >= b["flagged_robust"] >= b["flagged_day_robust"]
    assert b["flagged_day_robust"] >= b["flagged_day_weighted"]


def _overlap_tier(store, tier="flagged"):
    return build_atlas(store.conn)["cross_bucket_overlap"]["tiers"][tier]


def test_same_markets_at_two_horizons_are_one_finding(tmp_path):
    # 250 miscalibrated markets whose only candle sits 7h before close, so each
    # one enters BOTH the 1h and the 6h bucket at the same decile. Two buckets
    # flag, but they are the identical market population and a market settles
    # the same way at every horizon — so this is ONE finding, not two. The
    # survivor COUNT was the number the status log has been reporting.
    store = Store(tmp_path / "a.duckdb")
    infos, candles = [], []
    t = (CLOSE - timedelta(hours=7)).replace(tzinfo=None)
    for i in range(250):
        infos.append(MarketInfo(venue="kalshi", market_id=f"F{i}", series=f"S{i}", result="yes", close_time=CLOSE))
        candles.append(_candle(0.90, f"F{i}", t))
    store.upsert_markets(infos)
    store.insert_candles(candles)
    tierinfo = _overlap_tier(store)
    assert tierinfo["buckets"] == 2
    assert tierinfo["groups"] == 1
    assert tierinfo["max_share_of_smaller"] == 1.0
    assert tierinfo["shared_groups"] == [["?|1h|d9", "?|6h|d9"]]
    store.close()


def test_disjoint_flagged_buckets_stay_separate(tmp_path):
    # the discrimination control: two flagged buckets built from disjoint
    # markets must stay two findings, or the tier would collapse everything and
    # be as useless as it is conservative
    store = Store(tmp_path / "a.duckdb")
    infos, candles = [], []
    t = (CLOSE - timedelta(hours=2)).replace(tzinfo=None)
    for i in range(250):
        infos.append(MarketInfo(venue="kalshi", market_id=f"H{i}", series=f"S{i}", result="yes", close_time=CLOSE))
        candles.append(_candle(0.90, f"H{i}", t))
        # a second, disjoint population landing in decile 1 and also flagging
        infos.append(MarketInfo(venue="kalshi", market_id=f"L{i}", series=f"T{i}", result="yes", close_time=CLOSE))
        candles.append(_candle(0.10, f"L{i}", t))
    store.upsert_markets(infos)
    store.insert_candles(candles)
    tierinfo = _overlap_tier(store)
    assert tierinfo["buckets"] == 2 and tierinfo["groups"] == 2
    assert tierinfo["shared_groups"] == [] and tierinfo["max_share_of_smaller"] == 0.0
    store.close()


def test_overlap_share_is_measured_against_the_smaller_bucket(tmp_path):
    # the load-bearing choice. 1h|d9 holds 250 markets, 6h|d9 holds 350, and
    # they share 100. Against the SMALLER that is 0.40 (redundant, must link);
    # against the larger it is 0.286 and would fall under the threshold. A
    # small bucket wholly inside a big one is fully redundant regardless of
    # what fraction of the big one it is.
    store = Store(tmp_path / "a.duckdb")
    infos, candles = [], []
    seven = (CLOSE - timedelta(hours=7)).replace(tzinfo=None)
    two = (CLOSE - timedelta(hours=2)).replace(tzinfo=None)

    def add(mid_6h, mid_1h, tag, count):
        for i in range(count):
            mid = f"{tag}{i}"
            infos.append(MarketInfo(venue="kalshi", market_id=mid, series=f"S{tag}{i}", result="yes", close_time=CLOSE))
            if mid_6h is not None:
                candles.append(_candle(mid_6h, mid, seven))
            if mid_1h is not None:
                candles.append(_candle(mid_1h, mid, two))

    add(0.90, 0.55, "A", 250)  # 6h d9 only (the 1h read is a later, lower candle)
    add(0.90, None, "B", 100)  # both 6h d9 and 1h d9 — the shared markets
    add(None, 0.90, "C", 150)  # 1h d9 only
    store.upsert_markets(infos)
    store.insert_candles(candles)
    tierinfo = _overlap_tier(store)
    pair = [
        p for p in tierinfo["linked_pairs"]
        if {p["a"], p["b"]} == {"?|1h|d9", "?|6h|d9"}
    ]
    assert len(pair) == 1, tierinfo["linked_pairs"]
    assert pair[0]["shared_markets"] == 100
    assert pair[0]["share_of_smaller"] == pytest.approx(0.4)
    store.close()


def test_fingerprint_counts_settled_markets_per_category(tmp_path):
    # two categories settling at different rates: consecutive atlas runs
    # are only INDEPENDENT evidence for a category that actually gained
    # settled markets, so the fingerprint must break the count out by
    # category rather than reporting one archive-wide total
    store = Store(tmp_path / "a.duckdb")
    store.upsert_series(
        [
            ("kalshi", "KXDJI", "Dow", "Financials", None, None, None),
            ("kalshi", "KXHIGHNY", "NY high", "Climate and Weather", None, None, None),
        ]
    )
    store.upsert_markets(
        [
            MarketInfo(venue="kalshi", market_id="F1", series="KXDJI", result="yes", close_time=CLOSE),
            MarketInfo(venue="kalshi", market_id="W1", series="KXHIGHNY", result="yes", close_time=CLOSE),
            MarketInfo(venue="kalshi", market_id="W2", series="KXHIGHNY", result="no", close_time=CLOSE),
            # unsettled markets never count toward the fingerprint
            MarketInfo(venue="kalshi", market_id="W3", series="KXHIGHNY", close_time=CLOSE),
        ]
    )
    by_cat = build_atlas(store.conn)["data_fingerprint"]["settled_by_category"]
    assert by_cat == {"Financials": 1, "Climate and Weather": 2}
    store.close()


def _financials_store(tmp_path):
    """One Financials series; F_SHORT has a book only 2h before close (so it
    reaches the 1h bucket alone), F_LONG has one 30h before close (so it
    reaches 1h/6h/24h). This is the production shape: same-day index ladders
    never carry a candle 24h before their own close."""
    store = Store(tmp_path / "a.duckdb")
    store.upsert_series([("kalshi", "KXDJI", "Dow", "Financials", None, None, None)])
    store.upsert_markets(
        [
            MarketInfo(venue="kalshi", market_id="F_SHORT", series="KXDJI", result="yes", close_time=CLOSE),
            MarketInfo(venue="kalshi", market_id="F_LONG", series="KXDJI", result="no", close_time=CLOSE),
        ]
    )
    store.insert_candles(
        [
            _candle(0.40, "F_SHORT", (CLOSE - timedelta(hours=2)).replace(tzinfo=None)),
            _candle(0.40, "F_LONG", (CLOSE - timedelta(hours=30)).replace(tzinfo=None)),
        ]
    )
    return store


def test_fingerprint_breaks_observations_out_by_horizon(tmp_path):
    # a category can gain settled markets while a given HORIZON gains none:
    # a market only enters the horizon-h bucket if it has a candle h before
    # close. settled_by_category cannot express that, so a category-level
    # count reads "new evidence" for buckets that are bit-identical.
    store = _financials_store(tmp_path)
    fp = build_atlas(store.conn)["data_fingerprint"]
    obs = fp["observations_by_category_horizon"]
    assert obs["Financials|1h"] == 2
    assert obs["Financials|24h"] == 1
    # the category total is blind to the split that matters
    assert fp["settled_by_category"]["Financials"] == 2
    # horizons no market reaches are absent, not silently zero-filled
    assert "Financials|7d" not in obs
    store.close()


def test_fingerprint_horizon_counts_match_bucket_population(tmp_path):
    # contract: the fingerprint must describe exactly the observations the
    # buckets are built from. settled_by_category is a separate query over
    # `markets` and can drift from BUCKET_SQL's candle gates; this one must
    # not.
    store = _financials_store(tmp_path)
    atlas = build_atlas(store.conn)
    summed = {}
    for b in atlas["buckets"]:
        k = f"{b['category']}|{b['horizon']}"
        summed[k] = summed.get(k, 0) + b["n"]
    assert atlas["data_fingerprint"]["observations_by_category_horizon"] == summed
    store.close()


# --- quoted-book tier (2026-08-02) -------------------------------------
#
# Every tier above bounds correlation; none bounds whether `implied` was a
# price. A book quoted 0.05 / 0.95 contributes mid 0.50 and is
# indistinguishable from a genuine coin flip, so an empty book manufactures
# the largest implied-minus-realized gaps -- exactly what the tiers select
# on. Measured on the live archive, spread > 0.5 covers 4.6% of the flagged
# tier and 34.2% of the day-weighted tier: contamination RISES with
# strictness, because an empty book is stably empty and more days of it
# TIGHTEN the Wilson interval rather than widen it.

def _spread_atlas(tmp_path, day_specs):
    """1h / decile-5 bucket at mid 0.50, built from `day_specs` = one entry
    per settlement day, each a list of (n_markets, n_yes, spread). Every
    market gets its own series so the cluster tier never interferes."""
    store = Store(tmp_path / "a.duckdb")
    infos, candles, i = [], [], 0
    for day, groups in enumerate(day_specs):
        close = CLOSE - timedelta(days=day)
        for n, n_yes, spread in groups:
            for k in range(n):
                mid_ = f"Q{i}"
                infos.append(
                    MarketInfo(
                        venue="kalshi",
                        market_id=mid_,
                        series=f"T{i}",
                        result="yes" if k < n_yes else "no",
                        close_time=close,
                    )
                )
                candles.append(
                    _candle(
                        0.50,
                        mid_,
                        (close - timedelta(hours=2)).replace(tzinfo=None),
                        spread=spread,
                    )
                )
                i += 1
    store.upsert_markets(infos)
    store.insert_candles(candles)
    atlas = build_atlas(store.conn)
    store.close()
    return [
        b for b in atlas["buckets"] if b["horizon"] == "1h" and b["decile"] == 5
    ][0]


def _uniform(spread, n=10):
    """42 all-yes days and 8 all-no days at implied 0.50 -- day-weighted
    realized 0.84, a +0.34 gap that clears every Wilson tier."""
    return [[(n, n, spread)]] * 42 + [[(n, 0, spread)]] * 8


_EMPTY_BOOK = 0.90  # bid 0.05 / ask 0.95: passes the crossed and sentinel gates
_QUOTED = 0.02


def test_empty_books_clear_every_wilson_tier_and_fail_the_quoted_tier(tmp_path):
    # THE load-bearing test. Identical outcomes, identical day balance,
    # identical implied -- the ONLY difference is the width of the book the
    # mid was taken from, and it is the one thing no correlation tier sees.
    wide = _spread_atlas(tmp_path / "wide", _uniform(_EMPTY_BOOK))
    assert wide["flagged_day_weighted"], "fixture must reach the strictest tier"
    assert wide["realized_day_weighted"] - wide["implied_day_weighted"] == (
        pytest.approx(0.34, abs=1e-3)
    )
    # ...and not one of its 500 observations was quoted, so the gap is
    # carried entirely by books that were never two-sided
    assert wide["quoted_n"] == 0
    assert wide["wide_share"] == 1.0
    assert not wide["flagged_quoted"]

    tight = _spread_atlas(tmp_path / "tight", _uniform(_QUOTED))
    # the discrimination control: same numbers, real books, tier survives.
    # Without this the gate would be indistinguishable from one that simply
    # rejects everything at the strictest tier.
    assert tight["flagged_day_weighted"] and tight["flagged_quoted"]
    assert tight["quoted_n"] == 500 and tight["wide_share"] == 0.0
    assert tight["realized_day_weighted"] == pytest.approx(
        wide["realized_day_weighted"], abs=1e-9
    )


def test_quoted_subsample_that_flips_sign_is_not_a_confirmation(tmp_path):
    # 16 wide all-yes + 4 quoted all-no per day, 50 days. Pooled, realized
    # runs ABOVE implied and every tier flags; on the quoted books alone it
    # runs BELOW. Both exclusions are individually significant, so this must
    # fail on DIRECTION rather than on lack of evidence.
    b = _spread_atlas(
        tmp_path, [[(16, 16, _EMPTY_BOOK), (4, 0, _QUOTED)]] * 50
    )
    assert b["flagged_day_weighted"]
    assert b["realized_day_weighted"] > b["implied_day_weighted"]
    # the quoted subsample cleared the evidence bar and the interval on its
    # own -- it is rejected purely for pointing the other way
    assert b["quoted_n"] == MIN_N
    assert not (
        b["wilson_quoted_lo"]
        <= b["quoted_implied_day_weighted"]
        <= b["wilson_quoted_hi"]
    )
    assert b["quoted_realized_day_weighted"] < b["quoted_implied_day_weighted"]
    assert not b["flagged_quoted"]


def test_too_few_quoted_observations_is_not_a_flag(tmp_path):
    # same direction on the quoted books, but only 150 of them. Agreement
    # under the evidence bar is not a confirmation; it is silence.
    b = _spread_atlas(
        tmp_path,
        [[(17, 17, _EMPTY_BOOK), (3, 3, _QUOTED)]] * 42
        + [[(17, 0, _EMPTY_BOOK), (3, 0, _QUOTED)]] * 8,
    )
    assert b["flagged_day_weighted"]
    assert b["quoted_n"] == 150 < MIN_N
    assert b["quoted_realized_day_weighted"] > b["quoted_implied_day_weighted"]
    assert not b["flagged_quoted"]


def test_spread_is_read_from_the_mid_s_own_candle(tmp_path):
    # `mid` is arg_max over end_ts, so the width must be too: a bucket whose
    # freshest candle is empty must not read as quoted because an older
    # candle happened to be tight.
    store = Store(tmp_path / "a.duckdb")
    store.upsert_markets(
        [MarketInfo(venue="kalshi", market_id="A", result="yes", close_time=CLOSE)]
    )
    old = (CLOSE - timedelta(hours=25)).replace(tzinfo=None)
    new = (CLOSE - timedelta(hours=24)).replace(tzinfo=None)
    store.insert_candles(
        [
            _candle(0.50, "A", old, spread=_QUOTED),  # tight, stale
            _candle(0.50, "A", new, spread=_EMPTY_BOOK),  # empty, fresh
        ]
    )
    b = [
        x
        for x in build_atlas(store.conn)["buckets"]
        if x["horizon"] == "24h" and x["decile"] == 5
    ][0]
    store.close()
    assert b["n"] == 1
    assert b["median_spread"] == pytest.approx(_EMPTY_BOOK, abs=1e-6)
    assert b["wide_share"] == 1.0 and b["quoted_n"] == 0


def test_every_bucket_reports_spread_and_the_quoted_tier_nests(tmp_path):
    # spread is reported unconditionally, so contamination stays readable in
    # buckets no tier selected; and a quoted survivor must be a
    # day-weighted survivor, or the report claims a flag a looser tier
    # already rejected.
    store = Store(tmp_path / "a.duckdb")
    store.upsert_markets(
        [MarketInfo(venue="kalshi", market_id="A", result="yes", close_time=CLOSE)]
    )
    store.insert_candles(
        [_candle(0.50, "A", (CLOSE - timedelta(hours=2)).replace(tzinfo=None))]
    )
    atlas = build_atlas(store.conn)
    store.close()
    for b in atlas["buckets"]:
        assert b["median_spread"] is not None and b["mean_spread"] is not None
        assert 0.0 <= b["wide_share"] <= 1.0
        assert b["flagged_day_weighted"] >= b["flagged_quoted"]
        # a bucket with no quoted observations reports None, not 0.0: the
        # two would print identically as a finding the data cannot support
        if b["quoted_n"] == 0:
            assert b["quoted_implied"] is None
    assert "flagged_quoted" in atlas and "flag_rule_quoted" in atlas
    assert MAX_QUOTED_SPREAD == 0.20
