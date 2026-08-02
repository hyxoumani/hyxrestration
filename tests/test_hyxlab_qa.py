"""Daily QA checks against synthetic archives: healthy DBs pass, each
seeded defect trips its check."""

import json
from datetime import UTC, datetime, timedelta

import pytest

import collector.qa as qa
from collector.venues.kalshi_ws import parse_message
from hyxlab.store import Store
from hyxlab.streamstore import StreamStore

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Every test gets its own section-completion record. Without this the
    suite would read and WRITE the deployment's real reports/qa/sections.json,
    which decides whether production QA alarms."""
    monkeypatch.setattr(qa, "STATE", tmp_path / "sections.json")
    qa._skipped.clear()
    qa._passes = 0
    yield
    qa._skipped.clear()


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


def _void_frame(typ, seq, sid=1):
    return {"type": typ, "sid": sid, "seq": seq, "msg": {"market_ticker": "M1"}}


def test_unknown_frame_type_is_invisible_to_the_seq_check_but_trips_void_check(tmp_path):
    """THE load-bearing one. A Kalshi frame type this parser does not
    recognise archives no book level. Since void rows were introduced it
    also CLOSES the seq hole it used to leave — so the seq check, which
    caught exactly this before, now reads green. Assert both halves: the
    seq check must be silent (proving it is blind, not merely redundant)
    and the void check must fire naming the type."""
    frames = [
        (_book_frame("delta", 1, "0.40", "1.00"), NOW - timedelta(minutes=30)),
        (_void_frame("orderbook_snapshot_v2", 2), NOW - timedelta(minutes=29)),
        (_book_frame("delta", 3, "0.41", "1.00"), NOW - timedelta(minutes=28)),
    ]
    _stream_with_books(tmp_path / "s.duckdb", frames)
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "book seq contiguous or gap-marked" not in failed
    assert "void frames are known types" in failed


def test_empty_snapshot_void_is_benign(tmp_path):
    """Discrimination control: the void producer that actually occurs in
    production (a snapshot whose ladders are empty — a market with no
    resting book) must NOT trip the check, or it is merely always-red."""
    empty_snap = {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": 2,
        "msg": {"market_ticker": "M1", "yes_dollars_fp": []},
    }
    frames = [
        (_book_frame("delta", 1, "0.40", "1.00"), NOW - timedelta(minutes=30)),
        (empty_snap, NOW - timedelta(minutes=29)),
        (_book_frame("delta", 3, "0.41", "1.00"), NOW - timedelta(minutes=28)),
    ]
    _stream_with_books(tmp_path / "s.duckdb", frames)
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "void frames are known types" not in failed


def test_legacy_void_row_without_frame_type_cannot_trip_the_check(tmp_path):
    """Void rows written between 2026-07-30 08:26 and the frame-type commit
    carry '' in `side`. They are unattributable, not unknown — counting them
    as unknown would red the check for a day on rows that predate the
    field."""
    store = _stream_with_books(
        tmp_path / "s.duckdb",
        [(_book_frame("delta", 1, "0.40", "1.00"), NOW - timedelta(minutes=30))],
    )
    ev = parse_message(_void_frame("something_new", 2), NOW - timedelta(minutes=29))[0]
    ev[0].side = ""  # legacy shape
    store.append_events(ev)
    store.flush()
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "void frames are known types" not in failed


def test_void_row_records_the_frame_type(tmp_path):
    """Field-level: the type must reach `side`, else the check above can
    only ever see ''."""
    events = parse_message(_void_frame("orderbook_snapshot_v2", 7), NOW)[0]
    assert [(e.kind, e.side, e.seq) for e in events] == [("void", "orderbook_snapshot_v2", 7)]


def test_reconnect_seq_restart_is_not_a_hole(tmp_path):
    """seq is connection-scoped and restarts at 1 on reconnect, while Kalshi
    reuses sid=1 for each new connection. Grouping by sid alone welds the
    runs into one min..max range: a window-truncated run at seq 50..52
    followed by a fresh run at 1..3 reads as 47 phantom holes. Both runs are
    internally contiguous, so the check must stay silent."""
    frames = [(_book_frame("delta", s, "0.40", "1.00"), NOW - timedelta(minutes=m)) for s, m in
              ((50, 40), (51, 39), (52, 38), (1, 20), (2, 19), (3, 18))]
    _stream_with_books(tmp_path / "s.duckdb", frames)
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "book seq contiguous or gap-marked" not in failed


def test_hole_not_excused_by_non_overlapping_gap_row(tmp_path):
    """A gap row excuses only the run it overlaps. The old check passed on
    ANY gap row in the window, which made it vacuous in production (392 gap
    rows in a 26h window would excuse every hole in it)."""
    frames = [(_book_frame("delta", s, "0.40", "1.00"), NOW - timedelta(minutes=m)) for s, m in
              ((1, 20), (2, 19), (9, 18))]
    store = _stream_with_books(tmp_path / "s.duckdb", frames)
    stale = NOW - timedelta(hours=10)
    store.append_gap("kalshi", "books", stale, stale, "unrelated")
    store.flush()
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "book seq contiguous or gap-marked" in failed


def test_hole_excused_by_overlapping_gap_row(tmp_path):
    """The mirror of the above: the tier must not kill everything — a gap
    row spanning the run's own interval still excuses its hole."""
    frames = [(_book_frame("delta", s, "0.40", "1.00"), NOW - timedelta(minutes=m)) for s, m in
              ((1, 20), (2, 19), (9, 18))]
    store = _stream_with_books(tmp_path / "s.duckdb", frames)
    store.append_gap(
        "kalshi", "books", NOW - timedelta(minutes=19), NOW - timedelta(minutes=18), "seq_gap"
    )
    store.flush()
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "book seq contiguous or gap-marked" not in failed


