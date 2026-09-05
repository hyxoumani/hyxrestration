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
    frames = [
        (_book_frame("delta", s, "0.40", "1.00"), NOW - timedelta(minutes=m))
        for s, m in ((50, 40), (51, 39), (52, 38), (1, 20), (2, 19), (3, 18))
    ]
    _stream_with_books(tmp_path / "s.duckdb", frames)
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "book seq contiguous or gap-marked" not in failed


def test_hole_not_excused_by_non_overlapping_gap_row(tmp_path):
    """A gap row excuses only the run it overlaps. The old check passed on
    ANY gap row in the window, which made it vacuous in production (392 gap
    rows in a 26h window would excuse every hole in it)."""
    frames = [
        (_book_frame("delta", s, "0.40", "1.00"), NOW - timedelta(minutes=m))
        for s, m in ((1, 20), (2, 19), (9, 18))
    ]
    store = _stream_with_books(tmp_path / "s.duckdb", frames)
    stale = NOW - timedelta(hours=10)
    store.append_gap("kalshi", "books", stale, stale, "unrelated")
    store.flush()
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert "book seq contiguous or gap-marked" in failed


def test_hole_excused_by_overlapping_gap_row(tmp_path):
    """The mirror of the above: the tier must not kill everything — a gap
    row spanning the run's own interval still excuses its hole."""
    frames = [
        (_book_frame("delta", s, "0.40", "1.00"), NOW - timedelta(minutes=m))
        for s, m in ((1, 20), (2, 19), (9, 18))
    ]
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
    frames = [
        (_book_frame("delta", s, "0.40", "1.00"), NOW - timedelta(minutes=m))
        for s, m in ((1, 200), (2, 199), (9, 198), (10, 30))
    ]
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
    frames = [
        (_book_frame("delta", s, "0.40", "1.00"), NOW - timedelta(minutes=m))
        for s, m in ((1, 20), (2, 19), (9, 18))
    ]
    store = _stream_with_books(tmp_path / "s.duckdb", frames)
    store.append_gap(
        "polymarket",
        "market",
        NOW - timedelta(minutes=19),
        NOW - timedelta(minutes=18),
        "reconnect",
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


def test_unapplied_migration_is_reported(tmp_path):
    """promote.sh ships code but never migrates, and nothing asserts the
    version at open — so a pending migration must be visible in QA rather
    than silently changing what every read means."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    store.set_schema_version(0)
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "archive schema at current version" in failed


def test_migrated_archive_passes_the_schema_check(tmp_path):
    db = tmp_path / "a.duckdb"
    Store(db).close()  # fresh DBs are born current
    failed = _run(None, tmp_path, archive=db)
    assert "archive schema at current version" not in failed


def _poly_runs(store, markets_per_run):
    """markets_per_run: {hours_ago: distinct market count}. One sweep RUN
    per entry — every row in a run shares the run's start instant, which is
    how the archive stores it and how the tripwire regroups it. Seeded in
    poly_prices too, since the check is gated on poly having ever swept."""
    store.insert_poly_prices([("t0", "pm0", "yes", NOW - timedelta(hours=1), 0.5)])
    for hours_ago, count in markets_per_run.items():
        ts = NOW - timedelta(hours=hours_ago)
        store.insert_poly_stats([(f"pm{i}", ts, 0.0, 0.0) for i in range(count)])


def test_poly_universe_shrink_trips(tmp_path):
    """Regression class: 2026-07-08 Gamma offset cap halved the swept
    universe and no check noticed. A sharp drop vs the prior runs' peak
    must trip."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _poly_runs(store, {72: 700, 48: 720, 24: 100})  # last run collapsed
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "poly swept universe not shrinking" in failed


def test_poly_universe_steady_passes(tmp_path):
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _poly_runs(store, {72: 700, 48: 720, 24: 710})
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "poly swept universe not shrinking" not in failed


def test_poly_universe_quarter_drop_trips(tmp_path):
    """The old poly_prices-based check floored at 0.5 and could only see a
    halving. On the enumeration signal a 26% loss is far outside the
    observed 3.4% run-to-run spread and must trip."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _poly_runs(store, {72: 1000, 48: 1000, 24: 740})
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "poly swept universe not shrinking" in failed


def test_in_flight_sweep_is_not_read_as_a_collapse(tmp_path):
    """The walk takes up to ~15h, so at QA time the newest run is still
    enumerating. Reading its partial count as 'yesterday' is a false red —
    the failure mode that made the day-bucketed version unusable."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _poly_runs(store, {48: 700, 24: 710, 6: 90})  # 6h ago = mid-walk
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "poly swept universe not shrinking" not in failed


def test_single_sweep_run_does_not_trip(tmp_path):
    """Nothing to compare against on a fresh archive."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _poly_runs(store, {24: 700})
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "poly swept universe not shrinking" not in failed


def _aged_poly_state(tmp_path, hours):
    """Pretend the tripwire last actually MEASURED something `hours` ago, so
    the escalation clock is testable without waiting a day and a half."""
    (tmp_path / "sections.json").write_text(
        json.dumps(
            {
                qa._POLY_UNIVERSE_SECTION: {
                    "first_seen": (NOW - timedelta(hours=hours)).replace(tzinfo=None).isoformat(),
                    "last_ok": (NOW - timedelta(hours=hours)).replace(tzinfo=None).isoformat(),
                }
            }
        )
    )


def test_inert_stats_writer_is_not_silent(tmp_path, capsys):
    """EXP-1381, the defect this branch exists for: the guard reads a
    WINDOWED slice, so the stats half of the walk going inert empties `runs`
    and the tripwire simply stopped being printed — while `poly prices fresh`
    stayed green off the CLOB half of the same sweep."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    store.insert_poly_prices([("t0", "pm0", "yes", NOW - timedelta(hours=1), 0.5)])
    store.close()
    _aged_poly_state(tmp_path, qa.SKIP_MAX_AGE_H + 1)
    failed = _run(None, tmp_path, archive=db)
    assert "poly prices fresh (< 30h old)" not in failed  # the sweep looks alive
    assert "poly swept universe not shrinking" in failed
    assert "TRIPWIRE INERT" in capsys.readouterr().out


def test_collapse_below_the_ratio_floor_is_not_silent(tmp_path, capsys):
    """The sharper half: the floor exists to make a RATIO meaningful, so a
    universe that collapsed under it and stayed there ten days would consume
    the very check written to catch a collapse."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _poly_runs(store, {72: 400, 48: 410, 24: 30})
    store.close()
    _aged_poly_state(tmp_path, qa.SKIP_MAX_AGE_H + 1)
    failed = _run(None, tmp_path, archive=db)
    assert "poly swept universe not shrinking" in failed
    assert f"under the {qa.POLY_UNIVERSE_MIN_PRIOR}" in capsys.readouterr().out


def test_unevaluable_tripwire_is_a_bounded_skip_before_it_is_a_failure(tmp_path, capsys):
    """A fresh archive that has swept prices but settled no run yet is
    genuinely undecidable on sight — the qa_signals_fetch shape. It must SKIP
    and start a clock, not red on the first day of its own life."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    store.insert_poly_prices([("t0", "pm0", "yes", NOW - timedelta(hours=1), 0.5)])
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "poly swept universe not shrinking" not in failed
    out = capsys.readouterr().out
    assert "SKIP  poly swept universe not shrinking" in out
    assert "escalates to FAIL" in out
    assert qa._POLY_UNIVERSE_SECTION in qa._skipped


def test_a_stopped_sweep_is_reported_once_not_twice(tmp_path, capsys):
    """Prices stale means the sweep itself is down, which `poly prices fresh`
    already reds. The tripwire has nothing to add and must not add a second
    red line for one cause — the `candles`/`ok_sweeps` nesting rule."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    store.insert_poly_prices([("t0", "pm0", "yes", NOW - timedelta(hours=40), 0.5)])
    store.close()
    _aged_poly_state(tmp_path, qa.SKIP_MAX_AGE_H * 10)
    failed = _run(None, tmp_path, archive=db)
    assert "poly prices fresh (< 30h old)" in failed
    assert "poly swept universe not shrinking" not in failed
    assert "looks stopped too" in capsys.readouterr().out


def test_a_measuring_tripwire_records_its_own_completion(tmp_path):
    """The escalation clock must measure "last MEASURED", not "last seen" —
    conflating them is how a check that never evaluates borrows a date it
    never earned (the _last_ok rule)."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _poly_runs(store, {72: 700, 48: 720, 24: 710})
    store.close()
    _run(None, tmp_path, archive=db)
    assert qa._last_ok(qa._POLY_UNIVERSE_SECTION) is not None


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


def _tape_archive(db):
    """One settled+traded kalshi market, M1, unswept and inside retention."""
    from hyxlab.models import MarketInfo, Snapshot

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
    close = NOW - timedelta(days=10)
    store.upsert_markets(
        [MarketInfo(venue="kalshi", market_id="M1", result="yes", close_time=close)]
    )
    store.insert_candles(
        [("kalshi", "M1", close, 3600, None, None, None, 0.5, 0.49, 0.51, None, None, 10.0, 5.0)]
    )
    return store


def test_tape_draining_tail_is_watch_not_fail(tmp_path, capsys):
    """EXP-962: a fresh unswept market while sweeps are actively landing is a
    draining backfill, not rot — WATCH, non-failing."""
    db = tmp_path / "a.duckdb"
    store = _tape_archive(db)
    store.mark_trades_swept("OTHER", 5, "ok")  # sweeper landed rows just now
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "trade tape covers retention window" not in failed
    out = capsys.readouterr().out
    assert "WATCH trade tape covers retention window" in out
    assert "draining tail, not rot" in out


def test_tape_stuck_market_fails_despite_active_sweeper(tmp_path):
    """A market unswept past the drain grace while sweeps keep landing is
    stuck, not draining — the drain story expired."""
    db = tmp_path / "a.duckdb"
    store = _tape_archive(db)
    store.mark_trades_swept("OTHER", 5, "ok")
    store.close()
    first_seen = (NOW - timedelta(hours=qa.TAPE_DRAIN_GRACE_H + 10)).isoformat()
    qa.STATE.parent.mkdir(parents=True, exist_ok=True)
    qa.STATE.write_text(json.dumps({"tape-coverage": {"first_seen": {"M1": first_seen}}}))
    failed = _run(None, tmp_path, archive=db)
    assert "trade tape covers retention window" in failed


def test_tape_dead_sweeper_fails_even_inside_grace(tmp_path, capsys):
    """If nothing has landed in trades_swept for > TAPE_SWEEP_STALL_H, there
    is no evidence anything is draining — FAIL even on a young tail."""
    db = tmp_path / "a.duckdb"
    store = _tape_archive(db)
    stale = (NOW - timedelta(hours=qa.TAPE_SWEEP_STALL_H + 5)).replace(tzinfo=None)
    store.conn.execute("INSERT INTO trades_swept VALUES (?,?,?,?)", ["OTHER", stale, 5, "ok"])
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "trade tape covers retention window" in failed
    assert "not draining anything" in capsys.readouterr().out


def test_tape_first_seen_ledger_prunes_covered_markets(tmp_path):
    """Once a market is swept its first-seen entry must go, or a later
    re-appearance would inherit a stale clock and skip its grace."""
    db = tmp_path / "a.duckdb"
    store = _tape_archive(db)
    store.mark_trades_swept("OTHER", 5, "ok")
    store.close()
    _run(None, tmp_path, archive=db)
    assert "M1" in json.loads(qa.STATE.read_text())["tape-coverage"]["first_seen"]
    store = Store(db)
    store.mark_trades_swept("M1", 3, "ok")
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "trade tape covers retention window" not in failed
    assert json.loads(qa.STATE.read_text())["tape-coverage"]["first_seen"] == {}


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


# --- clock offset vs transport latency (2026-08-23) -------------------
# `recv_ts - src_ts` mixes a box-clock offset with transport latency. The
# retired check `trade latency p99 sane` bounded the sum, so a drifting
# clock pinned it red while a real stream stall would have been invisible
# underneath the offset. These drive the two replacement checks apart.

_LAT_DISP = "trade latency dispersion sane"
_LAT_OFF = "box clock offset within tolerance"


def _trades_with_offsets(path, offsets):
    """Seed one trade per entry in `offsets` (seconds of recv_ts - src_ts)."""
    store = StreamStore(path)
    for i, off in enumerate(offsets):
        src = NOW - timedelta(seconds=off)
        frame = {
            "type": "trade",
            "sid": 1,
            "seq": i + 1,
            "msg": {
                "market_ticker": "M1",
                "yes_price_dollars": "0.4000",
                "count_fp": "5.00",
                "taker_side": "yes",
                "ts_ms": int(src.timestamp() * 1000),
            },
        }
        store.append_trades(parse_message(frame, NOW)[1])
    store.flush()
    return store


def test_large_constant_offset_no_longer_trips_latency(tmp_path):
    """The production condition: a uniform +30s offset, past the retired
    check's 25s ceiling. Nothing about the STREAM is wrong, so neither
    replacement check may fire."""
    _trades_with_offsets(tmp_path / "s.duckdb", [30.0] * 50)
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert _LAT_DISP not in failed
    assert _LAT_OFF not in failed


def test_runaway_clock_offset_trips_only_the_offset_check(tmp_path):
    _trades_with_offsets(tmp_path / "s.duckdb", [90.0] * 50)
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert _LAT_OFF in failed
    assert _LAT_DISP not in failed  # a constant offset has zero dispersion


def test_slow_box_clock_trips_the_offset_check(tmp_path):
    """The lookahead-critical direction: a clock behind the venue stamps
    post-close snapshots as pre-close."""
    _trades_with_offsets(tmp_path / "s.duckdb", [-10.0] * 50)
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert _LAT_OFF in failed


def test_latency_tail_trips_dispersion_under_a_healthy_offset(tmp_path):
    """A real stream stall: the offset stays fine, the tail blows out. This
    is exactly what the retired check could not see once the offset had
    eaten its headroom."""
    _trades_with_offsets(tmp_path / "s.duckdb", [1.0] * 90 + [40.0] * 10)
    failed = _run(None, tmp_path, stream=tmp_path / "s.duckdb")
    assert _LAT_DISP in failed
    assert _LAT_OFF not in failed


# --- retrospective collection continuity (2026-08-24) -----------------
# `collector fresh (snapshots < 20 min old)` is instantaneous, so the
# 2026-08-20 4h19m box outage — which healed 8h before the next daily QA
# run — was structurally invisible to it. These drive the replacement.

_CONT = "collection continuous over last 24h"


def _cycles(db, ages_min):
    """Seed one collector cycle per entry, `ages_min` minutes before NOW."""
    from hyxlab.models import Snapshot

    store = Store(db)
    store.insert_snapshots(
        [
            Snapshot(
                venue="kalshi",
                market_id="M1",
                ts=NOW - timedelta(minutes=a),
                yes_bid=0.44,
                yes_ask=0.46,
                no_bid=0.54,
                no_ask=0.56,
                yes_bid_size=1,
                yes_ask_size=1,
                no_bid_size=1,
                no_ask_size=1,
            )
            for a in ages_min
        ]
    )
    store.close()


def test_steady_five_minute_cadence_passes_continuity(tmp_path):
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    assert _CONT not in _run(None, tmp_path, archive=db)


def test_healed_outage_trips_continuity(tmp_path):
    """The 08-20 shape: a multi-hour hole that is CLOSED by QA time, so the
    freshness check reads green and only a retrospective check can see it."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24)] + [5 * i for i in range(60, 100)])
    failed = _run(None, tmp_path, archive=db)
    assert _CONT in failed
    assert "collector fresh (snapshots < 20 min old)" not in failed


def test_single_skipped_cycle_stays_inside_budget(tmp_path):
    """p99.9 of 21 days is one dropped cycle. Alarming on it would make the
    check unreadable, which is how the retired latency check died."""
    db = tmp_path / "a.duckdb"
    ages = [5 * i for i in range(0, 24 * 12) if i != 40]
    _cycles(db, ages)
    assert _CONT not in _run(None, tmp_path, archive=db)


def test_outage_straddling_the_window_edge_is_not_lost(tmp_path):
    """An outage whose predecessor cycle sits OUTSIDE the 24h window. Without
    the pre-window anchor the first in-window cycle has no lag and the hole
    reads as if the day simply started late."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 12 * 20)] + [60 * 26])
    assert _CONT in _run(None, tmp_path, archive=db)


def test_one_cycle_is_unmeasured_not_healthy(tmp_path, capsys):
    db = tmp_path / "a.duckdb"
    _cycles(db, [0])
    failed = _run(None, tmp_path, archive=db)
    assert _CONT not in failed  # a single cycle cannot exhibit a gap
    assert f"WATCH {_CONT}" in capsys.readouterr().out


# --- EXP-1360: econ-vintage ingest, split into pull-liveness and per-series
# coverage. The retired `econ vintages fresh (< 8 days)` added a signal to a
# nuisance term and pooled seven cadences into one max, and on 2026-08-24 it
# printed "age -0.6d" and PASSED while four of seven series sat past its own
# 8-day budget.


def _vintages(store, per_series):
    """per_series: {series_id: [vintage_date, ...]}. Stamped exactly the way
    `alfred.pessimistic_knowable_at` does, so the tests exercise the real
    nuisance term and not a convenient one."""
    from collector.venues.alfred import pessimistic_knowable_at

    rows = []
    for sid, vds in per_series.items():
        for i, vd in enumerate(vds):
            rows.append(
                (sid, NOW.date() - timedelta(days=400 + i), float(i), pessimistic_knowable_at(vd))
            )
    store.conn.executemany("INSERT INTO econ_vintages VALUES (?, ?, ?, ?)", rows)


def test_pessimistic_stamp_cannot_make_a_stale_pull_read_fresh(tmp_path):
    """The stamp is vintage_date 23:59 ET, i.e. up to ~28h in the FUTURE, so
    `now - max(knowable_at)` is (staleness - pessimism) and goes negative on a
    perfectly current archive. Measuring the vintage DATE cancels it exactly:
    a pull that stopped 10 days ago must read 10 days, not 9."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _vintages(store, {"DFEDTARU": [NOW.date() - timedelta(days=10)]})
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "econ pull live (any series, last vintage date)" in failed


def test_todays_vintage_passes_without_a_negative_age(tmp_path):
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _vintages(store, {"DFEDTARU": [NOW.date()]})
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "econ pull live (any series, last vintage date)" not in failed


def test_pull_liveness_is_blind_to_a_dropped_series_by_construction(tmp_path):
    """Not a defect in `econ pull live` — its stated scope. The daily pair
    keeps the pooled max current while the monthly series are long dead, and
    that is exactly why per-series coverage cannot live in the archive."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    _vintages(
        store,
        {
            "DFEDTARU": [NOW.date()],
            "UNRATE": [NOW.date() - timedelta(days=200)],
        },
    )
    store.close()
    failed = _run(None, tmp_path, archive=db)
    assert "econ pull live (any series, last vintage date)" not in failed


