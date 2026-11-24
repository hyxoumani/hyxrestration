"""EDG-164: the shadow's stream poll starts inside the daemon's flush gap.

The flush cycle is read from two `stat` calls on files the daemon already
writes (the archive's mtime, the WAL's existence); the key is off unless
set. Fake-clock tests pin the policy; fixture tests pin that the WAL/mtime
signal is what a real `StreamStore` flush leaves behind and that the
shadow's fills are the same with the aligner on as off.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest

from collector.venues.kalshi_ws import parse_message
from hyxlab.models import MarketInfo, Order
from hyxlab.streamstore import StreamStore
from simulator import shadow
from simulator.shadow import (
    ALIGN_DEFAULT_HOLD_S,
    ALIGN_GAP_S,
    ALIGN_HOLD_WINDOW,
    ALIGN_MAX_WAIT_S,
    ALIGN_SETTLE_S,
    FLUSH_ALIGN_ENV,
    FlushAligner,
    ShadowLedger,
    ShadowRunner,
    flush_aligner_from_env,
)
from simulator.strategy import Strategy

T0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
PATH = "/fake/hyxstream.duckdb"


class FakeDaemon:
    """A flush every `gap + hold` seconds: open at `o`, WAL from `o + wal_lag`
    to the end `e = o + hold`, main-file writes during the close (the last
    `close_s` of the hold), then `gap` seconds of quiet before the next open.
    One clock serves wall and monotonic; `sleep` advances it."""

    def __init__(self, t0=1000.0, gap=ALIGN_GAP_S, hold=1.5, wal_lag=0.3, close_s=0.7, n=200):
        self.t = t0
        self.flushes = []
        o = t0 - 20.0
        for _ in range(n):
            self.flushes.append((o, o + wal_lag, o + hold, o + hold - close_s))
            o = o + hold + gap
        self.sleeps = []
        self.missing = False
        self.frozen_main = None  # a stale file: mtime pinned
        self.stuck_wal = None  # a WAL left behind, mtime pinned

    def wall(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def stat(self, path):
        if self.missing:
            raise FileNotFoundError(path)
        if path.endswith(".wal"):
            if self.stuck_wal is not None:
                return SimpleNamespace(st_mtime=self.stuck_wal)
            for _o, w, e, _c in self.flushes:
                if w <= self.t < e:
                    return SimpleNamespace(st_mtime=min(self.t, e))
            raise FileNotFoundError(path)
        if self.frozen_main is not None:
            return SimpleNamespace(st_mtime=self.frozen_main)
        m = None
        for _o, _w, e, c in self.flushes:
            if c <= self.t:
                m = min(self.t, e)
        return SimpleNamespace(st_mtime=m)

    def busy(self, a, b):
        """True when the daemon would OPEN inside [a, b] (a refused flush)."""
        return any(a <= o <= b for o, _w, _e, _c in self.flushes)

    def at_phase(self, k, phase):
        """Put the clock `phase` seconds after flush k's end."""
        self.t = self.flushes[k][2] + phase

    def aligner(self, **kw):
        return FlushAligner(
            PATH, wall=self.wall, mono=self.wall, sleep=self.sleep, stat=self.stat, **kw
        )


def test_gap_is_the_daemons_flush_period():
    from collector import streamd

    assert ALIGN_GAP_S == streamd.FLUSH_SECS


def test_a_poll_inside_the_gap_starts_at_once():
    d = FakeDaemon()
    d.at_phase(50, 3.0)
    a = d.aligner()
    assert a.wait() == "in_gap"
    assert d.sleeps == []
    assert a.stats["in_gap_n"] == 1 and a.stats["waited_s_total"] == 0.0


def test_a_poll_late_in_the_gap_waits_for_the_next_flush_to_end():
    d = FakeDaemon()
    d.at_phase(50, 12.0)  # past hi = 15 - 2 - 3 = 10
    a = d.aligner()
    start = d.t
    assert a.wait() == "waited"
    next_end = d.flushes[51][2]
    assert next_end + ALIGN_SETTLE_S <= d.t <= next_end + ALIGN_SETTLE_S + 0.11
    assert not d.busy(d.t, d.t + ALIGN_DEFAULT_HOLD_S)  # the hold, not the wait
    assert a.stats["waited_n"] == 1
    assert a.stats["waited_s_max"] == pytest.approx(d.t - start)


def test_a_poll_during_a_flush_waits_for_its_close():
    d = FakeDaemon()
    d.t = d.flushes[50][0] + 0.8  # WAL present, close running
    a = d.aligner()
    assert a.wait() == "waited"
    assert d.flushes[50][2] + ALIGN_SETTLE_S <= d.t <= d.flushes[50][2] + ALIGN_SETTLE_S + 0.11


def test_an_off_cycle_flush_is_seen_by_its_wal():
    """A flush 3 s after the last one (a daemon restart's first, or any
    writer off the 15 s clock): the main file's age says "in the gap", the
    WAL says a flush is running, and the WAL wins."""
    d = FakeDaemon()
    o, _w, e, c = d.flushes[50]
    d.flushes.insert(51, (e + 3.0, e + 3.3, e + 4.5, e + 3.8))
    d.t = e + 3.5  # WAL present, main untouched since e
    a = d.aligner()
    assert a.wait() == "waited"
    assert e + 4.5 + ALIGN_SETTLE_S <= d.t <= e + 4.5 + ALIGN_SETTLE_S + 0.11


