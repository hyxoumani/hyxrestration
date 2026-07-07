"""Fee models against the published schedules (see hyxlab/fees.py sources)."""

import pytest

from hyxlab.fees import KALSHI, POLYMARKET_INTL_CRYPTO, POLYMARKET_US, kalshi_model


def test_kalshi_taker_peak():
    # 100 contracts at 50c: 0.07 * 100 * 0.25 = $1.75 (schedule's max example)
    assert KALSHI.fee(0.50, 100, taker=True) == 1.75


def test_kalshi_taker_rounds_up_to_cent():
    # 1 contract at 50c: 0.0175 -> ceil -> $0.02
    assert KALSHI.fee(0.50, 1, taker=True) == 0.02


def test_kalshi_makers_free_on_default_quadratic_series():
    # /series metadata (2026-07-06): 11,040 of 11,170 series are fee_type
    # "quadratic" -> makers pay nothing.
    assert KALSHI.fee(0.50, 100, taker=False) == 0.0


def test_kalshi_maker_fee_series_quarter_of_taker():
    # The 130 "quadratic_with_maker_fees" series: 0.0175*100*0.25 -> ceil -> $0.44
    m = kalshi_model("quadratic_with_maker_fees")
    assert m.fee(0.50, 100, taker=False) == 0.44
    assert m.fee(0.50, 100, taker=True) == 1.75


def test_kalshi_model_resolves_multiplier_and_free_series():
    assert kalshi_model("quadratic", 0).fee(0.50, 100, taker=True) == 0.0
    assert kalshi_model("quadratic", 1).fee(0.50, 100, taker=True) == 1.75


def test_kalshi_fee_shrinks_at_extremes():
    assert KALSHI.fee(0.90, 100, taker=True) == 0.63  # 0.07*100*0.09


def test_polymarket_us_taker_peak():
    # Cap $1.25 per 100 contracts at 50c
    assert POLYMARKET_US.fee(0.50, 100, taker=True) == 1.25


def test_polymarket_us_maker_rebate_is_negative():
    fee = POLYMARKET_US.fee(0.50, 100, taker=False)
    assert fee == -0.3125


def test_polymarket_intl_crypto_peak():
    assert POLYMARKET_INTL_CRYPTO.fee(0.50, 100, taker=True) == pytest.approx(1.75)
    assert POLYMARKET_INTL_CRYPTO.fee(0.50, 100, taker=False) == 0.0


def test_cross_venue_fee_wall_near_mid():
    # The deepdive_reanalysis §2 worked example at 100-contract size:
    # ~3c/share combined taker fees near 50c closes a 3c gross arb.
    kalshi_leg = KALSHI.fee(0.48, 100, taker=True)  # 1.7472 -> ceil -> 1.75
    poly_leg = POLYMARKET_US.fee(0.49, 100, taker=True)  # 1.2495
    per_share = (kalshi_leg + poly_leg) / 100
    assert 0.028 <= per_share <= 0.032