def _fetch_log(tmp_path, runs):
    """runs: [(days_ago, {series: ok_bool})]"""
    p = tmp_path / "signals_fetch.jsonl"
    with p.open("w") as fh:
        for days_ago, outcomes in runs:
            at = NOW - timedelta(days=days_ago)
            fh.write(
                json.dumps(
                    {
                        "at": at.isoformat(),
                        "vintage_date": at.date().isoformat(),
                        "series": {
                            s: {
                                "ok": ok,
                                "rows": 5 if ok else 0,
                                "error": None if ok else "HTTPError",
                            }
                            for s, ok in outcomes.items()
                        },
                    }
                )
                + "\n"
            )
    return str(p)


def _run_fetch(pull_age_d, path, series):
    qa._failures.clear()
    qa.qa_signals_fetch(pull_age_d, path=path, series=series)
    failed = set(qa._failures)
    qa._failures.clear()
    return failed


NAME_FETCH = "econ series all fetched, not just the fast ones"
SERIES3 = ["DFEDTARU", "ICSA", "UNRATE"]


def test_dropped_monthly_series_trips_even_though_the_archive_looks_healthy(tmp_path):
    """The whole point. UNRATE has not been fetched successfully for a week
    while the daily series arrive every day — invisible to every archive-side
    rule, because econ_vintages only gains a row when a value CHANGES and a
    monthly series looks identical dead or alive for a month."""
    path = _fetch_log(
        tmp_path,
        [(d, {"DFEDTARU": True, "ICSA": True, "UNRATE": d > 6}) for d in range(7, -1, -1)],
    )
    assert NAME_FETCH in _run_fetch(0, path, SERIES3)


