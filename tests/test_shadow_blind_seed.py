"""The blind-seed window (mistakes #196), as the kernel states it: which
runs are in the population, where the window ends, and what a whole-life
report says about it. Synthetic ledgers, no network."""

from datetime import datetime, timedelta

import duckdb

from hyxlab.shadowruns import (
    BLIND_SEED_HOURS,
    BLIND_SEED_STATUSES,
    PER_SHARD_GAPS_FROM,
    blind_seed_block,
    blind_seed_until,
)
from simulator.shadow_attribution import build_attribution
from simulator.shadow_coverage import build_coverage

ERA = datetime(2026, 11, 3, 2, 21)


def _runs(rows, floor_column=True):
    """rows: [(run_id, started_at, seed_floor)]."""
    conn = duckdb.connect(":memory:")
    if floor_column:
        conn.execute(
            "CREATE TABLE shadow_runs (run_id VARCHAR, started_at TIMESTAMP, seed_floor TIMESTAMP)"
        )
        conn.executemany("INSERT INTO shadow_runs VALUES (?,?,?)", rows)
    else:
        conn.execute("CREATE TABLE shadow_runs (run_id VARCHAR, started_at TIMESTAMP)")
        conn.executemany("INSERT INTO shadow_runs VALUES (?,?)", [r[:2] for r in rows])
    return conn


def test_window_ends_twelve_hours_after_a_legacy_start_in_the_era():
    conn = _runs([("R", ERA, None)])
    assert blind_seed_until(conn, "R") == ERA + timedelta(hours=BLIND_SEED_HOURS)


def test_the_era_boundary_is_inclusive_and_one_second_before_it_is_out():
    conn = _runs(
        [
            ("AT", PER_SHARD_GAPS_FROM, None),
            ("PRE", PER_SHARD_GAPS_FROM - timedelta(seconds=1), None),
        ]
    )
    assert blind_seed_until(conn, "AT") is not None
    assert blind_seed_until(conn, "PRE") is None


def test_a_recorded_seed_floor_takes_a_run_out_of_the_population():
    conn = _runs([("R", ERA, ERA - timedelta(hours=10))])
    assert blind_seed_until(conn, "R") is None
    assert blind_seed_block(conn, "R", ERA + timedelta(hours=30))["blind_seed_status"] == (
        "not_in_population"
    )


def test_a_ledger_without_the_floor_column_reads_every_floor_as_null():
    conn = _runs([("R", ERA, None)], floor_column=False)
    assert blind_seed_until(conn, "R") == ERA + timedelta(hours=BLIND_SEED_HOURS)


def test_block_partitions_a_long_run_from_a_short_one():
    """A run that outlived the window is partly blind for exactly the
    window; one that ended inside it is wholly blind for its own life."""
    conn = _runs([("LONG", ERA, None), ("SHORT", ERA, None)])
    long = blind_seed_block(conn, "LONG", ERA + timedelta(hours=261))
    short = blind_seed_block(conn, "SHORT", ERA + timedelta(hours=10))
    assert (long["blind_seed_status"], long["blind_seed_life_hours"]) == ("partly_blind", 12.0)
    assert (short["blind_seed_status"], short["blind_seed_life_hours"]) == ("wholly_blind", 10.0)
    assert long["blind_seed_until"] == "2026-11-03T14:21:00Z"


def test_a_run_the_ledger_cannot_place_is_unplaceable_not_outside():
    """No row and no table are the same answer, and it is not the answer a
    pre-era run gets: its blind hours are None, never a measured 0.0."""
    no_row = _runs([("OTHER", ERA, None)])
    no_table = duckdb.connect(":memory:")
    for conn in (no_row, no_table):
        block = blind_seed_block(conn, "R", ERA)
        assert block["blind_seed_status"] == "unplaceable"
        assert block["blind_seed_life_hours"] is None
    pre = _runs([("R", datetime(2026, 10, 23, 14, 31), None)])
    assert blind_seed_block(pre, "R", ERA)["blind_seed_life_hours"] == 0.0


def test_every_status_the_block_can_return_is_in_the_published_enum():
    conn = _runs([("A", ERA, None), ("B", ERA, ERA)])
    seen = {
        blind_seed_block(conn, "A", ERA + timedelta(hours=1))["blind_seed_status"],
        blind_seed_block(conn, "A", ERA + timedelta(hours=99))["blind_seed_status"],
        blind_seed_block(conn, "B", ERA)["blind_seed_status"],
        blind_seed_block(conn, "ZZ", ERA)["blind_seed_status"],
    }
    assert seen == set(BLIND_SEED_STATUSES)


def test_attribution_run_carries_the_blind_share_of_the_life_it_sums():
    conn = _runs([("R", ERA, None)])
    conn.execute(
        "CREATE TABLE shadow_fills (run_id VARCHAR, strategy VARCHAR, venue VARCHAR,"
        " market_id VARCHAR, side VARCHAR, qty DOUBLE, price DOUBLE, fee DOUBLE, ts TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE shadow_settlements (run_id VARCHAR, market_id VARCHAR,"
        " side VARCHAR, payout DOUBLE, ts TIMESTAMP)"
    )
    conn.execute("CREATE TABLE shadow_equity (run_id VARCHAR, ts TIMESTAMP, equity DOUBLE)")
    conn.execute(
        "INSERT INTO shadow_fills VALUES ('R','probe','kalshi','M-1','yes',1,0.5,0.01,?)", [ERA]
    )
    conn.executemany(
        "INSERT INTO shadow_equity VALUES ('R',?,?)",
        [(ERA, 0.0), (ERA + timedelta(hours=48), -5.0)],
    )
    run = build_attribution(conn)["runs"][0]
    assert run["blind_seed_status"] == "partly_blind"
    assert run["blind_seed_life_hours"] == 12.0


def test_coverage_run_carries_the_blind_share_of_the_life_it_counts():
    conn = _runs([("R", ERA, None)], floor_column=False)
    conn.execute(
        "CREATE TABLE shadow_fills (run_id VARCHAR, market_id VARCHAR, qty DOUBLE, price DOUBLE)"
    )
    conn.execute("CREATE TABLE shadow_equity (run_id VARCHAR, ts TIMESTAMP)")
    conn.execute("INSERT INTO shadow_fills VALUES ('R','M-1',1.0,0.5)")
    conn.execute("INSERT INTO shadow_equity VALUES ('R',?)", [ERA + timedelta(hours=6)])
    markets = duckdb.connect(":memory:")
    markets.execute(
        "CREATE TABLE markets (market_id VARCHAR, close_time TIMESTAMP,"
        " result VARCHAR, updated_at TIMESTAMP)"
    )
    report = build_coverage(conn, markets, now=ERA + timedelta(days=5))
    run = next(r for r in report["runs"] if r["run_id"] == "R")
    assert run["blind_seed_status"] == "wholly_blind"
    assert run["blind_seed_life_hours"] == 6.0
