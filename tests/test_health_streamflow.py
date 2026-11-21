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
        "10:24:05Z 20 rows, 10:29:15Z 20 rows | no channel under 25% of its own median for 8 round(s) while the total held"
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


def _thursday(rows: list[tuple[str, int, int]]) -> list[str]:
    return [_stats(f"2026-11-12T{hm}:00+00:00", c, t) for hm, c, t in rows]


def test_dead_air_inside_the_venues_thursday_closure_is_named_not_alarmed() -> None:
    """mistakes #182: the real 2026-11-12 shape. Kalshi shuts 03:00-05:00 ET
    (08:00-10:00Z under EST) and two passes re-derived that by hand."""
    flow = health.parse_stream_flow(
        _thursday(
            [
                ("07:50", 10 * M, 1000),
                ("07:55", 12 * M, 1000),
                ("08:00", 14 * M, 1000),
                ("08:05", 14 * M + 10, 0),
                ("08:10", 14 * M + 20, 0),
                ("10:15", 16 * M, 1000),
                ("10:20", 18 * M, 1000),
            ]
        )
    )
    assert flow is not None and len(flow.dead) == 1
    line = health.stream_flow_line(flow)
    assert "DEAD-AIR" not in line
    assert "VENUE-CLOSED 2 round(s) under 10% in 1 episode(s): 11-12 08:05Z..08:10Z" in line
    assert (
        "-- inside Kalshi's Thursday closure 08:00-10:00Z (venue shut, QA excuses 07-11Z)" in line
    )
    assert line.endswith(
        "(2 inside Kalshi's Thursday closure window) | no channel under 25% of its own median for 8 round(s) while the total held"
    )


def test_dead_air_that_starts_before_the_closure_keeps_the_alarm() -> None:
    # THE MUTANT THIS KILLS: excusing an episode because it TOUCHES the
    # window. Dead from 06:45Z is 15 minutes of open market.
    flow = health.parse_stream_flow(
        _thursday(
            [
                ("06:35", 10 * M, 1000),
                ("06:40", 12 * M, 1000),
                ("06:45", 14 * M, 1000),
                ("06:50", 14 * M + 10, 1000),
                ("07:10", 14 * M + 20, 1000),
                ("07:15", 16 * M, 1000),
                ("07:20", 18 * M, 1000),
            ]
        )
    )
    assert flow is not None and len(flow.dead) == 1
    line = health.stream_flow_line(flow)
    assert "DEAD-AIR 2 round(s)" in line and "VENUE-CLOSED" not in line
    assert "overlaps Kalshi's Thursday closure 08:00-10:00Z but 15.0 min of it is outside" in line


def test_the_same_clock_hours_on_another_day_are_plain_dead_air() -> None:
    assert health.closure_note(
        datetime(2026, 11, 11, 8, 5, tzinfo=UTC), datetime(2026, 11, 11, 8, 10, tzinfo=UTC)
    ) == ("", False)
    # ...and under EDT the hour 09-10Z is past the reopen but inside the pad,
    # while 10:05Z is outside it.
    assert health.closure_note(
        datetime(2026, 9, 17, 9, 5, tzinfo=UTC), datetime(2026, 9, 17, 9, 10, tzinfo=UTC)
    )[1]
    assert not health.closure_note(
        datetime(2026, 9, 17, 9, 55, tzinfo=UTC), datetime(2026, 9, 17, 10, 5, tzinfo=UTC)
    )[1]


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


