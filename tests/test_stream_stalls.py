"""How long does streamd hold the tape in RAM, and did it ever spill?

Until 2026-09-11 nothing could answer that. A failed flush printed one
journal line and nothing else: no end time (the SUCCESS that ends an
episode was never logged), no aggregate, and gone at the host's journal
retention. The backlog was the daemon's one measured-by-nobody quantity,
and it was on the status page as "a measurement to design" for four
passes running.

THE BASELINE THAT MOTIVATES THE SHAPE, read off 7 days of journal
(2026-09-04..11, 101 episodes, durations interpolated between the first
and last failure line because the journal cannot do better):

    76 episodes   a single 15 s flush
    median        15 s
    tail          1756 s, 1801 s, 1876 s
    SPILL         TWO of those three reached `StreamStore.SPILL_CAP` and
                  moved 34,691 and 286 rows to the JSONL sidecar

So the harmful end is twice a week, not hypothetical -- and the cause is
a long-lived READ-ONLY reader (`simulator.shadow` is named as the lock
holder in both), because a duckdb read-only handle takes a shared lock
the writer cannot get past.

Three groups: the producer (`streamd.FlushStalls`, the only scope that
sees both ends of an episode), the same producer driven through the real
`Daemon.flusher`, and the QA reader -- including its inert-producer arm,
since a ledger nobody witnesses is the failure mode this repo keeps
rediscovering (#43, #46, and #53 one layer up).
"""

from __future__ import annotations

import builtins
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from collector import qa, streamd

T0 = datetime(2026, 9, 10, 20, 20, 22, tzinfo=UTC)


def _err(
    msg: str = "IO Error: Could not set lock on file\nsee also https://duckdb.org",
) -> Exception:
    return OSError(msg)


