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


def _archive(tmp_path, stops, trades_by_market):
    store = Store(tmp_path / "a.duckdb")
    if stops:
        store.insert_poly_tail_stops(stops)
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


def test_a_market_no_deeper_than_its_worst_stop_fails(tmp_path):
    """The failure the ladder-less design cannot survive: the sweep stopped
    re-visiting the market, so every stop on it is a real hole."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    store = _archive(tmp_path, [("0xaa", old, 500, 429)], {"0xaa": 500})
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == [NAME]


def test_a_zero_print_stop_with_nothing_archived_fails(tmp_path):
    """The strictly worse subset: the tail came back empty and the archive
    still holds nothing for the market. `archived <= worst` catches 0 <= 0
    only because the comparison is not strict -- a `<` would call the
    emptiest possible case healthy."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    store = _archive(tmp_path, [("0xaa", old, 0, 429)], {})
    qa.qa_poly_tail_absorbed(store.conn, NOW)
    store.close()
    assert qa._failures == [NAME]


def test_the_worst_stop_is_the_bar_not_the_latest_one(tmp_path):
    """Two stops on one market: 500 prints then 100. The absorber's promise
    is that the archive beats the DEEPEST truncated pass; judging against the
    most recent one would pass a market holding 200 prints of a 500-print
    tail."""
    old = NOW - timedelta(hours=qa.POLY_TAIL_ABSORB_GRACE_H + 1)
    stops = [("0xaa", old - timedelta(days=1), 500, 429), ("0xaa", old, 100, 408)]
    store = _archive(tmp_path, stops, {"0xaa": 200})
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