def test_a_poll_just_after_the_close_waits_out_the_settle():
    d = FakeDaemon()
    d.at_phase(50, 0.05)
    a = d.aligner()
    assert a.wait() == "waited"
    assert ALIGN_SETTLE_S <= d.t - d.flushes[50][2] <= ALIGN_SETTLE_S + 0.11


def test_the_window_shrinks_with_the_measured_hold():
    d = FakeDaemon()
    d.at_phase(50, 8.0)
    a = d.aligner()
    for h in (0.9, 1.0, 1.1):
        a.note_hold(h)
    assert a.hold_estimate() == 1.1
    assert a.wait() == "in_gap"  # 8 <= 15 - 2 - 1.1
    a.note_hold(6.0)
    d.at_phase(60, 8.0)
    assert a.wait() == "waited"  # 8 > 15 - 2 - 6


def test_hold_estimate_is_the_longest_of_the_recent_window():
    a = FlushAligner(PATH)
    assert a.hold_estimate() == ALIGN_DEFAULT_HOLD_S
    a.note_hold(9.0)
    for _ in range(ALIGN_HOLD_WINDOW):
        a.note_hold(1.0)
    assert a.hold_estimate() == 1.0


def test_a_hold_that_cannot_fit_polls_outside_a_flush_and_says_so():
    d = FakeDaemon()
    d.at_phase(50, 5.0)
    a = d.aligner()
    a.note_hold(14.0)  # 15 - 2 - 14 < settle: no start avoids the next open
    assert a.wait() == "misfit"
    assert d.sleeps == []
    d.t = d.flushes[60][0] + 0.8  # mid-flush: still waits for the close
    assert a.wait() == "misfit"
    assert d.flushes[60][2] + ALIGN_SETTLE_S <= d.t <= d.flushes[60][2] + ALIGN_SETTLE_S + 0.11
    assert a.stats["misfit_n"] == 2


def test_fails_open_without_the_file():
    d = FakeDaemon()
    d.missing = True
    a = d.aligner()
    assert a.wait() == "open"
    assert d.sleeps == []


def test_fails_open_on_a_stale_file_or_a_left_wal():
    d = FakeDaemon()
    d.frozen_main = d.t - 300.0
    a = d.aligner()
    assert a.wait() == "open" and d.sleeps == []
    d.stuck_wal = d.t - 300.0
    assert a.wait() == "open" and d.sleeps == []


def test_a_long_flush_delays_a_poll_by_at_most_the_cap():
    d = FakeDaemon(hold=60.0, close_s=1.0)
    d.t = d.flushes[50][0] + 1.0
    a = d.aligner()
    start = d.t
    assert a.wait() == "timeout"
    assert d.t - start == pytest.approx(ALIGN_MAX_WAIT_S)
    assert a.stats["timeout_n"] == 1


@pytest.mark.parametrize("hold", [0.5, 1.0, 2.6, 6.0])
def test_no_phase_collides_and_no_phase_waits_past_one_cycle(hold):
    """Every arrival phase across a cycle, hold measured and known: the hold
    never covers the daemon's next open, and the wait is under one cycle."""
    d = FakeDaemon()
    cycle = d.flushes[1][0] - d.flushes[0][0]
    worst = 0.0
    for i in range(400):
        d.at_phase(60, cycle * i / 400 - 1.0)
        a = d.aligner()
        for _ in range(ALIGN_HOLD_WINDOW):
            a.note_hold(hold)
        t = d.t
        a.wait()
        worst = max(worst, d.t - t)
        assert not d.busy(d.t, d.t + hold), (i, d.t, hold)
    assert worst <= cycle + ALIGN_SETTLE_S + 0.11


def test_the_line_is_json_after_its_prefix():
    d = FakeDaemon()
    a = d.aligner()
    a.wait()
    line = a.line()
    assert line.startswith("[shadow] flush_align {")
    body = json.loads(line.split("flush_align ", 1)[1])
    assert body["n"] == 1 and body["hold_est_s"] == ALIGN_DEFAULT_HOLD_S


@pytest.mark.parametrize("raw", [None, "", "0", "off", "False", "no"])
def test_the_key_is_off_unless_set(raw):
    env = {} if raw is None else {FLUSH_ALIGN_ENV: raw}
    assert flush_aligner_from_env(env, PATH) == (None, None)


@pytest.mark.parametrize("raw", ["1", "true", "ON", " yes "])
def test_the_key_turns_it_on_and_says_so(raw):
    aligner, line = flush_aligner_from_env({FLUSH_ALIGN_ENV: raw}, PATH)
    assert isinstance(aligner, FlushAligner) and aligner.path == PATH
    assert line.startswith("[shadow] flush align (EDG-164): on")