def test_all_series_fetched_passes(tmp_path):
    path = _fetch_log(tmp_path, [(d, dict.fromkeys(SERIES3, True)) for d in range(7, -1, -1)])
    assert NAME_FETCH not in _run_fetch(0, path, SERIES3)


def test_a_series_never_seen_in_the_record_is_stale_not_absent(tmp_path):
    """A series missing from every run is the ALFRED-dropped-the-id case;
    treating "no row" as "nothing to check" is how it would hide."""
    path = _fetch_log(tmp_path, [(0, {"DFEDTARU": True, "ICSA": True})])
    assert NAME_FETCH in _run_fetch(0, path, SERIES3)


def test_recent_success_survives_a_later_failed_attempt(tmp_path):
    """A single failed fetch inside the budget is a transient, not a drop —
    the check measures time since last SUCCESS, not the newest outcome."""
    path = _fetch_log(
        tmp_path,
        [(1, dict.fromkeys(SERIES3, True)), (0, {**dict.fromkeys(SERIES3, True), "UNRATE": False})],
    )
    assert NAME_FETCH not in _run_fetch(0, path, SERIES3)


def test_absent_sidecar_with_a_live_pull_is_a_bounded_skip_then_a_failure(tmp_path):
    """An alarm whose producer is dead is worse than no alarm (EXP-943) — but
    a never-written sidecar cannot be told from a just-shipped one on sight,
    so the escalation is on a clock rather than immediate."""
    absent = str(tmp_path / "nope.jsonl")
    assert NAME_FETCH not in _run_fetch(0, absent, SERIES3)  # first sighting: SKIP
    assert qa._skipped == ["signals-fetch"]
    qa._skipped.clear()
    # ... and once a pull cycle has provably had time to run, it is a failure
    state = json.loads(qa.STATE.read_text())
    state["signals-fetch"]["first_seen"] = (
        NOW.replace(tzinfo=None) - timedelta(hours=qa.SKIP_MAX_AGE_H + 1)
    ).isoformat()
    qa.STATE.write_text(json.dumps(state))
    assert NAME_FETCH in _run_fetch(0, absent, SERIES3)


