"""`rx_lag=` on the stream stats line: receive time minus the venue's send
time, per watched connection and period.

The hourly lag ledger reads the raw-frame files, and the raw copy yields the
disk under 6 GB free -- so from 2026-11-22 09:50Z the ledger read UNMEASURED
exactly while the path was worst. The daemon has both clocks for every frame.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from collector import health, rawlag, streamd

T0 = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def test_no_watched_frame_is_unmeasured_not_zero():
    w = streamd.RecvLagWatch()
    assert w.take() is None
    w.note("books", T0, _ms(T0))  # the hot path is not sampled
    w.note("lifecycle", T0, _ms(T0))
    w.note("trades", T0, None)  # a control ack carries no send time
    w.note("trades", T0, "1795370400000")  # nor is a string a clock
    w.note("trades", T0, True)
    assert w.take() is None


def test_quantiles_per_channel_and_the_period_resets():
    w = streamd.RecvLagWatch()
    sent = _ms(T0)
    for i in range(100):  # 0.00 .. 0.99 s late, in a scrambled order
        w.note("trades", T0 + timedelta(milliseconds=10 * ((i * 37) % 100)), sent)
    for lag_s in (2.0, 4.0, 30.0):
        w.note("ticker", T0 + timedelta(seconds=lag_s), sent)
    got = w.take()
    assert set(got) == {"trades", "ticker"}
    p50, p99, n = got["trades"]
    assert (p50, p99, n) == (pytest.approx(0.50), pytest.approx(0.99), 100)
    assert got["ticker"] == (pytest.approx(4.0), pytest.approx(30.0), 3)
    # Handed over once: a quiet next period is unmeasured, not the old reading.
    assert w.take() is None
    w.note("trades", T0 + timedelta(seconds=7), sent)
    assert w.take() == {"trades": (pytest.approx(7.0), pytest.approx(7.0), 1)}


def test_the_quantile_is_the_ledgers_so_the_two_readings_compare():
    w = streamd.RecvLagWatch()
    sent = _ms(T0)
    lags = [0.01 * ((i * 53) % 997) for i in range(997)]
    for lag_s in lags:
        w.note("trades", T0 + timedelta(seconds=lag_s), sent)
    p50, p99, n = w.take()["trades"]
    vals = sorted(lags)
    assert n == 997
    assert p50 == pytest.approx(rawlag._q(vals, 0.5), abs=1e-3)
    assert p99 == pytest.approx(rawlag._q(vals, 0.99), abs=1e-3)


def test_a_venue_clock_ahead_of_ours_prints_negative_not_clamped():
    # NTP is unverified on this box: a clamp would hide the skew it measures.
    w = streamd.RecvLagWatch()
    w.note("trades", T0, _ms(T0 + timedelta(seconds=1.5)))
    assert w.take()["trades"][0] == pytest.approx(-1.5)


def test_stats_line_carries_the_lag_between_the_silence_and_the_holds(monkeypatch):
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 1.0)
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: None)
    args = ("", {"a": 1}, 5, "sock_throttled=0 (+0)", (19, 17.34, 2.06), (0.1, 0.2, 0.3, 0.4))
    lags = {"trades": (0.137, 5.861, 1234), "ticker": (0.161, 4.853, 250000)}
    line = streamd.stats_line(*args, (0.03, 0), (1.25, 0), lags)
    assert (
        " rx_gap=1.25/0 rx_lag=ticker:0.16/4.85/250000,trades:0.14/5.86/1234"
        " flush_hold=19/17.3/2.1 flush_steps="
    ) in line
    # Perturbation, one field at a time: each reaches the line (#95).
    for ch in lags:
        for i, bump in enumerate((1.0, 1.0, 1)):
            moved = dict(lags)
            vals = list(lags[ch])
            vals[i] += bump
            moved[ch] = tuple(vals)
            assert streamd.stats_line(*args, (0.03, 0), (1.25, 0), moved) != line
    one = streamd.stats_line(*args, (0.03, 0), (1.25, 0), {"trades": lags["trades"]})
    assert " rx_lag=trades:0.14/5.86/1234 " in one
    # No reading is no clause.
    bare = streamd.stats_line(*args, (0.03, 0), (1.25, 0))
    assert "rx_lag" not in bare
    # The digest's patterns read the line exactly as they did without it.
    for pat in (health._STREAMHOLD_RE, health._STREAMRX_RE):
        with_lag, without = pat.search(line), pat.search(bare)
        assert with_lag is not None and without is not None
        assert with_lag.groups() == without.groups()


def test_both_publishers_and_the_kalshi_loop_reach_the_watch():
    src = inspect.getsource(streamd)
    assert src.count("self.recv_lag.take()") == 2
    # After the parse, before the control branch: a `continue` there must not
    # skip a data frame, and an ack with no send time is not a sample anyway.
    assert src.count('self.recv_lag.note(channel, recv_ts, frame.get("sending_ts_ms"))') == 1
    parse = src.index("frame = json.loads(raw)\n                        self.recv_lag.note(")
    assert parse < src.index("if typ in kalshi_ws.CONTROL_TYPES:", parse)
    d = streamd.Daemon.__new__(streamd.Daemon)
    assert d.recv_lag is d.recv_lag  # built once, on a `__new__` daemon too
