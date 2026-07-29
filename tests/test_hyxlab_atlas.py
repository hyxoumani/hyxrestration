"""Calibration atlas: Wilson interval against known values, bucket
construction on a hand-computed fixture, crossed/sentinel exclusion."""

from datetime import UTC, datetime, timedelta

import pytest

from hyxlab.models import MarketInfo
from hyxlab.store import Store
from simulator.atlas import BUCKET_SQL, build_atlas, wilson

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