def test_absent_sidecar_with_a_dead_pull_is_unverified_not_a_failure(tmp_path):
    """Neither witness saw anything, so nothing was measured. The pull's own
    outage is already reported by `econ pull live`; re-reporting it here
    would be two alarms for one fact."""
    absent = str(tmp_path / "nope.jsonl")
    assert NAME_FETCH not in _run_fetch(99, absent, SERIES3)
    assert qa._skipped == ["signals-fetch"]


def test_a_sidecar_that_produced_and_went_quiet_needs_no_grace(tmp_path):
    """Distinct from the absent case: this producer demonstrably ran once, so
    'it may not have shipped yet' is not available as an explanation."""
    path = _fetch_log(tmp_path, [(30, dict.fromkeys(SERIES3, True))])
    assert NAME_FETCH in _run_fetch(0, path, SERIES3)


def test_malformed_rows_are_counted_not_swallowed(tmp_path):
    path = _fetch_log(tmp_path, [(0, dict.fromkeys(SERIES3, True))])
    with open(path, "a") as fh:
        fh.write("{not json\n")
    assert NAME_FETCH not in _run_fetch(0, path, SERIES3)


def test_reported_age_is_the_true_day_gap_not_the_stamp_offset(tmp_path, capsys):
    """Pins the nuisance removal itself rather than a threshold margin: a pull
    that last landed 3 days ago must REPORT 3 days. Reading `knowable_at`
    directly reports ~1.8 — the ~28h pessimism margin subtracted from the
    staleness — which is the arithmetic that let the retired check print a
    negative age on a healthy archive and would let a 5-day outage read
    inside a 4-day budget."""
    db = tmp_path / "a.duckdb"
    store = Store(db)
    vd = NOW.date() - timedelta(days=3)
    _vintages(store, {"DFEDTARU": [vd]})
    store.close()
    _run(None, tmp_path, archive=db)
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "econ pull live" in ln)
    assert f"newest vintage date {vd:%Y-%m-%d}, 3d ago" in line