def test_hole_not_excused_by_run_terminating_gap_row(tmp_path):
    """Excusal is scoped to the HOLE, not to the run that contains it. Every
    completed run ends in a logged reconnect whose gap row touches the run's
    endpoint — under run-scoped excusal that pardoned every hole in the run
    however far away (measured 2026-07-29: 22 holes at 17:55 pardoned by a
    21:29 reconnect), leaving only the still-open run able to fail."""
    frames = [(_book_frame("delta", s, "0.40", "1.00"), NOW - timedelta(minutes=m)) for s, m in
              ((1, 200), (2, 199), (9, 198), (10, 30))]
    store = _stream_with_books(tmp_path / "s.duckdb", frames)
    # the run's terminating reconnect, ~3h after the hole at minute 198
    store.append_gap(
        "kalshi", "books", NOW - timedelta(minutes=30), NOW - timedelta(minutes=29), "reconnect"
    )
    store.flush()
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "book seq contiguous or gap-marked" in failed


def test_hole_not_excused_by_another_channels_gap_row(tmp_path):
    """A polymarket or kalshi-trades reconnect says nothing about the kalshi
    books connection these runs belong to. Production carried 3 such foreign
    gap rows out of 13 in the 2026-07-30 window."""
    frames = [(_book_frame("delta", s, "0.40", "1.00"), NOW - timedelta(minutes=m)) for s, m in
              ((1, 20), (2, 19), (9, 18))]
    store = _stream_with_books(tmp_path / "s.duckdb", frames)
    store.append_gap(
        "polymarket", "market", NOW - timedelta(minutes=19), NOW - timedelta(minutes=18), "reconnect"
    )
    store.flush()
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "book seq contiguous or gap-marked" in failed


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


def _poly_days(store, markets_per_day):
    """markets_per_day: {days_ago: distinct market count}. Seeds at NOON
    of each target calendar day — an hour-offset from now drifts into
    the wrong day bucket right after UTC midnight (flaked 2026-07-12)."""
    rows = []
    for days_ago, count in markets_per_day.items():
        day = (NOW - timedelta(days=days_ago)).date()
        ts = datetime(day.year, day.month, day.day, 12, 0)
        rows += [(f"t{i}", f"pm{i}", "yes", ts, 0.5) for i in range(count)]
    store.insert_poly_prices(rows)


