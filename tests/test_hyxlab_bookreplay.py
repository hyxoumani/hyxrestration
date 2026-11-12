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


# --- void rows: an empty orderbook_snapshot is a book-clearing image ---
# Production case (verified 2026-07-31 on the live archive): Kalshi wipes an
# expired daily market's ladder at the top of the hour by sending an
# orderbook_snapshot with empty levels. That frame archives no book row, so
# it lands as kind='void'. Ignoring it left KXHIGHNY-26JUL30-B79.5 replaying
# a 6-level NO ladder (79 @ 0.99) forever past a book that no longer existed.


def void(type_, seq=9, sid=1, mid="M1", ts_off=0.0):
    return ev("void", type_, 0.0, 0.0, seq, sid, mid, ts_off)


def test_empty_snapshot_clears_a_seeded_book_and_emits():
    r = BookReplayer()
    seed(r)
    s = r.apply(void("orderbook_snapshot", ts_off=5))
    # Emits, and the emitted top is EMPTY on both sides — not the stale
    # 0.40/0.55 image. Asserting the values, not just that something fired,
    # so a no-op-but-emit implementation fails on the numbers.
    assert s is not None
    assert (s.yes_bid, s.no_bid, s.yes_ask, s.no_ask) == (None, None, None, None)
    assert r.depth("M1") == {"yes": [], "no": []}
    # A later delta builds from empty, not from the phantom ladder.
    s2 = r.apply(ev("delta", "yes", 0.10, 7, seq=10, ts_off=6))
    assert (s2.yes_bid, s2.yes_bid_size) == (0.10, 7.0)


def test_empty_snapshot_seeds_an_unknown_book_so_later_deltas_apply():
    # Class A: a market whose book opens EMPTY at connection start. Before
    # the fix it stayed unseeded and every delta until the next reconnect
    # image was discarded (4 real KXCPI-26OCT-T0.1 deltas on 07-30, 5.3h).
    r = BookReplayer()
    assert r.apply(ev("delta", "yes", 0.10, 7, seq=1)) is None  # unseeded
    r.apply(void("orderbook_snapshot", seq=2, ts_off=1))
    s = r.apply(ev("delta", "yes", 0.10, 7, seq=3, ts_off=2))
    assert s is not None and s.yes_bid == 0.10


def test_control_ack_and_legacy_void_rows_do_not_touch_the_book():
    # Discrimination control: the tier must key on the frame TYPE, not on
    # kind='void', or every sequenced control ack would wipe a live book.
    r = BookReplayer()
    seed(r)
    assert r.apply(void("ok", ts_off=5)) is None
    assert r.apply(void("", ts_off=6)) is None  # legacy, unattributable
    assert r.depth("M1") == {"yes": [(0.40, 100.0), (0.39, 50.0)], "no": [(0.55, 30.0)]}


def test_repeated_empty_snapshot_does_not_re_emit():
    r = BookReplayer()
    seed(r)
    assert r.apply(void("orderbook_snapshot", ts_off=5)) is not None
    assert r.apply(void("orderbook_snapshot", seq=10, ts_off=6)) is None


def test_empty_snapshot_does_not_open_a_row_group_in_replay_snapshots():
    # replay_snapshots groups multi-row snap images by (market, sid, seq);
    # a void row is a complete image on its own, so it must emit inline
    # rather than waiting for a finalize that never comes.
    events = [
        ev("snap", "yes", 0.40, 100, seq=1),
        ev("snap", "no", 0.55, 30, seq=1),
        void("orderbook_snapshot", seq=2, ts_off=1),
    ]
    out = list(replay_snapshots(events))
    assert len(out) == 2
    assert out[0].yes_bid == 0.40
    assert (out[1].yes_bid, out[1].no_bid) == (None, None)


def test_gap_does_not_wipe_an_image_newer_than_its_start():
    """The stream writes one `books` gap row PER SHARD with a shared
    `started_at`, and the shadow reads them across several polls. The
    second poll's copy of the gap must not wipe the book the first poll's
    reconnect image re-seeded (measured 2026-11-02: 28 blind hours on run
    20261023T143153, see `BookReplayer.invalidate`)."""
    r = BookReplayer()
    gap_start = T0 + timedelta(seconds=5)
    # Poll 1: shard A's gap row has closed; its reconnect image re-seeds.
    poll1 = [
        ev("snap", "yes", 0.40, 100, seq=1, ts_off=0),
        ev("snap", "yes", 0.35, 20, seq=3, ts_off=20),  # reconnect image
    ]
    gap_a = (gap_start, T0 + timedelta(seconds=20))
    assert [s.yes_bid for s in replay_snapshots(poll1, gaps=[gap_a], replayer=r)] == [0.40, 0.35]
    # Poll 2: shard B's row for the SAME break closes later; the delta on
    # the re-seeded book must still emit.
    poll2 = [ev("delta", "yes", 0.35, -20, seq=4, ts_off=40)]
    gap_b = (gap_start, T0 + timedelta(seconds=30))
    snaps = list(replay_snapshots(poll2, gaps=[gap_b], replayer=r))
    assert len(snaps) == 1 and snaps[0].yes_bid is None  # top consumed, book known
    # A book whose image PREDATES the gap start is still wiped.
    r2 = BookReplayer()
    seed(r2)
    r2.invalidate(before=T0 + timedelta(seconds=5))
    assert r2.apply(ev("delta", "yes", 0.40, -100, seq=5)) is None


