"""The STREAMFLOW digest line: did the stream RECEIVE anything, round by round.

Fixture lines are the real `journalctl -o short-iso --utc` shapes from the
2026-11-09 router outage (mistakes #168): the round closing 10:29:15Z
received 25% of the median and its last flush tick landed 20 rows; the
round before it received 62% with the same dead tick.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from collector import health

P = "hyz python[98880]: [streamd] "
KEYS = health.STREAMFLOW_RECV_KEYS


def _stats(ts: str, recv: int, tick: int, label: str = "last flush {n} rows") -> str:
    """One stats line whose six received counters sum to `recv` (cumulative)."""
    d = {k: recv // 6 for k in KEYS}
    d["kalshi_books_events"] += recv - 6 * (recv // 6)
    d["books_removed"] = 29613  # bookkeeping keys are ignored
    return f"{ts} {P}{ts[11:19]} stats {d} ({label.format(n=tick)}) rss_anon_mb=390.9 arena_mb=93.3/150 sock_throttled=0 (+0)"


def _gap(ts: str, shard: int) -> str:
    return f"{ts} {P}{ts[11:19]} kalshi/books GAP (reconnect) shard={shard}"


M = 1_000_000
OUTAGE = [
    _stats("2026-11-09T10:08:42+00:00", 10 * M, 93411),
    _stats("2026-11-09T10:13:48+00:00", 12 * M, 70676),
    _stats("2026-11-09T10:18:57+00:00", 14 * M, 86521),
    _stats("2026-11-09T10:24:05+00:00", 15_240_000, 20),  # 62% of a 2M median
    *[_gap(f"2026-11-09T10:25:{44 + i % 10:02d}+00:00", i) for i in range(19)],
    _stats("2026-11-09T10:29:15+00:00", 15_740_000, 20, "flushed {n} this round"),  # 25%
    *[_gap(f"2026-11-09T10:29:{20 + i % 30:02d}+00:00", i) for i in range(20)],
    _stats("2026-11-09T10:34:23+00:00", 17_740_000, 70650),
    _stats("2026-11-09T10:39:38+00:00", 19_740_000, 72951),
    "-- No entries --",
]


def test_received_is_the_delta_of_the_counters_and_low_rounds_are_named() -> None:
    flow = health.parse_stream_flow(OUTAGE)
    assert flow is not None
    assert flow.rounds == 6 and flow.median_received == 2 * M and flow.median_tick == 70676
    assert flow.sample_gap_max_s == pytest.approx(315.0)
    assert flow.dead == ()
    assert [r.received for r in flow.low] == [500_000]
    assert flow.low[0].gaps_in_period == 19 and flow.low[0].gaps_after == 20
    line = health.stream_flow_line(flow)
    assert line.startswith(
        "24h: 6 rounds, median 2,000,000 events received/round, median last tick 70,676 rows "
        "(stats every 300 s nominal, widest 315 s) | no round under 10% | "
        "LOW 1 round(s) under 50% of their neighbours: 11-09 10:29Z 25% of its neighbours "
        "(25% of the day; books 83k, ticker 83k, trades 83k, poly 83k; GAP lines 19 in round, 20 next)"
    )


def test_a_dead_last_tick_is_an_instant_not_a_span() -> None:
    flow = health.parse_stream_flow(OUTAGE)
    assert flow is not None
    assert [r.last_tick for r in flow.dead_ticks] == [20, 20]
    assert health.stream_flow_line(flow).endswith(
        "| last tick dead (under 10% of the tick median) at 2 instant(s): "
        "10:24:05Z 20 rows, 10:29:15Z 20 rows"
    )


def test_both_labels_parse_the_old_one_for_the_day_the_journal_still_holds_it() -> None:
    old = [_stats("2026-11-09T10:00:00+00:00", M, 5, "flushed {n} this round")]
    new = [_stats("2026-11-09T10:05:00+00:00", 2 * M, 7)]
    flow = health.parse_stream_flow(old + new)
    assert flow is not None and flow.rounds == 1 and flow.rounds and flow.median_tick == 7


def test_consecutive_dead_rounds_are_one_episode_with_both_bounds() -> None:
    lines = [
        _stats(f"2026-11-09T10:{m:02d}:00+00:00", c, t)
        for m, c, t in [
            (0, 10 * M, 1000),
            (5, 12 * M, 1000),
            (10, 14 * M, 1000),
            (15, 14 * M + 10, 0),
            (20, 14 * M + 20, 0),
            (25, 16 * M, 1000),
            (30, 18 * M, 1000),
        ]
    ]
    flow = health.parse_stream_flow(lines)
    assert flow is not None and len(flow.dead) == 1
    ep = flow.dead[0]
    assert len(ep.rounds) == 2
    assert ep.dead_lower_s == 300.0 and ep.dead_upper_s == 900.0
    assert (
        "DEAD-AIR 2 round(s) under 10% in 1 episode(s): 11-09 10:15Z..10:20Z 2 round(s) (0%, 0%): dead air 5.0 min..15.0 min, GAP lines 0"
        in health.stream_flow_line(flow)
    )


def test_a_counter_reset_is_a_restart_not_a_negative_round() -> None:
    lines = [
        _stats("2026-11-09T10:00:00+00:00", 10 * M, 100),
        _stats("2026-11-09T10:05:00+00:00", 12 * M, 100),
        _stats("2026-11-09T10:10:00+00:00", 1 * M, 100),  # the daemon restarted
        _stats("2026-11-09T10:15:00+00:00", 3 * M, 100),
    ]
    flow = health.parse_stream_flow(lines)
    assert flow is not None and flow.rounds == 2 and flow.dead == () and flow.low == ()


def test_rounds_before_the_daemon_start_floor_are_dropped() -> None:
    flow = health.parse_stream_flow(OUTAGE, floor=datetime(2026, 11, 9, 10, 20, tzinfo=UTC))
    assert flow is not None and flow.rounds == 3
    assert health.parse_stream_flow(OUTAGE, floor=datetime(2026, 11, 10, tzinfo=UTC)) is None


def test_empty_and_unreadable_journals_are_different_lines() -> None:
    assert health.parse_stream_flow("-- No entries --\n") is None
    assert health.parse_stream_flow(OUTAGE[:1]) is None  # one line has no delta
    assert health.stream_flow_line(None).startswith("24h: fewer than two stats lines")
    assert health.stream_flow_line("unreadable") == "24h: journal unreadable"


def test_read_stream_flow_distinguishes_a_failed_journalctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake(args: list[str], hours: int) -> str | None:
        seen.append(args)
        return None

    monkeypatch.setattr(health, "_journal_hourly", fake)
    assert health.read_stream_flow() == "unreadable"
    assert "-u" in seen[0] and "hyxlab-stream.service" in seen[0] and "--utc" in seen[0]
    monkeypatch.setattr(health, "_journal_hourly", lambda args, hours: "\n".join(OUTAGE))
    flow = health.read_stream_flow()
    assert isinstance(flow, health.StreamFlow) and flow.rounds == 6


def test_the_digest_prints_the_streamflow_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(health, "read_stream_flow", lambda: health.parse_stream_flow(OUTAGE))
    monkeypatch.setattr(health, "discover_units", lambda: [])
    monkeypatch.setattr(health, "read_unit_failures", lambda units: [])
    monkeypatch.setattr(health, "live_cgmem", lambda units: [])
    monkeypatch.setattr(health, "journal_floor", lambda: None)
    monkeypatch.setattr(health, "drift_report", lambda: [])
    monkeypatch.setattr(health, "show", lambda unit: {})
    monkeypatch.setattr(health, "judge_qa", lambda rec, svc, now: "QA (stubbed)")
    health.main()
    out = capsys.readouterr().out
    assert "STREAMFLOW hyxlab-stream.service" in out and "24h: 6 rounds, median 2,000,000" in out


def test_every_field_of_the_reading_perturbs_the_line() -> None:
    """#95: a field read into a local and dropped renders identically."""
    base = health.parse_stream_flow(OUTAGE)
    assert base is not None
    ref = health.stream_flow_line(base)
    r = base.low[0]
    fields = dict(
        rounds=base.rounds,
        median_received=base.median_received,
        median_tick=base.median_tick,
        sample_gap_max_s=base.sample_gap_max_s,
        dead=base.dead,
        low=base.low,
        dead_ticks=base.dead_ticks,
    )
    variants = [
        {**fields, "rounds": base.rounds + 1},
        {**fields, "median_received": base.median_received + 1},
        {**fields, "median_tick": base.median_tick + 1},
        {**fields, "sample_gap_max_s": 999.0},
        {**fields, "low": ()},
        {**fields, "dead_ticks": base.dead_ticks[:1]},
        {**fields, "dead": (health.DeadEpisode((r,), 300.0),)},
    ]
    for v in variants:
        assert health.stream_flow_line(health.StreamFlow(**v)) != ref


def test_a_quiet_diurnal_hour_is_not_low_but_a_dip_against_its_neighbours_is() -> None:
    """2026-11-09: a routine Monday's 08-09Z rounds ran 48-50% of the 24h
    median; a day-median tier named 09:01Z LOW with 0 GAP lines. The bar is
    each round's neighbours. Night rounds at 3M, morning at 1M (33% of the
    day's... the day's median lands between), one morning round at 0.4M."""
    night = [(m, 3_000_000) for m in range(0, 60, 5)]
    morning = [(60 + m, 1_000_000) for m in range(0, 60, 5)]
    morning[6] = (morning[6][0], 400_000)  # 40% of its neighbours, 13-20% of the day
    cum, lines = 0, []
    for m, v in night + morning:
        cum += v
        lines.append(_stats(f"2026-11-09T{m // 60:02d}:{m % 60:02d}:00+00:00", cum, 1000))
    flow = health.parse_stream_flow(lines)
    assert flow is not None
    assert [f"{r.at:%H:%M}" for r in flow.low] == ["01:30"]
    assert flow.low[0].local == 1_000_000
    assert flow.dead == ()  # 400k is 13% of a 3M day median: a dip, not dead air
