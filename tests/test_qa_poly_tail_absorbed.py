"""The absorber that licenses `trades_tail`'s missing retry ladder, as a
standing check rather than a journal scrape.

`collector/venues/polymarket.py` declines to retry a truncated trade tail on
one argument: the next daily sweep re-fetches the same tail from offset 0, so
a stop is a delay and not a loss. That argument is a claim about DEPTH per
market -- the archived tail must end up holding MORE prints than the truncated
pass returned -- and the run counter the module published (`tail_truncated`)
records neither the market nor the depth. Establishing it on 2026-09-24 took a
1,358-line journal scrape plus two archive queries; `poly_tail_stops` plus
this check reduce the same question to SQL, daily.
"""

from datetime import UTC, datetime, timedelta

import pytest

from collector import qa
from hyxlab.store import Store

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _clean_ledger():
    qa._failures.clear()
    qa._ran.clear()
    yield
    qa._failures.clear()
    qa._ran.clear()


def _archive(tmp_path, stops, trades_by_market, runs_by_market=None):
    """`runs_by_market` writes the re-visit oracle: one `poly_market_stats`
    row per (market, sweep-run start instant), which is exactly what
    `poly_sweep` writes for every market it ENUMERATES."""
    store = Store(tmp_path / "a.duckdb")
    if stops:
        store.insert_poly_tail_stops(stops)
    for market_id, run_ts in (runs_by_market or {}).items():
        store.insert_poly_stats([(market_id, ts, 1.0, 1.0) for ts in run_ts])
    rows = []
    for market_id, n in trades_by_market.items():
        rows += [
            ("polymarket", market_id, f"{market_id}-{i}", NOW, 0.5, 1.0, "yes", False)
            for i in range(n)
        ]
    if rows:
        store.insert_trades(rows)
    return store


NAME = "polymarket truncated tails absorbed by a later sweep"


def test_empty_table_is_green_not_alarming(tmp_path):
    """The table fills only once a sweep runs with the recorder. An archive
    that predates it must not alarm about its own age -- the poly_prices /
    news_items guard idiom."""
    store = _archive(tmp_path, [], {})
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == []
    assert NAME in qa._ran


def test_a_deeper_later_tail_is_the_absorber_working(tmp_path):
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    store = _archive(tmp_path, [("0xaa", old, 500, 429)], {"0xaa": 1200})
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == []


def test_a_stranded_market_no_deeper_than_its_worst_stop_fails(tmp_path, capsys):
    """The one reachable hole: no deeper than the truncated pass returned AND
    no later sweep run ever came back, so whether the tape ended at the
    truncation boundary is now unknowable and anything past it is lost."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    store = _archive(tmp_path, [("0xaa", old, 500, 429)], {"0xaa": 500})
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == [NAME]
    assert "never re-visited by a later sweep run" in capsys.readouterr().out


def test_equal_depth_plus_a_later_run_is_absorbed_not_a_hole(tmp_path, capsys):
    """The reading depth alone gets WRONG, and it is not hypothetical: 3 of
    the 1,299 markets ever logged stopping early sit at exactly equal depth
    (worst 500, archived 500), and all three were re-visited by later runs
    that found the same 500 prints. `prints` is always a multiple of the
    500-row page, so a tape of exactly 500 stops there and only the request
    that would have CONFIRMED the end is what got the 429. Nothing was lost,
    and the depth-only check called it a hole."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    store = _archive(
        tmp_path,
        [("0xaa", old, 500, 429)],
        {"0xaa": 500},
        {"0xaa": [old + timedelta(days=1)]},
    )
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == []
    assert "1 re-visited by a later run" in capsys.readouterr().out


