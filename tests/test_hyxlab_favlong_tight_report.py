"""The FavLongTight runner's REPORTING block, exercised without the replay.

The 2026-08-23 registered run reached this code after 2h43m of replay
and died in it (`median` over datetimes on an even fill count), losing
the whole pass. The strategy was well tested; the block that turns its
fills into the registered verdict was not tested at all. These tests
close that gap: every threshold path and the even/odd close_time split.
"""

from datetime import datetime, timedelta

from hyxlab.models import Fill, MarketInfo
from simulator.run_favlong_tight import BANDS, _band_block

T0 = datetime(2026, 7, 20, 12, 0)
SPEC_A = BANDS["A"]


def settled_row(price, won, *, series="KXHIGHNY", close_offset_h=0, fee=0.01, qty=10.0):
    """One (fill, market, payout) triple in the shape main() assembles."""
    f = Fill(
        strategy=SPEC_A["name"],
        venue="kalshi",
        market_id=f"M{price}-{close_offset_h}-{series}",
        side="yes",
        qty=qty,
        price=price,
        fee=fee,
        ts=T0 - timedelta(hours=24),
        maker=False,
    )
    info = MarketInfo(
        venue="kalshi",
        market_id=f.market_id,
        series=series,
        close_time=T0 + timedelta(hours=close_offset_h),
        result="yes" if won else "no",
    )
    return (f, info, qty if won else 0.0)


CATS = {f"KXCAT{i}": f"cat{i}" for i in range(6)} | {"KXHIGHNY": "Climate"}


def test_even_fill_count_splits_halves_without_datetime_arithmetic():
    # The exact 2026-08-23 crash: an even n makes statistics.median average
    # the two middle close_times, which datetimes cannot do.
    rows = [settled_row(0.85, True, close_offset_h=h) for h in range(4)]
    block = _band_block(SPEC_A, rows, CATS)
    assert set(block["halves_pnl"]) == {"H1", "H2"}


def test_odd_fill_count_also_splits():
    rows = [settled_row(0.85, True, close_offset_h=h) for h in range(5)]
    assert set(_band_block(SPEC_A, rows, CATS)["halves_pnl"]) == {"H1", "H2"}


def test_low_median_keeps_h1_non_empty_when_closes_tie():
    # Every market closing at the same instant is a real archive shape
    # (one daily ladder). `close_time <= med_close` must not put all of
    # them in H2 and read "H1 flat" as a threshold failure.
    rows = [settled_row(0.85, True, close_offset_h=0) for _ in range(4)]
    block = _band_block(SPEC_A, rows, CATS)
    assert block["halves_pnl"]["H1"] > 0
    assert block["halves_pnl"]["H2"] == 0.0


def test_no_settled_fills_reads_underpowered():
    block = _band_block(SPEC_A, [], CATS)
    assert block["settled_fills"] == 0
    assert block["verdict"].startswith("UNDERPOWERED")


def test_price_outside_the_registered_band_aborts_on_g2():
    rows = [settled_row(0.60, True), settled_row(0.85, True)]
    block = _band_block(SPEC_A, rows, CATS)
    assert block["verdict"].startswith("ABORT G2")


def test_thin_sample_reads_underpowered_not_fail():
    # Below MIN_FILLS a losing band must not be reported as a kill.
    rows = [settled_row(0.85, False) for _ in range(10)]
    block = _band_block(SPEC_A, rows, CATS)
    assert block["roi"] < 0
    assert block["verdict"].startswith("UNDERPOWERED")


def _powered_rows(win_rate, n=2400, **kw):
    return [settled_row(0.85, i % 10 < win_rate * 10, **kw) for i in range(n)]


def test_powered_losing_band_fails():
    block = _band_block(SPEC_A, _powered_rows(0.5), CATS)
    assert block["settled_fills"] >= 2000
    assert block["verdict"] == "FAIL (kill)"


def test_survive_needs_four_positive_categories():
    # Profitable everywhere, but concentrated in 3 categories: the
    # breadth threshold, not ROI, is what must decide.
    rows = []
    for i in range(3):
        rows += _powered_rows(1.0, n=800, series=f"KXCAT{i}", close_offset_h=i)
    block = _band_block(SPEC_A, rows, CATS)
    assert block["roi"] > 0.01
    assert block["thresholds"]["n_cats_ge_100_fills"] == 3
    assert block["verdict"] == "FAIL (kill)"


def test_survive_when_every_registered_threshold_holds():
    rows = []
    for i in range(6):
        rows += _powered_rows(1.0, n=500, series=f"KXCAT{i}", close_offset_h=i)
    # both sub-bands of A must be positive: 0.80-0.875 and 0.875-0.95
    rows += _powered_rows(1.0, n=500, series="KXCAT0", close_offset_h=7)
    rows += [settled_row(0.90, True, series="KXCAT1", close_offset_h=8) for _ in range(500)]
    block = _band_block(SPEC_A, rows, CATS)
    assert block["thresholds"]["both_sub_bands_positive"]
    assert block["verdict"].startswith("SURVIVE")