# --- breadth coverage (2026-09-04) ------------------------------------
# EXP-928 breadth has written this archive every 5 min since 2026-08-03 —
# 8.26M rows, the only exchange-wide quote history — and NO check has ever
# read it. These drive the coverage.

_BFRESH = "breadth fresh (snapshots < 20 min old)"
_BCONT = "breadth continuous over last 24h"


def _breadth_cycles(db, ages_min):
    """Seed one breadth cycle per entry, `ages_min` minutes before NOW. Two
    markets per cycle, because a cycle is a rank list, not a row."""
    from collector.breadth import breadth_row
    from hyxlab.models import Snapshot

    store = Store(db)
    store.insert_breadth_snapshots(
        [
            breadth_row(
                Snapshot(
                    venue="kalshi",
                    market_id=mid,
                    ts=NOW - timedelta(minutes=a),
                    yes_bid=0.44,
                    yes_ask=0.46,
                    no_bid=0.54,
                    no_ask=0.56,
                    yes_bid_size=1,
                    yes_ask_size=1,
                    no_bid_size=1,
                    no_ask_size=1,
                ),
                1000.0,
                rank,
            )
            for a in ages_min
            for rank, mid in enumerate(("B1", "B2"), start=1)
        ]
    )
    store.close()


def test_never_enabled_breadth_is_not_a_defect(tmp_path, capsys):
    """Breadth is DEFAULT DISABLED and installing its timer is the enabling
    act. An archive that never ran it must not be told it is broken — and
    must not be told it is fine either, so neither check may appear."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    failed = _run(None, tmp_path, archive=db)
    out = capsys.readouterr().out
    assert _BFRESH not in failed and _BCONT not in failed
    assert _BFRESH not in out and _BCONT not in out


def test_steady_breadth_cadence_passes(tmp_path):
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _breadth_cycles(db, [5 * i for i in range(0, 24 * 12)])
    failed = _run(None, tmp_path, archive=db)
    assert _BFRESH not in failed and _BCONT not in failed


def test_dead_breadth_trips_freshness(tmp_path):
    """The timer stops. The collector keeps running, so every other check
    reads green and only breadth's own freshness can say so."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _breadth_cycles(db, [90 + 5 * i for i in range(0, 24 * 12)])
    failed = _run(None, tmp_path, archive=db)
    assert _BFRESH in failed
    assert "collector fresh (snapshots < 20 min old)" not in failed


