"""Daily QA checks against synthetic archives: healthy DBs pass, each
seeded defect trips its check."""

from datetime import UTC, datetime, timedelta

import collector.qa as qa
from collector.venues.kalshi_ws import parse_message
from hyxlab.store import Store
from hyxlab.streamstore import StreamStore

NOW = datetime.now(UTC)


def _fresh_stream(path):
    store = StreamStore(path)
    frame = {
        "type": "trade",
        "sid": 1,
        "seq": 1,
        "msg": {
            "market_ticker": "M1",
            "yes_price_dollars": "0.4000",
            "count_fp": "5.00",
            "taker_side": "yes",
            "ts": int(NOW.timestamp()),
            "ts_ms": int(NOW.timestamp() * 1000),
        },
    }
    store.append_trades(parse_message(frame, NOW)[1])
    store.flush()
    return store


def _run(checks, tmp_path, stream=None, archive=None):
    qa._failures.clear()
    if stream is not None:
        qa.qa_stream(26.0, path=str(stream))
    if archive is not None:
        qa.qa_archive(26.0, path=str(archive))
    failed = set(qa._failures)
    qa._failures.clear()
    return failed


def test_healthy_stream_passes(tmp_path):
    _fresh_stream(tmp_path / "s.duckdb")
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert failed == set()


def test_stale_stream_trips_freshness(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    old = NOW - timedelta(hours=2)
    frame = {
        "type": "trade",
        "sid": 1,
        "seq": 1,
        "msg": {
            "market_ticker": "M1",
            "yes_price_dollars": "0.4000",
            "count_fp": "5.00",
            "ts_ms": int(old.timestamp() * 1000),
        },
    }
    store.append_trades(parse_message(frame, old)[1])
    store.flush()
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "stream fresh (trades < 5 min old)" in failed


def test_seq_hole_without_gap_row_trips(tmp_path):
    store = StreamStore(tmp_path / "s.duckdb")
    for seq in (1, 2, 9):  # hole 3..8, no gap row
        frame = {
            "type": "orderbook_delta",
            "sid": 7,
            "seq": seq,
            "msg": {
                "market_ticker": "M1",
                "price_dollars": "0.4000",
                "delta_fp": "1.00",
                "side": "yes",
                "ts_ms": int(NOW.timestamp() * 1000),
            },
        }
        store.append_events(parse_message(frame, NOW)[0])
    # keep trades fresh so only the seq check should fire
    _fresh_stream(tmp_path / "unused.duckdb")
    frame_t = {
        "type": "trade",
        "sid": 1,
        "seq": 1,
        "msg": {
            "market_ticker": "M1",
            "yes_price_dollars": "0.4000",
            "count_fp": "5.00",
            "ts_ms": int(NOW.timestamp() * 1000),
        },
    }
    store.append_trades(parse_message(frame_t, NOW)[1])
    store.flush()
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "book seq contiguous or gap-marked" in failed
    # same hole WITH a gap row is acceptable
    store.append_gap("kalshi", "books", NOW, NOW, "seq_gap")
    store.flush()
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "book seq contiguous or gap-marked" not in failed


def _book_frame(kind, seq, price, qty, sid=1):
    if kind == "snap":
        return {
            "type": "orderbook_snapshot",
            "sid": sid,
            "seq": seq,
            "msg": {"market_ticker": "M1", "yes_dollars_fp": [[price, qty]]},
        }
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {"market_ticker": "M1", "price_dollars": price, "delta_fp": qty, "side": "yes"},
    }


def _stream_with_books(path, frames_at):
    """frames_at: list of (frame, recv_ts). Adds a fresh trade so only
    book checks can fire."""
    store = StreamStore(path)
    for frame, ts in frames_at:
        store.append_events(parse_message(frame, ts)[0])
    frame_t = {
        "type": "trade",
        "sid": 9,
        "seq": 1,
        "msg": {
            "market_ticker": "M1",
            "yes_price_dollars": "0.4000",
            "count_fp": "5.00",
            "ts_ms": int(NOW.timestamp() * 1000),
        },
    }
    store.append_trades(parse_message(frame_t, NOW)[1])
    store.flush()
    return store


def test_negative_levels_seq_reset_across_epochs_passes(tmp_path):
    """Kalshi seq resets per re-subscription, so an early snapshot can
    carry the highest seq. Reconstruction must key on the time-latest
    snapshot, not max(seq) — the old check false-positived here."""
    t = [NOW - timedelta(minutes=m) for m in (40, 30, 20, 10)]
    _stream_with_books(
        tmp_path / "s.duckdb",
        [
            (_book_frame("snap", 306, "0.4000", "20.00"), t[0]),  # epoch A, high seq
            (_book_frame("delta", 500, "0.4000", "-20.00"), t[1]),  # empties level
            (_book_frame("snap", 280, "0.4000", "30.00"), t[2]),  # re-sub, low seq
            (_book_frame("delta", 400, "0.4000", "-30.00"), t[3]),  # empties level
        ],
    )
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "reconstructed book levels non-negative" not in failed


def test_negative_levels_genuine_overdraw_trips(tmp_path):
    t = [NOW - timedelta(minutes=m) for m in (20, 10)]
    _stream_with_books(
        tmp_path / "s.duckdb",
        [
            (_book_frame("snap", 1, "0.4000", "10.00"), t[0]),
            (_book_frame("delta", 2, "0.4000", "-15.00"), t[1]),  # -5 net
        ],
    )
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "reconstructed book levels non-negative" in failed


def test_negative_levels_forgiven_when_gap_intersects(tmp_path):
    """Deltas applied across a marked coverage gap can legitimately
    overdraw — the book is unknown until the next snapshot re-seeds."""
    t = [NOW - timedelta(minutes=m) for m in (20, 10)]
    store = _stream_with_books(
        tmp_path / "s.duckdb",
        [
            (_book_frame("snap", 1, "0.4000", "10.00"), t[0]),
            (_book_frame("delta", 2, "0.4000", "-15.00"), t[1]),
        ],
    )
    store.append_gap("kalshi", "books", t[0] + timedelta(minutes=2), t[1], "flush_failure")
    store.flush()
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "reconstructed book levels non-negative" not in failed


def test_healthy_archive_passes_and_unswept_tape_trips(tmp_path):
    from hyxlab.models import MarketInfo, Snapshot

    db = tmp_path / "a.duckdb"
    store = Store(db)
    store.insert_snapshots(
        [
            Snapshot(
                venue="kalshi",
                market_id="M1",
                ts=NOW,
                yes_bid=0.44,
                yes_ask=0.46,
                no_bid=0.54,
                no_ask=0.56,
                yes_bid_size=1,
                yes_ask_size=1,
                no_bid_size=1,
                no_ask_size=1,
            )
        ]
    )
    store.log_sweep("KXTEST", NOW, NOW, 1, 1, "ok")
    # settled + traded market 10 days old, inside retention, no tape sweep
    close = NOW - timedelta(days=10)
    store.upsert_markets(
        [MarketInfo(venue="kalshi", market_id="M1", result="yes", close_time=close)]
    )
    store.insert_candles(
        [("kalshi", "M1", close, 3600, None, None, None, 0.5, 0.49, 0.51, None, None, 10.0, 5.0)]
    )
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "trade tape covers retention window" in failed
    assert "collector fresh (snapshots < 20 min old)" not in failed
    # marking it swept clears the coverage failure
    store = Store(db)
    store.mark_trades_swept("M1", 3, "ok")
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "trade tape covers retention window" not in failed
