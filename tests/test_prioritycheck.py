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


def test_late_decrement_contributes_its_delay_to_the_timing_sample(conn):
    """The tail that sizes ABSORB_WINDOW must be IN the timing sample.

    A sample drawn only from exact-window matches is bounded by ±5ms by
    construction, so it cannot report a delay wider than the window it is
    quoted to justify -- and the prints it drops are exactly the late
    ones. The 2026-07-13 reading dropped 159 of them and published a
    `max_ms` of 4.93, i.e. the window edge.
    """
    _trade(conn, "2026-07-13 12:00:00.000", 0.40, 7, "no")
    _delta(conn, "2026-07-13 12:00:00.001", "yes", 0.40, -7)  # +1ms: exact
    _trade(conn, "2026-07-13 12:00:10.000", 0.40, 7, "no")
    _delta(conn, "2026-07-13 12:00:11.000", "yes", 0.40, -7)  # +1s: late
    r = check_market(conn, "M", T0)
    assert r["exact_match"] == 1
    assert r["late_decrement"] == 1
    assert r["absorb_match"] == 2
    assert [round(x, 3) for x in r["dt_ms"]] == [1.0, 1000.0]


def test_timing_publishes_the_absorb_population_and_the_conditioned_one():
    from simulator.prioritycheck import EXACT_WINDOW, _timing

    dt_ms = sorted([-4.0, 0.5, 1.5, 900.0])
    full = _timing(dt_ms)
    assert full["n"] == 4
    assert full["max_ms"] == 900.0  # the tail, not the window edge
    assert full["within_1ms_frac"] == 0.25

    tight = _timing([x for x in dt_ms if abs(x) <= EXACT_WINDOW * 1000])
    assert tight["n"] == 3
    assert tight["max_ms"] == 1.5
    assert tight["within_1ms_frac"] == 0.3333  # 0.8495 on the 07-13 reading


def test_p95_is_the_nearest_rank_not_the_maximum():
    """`ordered[int(0.95 * n)]` is the maximum for every n <= 20.

    It takes the (floor(0.95n)+1)-th order statistic, so its rank is at
    or above 95% always, and exactly the maximum whenever 0.95n is an
    integer -- publishing `max_ms` a second time under the name `p95_ms`.
    """
    from simulator.prioritycheck import _p95

    xs = list(range(1, 21))  # n=20: 0.95n = 19 exactly
    assert xs[int(len(xs) * 0.95)] == 20  # the old index: the maximum
    assert _p95(xs) == 19  # nearest rank: the smallest value >= 95%

    assert _p95([7.0]) == 7.0  # n=1 must not underflow to index -1
    assert _p95(list(range(1, 101))) == 95


def test_timing_censuses_the_tail_that_sizes_the_window():
    """"ABSORB_WINDOW=2s is generous" has to be checkable FROM the artifact.

    The 2026-09-20 reading needed 1796ms of window on its widest pairing
    -- 90% of the 2s budget -- which no statistic the old timing block
    published could have shown.
    """
    from simulator.prioritycheck import _timing

    out = _timing(sorted([-1796.2, -0.5, 0.5, 6.0, 60.0]))
    assert out["over_ms"] == {"5": 3, "50": 2, "500": 1}