def test_healed_breadth_outage_trips_continuity_only(tmp_path):
    """The 08-20 shape applied to breadth: a multi-hour hole CLOSED by QA
    time. Freshness reads green; the retrospective check is the only witness."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _breadth_cycles(db, [5 * i for i in range(0, 24)] + [5 * i for i in range(60, 100)])
    failed = _run(None, tmp_path, archive=db)
    assert _BCONT in failed
    assert _BFRESH not in failed


def test_single_skipped_breadth_cycle_stays_inside_budget(tmp_path):
    """Breadth's measured p99 is 314.7s and its benign worst gap 20.0 min.
    Alarming on one dropped cycle would make the check unreadable."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _breadth_cycles(db, [5 * i for i in range(0, 24 * 12) if i != 40])
    failed = _run(None, tmp_path, archive=db)
    assert _BCONT not in failed and _BFRESH not in failed


def test_breadth_outage_straddling_the_window_edge_is_not_lost(tmp_path):
    """The shared anchor, exercised through breadth: the hole's PREDECESSOR
    cycle must sit outside the 24h window (26h back), or the gap is wholly
    in-window and an unanchored query finds it anyway — which is what this
    test did until 2026-09-04, asserting nothing about the anchor."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _breadth_cycles(db, [5 * i for i in range(0, 12 * 20)] + [60 * 26])
    assert _BCONT in _run(None, tmp_path, archive=db)


# --- nws forecast coverage (2026-09-04) --------------------------------
# The SECOND unwatched live writer, and the one the derived coverage test
# (tests/test_qa_table_coverage.py) found rather than a human: 580,270 rows
# written in the same 5-min cycle as `snapshots`, never named in qa.py.

_NFRESH = "nws forecasts fresh (< 20 min old)"
_NCONT = "nws forecasts continuous over last 24h"


def _nws_cycles(db, ages_min):
    """Seed one NWS pull per entry, `ages_min` minutes before NOW. Several
    stations per pull, each with its own `fetched_at` milliseconds apart —
    which is what the archive really holds (measured: the raw p50 gap between
    distinct `fetched_at` values is 0.03s, the within-cycle station walk)."""
    from datetime import date

    from hyxlab.models import Forecast

    store = Store(db)
    store.insert_forecasts(
        [
            Forecast(
                station=st,
                fetched_at=NOW - timedelta(minutes=a) + timedelta(milliseconds=100 * i),
                target_date=date(2026, 9, 5),
                high_f=70 + i,
            )
            for a in ages_min
            for i, st in enumerate(("NYC", "CHI", "MIA"))
        ]
    )
    store.close()


def test_never_pulled_nws_is_not_a_defect(tmp_path, capsys):
    """The station list is watchlist-driven. A deployment with no
    `nws_stations` must be told neither that it is broken nor that it is
    fine — a green line about a pull that does not exist is the same lie."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    failed = _run(None, tmp_path, archive=db)
    out = capsys.readouterr().out
    assert _NFRESH not in failed and _NCONT not in failed
    assert _NFRESH not in out and _NCONT not in out


