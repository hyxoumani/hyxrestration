"""Empirical trade→decrement mapping probe (simulator/prioritycheck).

Covers the complement mapping, the exact/absorb-window split, and the
no-match decomposition (late decrement, coverage gap, naive-would-match
staying zero) on a crafted in-memory stream.
"""

import duckdb
import pytest

from simulator.prioritycheck import check_market, naive_level, predicted_level

T0 = "2026-07-13 11:59:00"  # strictly before the crafted events (recv_ts > since)


def test_predicted_level_is_the_complement_mapping():
    # no-taker lifts resting YES at p; yes-taker lifts resting NO at 1-p
    assert predicted_level("no", 0.40) == ("yes", 0.40)
    assert predicted_level("yes", 0.40) == ("no", 0.60)
    assert predicted_level("bogus", 0.40) is None


def test_naive_level_is_the_wrong_same_side_mapping():
    # the mapping the complement rule replaces; probe must show it never fits
    assert naive_level("yes", 0.40) == ("yes", 0.40)
    assert naive_level("no", 0.60) == ("no", 0.60)


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE stream_trades(venue VARCHAR, market_id VARCHAR,"
        " recv_ts TIMESTAMP, price DOUBLE, qty DOUBLE, taker_side VARCHAR)"
    )
    c.execute(
        "CREATE TABLE book_events(venue VARCHAR, market_id VARCHAR,"
        " recv_ts TIMESTAMP, kind VARCHAR, side VARCHAR, price DOUBLE, qty DOUBLE)"
    )
    return c


def _trade(c, ts, price, qty, taker, mid="M"):
    c.execute(
        "INSERT INTO stream_trades VALUES ('kalshi', ?, ?, ?, ?, ?)",
        [mid, ts, price, qty, taker],
    )


def _delta(c, ts, side, price, qty, mid="M"):
    c.execute(
        "INSERT INTO book_events VALUES ('kalshi', ?, ?, 'delta', ?, ?, ?)",
        [mid, ts, side, price, qty],
    )


def test_exact_size_decrement_at_predicted_level_matches(conn):
    _trade(conn, "2026-07-13 12:00:00.000", 0.40, 7, "no")  # → yes@0.40
    _delta(conn, "2026-07-13 12:00:00.001", "yes", 0.40, -7)
    r = check_market(conn, "M", T0)
    assert r["exact_match"] == 1
    assert r["absorb_match"] == 1
    assert r["naive_would_match"] == 0


def test_yes_taker_maps_to_no_complement_and_same_side_never_fits(conn):
    # yes-taker at 0.40 must consume no@0.60, and a same-side yes@0.40
    # decrement of the same size must NOT be counted as a match
    _trade(conn, "2026-07-13 12:00:00.000", 0.40, 5, "yes")  # → no@0.60
    _delta(conn, "2026-07-13 12:00:00.000", "yes", 0.40, -5)  # decoy same-side
    r = check_market(conn, "M", T0)
    assert r["exact_match"] == 0
    assert r["absorb_match"] == 0
    assert r["no_decrement_at_level"] == 1
    assert r["naive_would_match"] == 1  # the decoy is exactly the naive fit


def test_late_decrement_misses_exact_but_lands_in_absorb_window(conn):
    _trade(conn, "2026-07-13 12:00:00.000", 0.40, 7, "no")
    _delta(conn, "2026-07-13 12:00:01.000", "yes", 0.40, -7)  # +1s: outside 5ms, inside 2s
    r = check_market(conn, "M", T0)
    assert r["exact_match"] == 0
    assert r["absorb_match"] == 1
    assert r["late_decrement"] == 1


def test_no_decrement_at_level_is_a_coverage_gap_not_a_mapping_miss(conn):
    _trade(conn, "2026-07-13 12:00:00.000", 0.40, 7, "no")  # → yes@0.40
    _delta(conn, "2026-07-13 12:00:00.001", "yes", 0.55, -7)  # wrong price entirely
    r = check_market(conn, "M", T0)
    assert r["absorb_match"] == 0
    assert r["no_decrement_at_level"] == 1
    assert r["naive_would_match"] == 0