def test_the_run_that_produced_the_stop_does_not_count_as_a_re_visit(tmp_path):
    """The subtlety the whole oracle turns on. `poly_market_stats.ts` is the
    run's START instant and the sweep takes ~15h, so a stop's wall clock is
    always HOURS AFTER the stats row written by its own run. Comparing
    against anything but the LAST stop would read every truncating run as its
    own absorber and pass the hole it exists to catch."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    store = _archive(
        tmp_path,
        [("0xaa", old, 500, 429)],
        {"0xaa": 500},
        {"0xaa": [old - timedelta(hours=10)]},
    )
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == [NAME]


def test_a_shallower_tape_is_reported_as_its_own_class(tmp_path, capsys):
    """`archived >= worst` holds STRUCTURALLY — the truncated pass archives
    the prints it got and nothing in this repo deletes from `trades` — so a
    shallower tape is not a hole in the absorber but something pruning the
    tape, and it must not be reported under the stranded-market wording whose
    remedy is unrelated."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    store = _archive(
        tmp_path,
        [("0xaa", old, 500, 429)],
        {"0xaa": 200},
        {"0xaa": [old + timedelta(days=1)]},
    )
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == [NAME]
    assert "FEWER prints" in capsys.readouterr().out


def test_a_zero_print_stop_never_re_visited_fails(tmp_path):
    """The emptiest possible case: the tail came back with nothing and the
    archive still holds nothing. 0 == 0 is equality, so it is judged by the
    re-visit oracle like any other -- and with no later run it is stranded.
    A strict `archived < worst` would call this healthy."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    store = _archive(tmp_path, [("0xaa", old, 0, 429)], {})
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == [NAME]


def test_a_zero_print_stop_a_later_run_confirms_is_absorbed(tmp_path):
    """Same 0 == 0, opposite verdict: a later run enumerated the market and
    the archive still holds nothing, so the market has no tape rather than a
    lost one. 688 of the 1,358 recorded stops returned zero prints, so this
    is the majority class and failing it wholesale would bury the check."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    store = _archive(tmp_path, [("0xaa", old, 0, 429)], {}, {"0xaa": [old + timedelta(days=1)]})
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == []


def test_the_worst_stop_is_the_bar_not_the_latest_one(tmp_path):
    """Two stops on one market: 500 prints then 100. The absorber's promise
    is that the archive beats the DEEPEST truncated pass; judging against the
    most recent one would pass a market holding 200 prints of a 500-print
    tail."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    stops = [("0xaa", old - timedelta(days=1), 500, 429), ("0xaa", old, 100, 408)]
    store = _archive(tmp_path, stops, {"0xaa": 200}, {"0xaa": [old + timedelta(days=1)]})
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == [NAME]


def test_a_stop_inside_the_grace_is_excluded_not_passed(tmp_path):
    """The next sweep has not run yet, so the archive holds no evidence
    either way. Calling that green would make the check loudest exactly when
    it knows least -- a market truncated an hour ago is SUPPOSED to be
    shallow."""
    fresh = NOW - timedelta(hours=1)
    store = _archive(tmp_path, [("0xaa", fresh, 500, 429)], {"0xaa": 10})
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == []


def test_grace_covers_two_full_sweep_cycles(tmp_path):
    """The poly sweep takes ~15h (895.9 min measured 2026-09-24), so one
    cycle is not one day. A market truncated 30h ago may legitimately not
    have been re-visited yet."""
    assert qa.POLY_TAIL_ABSORB_GRACE_H >= 2 * 24.0
    store = _archive(tmp_path, [("0xaa", NOW - timedelta(hours=30), 500, 429)], {"0xaa": 10})
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == []


def test_a_kalshi_tape_cannot_stand_in_for_the_poly_one(tmp_path):
    """The join must be venue-scoped: a kalshi market sharing an id would
    otherwise absorb a polymarket hole."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    store = _archive(tmp_path, [("0xaa", old, 100, 429)], {})
    store.insert_trades(
        [("kalshi", "0xaa", f"k{i}", NOW, 0.5, 1.0, "yes", False) for i in range(900)]
    )
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == [NAME]