def test_steady_nws_cadence_passes(tmp_path):
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _nws_cycles(db, [5 * i for i in range(0, 24 * 12)])
    failed = _run(None, tmp_path, archive=db)
    assert _NFRESH not in failed and _NCONT not in failed


def test_dead_nws_pull_trips_freshness_while_the_collector_reads_green(tmp_path):
    """THE FAILURE THIS EXISTS FOR. The NWS pull sits inside the collect
    cycle under a per-station try/except, so an NWS outage or a station
    rename drops forecasts while snapshots keep flowing. Every other archive
    check passes; only the pull's own freshness can say so."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _nws_cycles(db, [90 + 5 * i for i in range(0, 24 * 12)])
    failed = _run(None, tmp_path, archive=db)
    assert _NFRESH in failed
    assert "collector fresh (snapshots < 20 min old)" not in failed


def test_healed_nws_outage_trips_continuity_only(tmp_path):
    """The 08-20 shape applied to the pull: a multi-hour hole CLOSED before
    QA looked. Freshness reads green; the retrospective check is the only
    witness, and the forecasts in the hole are unrecoverable."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _nws_cycles(db, [5 * i for i in range(0, 24)] + [5 * i for i in range(60, 100)])
    failed = _run(None, tmp_path, archive=db)
    assert _NCONT in failed
    assert _NFRESH not in failed