def _epoch(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()


def test_a_low_round_and_a_dead_tick_inside_a_no_path_span_say_so() -> None:
    """Mistakes #221: the cause sat one digest line above, uncited."""
    flow = health.parse_stream_flow(OUTAGE)
    assert flow is not None and len(flow.dead_ticks) == 2
    # No path 10:27:15Z..10:29:05Z: 110 s of the 10:24:15..10:29:15Z round,
    # ending 10 s before the second dead tick and 5 min after the first.
    net = [(_epoch("2026-11-09T10:27:15"), _epoch("2026-11-09T10:29:05"))]
    line = health.stream_flow_line(flow, net=net)
    assert "20 next; uplink had no path for 1.8 min of the round (HOSTNET))" in line
    assert line.endswith(
        "(1 within 15 s of a HOSTNET no-path span) | no channel under 25% of its own median for 8 round(s) while the total held"
    )
    # A span that ended a minute before the tick is not that tick's cause.
    early = [(_epoch("2026-11-09T10:27:15"), _epoch("2026-11-09T10:28:15"))]
    assert "within 15 s" not in health.stream_flow_line(flow, net=early)


def test_no_span_or_no_cross_reading_claims_nothing() -> None:
    flow = health.parse_stream_flow(OUTAGE)
    ref = health.stream_flow_line(flow)
    assert "HOSTNET" not in ref
    assert health.stream_flow_line(flow, net=[]) == ref
    far = [(_epoch("2026-11-09T09:00:00"), _epoch("2026-11-09T09:01:00"))]
    assert health.stream_flow_line(flow, net=far) == ref


# --- one channel starved while the total held (mistakes #228) -----------------


def _chan_lines(per_round: list[dict[str, int]], start: str = "2026-11-21T15:00:00+00:00"):
    """Stats lines 300 s apart whose per-key DELTAS are `per_round` (missing
    keys repeat the first round's value)."""
    from datetime import timedelta

    t = datetime.fromisoformat(start)
    base = {k: 0 for k in KEYS}
    out = []
    for i, r in enumerate([per_round[0], *per_round]):
        if i:
            for k in KEYS:
                base[k] += r.get(k, per_round[0][k])
        ts = (t + timedelta(seconds=300 * i)).isoformat()
        out.append(f"{ts} {P}{ts[11:19]} stats {dict(base)} (last flush 100000 rows)")
    return out


NORMAL = {
    "kalshi_lifecycle": 1_200,
    "kalshi_trades": 70_000,
    "kalshi_ticker": 750_000,
    "kalshi_books_events": 1_500_000,
    "poly_events": 70_000,
    "poly_trades": 260,
}
# 17:05Z 11-21: books x2.7, ticker to 6-12% -- the total goes UP.
STARVED = {"kalshi_ticker": 60_000, "kalshi_books_events": 4_000_000}


def test_a_channel_starved_under_a_rising_total_is_named_with_its_span() -> None:
    deeper = {**STARVED, "kalshi_ticker": 30_000}
    flow = health.parse_stream_flow(
        _chan_lines([NORMAL] * 14 + [STARVED] * 4 + [deeper] + [STARVED] * 5)
    )
    assert flow is not None and flow.low == () and flow.dead == ()  # the sum saw nothing
    (st,) = flow.starved
    assert st.key == "kalshi_ticker" and st.median == 750_000 and len(st.rounds) == 10
    line = health.stream_flow_line(flow)
    assert line.endswith(
        "| CHANNEL STARVED under 25% of its own median while the total held: kalshi_ticker in "
        "10 round(s) 11-21 16:15Z..17:00Z (median 750,000/round, worst 30,000 at 16:35Z), "
        "STILL at 8% in the newest round (17:00Z) -- count that connection's "
        "`keepalive ping timeout` lines by the minute"
    )


def test_a_starvation_that_ended_says_so_and_a_clean_window_says_what_was_looked_for() -> None:
    flow = health.parse_stream_flow(_chan_lines([NORMAL] * 14 + [STARVED] * 8 + [NORMAL] * 2))
    assert flow is not None
    assert "back to 100% in the newest round (17:00Z)" in health.stream_flow_line(flow)
    clean = health.parse_stream_flow(_chan_lines([NORMAL] * 12))
    assert clean is not None and clean.starved == ()
    assert health.stream_flow_line(clean).endswith(
        "| no channel under 25% of its own median for 8 round(s) while the total held"
    )


def test_fewer_starved_rounds_than_the_floor_and_a_share_at_the_bar_are_not_named() -> None:
    assert health.STREAMFLOW_STARVED_MIN_ROUNDS == 8 and health.STREAMFLOW_STARVED_FRAC == 0.25
    seven = health.parse_stream_flow(_chan_lines([NORMAL] * 14 + [STARVED] * 7))
    assert seven is not None and seven.starved == ()
    at_bar = {"kalshi_ticker": 187_500, "kalshi_books_events": 4_000_000}  # exactly 25%
    flow = health.parse_stream_flow(_chan_lines([NORMAL] * 14 + [at_bar] * 10))
    assert flow is not None and flow.starved == ()


def test_a_thin_channel_is_never_named_and_an_outage_round_is_not_a_starved_channel() -> None:
    assert health.STREAMFLOW_STARVED_MIN_MEDIAN == 5_000
    thin = {"kalshi_lifecycle": 0, "poly_trades": 0}  # medians 1,200 and 260
    flow = health.parse_stream_flow(_chan_lines([NORMAL] * 14 + [thin] * 10))
    assert flow is not None and flow.starved == ()
    # Every channel at 20%: a partial outage, not one channel losing to another.
    out = {k: v // 5 for k, v in NORMAL.items()}
    flow = health.parse_stream_flow(_chan_lines([NORMAL] * 14 + [out] * 10 + [NORMAL] * 14))
    assert flow is not None and flow.starved == ()
    assert flow.low == ()  # ten low rounds are each other's neighbours: no tier names them