def test_an_unreadable_value_is_off_and_said():
    aligner, line = flush_aligner_from_env({FLUSH_ALIGN_ENV: "maybe"}, PATH)
    assert aligner is None and "not understood; off" in line


def test_the_runner_has_no_aligner_by_default(tmp_path):
    runner = ShadowRunner(
        [],
        stream_db=str(tmp_path / "s.duckdb"),
        archive_db=str(tmp_path / "a.duckdb"),
        ledger=ShadowLedger(tmp_path / "l.duckdb"),
    )
    assert runner.aligner is None


def test_main_builds_no_aligner_without_the_key(monkeypatch):
    monkeypatch.delenv(FLUSH_ALIGN_ENV, raising=False)
    src = shadow.main.__code__.co_names
    assert "flush_aligner_from_env" in src  # wired through the one parser
    assert flush_aligner_from_env(os.environ, shadow.STREAM_DB) == (None, None)


# --- fixtures on real DuckDB files ---------------------------------------


def _snapshot_frame(mid, seq, bid_cents, ask_no_cents, ts):
    return parse_message(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": seq,
            "msg": {
                "market_ticker": mid,
                "yes_dollars_fp": [[f"{bid_cents / 100:.4f}", "100.00"]],
                "no_dollars_fp": [[f"{ask_no_cents / 100:.4f}", "100.00"]],
            },
        },
        ts,
    )[0]


class BuyEveryMove(Strategy):
    name = "buy_every_move"

    def on_snapshot(self, s, ctx):
        return [Order(s.venue, s.market_id, "yes", 1, tif="IOC")]


def test_a_real_flush_leaves_the_signal_the_aligner_reads(tmp_path):
    """After a `StreamStore` flush returns, the WAL is gone and the main
    file's mtime has moved: the two facts the policy rests on, on this
    DuckDB."""
    db = tmp_path / "stream.duckdb"
    s = StreamStore(db)
    s.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    s.flush()
    os.utime(db, (1_000_000_000, 1_000_000_000))
    s.append_events(_snapshot_frame("M1", 2, 41, 58, T0 + timedelta(seconds=5)))
    assert s.flush() > 0
    assert not os.path.exists(str(db) + ".wal")
    assert os.stat(db).st_mtime > 1_000_000_000 + 1


class _Recording(FlushAligner):
    def __init__(self, path, events):
        super().__init__(path, settle_s=0.0, max_wait_s=0.5)
        self.events = events

    def wait(self):
        self.events.append("wait")
        return super().wait()

    def note_hold(self, held_s):
        self.events.append(("hold", held_s))
        super().note_hold(held_s)


def _fills(tmp_path, aligned: bool):
    from hyxlab.store import Store

    stream_db = tmp_path / "stream.duckdb"
    archive_db = tmp_path / "archive.duckdb"
    store = Store(archive_db)
    store.upsert_markets([MarketInfo(venue="kalshi", market_id=m) for m in ("M1", "M2")])
    store.close()
    sstore = StreamStore(stream_db)
    sstore.append_events(_snapshot_frame("M1", 1, 40, 59, T0))
    sstore.flush()
    events: list = []
    runner = ShadowRunner(
        [BuyEveryMove()],
        latency=2.0,
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        ledger=ShadowLedger(tmp_path / "shadow.duckdb"),
        aligner=_Recording(str(stream_db), events) if aligned else None,
    )
    runner.poll_once()  # anchor + seed
    for i in range(1, 13):
        mid = "M1" if i % 3 else "M2"
        sstore.append_events(
            _snapshot_frame(mid, i + 1, 40 + i, 58 - i, T0 + timedelta(seconds=5 * i))
        )
        sstore.flush()
        if i % 2 == 0:
            runner.poll_once()
    runner.poll_once()
    fills = [(f.ts, f.market_id, f.side, f.price, f.qty, f.fee) for f in runner.sim.result.fills]
    with duckdb.connect(str(tmp_path / "shadow.duckdb"), read_only=True) as conn:
        ledger = conn.execute(
            "SELECT strategy, venue, market_id, side, qty, price, fee, maker, ts"
            " FROM shadow_fills ORDER BY ts, market_id"
        ).fetchall()
        equity = conn.execute("SELECT ts, equity FROM shadow_equity ORDER BY ts").fetchall()
    return fills, ledger, equity, runner.stats, events


def test_shadow_outputs_are_identical_with_the_aligner_on(tmp_path):
    off = _fills(tmp_path / "off", aligned=False)
    on = _fills(tmp_path / "on", aligned=True)
    assert len(off[0]) >= 8  # non-vacuous
    assert on[0] == off[0]  # fills, in memory
    assert on[1] == off[1]  # fills, ledger
    assert on[2] == off[2]  # equity points
    assert on[3] == off[3]  # snapshots / events / polls
    ev = on[4]
    assert ev.count("wait") == 8  # every poll waited first, the seed poll too
    holds = [e[1] for e in ev if isinstance(e, tuple)]
    assert len(holds) == 7 and all(h > 0 for h in holds)  # tail polls only
    assert ev[0] == "wait" and ev[1] == "wait"  # the seed poll noted no hold