def test_within_cycle_station_walk_is_not_a_gap(tmp_path):
    """`fetched_at` is stamped per STATION, not per cycle, so the table holds
    sub-second gaps by construction. The check must read the cycle cadence
    through them — measured p99 over 32 days is 300.0s once bucketed."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _nws_cycles(db, [5 * i for i in range(0, 24 * 12)])
    failed = _run(None, tmp_path, archive=db)
    assert _NCONT not in failed


def test_single_skipped_nws_pull_stays_inside_budget(tmp_path):
    """Measured benign worst gap is 25.0 min against a 60 min budget.
    Alarming on one dropped pull would make the check unreadable."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _nws_cycles(db, [5 * i for i in range(0, 24 * 12) if i != 40])
    failed = _run(None, tmp_path, archive=db)
    assert _NCONT not in failed and _NFRESH not in failed


def test_nws_outage_straddling_the_window_edge_is_not_lost(tmp_path):
    """The shared anchor, exercised through a THIRD writer and a non-`ts`
    column: the hole's predecessor pull sits outside the 24h window (26h
    back), so an unanchored query gives the first in-window pull no lag and
    the hole reads as if the day simply started late."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _nws_cycles(db, [5 * i for i in range(0, 12 * 20)] + [60 * 26])
    assert _NCONT in _run(None, tmp_path, archive=db)


# --- candle ingest (2026-09-04) -----------------------------------------
# The only live-written archive table with NO ingest stamp: end_ts is the
# candle's period end and the sweep walks settled history, so max(end_ts)
# moves backwards on a healthy run. Found by the derived staleness coverage
# (tests/test_qa_staleness_coverage.py), which asks per table whether anyone
# would notice it stopping — `candles` was the only one with no answer.

_CANDLES = "candle ingest landing (36h)"


def _sweeps(db, entries):
    """Seed sweep_log rows: (hours_ago, n_candles, status).

    Inserted directly, not via `log_sweep`, which stamps `swept_at` with
    now() — the window is exactly what these tests need to place rows in.
    """
    store = Store(db)
    for h, n, status in entries:
        t = (NOW - timedelta(hours=h)).replace(tzinfo=None)
        store.conn.execute(
            "INSERT INTO sweep_log VALUES ('KXTEST', ?, ?, ?, 1, ?, ?, '')", [t, t, t, n, status]
        )
    store.close()


def test_landing_candles_pass(tmp_path):
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _sweeps(db, [(h, 5000, "ok") for h in (2, 14, 26)])
    failed = _run(None, tmp_path, archive=db)
    assert _CANDLES not in failed and "sweep ran in last 36h" not in failed


def test_sweeps_landing_zero_candles_trip_while_the_sweep_reads_green(tmp_path):
    """THE FAILURE THIS EXISTS FOR. sweep.py logs status='ok' on the MARKET
    count, so a candlestick endpoint returning an empty list under HTTP 200
    keeps filling sweep_log with ok entries while the archive stops gaining
    candles. 'sweep ran in last 36h' passes; only this can say so."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _sweeps(db, [(h, 0, "ok") for h in (2, 14, 26)])
    failed = _run(None, tmp_path, archive=db)
    assert _CANDLES in failed
    assert "sweep ran in last 36h" not in failed


def test_candle_ingest_that_stopped_36h_ago_trips(tmp_path):
    """The window is what makes it a check: candles landed, but outside it."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _sweeps(db, [(40, 500_000, "ok"), (2, 0, "ok")])
    assert _CANDLES in _run(None, tmp_path, archive=db)


def test_no_sweep_in_window_reports_the_sweep_once_not_twice(tmp_path, capsys):
    """Nested under ok_sweeps on purpose: with no sweep there is no candle to
    expect, and the cause is already named. Two red lines for one cause is
    noise — assert against stdout, not just the failure list."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _sweeps(db, [(40, 500_000, "ok")])
    failed = _run(None, tmp_path, archive=db)
    out = capsys.readouterr().out
    assert "sweep ran in last 36h" in failed
    assert _CANDLES not in failed and _CANDLES not in out


def test_a_truncated_sweeps_candles_still_count(tmp_path):
    """status='truncated' means the market budget was reached, not that the
    candles it did fetch are absent — the question here is whether ingest is
    alive, and those rows are in the archive."""
    db = tmp_path / "a.duckdb"
    _cycles(db, [5 * i for i in range(0, 24 * 12)])
    _sweeps(db, [(2, 1, "ok"), (3, 9000, "truncated")])
    assert _CANDLES not in _run(None, tmp_path, archive=db)
