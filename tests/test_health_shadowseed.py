"""SHADOWSEED: the digest names a shadow run that started with its books
unknown (mistakes #196). The seed lines are the journal's own, verbatim."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from collector import health

T = datetime(2026, 11, 14, 4, 29, 36, tzinfo=UTC)

OLD_BLIND = (
    "2026-11-14T14:32:39+00:00 hyz python[736899]: [shadow] seeded books from 452312 archived"
    " events (0 top states; trading starts at 2026-11-14 14:32:36.000753)"
)
OLD_PARTIAL = (
    "2026-11-14T04:29:36+00:00 hyz python[798]: [shadow] seeded books from 4062 archived events"
    " (118 top states; trading starts at 2026-11-14 04:29:20.882784)"
)
NEW_WHOLE = (
    "2026-11-14T14:55:13+00:00 hyz python[949327]: [shadow] seeded books from 3898917 archived"
    " events since 2026-11-14 04:28:06.005508 (332918 top states, 20 shard(s), 72 gap row(s)"
    " replayed; trading starts at 2026-11-14 14:54:55.328607)"
)
NEW_CAPPED = (
    "2026-11-16T14:55:13+00:00 hyz python[5]: [shadow] seeded books from 900000 archived"
    " events since 2026-11-15 14:55:00 (200000 top states, 20 shard(s), 72 gap row(s)"
    " replayed; shard(s) [5, 14] last re-imaged over 24h ago, unknown until they reconnect;"
    " trading starts at 2026-11-16 14:54:55.328607)"
)


def _stat(ts: datetime, pid: int, snaps: int, events: int) -> str:
    return (
        f"{ts:%Y-%m-%dT%H:%M:%S}+00:00 hyz python[{pid}]: [shadow] {{'snapshots': {snaps},"
        f" 'events': {events}, 'polls': 9}} fills=1 rss_anon_mb=1.0 stream_holds={{}}"
    )


def _stats(start: datetime, pid: int, hourly_yield: list[float]) -> list[str]:
    """Twelve lines an hour, 10,000 events a line, at each hour's yield."""
    out, snaps, events = [_stat(start + timedelta(minutes=3), pid, 0, 0)], 0, 0
    for h, y in enumerate(hourly_yield):
        for i in range(12):
            events += 10_000
            snaps += int(10_000 * y)
            out.append(
                _stat(start + timedelta(hours=h, minutes=3 + 5 * (i + 1)), pid, snaps, events)
            )
    return out


def test_a_large_seed_with_no_top_state_is_blind_at_once() -> None:
    seed = health.parse_shadow_seed([OLD_BLIND])
    assert (seed.events, seed.top_states, seed.shards) == (452312, 0, None)
    assert seed.verdict == "BLIND"
    assert health.shadow_seed_line(seed).startswith("BLIND -- ")


def test_a_yield_that_climbs_from_its_first_hour_is_a_blind_start() -> None:
    """The 11-14 04:29Z run: 118 top states at the seed, 0.7% of events in
    its first hour, 4-12% by the tenth as the stream's shards reconnected."""
    lines = [OLD_PARTIAL, *_stats(T, 798, [0.007, 0.01, 0.03, 0.012, 0.04, 0.08, 0.10, 0.12])]
    seed = health.parse_shadow_seed(lines)
    assert seed.verdict == "BLIND-START"
    assert "first hour 0.7%" in health.shadow_seed_line(seed)


def test_a_whole_start_with_a_flat_yield_is_ok_and_noise_is_not_a_climb() -> None:
    at = datetime(2026, 11, 14, 14, 55, 13, tzinfo=UTC)
    seed = health.parse_shadow_seed([NEW_WHOLE, *_stats(at, 949327, [0.10, 0.12, 0.064, 0.12])])
    assert (seed.shards, seed.gap_rows, seed.capped_shards) == (20, 72, ())
    assert seed.verdict == "ok"
    assert not health.shadow_seed_line(seed).startswith(("BLIND", "CAPPED"))


def test_a_run_too_young_for_a_second_hour_is_not_judged_on_yield() -> None:
    at = datetime(2026, 11, 14, 14, 55, 13, tzinfo=UTC)
    seed = health.parse_shadow_seed([NEW_WHOLE, *_stats(at, 949327, [0.10])])
    assert seed.first_hour is not None and seed.last_hour is None
    assert seed.verdict == "ok"
    assert "newest hour --" in health.shadow_seed_line(seed)


def test_shards_past_the_lookback_are_named() -> None:
    seed = health.parse_shadow_seed([NEW_CAPPED])
    assert seed.capped_shards == (5, 14)
    assert seed.verdict == "CAPPED"
    assert "[5, 14]" in health.shadow_seed_line(seed)


def test_the_newest_start_is_the_subject_and_another_runs_lines_do_not_count() -> None:
    at = datetime(2026, 11, 14, 14, 55, 13, tzinfo=UTC)
    lines = [
        OLD_PARTIAL,
        *_stats(T, 798, [0.007, 0.01, 0.10]),
        NEW_WHOLE,
        _stat(at + timedelta(minutes=1), 798, 999_999, 1_000_000),  # a stale process's line
        *_stats(at, 949327, [0.10, 0.10, 0.10]),
    ]
    seed = health.parse_shadow_seed(lines)
    assert seed.events == 3898917
    assert seed.first_hour == (11_000, 110_000)  # lines at +3..+58 min
    assert seed.verdict == "ok"


def test_no_seed_line_and_an_unreadable_journal_are_absences() -> None:
    assert health.parse_shadow_seed(["-- No entries --"]) is None
    assert "no seed line" in health.shadow_seed_line(None)
    assert "unreadable" in health.shadow_seed_line("unreadable")


def test_rendered_line_moves_with_every_field() -> None:
    """#95: a field parsed and dropped at the renderer is a reading nobody gets."""
    base = health.ShadowSeed(T, 1000, 50, 20, 72, (3,), (10, 1000), (20, 1000))
    line = health.shadow_seed_line(base)
    for v in (
        replace(base, at=T + timedelta(hours=1)),
        replace(base, events=1001),
        replace(base, top_states=51),
        replace(base, shards=19),
        replace(base, gap_rows=73),
        replace(base, capped_shards=(4,)),
        replace(base, first_hour=(11, 1000)),
        replace(base, last_hour=(20, 1001)),
    ):
        assert health.shadow_seed_line(v) != line


def test_a_first_hour_that_yields_almost_nothing_is_blind_before_there_is_a_second() -> None:
    """The 11-14 02:30Z run: 165 top states at the seed, then 14 snapshots
    from 292,416 events. It lived two hours; a verdict that waits for a
    second hour to compare never reaches a run like it."""
    at = datetime(2026, 11, 14, 2, 30, 37, tzinfo=UTC)
    seed_line = OLD_PARTIAL.replace("04:29:36", "02:30:37").replace("[798]", "[191745]")
    seed = health.parse_shadow_seed([seed_line, *_stats(at, 191745, [0.0005])])
    assert seed.last_hour is None
    assert seed.verdict == "BLIND-START"


def test_a_thin_first_span_is_not_judged() -> None:
    at = datetime(2026, 11, 14, 14, 55, 13, tzinfo=UTC)
    lines = [NEW_WHOLE, _stat(at + timedelta(minutes=3), 949327, 0, 0)]
    lines.append(_stat(at + timedelta(minutes=8), 949327, 10, 20_000))  # 0.05%, 20k events
    assert health.parse_shadow_seed(lines).verdict == "ok"
