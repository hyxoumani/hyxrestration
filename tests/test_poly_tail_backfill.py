"""The one-shot journal recovery for `poly_tail_stops`.

The recorder shipped after the 2026-09-24 sweep, so `qa_poly_tail_absorbed`
starts with an empty table while 24 days of real population sit in the
journal -- and the journal rotates, so the recovery expires. These tests pin
the three things it must not do: guess an id, silently drop a line it cannot
read, or write over the range the live recorder owns.
"""

from datetime import datetime, timedelta

import pytest

from collector import poly_tail_backfill as tf
from hyxlab.models import MarketInfo
from hyxlab.store import Store

LINE = (
    "2026-09-12T09:18:31-05:00 hyz python[349689]: [poly] trades_tail "
    "0xb23f06d3d85c91da stopped early at 500 prints (non-list body, status 429)"
)


def test_a_real_journal_line_parses_to_a_row():
    (prefix, at, prints, status) = tf.parse_stops(LINE)[0]
    assert (prefix, prints, status) == ("0xb23f06d3d85c91da", 500, 429)
    assert at == datetime(2026, 9, 12, 14, 18, 31)


def test_the_line_s_own_offset_is_applied_not_assumed_utc():
    """journald renders the BOX's local time (-05:00 here). Reading the stamp
    as UTC would shift every stop five hours forward -- straight through the
    48h grace boundary the check excludes on, and into the range the live
    recorder owns."""
    (_, at, _, _) = tf.parse_stops(LINE)[0]
    naive = datetime.fromisoformat(LINE.split()[0]).replace(tzinfo=None)
    assert at == naive + timedelta(hours=5)
    assert at != naive


def test_an_unreadable_stop_line_is_a_refusal_not_a_skip():
    """A parser that quietly drops what it does not understand is the defect
    that cost 78 days of poly delta capture (mistakes #82). A line carrying
    the marker must parse or stop the run."""
    broken = LINE.replace("status 429", "status ???")
    with pytest.raises(ValueError, match="unreadable stop line"):
        tf.parse_stops(broken)


def test_lines_without_the_marker_are_ignored_silently():
    assert tf.parse_stops("2026-09-12T09:18:31-05:00 hyz python[1]: [poly] 12000/17118") == []


def _store(tmp_path, market_ids):
    store = Store(tmp_path / "a.duckdb")
    store.upsert_markets([MarketInfo(venue="polymarket", market_id=m, title=m) for m in market_ids])
    return store


def test_an_ambiguous_prefix_is_dropped_never_guessed(tmp_path):
    """The log line carries `condition_id[:16]`. Two markets sharing that
    prefix make the id unknowable, and attributing a stop to the wrong market
    would both hide a hole and invent one."""
    store = _store(tmp_path, ["0xaaaaaaaaaaaaaaaa01", "0xaaaaaaaaaaaaaaaa02", "0xbbbbbbbbbbbbbbbb"])
    got = tf.resolve_prefixes(store, ["0xaaaaaaaaaaaaaaaa", "0xbbbbbbbbbbbbbbbb"])
    store.close()
    assert got == {"0xbbbbbbbbbbbbbbbb": "0xbbbbbbbbbbbbbbbb"}


def test_a_prefix_matching_no_polymarket_market_is_dropped(tmp_path):
    store = _store(tmp_path, ["0xbbbbbbbbbbbbbbbb"])
    assert tf.resolve_prefixes(store, ["0xcccccccccccccccc"]) == {}
    store.close()


def test_a_kalshi_market_cannot_resolve_a_poly_prefix(tmp_path):
    store = Store(tmp_path / "a.duckdb")
    store.upsert_markets([MarketInfo(venue="kalshi", market_id="0xbbbbbbbbbbbbbbbb", title="k")])
    assert tf.resolve_prefixes(store, ["0xbbbbbbbbbbbbbbbb"]) == {}
    store.close()