def test_poly_universe_shrink_trips(tmp_path):
    """Regression class: 2026-07-08 Gamma offset cap halved the swept
    universe and no check noticed. A sharp drop vs the prior week's
    peak must trip."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _poly_days(store, {3: 700, 2: 720, 1: 100})  # yesterday collapsed
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "poly swept universe not shrinking" in failed


def test_poly_universe_steady_passes(tmp_path):
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _poly_days(store, {3: 700, 2: 720, 1: 710})
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "poly swept universe not shrinking" not in failed


def test_archive_locked_by_live_writer_is_not_a_failure(tmp_path, monkeypatch):
    """The poly sweep holds the write lock for hours; QA colliding with
    it must skip, not alarm — alarm fatigue buries real failures."""
    import subprocess
    import sys
    import time as _time

    monkeypatch.setattr(qa.time, "sleep", lambda s: None)  # fast retries

    db = tmp_path / "a.duckdb"
    Store(db).close()  # create the file
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import duckdb,sys,time; c=duckdb.connect({str(db)!r}); print('locked',flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        _time.sleep(0.2)
        qa._failures.clear()
        qa.qa_archive(26.0, path=str(db))
        failed = set(qa._failures)
        qa._failures.clear()
        assert "main archive reachable" not in failed
    finally:
        holder.kill()
        holder.wait()


def _lock(db):
    """Hold the DB's write lock in a live subprocess, as the collector does."""
    import subprocess
    import sys

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            # bind the connection: an unbound one is GC'd and releases the lock
            f"import duckdb,time; c=duckdb.connect({str(db)!r}); print('locked',flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "locked"
    return holder


def test_locked_section_reads_skip_not_pass_and_summary_is_not_all_pass(
    tmp_path, monkeypatch, capsys
):
    """THE load-bearing one. A lock-held section printed `PASS  main archive
    reachable — skipped:` and main() then printed `all checks pass` with exit
    0 — so 8 of 16 checks never ran and every journal line read green (10 of
    14 runs, Jul 20 – Aug 02 2026). Assert the NUMBERS: exactly one line for
    the skipped section's reachability, it is SKIP not PASS, none of the
    archive checks ran, and the summary names the skip."""
    monkeypatch.setattr(qa.time, "sleep", lambda s: None)
    db = tmp_path / "a.duckdb"
    Store(db).close()
    holder = _lock(db)
    try:
        qa._failures.clear()
        qa.qa_archive(26.0, path=str(db))
        out = capsys.readouterr().out
    finally:
        holder.kill()
        holder.wait()

    assert "SKIP  main archive reachable" in out
    assert "PASS  main archive reachable" not in out
    # not a failure — a lock is not a data defect
    assert "main archive reachable" not in set(qa._failures)
    # and none of the archive's own checks ran
    for name in ("collector fresh", "kalshi mirror invariant", "trade tape covers"):
        assert name not in out
    assert qa._skipped == ["archive"]


def test_stale_skip_escalates_to_a_failure(tmp_path, monkeypatch):
    """A skip is tolerable once, not indefinitely. Past SKIP_MAX_AGE_H
    without the section ever completing, the silent-rot watch is genuinely
    off and that IS a failure."""
    monkeypatch.setattr(qa.time, "sleep", lambda s: None)
    db = tmp_path / "a.duckdb"
    Store(db).close()
    now = datetime.now(UTC).replace(tzinfo=None)
    stale = (now - timedelta(hours=40)).isoformat()
    qa.STATE.parent.mkdir(parents=True, exist_ok=True)
    qa.STATE.write_text(json.dumps({"archive": {"last_ok": stale, "first_seen": stale}}))

    holder = _lock(db)
    try:
        qa._failures.clear()
        qa.qa_archive(26.0, path=str(db))
        failed = set(qa._failures)
    finally:
        holder.kill()
        holder.wait()
    assert "archive checks completed within 36h" in failed


