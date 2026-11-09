"""The STREAMFLOW digest line: did the stream LAND anything, round by round.

Fixture lines are the real `journalctl -o short-iso --utc` shapes from the
2026-11-09 router outage (mistakes #168): two consecutive stats lines at
`flushed 20` against a ~106k median, GAP lines on the reconnect.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from collector import health

P = "hyz python[98880]: [streamd] "


def _stats(ts: str, flushed: int) -> str:
    return (
        f"{ts} {P}{ts[11:19]} stats {{'kalshi_lifecycle': 1}} (flushed {flushed} this round) "
        "rss_anon_mb=390.9 arena_mb=93.3/150 sock_throttled=0 (+0)"
    )


def _gap(ts: str, shard: int) -> str:
    return f"{ts} {P}{ts[11:19]} kalshi/books GAP (reconnect) shard={shard}"


OUTAGE = [
    _stats("2026-11-09T10:08:42+00:00", 93411),
    _stats("2026-11-09T10:13:48+00:00", 70676),
    _stats("2026-11-09T10:18:57+00:00", 86521),
    _stats("2026-11-09T10:24:05+00:00", 20),
    *[_gap(f"2026-11-09T10:25:{44 + i % 10:02d}+00:00", i) for i in range(19)],
    _stats("2026-11-09T10:29:15+00:00", 20),
    *[_gap(f"2026-11-09T10:29:{20 + i % 30:02d}+00:00", i) for i in range(20)],
    _stats("2026-11-09T10:34:23+00:00", 70650),
    _stats("2026-11-09T10:39:38+00:00", 72951),
    "-- No entries --",
]


def test_two_consecutive_dead_rounds_are_one_episode_with_both_bounds() -> None:
    flow = health.parse_stream_flow(OUTAGE)
    assert flow is not None
    assert flow.rounds == 7 and flow.median == 70676
    assert flow.sample_gap_max_s == pytest.approx(315.0)
    assert len(flow.dead) == 1
    ep = flow.dead[0]
    assert [r.flushed for r in ep.rounds] == [20, 20]
    # Proven: 10:24:05 -> 10:29:15; possible: one period either side.
    assert ep.dead_lower_s == pytest.approx(310.0)
    assert ep.dead_upper_s == pytest.approx(910.0)
    # 19 GAPs in the second round's period + 20 in the period after it.
    assert ep.rounds[0].gaps_after == 19 and ep.rounds[1].gaps_in_period == 19
    assert ep.rounds[1].gaps_after == 20 and ep.gaps == 39
    line = health.stream_flow_line(flow)
    assert line.startswith(
        "24h: 7 rounds, median 70,676 rows/round (stats every 300 s nominal, widest 315 s)"
    )
    assert (
        "DEAD-AIR 2 round(s) under 10% in 1 episode(s): 11-09 10:24Z..10:29Z 2 round(s) (20, 20 rows)"
        in line
    )
    assert "dead air 5.2 min..15.2 min, GAP lines 39" in line


def test_a_landed_round_between_dead_rounds_splits_the_episodes() -> None:
    lines = [
        _stats("2026-11-09T10:00:00+00:00", 100_000),
        _stats("2026-11-09T10:05:00+00:00", 10),
        _stats("2026-11-09T10:10:00+00:00", 100_000),
        _stats("2026-11-09T10:15:00+00:00", 10),
        _stats("2026-11-09T10:20:00+00:00", 100_000),
    ]
    flow = health.parse_stream_flow(lines)
    assert flow is not None and len(flow.dead) == 2
    # A single dead round proves nothing about the span, only the period bounds it.
    assert flow.dead[0].dead_lower_s == 0.0 and flow.dead[0].dead_upper_s == 600.0
    assert "2 episode(s)" in health.stream_flow_line(flow)
    assert "dead air 0.0 min..10.0 min" in health.stream_flow_line(flow)


def test_the_floor_is_a_fraction_of_the_median_not_a_constant() -> None:
    # A quiet weekend night at 2k rows/round is not dead air.
    quiet = [_stats(f"2026-11-09T03:{m:02d}:00+00:00", 2_000) for m in range(0, 60, 5)]
    flow = health.parse_stream_flow(quiet)
    assert flow is not None and flow.dead == ()
    assert health.stream_flow_line(flow).endswith("| no round under 10% of the median")
    # ... but 10% of that median still is.
    flow2 = health.parse_stream_flow([*quiet, _stats("2026-11-09T04:00:00+00:00", 150)])
    assert flow2 is not None and len(flow2.dead) == 1


def test_rounds_before_the_daemon_start_floor_are_dropped() -> None:
    floor = datetime(2026, 11, 9, 10, 20, tzinfo=UTC)
    flow = health.parse_stream_flow(OUTAGE, floor=floor)
    assert flow is not None and flow.rounds == 4
    assert health.parse_stream_flow(OUTAGE, floor=datetime(2026, 11, 10, tzinfo=UTC)) is None


def test_empty_and_unreadable_journals_are_different_lines() -> None:
    assert health.parse_stream_flow("-- No entries --\n") is None
    assert health.stream_flow_line(None) == (
        "24h: no stats lines (unit down the whole window, or the journal rotated)"
    )
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
    assert isinstance(flow, health.StreamFlow) and flow.rounds == 7


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
    assert "STREAMFLOW hyxlab-stream.service" in out and "24h: 7 rounds, median 70,676 rows/round" in out
    assert "DEAD-AIR 2 round(s)" in out


def test_every_field_of_the_reading_perturbs_the_line() -> None:
    """#95: a field read into a local and dropped renders identically."""
    base = health.parse_stream_flow(OUTAGE)
    assert base is not None
    ref = health.stream_flow_line(base)
    ep = base.dead[0]
    variants = [
        health.StreamFlow(base.rounds + 1, base.median, base.sample_gap_max_s, base.dead),
        health.StreamFlow(base.rounds, base.median + 1, base.sample_gap_max_s, base.dead),
        health.StreamFlow(base.rounds, base.median, 999.0, base.dead),
        health.StreamFlow(base.rounds, base.median, base.sample_gap_max_s, ()),
        health.StreamFlow(
            base.rounds,
            base.median,
            base.sample_gap_max_s,
            (health.DeadEpisode(ep.rounds[:1], ep.period_s),),
        ),
    ]
    for v in variants:
        assert health.stream_flow_line(v) != ref