def _records(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# --------------------------------------------------------------------------
# the producer
# --------------------------------------------------------------------------


def test_one_failed_flush_writes_exactly_one_closed_episode(tmp_path):
    """The 75% case: a blip. One record, exact duration, no heartbeat."""
    log = tmp_path / "stalls.jsonl"
    s = streamd.FlushStalls(str(log))
    s.failed(3458, 0, _err(), T0)
    s.ok(T0 + timedelta(seconds=15))

    (rec,) = _records(log)
    assert rec["state"] == "closed"
    assert rec["duration_s"] == 15.0
    assert rec["fails"] == 1
    assert rec["peak_pending"] == 3458
    assert rec["spilled"] == 0


def test_a_short_stall_writes_no_interim_record(tmp_path):
    """The quiet case stays quiet: nothing is written until it ends."""
    log = tmp_path / "stalls.jsonl"
    s = streamd.FlushStalls(str(log))
    for i in range(int(streamd.STALL_HEARTBEAT_S // streamd.FLUSH_SECS)):
        s.failed(100 * i, 0, _err(), T0 + timedelta(seconds=15 * i))
    assert not log.exists()


def test_a_long_stall_heartbeats_then_closes(tmp_path):
    """A 30-minute episode is on disk BEFORE it ends, because a daemon that
    dies mid-stall must not take the measurement with it."""
    log = tmp_path / "stalls.jsonl"
    s = streamd.FlushStalls(str(log))
    t = T0
    for i in range(120):  # 1800 s at 15 s per flush
        t = T0 + timedelta(seconds=15 * i)
        s.failed(3000 * i, 0, _err(), t)
    opens = _records(log)
    assert opens and all(r["state"] == "open" for r in opens)
    # One per heartbeat interval, not one per failed flush.
    assert len(opens) == 5, opens
    assert opens[0]["duration_s"] == pytest.approx(streamd.STALL_HEARTBEAT_S, abs=15)

    s.ok(t + timedelta(seconds=15))
    closed = _records(log)[-1]
    assert closed["state"] == "closed"
    assert closed["duration_s"] == 1800.0
    assert closed["started"] == opens[0]["started"]  # one episode, six records


def test_an_unclosed_episode_is_a_lower_bound_not_a_live_claim(tmp_path):
    """A daemon killed mid-stall leaves `open` records and no `closed` one.
    The reader must still be able to say how long it had run -- and must not
    read `open` as "running now", which would report a stall that ended
    weeks ago as live."""
    log = tmp_path / "stalls.jsonl"
    s = streamd.FlushStalls(str(log))
    for i in range(60):
        s.failed(1000, 0, _err(), T0 + timedelta(seconds=15 * i))
    # Reader is given a `now` a week later; the episode is still reported,
    # with the duration it had reached, and nothing about it is "ongoing".
    eps, _ = qa.read_stall_episodes(str(log), 24.0, T0 + timedelta(seconds=900))
    (ep,) = eps
    assert ep["state"] == "open"
    assert ep["duration_s"] >= streamd.STALL_HEARTBEAT_S


def test_the_spill_is_carried_and_reset_with_the_episode(tmp_path):
    """`spilled` is the verdict input, so it must belong to ONE episode: a
    spill in Monday's stall must not condemn Tuesday's."""
    log = tmp_path / "stalls.jsonl"
    s = streamd.FlushStalls(str(log))
    s.failed(400_003, 286, _err(), T0)
    s.failed(400_003, 12, _err(), T0 + timedelta(seconds=15))  # a LOWER report
    s.ok(T0 + timedelta(seconds=30))
    s.failed(500, 0, _err(), T0 + timedelta(hours=1))
    s.ok(T0 + timedelta(hours=1, seconds=15))

    first, second = _records(log)
    assert first["spilled"] == 286  # the peak, not the last sample
    assert second["spilled"] == 0
    assert second["peak_pending"] == 500


def test_a_success_with_no_open_episode_writes_nothing(tmp_path):
    """The steady state is a successful flush every 15 s, forever."""
    log = tmp_path / "stalls.jsonl"
    s = streamd.FlushStalls(str(log))
    for i in range(10):
        s.ok(T0 + timedelta(seconds=15 * i))
    assert not log.exists()


def test_the_path_is_resolved_at_write_time(monkeypatch, tmp_path):
    """The default-arg trap `acquire_writer_lock` documents: a constant bound
    at construction ignores the patched value, and here that means writing
    into PRODUCTION's `data/` from the suite."""
    s = streamd.FlushStalls()
    monkeypatch.setattr(streamd, "STALL_LOG", str(tmp_path / "late" / "stalls.jsonl"))
    s.failed(1, 0, _err(), T0)
    s.ok(T0 + timedelta(seconds=15))
    assert _records(tmp_path / "late" / "stalls.jsonl")


def test_an_unwritable_ledger_never_takes_down_the_daemon(tmp_path, capsys):
    """The daemon's one job is not losing what it saw. Observability failing
    must not cost it that."""
    blocked = tmp_path / "file"
    blocked.write_text("")
    s = streamd.FlushStalls(str(blocked / "stalls.jsonl"))  # parent is a file
    s.failed(1, 0, _err(), T0)
    s.ok(T0 + timedelta(seconds=15))  # must not raise
    assert "unwritable" in capsys.readouterr().out


# --------------------------------------------------------------------------
# the producer, through the real flusher
# --------------------------------------------------------------------------


class _Store:
    """Minimal StreamStore stand-in: `flush` raises while `wedged` is set."""

    PENDING_ALARM = streamd.StreamStore.PENDING_ALARM
    SPILL_CAP = streamd.StreamStore.SPILL_CAP

    def __init__(self) -> None:
        self.wedged = True
        self.pending = 1234
        self.spilled = 0
        self.spill_corrupt = 0
        self.flushes = 0

    def flush(self) -> int:
        if self.wedged:
            raise OSError("IO Error: Could not set lock on file")
        self.flushes += 1
        return 7

    def mark_startup_gap(self) -> None:
        pass


def test_the_flusher_records_the_episode_it_lives_through(monkeypatch, tmp_path):
    """Wired end to end: the ledger call has to be on BOTH paths of the real
    try/except, because the failure and the recovery are what bound an
    episode and they are in different branches."""
    import asyncio

    store = _Store()
    d = streamd.Daemon.__new__(streamd.Daemon)
    d.store = store
    d.stats = {}
    d._spill_corrupt_seen = 0
    d.stalls = streamd.FlushStalls(str(tmp_path / "stalls.jsonl"))

    rounds = {"n": 0}

    async def fake_sleep(_secs):
        rounds["n"] += 1
        if rounds["n"] == 3:
            store.wedged = False  # the reader lets go
        if rounds["n"] > 4:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(streamd.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(d.flusher())

    assert store.flushes >= 1  # the recovery really happened
    (rec,) = _records(tmp_path / "stalls.jsonl")
    assert rec["state"] == "closed"
    assert rec["fails"] == 2
    assert rec["peak_pending"] == 1234
    assert "OSError" in rec["error"]


def test_the_shutdown_drain_closes_an_open_episode():
    """`run()`'s final drain ends a stall outside the flusher. Without that
    call, a daemon restarted DURING an episode leaves it open forever and its
    true duration is never written down."""
    src = inspect.getsource(streamd.Daemon.run)
    tail = src.split("self.store.flush()")[-1]
    assert "self.stalls.ok(" in tail


def test_the_witness_marker_matches_the_line_the_daemon_prints():
    """One definition: a reword of the daemon's log must not silently turn
    the QA witness into a permanent zero."""
    src = inspect.getsource(streamd.Daemon.flusher)
    assert qa.STREAM_FLUSH_FAIL_MARK in src


# --------------------------------------------------------------------------
# the QA reader
# --------------------------------------------------------------------------


def _run_check(**kw) -> tuple[set, list, str]:
    qa._failures.clear()
    qa._skipped.clear()
    printed: list[str] = []
    real = builtins.print
    builtins.print = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    try:
        qa.qa_stream_stalls(**kw)
    finally:
        builtins.print = real
    failed, skipped = set(qa._failures), list(qa._skipped)
    qa._failures.clear()
    qa._skipped.clear()
    return failed, skipped, "\n".join(printed)


NOW = T0 + timedelta(hours=2)


def _ledger(tmp_path, *episodes) -> str:
    p = tmp_path / "stalls.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in episodes))
    return str(p)


def _ep(started: datetime, duration: float, *, state="closed", spilled=0, peak=1000) -> dict:
    return {
        "at": (started + timedelta(seconds=duration)).isoformat(),
        "state": state,
        "started": started.isoformat(),
        "duration_s": duration,
        "fails": max(1, int(duration // 15)),
        "peak_pending": peak,
        "spilled": spilled,
        "error": "OSError: IO Error",
    }


def test_stalls_inside_the_buffer_pass_and_report_the_shape(tmp_path):
    """Duration alone is NOT a failure: the box has legitimate multi-hour
    readers and a stall inside the buffer loses nothing. It is reported so
    the distribution stays visible as it drifts."""
    path = _ledger(
        tmp_path,
        _ep(T0, 15.0),
        _ep(T0 + timedelta(minutes=30), 1756.0, peak=180_000),
    )
    failed, skipped, out = _run_check(path=path, journal_fails=140, now=NOW)
    assert not failed and not skipped, out
    assert "2 episode(s)" in out
    assert "longest 1756s" in out
    assert "peak 180000 rows held" in out
    assert "1 over 600s" in out


def test_a_spill_fails_because_the_daemons_own_cap_was_reached(tmp_path):
    """The threshold is not invented here: past SPILL_CAP rows leave memory
    for a file whose torn-append path is a real archive-hole class."""
    path = _ledger(tmp_path, _ep(T0, 1801.0, spilled=34_691, peak=400_041))
    failed, _, out = _run_check(path=path, journal_fails=120, now=NOW)
    assert failed == {"streamd flush stalls stay inside the buffer"}
    assert "34691 row(s) out of memory" in out


def test_an_empty_ledger_with_a_journalled_failure_is_an_inert_producer(tmp_path):
    """The arm that makes the green line mean something. Without it a ledger
    that stopped being written reads exactly like a quiet week."""
    path = _ledger(tmp_path)
    failed, _, out = _run_check(path=path, journal_fails=17, now=NOW)
    assert failed == {"streamd flush stalls stay inside the buffer"}
    assert "PRODUCER INERT" in out


def test_a_quiet_window_is_unverified_not_a_pass(tmp_path):
    """Neither witness saw anything, so nothing was measured. EXP-943's
    lesson, applied to the second sidecar."""
    failed, skipped, out = _run_check(path=_ledger(tmp_path), journal_fails=0, now=NOW)
    assert not failed
    assert skipped == [qa._STREAM_STALL_SECTION]
    assert "UNVERIFIED" in out and "production is untested" in out


def test_an_unreadable_journal_is_unverified_not_a_pass(tmp_path):
    """None is not zero: an unreadable journal cannot testify that the
    producer is alive, so the empty ledger stays undecided."""
    failed, skipped, out = _run_check(path=_ledger(tmp_path), journal_fails=None, now=NOW)
    assert not failed
    assert skipped == [qa._STREAM_STALL_SECTION]
    assert "neither proven alive nor proven dead" in out


def test_the_two_counts_are_never_compared_by_size(tmp_path):
    """One 30-minute episode is 120 journal lines. Only the zero/non-zero
    split carries across the two namespaces -- comparing magnitudes is the
    #53 mistake with different nouns."""
    path = _ledger(tmp_path, _ep(T0, 1756.0))
    failed, _, out = _run_check(path=path, journal_fails=117, now=NOW)
    assert not failed, out


def test_heartbeats_and_their_close_are_one_episode(tmp_path):
    """Six records, one stall. Counting records would report a 30-minute
    outage as six outages and inflate every rate built on it."""
    started = T0
    path = _ledger(
        tmp_path,
        _ep(started, 300.0, state="open"),
        _ep(started, 600.0, state="open"),
        _ep(started, 1756.0),
    )
    failed, _, out = _run_check(path=path, journal_fails=117, now=NOW)
    assert not failed
    assert "1 episode(s)" in out
    assert "longest 1756s" in out


def test_the_episodes_length_does_not_depend_on_line_order(tmp_path):
    """`read_stall_episodes` takes the LONGEST record for an episode, not the
    last one. In a healthy file those coincide (records are appended as the
    stall grows), which is exactly why the distinction has to be pinned here:
    nothing in production would ever fail if the reader quietly became
    order-dependent, and then one truncated tail would shorten an outage on
    the report instead of losing it visibly."""
    path = _ledger(
        tmp_path,
        _ep(T0, 1756.0),
        _ep(T0, 300.0, state="open"),
    )
    (ep,), malformed = qa.read_stall_episodes(path, 24.0, NOW)
    assert not malformed
    assert ep["duration_s"] == 1756.0


def test_an_episode_outside_the_window_is_not_counted(tmp_path):
    path = _ledger(tmp_path, _ep(T0 - timedelta(days=3), 15.0))
    failed, skipped, out = _run_check(path=path, journal_fails=0, now=NOW)
    assert skipped == [qa._STREAM_STALL_SECTION], out


def test_malformed_rows_are_reported_not_swallowed(tmp_path):
    p = tmp_path / "stalls.jsonl"
    p.write_text(json.dumps(_ep(T0, 15.0)) + "\n{not json\n" + '{"at": "x"}\n')
    failed, _, out = _run_check(path=str(p), journal_fails=3, now=NOW)
    assert not failed
    assert "2 malformed rows" in out


def test_the_witness_window_ends_early_so_a_live_stall_is_not_inert(monkeypatch):
    """An episode in progress HAS journalled its failures and cannot yet have
    written a closed record. Without the grace, the check's own timing would
    manufacture an INERT verdict on a healthy daemon."""
    seen = {}

    def fake_journal(unit, since, until):
        seen.update(unit=unit, since=since, until=until)
        return "flush FAILED x\nflush FAILED y\n"

    monkeypatch.setattr(qa, "_journal", fake_journal)
    now = NOW
    since = now - timedelta(hours=24)
    assert qa.journal_stream_flush_fails(since, now) == 2
    assert seen["unit"] == qa.STREAM_UNIT
    assert (now - seen["until"]).total_seconds() == qa.STREAM_STALL_GRACE_S
    assert qa.STREAM_STALL_GRACE_S > streamd.STALL_HEARTBEAT_S
    assert seen["since"] == since


def test_an_unreadable_journal_is_none_not_zero(monkeypatch):
    monkeypatch.setattr(qa, "_journal", lambda *a, **k: None)
    assert qa.journal_stream_flush_fails(NOW - timedelta(hours=24), NOW) is None


def test_a_daemon_restart_does_not_indict_the_producer_that_followed_it(tmp_path, monkeypatch):
    """THE DEPLOYMENT ARM. The journal remembers failures from before the
    daemon was restarted onto the code that writes this ledger. Counting them
    makes the first QA run after every promote read INERT on a healthy box --
    the alarm a liveness check exists to make believable, spent on nothing."""
    armed = NOW - timedelta(minutes=20)
    p = tmp_path / "stalls.jsonl"
    p.write_text(json.dumps({"at": armed.isoformat(), "state": "armed"}) + "\n")

    seen = {}

    def fake_journal(unit, since, until):
        seen["since"] = since
        return ""  # nothing failed since the daemon armed

    monkeypatch.setattr(qa, "_journal", fake_journal)
    failed, skipped, out = _run_check(path=str(p), now=NOW)
    assert not failed, out
    assert skipped == [qa._STREAM_STALL_SECTION]
    assert seen["since"] == armed  # the epoch, not 24h ago


def test_a_ledger_that_was_never_armed_is_an_inert_producer(tmp_path):
    """The other half: a promote that installed the check but left the old
    daemon running writes no epoch at all, and that IS the inert case."""
    path = _ledger(tmp_path)
    failed, _, out = _run_check(path=path, journal_fails=17, now=NOW)
    assert failed == {"streamd flush stalls stay inside the buffer"}
    assert "never been armed" in out


def test_an_armed_record_is_an_epoch_not_an_episode(tmp_path):
    """It has no duration, so a reader that counted it would report a
    zero-second stall on every daemon start."""
    p = tmp_path / "stalls.jsonl"
    p.write_text(
        json.dumps({"at": (NOW - timedelta(hours=1)).isoformat(), "state": "armed"}) + "\n"
    )
    eps, malformed = qa.read_stall_episodes(str(p), 24.0, NOW)
    assert eps == [] and malformed == 0
    assert qa.stall_epoch(str(p)) == NOW - timedelta(hours=1)


def test_the_epoch_survives_a_daemon_older_than_the_window(tmp_path):
    """streamd has run 60h+ without a restart in production. A windowed
    lookup would lose its epoch and fall back to the full window -- which is
    right, but only by accident; this pins that the epoch itself is found."""
    old = NOW - timedelta(days=30)
    p = tmp_path / "stalls.jsonl"
    p.write_text(json.dumps({"at": old.isoformat(), "state": "armed"}) + "\n")
    assert qa.stall_epoch(str(p)) == old


def test_an_absent_ledger_is_not_a_crash(tmp_path):
    failed, skipped, out = _run_check(path=str(tmp_path / "nope.jsonl"), journal_fails=0, now=NOW)
    assert not failed and skipped == [qa._STREAM_STALL_SECTION], out
