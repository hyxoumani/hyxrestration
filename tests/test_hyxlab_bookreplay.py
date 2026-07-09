"""BookReplayer: synthetic Kalshi event sequences → exact top-of-book
snapshots, with gap honesty and mirror-derived asks."""

from datetime import UTC, datetime, timedelta

import pytest

from hyxlab.streamstore import BookEvent
from simulator.bookreplay import BookReplayer, replay_snapshots

T0 = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def ev(kind, side, price, qty, seq, sid=1, mid="M1", ts_off=0.0, venue="kalshi"):
    return BookEvent(
        venue=venue,
        market_id=mid,
        recv_ts=T0 + timedelta(seconds=ts_off),
        src_ts=None,
        sid=sid,
        seq=seq,
        kind=kind,
        side=side,
        price=price,
        qty=qty,
    )


def seed(r, seq=1, sid=1, mid="M1"):
    """Full image: yes bids 0.40(100)/0.39(50); no bid 0.55(30).
    Rows build silently; finalize emits the completed image."""
    for e in (
        ev("snap", "yes", 0.40, 100, seq, sid, mid),
        ev("snap", "yes", 0.39, 50, seq, sid, mid),
        ev("snap", "no", 0.55, 30, seq, sid, mid),
    ):
        assert r.apply(e) is None  # never emit a half-applied image
    return r.finalize_snap(mid)


def test_snapshot_seeds_book_with_mirror_derived_asks():
    r = BookReplayer()
    top = seed(r)
    assert top.yes_bid == 0.40 and top.yes_bid_size == 100
    assert top.no_bid == 0.55 and top.no_bid_size == 30
    assert top.yes_ask == 0.45  # 1 - best no bid
    assert top.no_ask == 0.60  # 1 - best yes bid
    assert top.yes_ask_size == 30 and top.no_ask_size == 100  # opposite side's size


def test_unchanged_reimage_does_not_emit():
    r = BookReplayer()
    seed(r, seq=1)
    assert seed(r, seq=2) is None  # identical image -> top unchanged -> silent


def test_delta_at_top_emits_and_off_top_does_not():
    r = BookReplayer()
    seed(r)
    assert r.apply(ev("delta", "yes", 0.39, +10, seq=2)) is None  # not top
    s = r.apply(ev("delta", "yes", 0.40, -100, seq=3))  # top consumed
    assert s is not None
    assert s.yes_bid == 0.39 and s.yes_bid_size == 60  # next level exposed (50+10)


def test_delta_before_seed_is_ignored():
    r = BookReplayer()
    assert r.apply(ev("delta", "yes", 0.40, +10, seq=1)) is None


def test_invalidate_blocks_until_reseed():
    r = BookReplayer()
    seed(r)
    r.invalidate()
    assert r.apply(ev("delta", "yes", 0.40, -100, seq=5)) is None  # unknown book
    s = seed(r, seq=6)  # fresh image re-seeds
    assert s.yes_bid == 0.40


def test_new_snapshot_replaces_old_image():
    r = BookReplayer()
    seed(r, seq=1)
    r.apply(ev("snap", "yes", 0.30, 5, seq=9))  # new (sid,seq) image
    s = r.finalize_snap("M1")
    assert s is not None
    assert s.yes_bid == 0.30  # 0.40/0.39 levels are gone, not merged
    assert s.no_bid is None


def test_negative_clamp_removes_level():
    r = BookReplayer()
    seed(r)
    s = r.apply(ev("delta", "yes", 0.40, -150, seq=2))  # over-remove
    assert s.yes_bid == 0.39  # clamped to removal, next level exposed


def test_replay_snapshots_applies_gap_intervals():
    events = [
        ev("snap", "yes", 0.40, 100, seq=1, ts_off=0),
        ev("delta", "yes", 0.40, -50, seq=2, ts_off=10),  # after gap start
        ev("snap", "yes", 0.35, 20, seq=3, ts_off=20),  # re-seed
    ]
    gap = (T0 + timedelta(seconds=5), T0 + timedelta(seconds=6))
    snaps = list(replay_snapshots(events, gaps=[gap]))
    # delta at t+10 must be suppressed (book unknown); only the two seeds emit
    assert [s.yes_bid for s in snaps] == [0.40, 0.35]


def test_non_kalshi_events_refused():
    r = BookReplayer()
    with pytest.raises(NotImplementedError):
        r.apply(ev("snap", "bid", 0.5, 1, seq=1, venue="polymarket"))


def test_multi_market_independence():
    r = BookReplayer()
    seed(r, mid="A")
    seed(r, mid="B", seq=2)
    s = r.apply(ev("delta", "yes", 0.40, -100, seq=3, mid="A"))
    assert s.market_id == "A" and s.yes_bid == 0.39