def test_recent_completion_keeps_a_skip_quiet(tmp_path, monkeypatch):
    """Discrimination control for the above: a section that completed hours
    ago must stay silent, or the escalation is merely always-red and the
    alarm fatigue this whole path exists to avoid comes straight back."""
    monkeypatch.setattr(qa.time, "sleep", lambda s: None)
    db = tmp_path / "a.duckdb"
    Store(db).close()
    now = datetime.now(UTC).replace(tzinfo=None)
    fresh = (now - timedelta(hours=5)).isoformat()
    qa.STATE.parent.mkdir(parents=True, exist_ok=True)
    qa.STATE.write_text(json.dumps({"archive": {"last_ok": fresh, "first_seen": fresh}}))

    holder = _lock(db)
    try:
        qa._failures.clear()
        qa.qa_archive(26.0, path=str(db))
        failed = set(qa._failures)
    finally:
        holder.kill()
        holder.wait()
    assert failed == set()


def test_first_skip_starts_the_clock_so_a_never_run_section_can_go_stale(tmp_path, monkeypatch):
    """A section locked from the very first QA run has no completion to
    measure staleness against. Failing immediately would false-alarm every
    fresh deployment; recording nothing would leave it green forever. The
    first SKIP records `first_seen`, so the clock starts either way."""
    monkeypatch.setattr(qa.time, "sleep", lambda s: None)
    db = tmp_path / "a.duckdb"
    Store(db).close()
    holder = _lock(db)
    try:
        qa._failures.clear()
        qa.qa_archive(26.0, path=str(db))
        assert set(qa._failures) == set()  # fresh deployment must not alarm
        state = json.loads(qa.STATE.read_text())
        assert "first_seen" in state["archive"] and "last_ok" not in state["archive"]

        # rewind the clock past the tolerance: the same never-completed
        # section must now fail
        old = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=40)).isoformat()
        qa.STATE.write_text(json.dumps({"archive": {"first_seen": old}}))
        qa._failures.clear()
        qa.qa_archive(26.0, path=str(db))
        assert "archive checks completed within 36h" in set(qa._failures)
    finally:
        holder.kill()
        holder.wait()


def test_completed_section_records_its_completion(tmp_path):
    """The staleness bound is only as good as the record feeding it."""
    db = tmp_path / "a.duckdb"
    Store(db).close()
    qa._failures.clear()
    qa.qa_archive(26.0, path=str(db))
    qa._failures.clear()
    assert "last_ok" in json.loads(qa.STATE.read_text())["archive"]


def test_retry_budget_outlasts_the_collector_lock_cycle(monkeypatch):
    """The race, in isolation. `hyxlab-collect` (*:0/5) and `hyxlab-qa`
    (07:00:00 UTC) start in the same second and the collector holds the write
    lock ~11s; the old 5-attempt x 2s budget gave up ~1s early. Simulate a
    holder that releases after 8 attempts — more than the old budget allowed
    — and require the connect to succeed."""
    import duckdb as _duckdb

    calls = {"n": 0}
    sentinel = object()

    def fake_connect(path, read_only=False):
        calls["n"] += 1
        if calls["n"] <= 8:
            raise _duckdb.Error("Conflicting lock is held in /usr/bin/python3.14 (PID 1)")
        return sentinel

    monkeypatch.setattr(qa.time, "sleep", lambda s: None)
    monkeypatch.setattr(qa.duckdb, "connect", fake_connect)
    assert qa._connect_ro("x.duckdb") is sentinel
    # and the old budget provably would NOT have gotten there
    calls["n"] = 0
    assert qa._connect_ro("x.duckdb", wait_s=10.0) is None


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


def test_stale_gdelt_news_trips_freshness(tmp_path):
    from hyxlab.models import NewsItem

    db = tmp_path / "a.duckdb"
    store = Store(db)
    store.insert_news(
        [
            NewsItem(
                source="gdelt",
                url_hash="old1",
                published_at=None,
                knowable_at=NOW - timedelta(hours=40),
                topics="inflation",
            )
        ]
    )
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "gdelt news fresh (< 30h)" in failed
