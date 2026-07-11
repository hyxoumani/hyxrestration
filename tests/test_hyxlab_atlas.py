"""Calibration atlas: Wilson interval against known values, bucket
construction on a hand-computed fixture, crossed/sentinel exclusion."""

from datetime import UTC, datetime, timedelta

import pytest

from hyxlab.models import MarketInfo
from hyxlab.store import Store
from simulator.atlas import build_atlas, wilson

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