# -- per-shard gap scoping (2026-11-02) ----------------------------------------
#
# The stream archives each books connection's sids under its own prefix
# (`SHARD_SID_STRIDE`) and, since 2026-11-02, names that shard on the gap
# rows the connection writes. A reconnect on one of ~14 shards used to wipe
# every book until each other shard happened to reconnect (11-01 03:52Z:
# 6 h of blindness for one connection's flap).

SID_SHARD_2 = 2 * 1_000_000 + 1


def test_gap_scoped_to_a_shard_leaves_other_shards_books_known():
    r = BookReplayer()
    seed(r, sid=1, mid="A")  # shard 0
    seed(r, sid=SID_SHARD_2, mid="B")  # shard 2
    r.invalidate(before=T0 + timedelta(seconds=5), shard=2)
    assert r.apply(ev("delta", "yes", 0.40, -100, seq=5, sid=1, mid="A")) is not None  # known
    assert r.apply(ev("delta", "yes", 0.40, -100, seq=5, sid=SID_SHARD_2, mid="B")) is None


def test_gap_with_no_shard_wipes_every_book():
    r = BookReplayer()
    seed(r, sid=1, mid="A")
    seed(r, sid=SID_SHARD_2, mid="B")
    r.invalidate(before=T0 + timedelta(seconds=5), shard=None)
    assert r.apply(ev("delta", "yes", 0.40, -100, seq=5, sid=1, mid="A")) is None
    assert r.apply(ev("delta", "yes", 0.40, -100, seq=5, sid=SID_SHARD_2, mid="B")) is None


def test_book_whose_image_carried_no_sid_is_wiped_by_any_shard_gap():
    r = BookReplayer()
    seed(r, sid=None, mid="A")
    r.invalidate(before=T0 + timedelta(seconds=5), shard=7)
    assert r.apply(ev("delta", "yes", 0.40, -100, seq=5, sid=None, mid="A")) is None


def test_replay_snapshots_reads_the_shard_from_a_three_tuple_gap():
    events = [
        ev("snap", "yes", 0.40, 100, seq=1, sid=1, mid="A", ts_off=0),
        ev("snap", "yes", 0.40, 100, seq=1, sid=SID_SHARD_2, mid="B", ts_off=0),
        ev("delta", "yes", 0.40, -50, seq=2, sid=1, mid="A", ts_off=10),  # shard 0: emits
        ev("delta", "yes", 0.40, -50, seq=2, sid=SID_SHARD_2, mid="B", ts_off=10),  # wiped
    ]
    gap = (T0 + timedelta(seconds=5), T0 + timedelta(seconds=6), 2)
    snaps = list(replay_snapshots(events, gaps=[gap]))
    assert [(s.market_id, s.yes_bid_size) for s in snaps] == [("A", 100), ("B", 100), ("A", 50)]
    # The two-tuple form is the pre-2026-11-02 reading: every book wiped.
    snaps = list(replay_snapshots(events, gaps=[gap[:2]]))
    assert [(s.market_id, s.yes_bid_size) for s in snaps] == [("A", 100), ("B", 100)]


def test_a_gap_no_event_reached_still_invalidates_for_the_next_batch():
    """Batch-invariance of gap handling. The shadow reads gap rows by an
    `ended_at` cursor and never re-offers one, so a gap row that lands a
    poll before the next event -- an empty batch, or one whose events all
    predate the gap's start -- must be applied when that batch ends.
    Otherwise the next poll's delta replays onto the pre-gap book: the same
    stream cut at a different place yields a different book (mistakes #186)."""
    gap = (T0 + timedelta(seconds=5), T0 + timedelta(seconds=6))
    delta = ev("delta", "yes", 0.40, -20, seq=4, ts_off=10)

    # Control: one batch -- the delta after the gap is suppressed.
    r1 = BookReplayer()
    seed(r1)
    assert list(replay_snapshots([delta], gaps=[gap], replayer=r1)) == []

    # Empty batch carries the gap; the delta arrives a batch later.
    r2 = BookReplayer()
    seed(r2)
    assert list(replay_snapshots([], gaps=[gap], replayer=r2)) == []
    assert list(replay_snapshots([delta], replayer=r2)) == []

    # Batch whose only event PREDATES the gap's start.
    r3 = BookReplayer()
    seed(r3)
    early = ev("delta", "yes", 0.40, 5, seq=2, ts_off=2)
    assert len(list(replay_snapshots([early], gaps=[gap], replayer=r3))) == 1
    assert list(replay_snapshots([delta], replayer=r3)) == []

    # Vacuity guard: with no gap at all the same delta DOES emit.
    r4 = BookReplayer()
    seed(r4)
    assert len(list(replay_snapshots([delta], replayer=r4))) == 1
