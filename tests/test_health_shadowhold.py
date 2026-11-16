"""The SHADOWHOLD digest line: the shadow's hold on the stream archive and
its climb, and the stream's flush collisions by holder.

Fixture lines are the real `journalctl -o short-iso --utc` shapes of
2026-11-09: the shadow's 300 s line carries cumulative `held_n` /
`held_s_total`; streamd's `flush FAILED` line names the holder's PID.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from collector import health

NOW = datetime(2026, 11, 9, 20, 15, tzinfo=UTC)


def _hold(ts: datetime, pid: int, n: int, held_s: float) -> str:
    holds = (
        f'{{"n": {n + 10}, "db": "hyxstream.duckdb", "retained_n": 123, "held_n": {n}, '
        f'"held_unknown_n": 0, "held_s_max": 5.458, "held_s_total": {held_s:.3f}}}'
    )
    return (
        f"{ts:%Y-%m-%dT%H:%M:%S}+00:00 hyz python[{pid}]: [shadow] {{'snapshots': 1, "
        f"'events': 2, 'polls': {n}}} fills=40387 rss_anon_mb=179.8 stream_holds={holds}"
    )


def _fail(ts: datetime, pid: int) -> str:
    return (
        f"{ts:%Y-%m-%dT%H:%M:%S}+00:00 hyz python[840252]: [streamd] {ts:%H:%M:%S} flush FAILED "
        '(IOException: IO Error: Could not set lock on file "/x/hyxstream.duckdb": Conflicting '
        f"lock is held in /usr/bin/python3.14 (PID {pid}) by user devs. However, you would be "
        "able to open this database in read-only mode); 124898 rows held for retry in 2s"
    )


def _run(start: datetime, hours: float, per_poll: float, pid: int, n0: int = 0) -> list[str]:
    """A shadow run: one line per 300 s, 15 polls a line, a constant hold per poll."""
    out = []
    for i in range(int(hours * 12) + 1):
        n = n0 + 15 * i
        out.append(_hold(start + timedelta(seconds=300 * i), pid, n, n * per_poll))
    return out


def test_climb_is_recent_over_oldest_and_collisions_split_by_holder() -> None:
    old = _run(NOW - timedelta(hours=168), 6, 0.275, 700)
    recent = _run(NOW - timedelta(hours=6), 6, 0.458, 798)
    fails = [_fail(NOW - timedelta(minutes=m), 798) for m in (5, 55, 300)] + [
        _fail(NOW - timedelta(hours=3), 3586240),
        "-- No entries --",
    ]
    hold = health.parse_shadow_hold(old + recent + ["-- No entries --"], fails, NOW)
    assert hold is not None
    assert hold.recent.polls == 15 * 72 and hold.recent.per_poll_s == pytest.approx(0.458)
    assert hold.recent.duty == pytest.approx(0.458 * 15 / 300)
    assert hold.oldest is not None and hold.oldest.per_poll_s == pytest.approx(0.275)
    assert hold.climb == pytest.approx(0.458 / 0.275)
    assert hold.pids == {700, 798}
    assert (hold.collisions_shadow, hold.collisions_other) == (3, 1)
    line = health.shadow_hold_line(hold)
    assert line.startswith(
        "hold on hyxstream.duckdb: 0.458 s/poll over 1,080 polls in the last 6h (2.3% duty) | "
        "0.275 s/poll at 11-02 20:15Z, x1.67 = CLIMB (+26 ms/poll/day); "
        "archive size over the same spans unread | "
        "stream flush FAILED 24h: 4 (shadow 3, other 1)"
    )


def test_a_restart_inside_the_span_yields_no_poll_and_the_rest_still_reads() -> None:
    first = _run(NOW - timedelta(hours=6), 3, 0.4, 700, n0=20_000)
    second = _run(NOW - timedelta(hours=3), 3, 0.4, 798)  # counters fall to 0
    hold = health.parse_shadow_hold(first + second, [], NOW)
    assert hold is not None
    assert hold.recent.polls == 15 * 72  # the falling pair contributes nothing
    assert hold.recent.per_poll_s == pytest.approx(0.4)
    assert hold.oldest is None  # the whole window is one recent span
    assert "no span 6h earlier" in health.shadow_hold_line(hold)


def test_flat_hold_is_not_a_climb() -> None:
    old = _run(NOW - timedelta(hours=48), 6, 0.30, 798)
    recent = _run(NOW - timedelta(hours=6), 6, 0.33, 798, n0=100_000)
    hold = health.parse_shadow_hold(old + recent, [], NOW)
    assert hold is not None and hold.climb == pytest.approx(1.1)
    assert "x1.10 = flat" in health.shadow_hold_line(hold)


def test_empty_and_unreadable_render_as_absences_not_zero() -> None:
    assert health.parse_shadow_hold(["-- No entries --"], [], NOW) is None
    stale = _run(NOW - timedelta(hours=30), 6, 0.3, 798)
    assert health.parse_shadow_hold(stale, [], NOW) is None  # nothing in the recent 6h
    assert "no pair of stream_holds lines" in health.shadow_hold_line(None)
    assert health.shadow_hold_line("unreadable") == "168h: journal unreadable"


def test_rendered_line_moves_with_every_field() -> None:
    """#95: a field read into the block and dropped at the renderer is unrecoverable."""
    base = health.ShadowHold(
        health.HoldReading(NOW - timedelta(hours=6), NOW, 1000, 400.0),
        health.HoldReading(NOW - timedelta(hours=168), NOW - timedelta(hours=162), 1000, 300.0),
        frozenset({798}),
        10,
        2,
    )
    line = health.shadow_hold_line(base)
    from dataclasses import replace

    variants = [
        replace(base, recent=replace(base.recent, polls=1100)),
        replace(base, recent=replace(base.recent, held_s=450.0)),
        replace(base, recent=replace(base.recent, start=NOW - timedelta(hours=5))),
        replace(base, oldest=replace(base.oldest, held_s=200.0)),
        replace(base, oldest=replace(base.oldest, start=NOW - timedelta(hours=160))),
        replace(base, collisions_shadow=11),
        replace(base, collisions_other=3),
        replace(base, recent=replace(base.recent, worst_s=90.0, worst_polls=5, worst_at=NOW)),
    ]
    ev = replace(base, recent=replace(base.recent, worst_s=90.0, worst_polls=5, worst_at=NOW))
    ev_line = health.shadow_hold_line(ev)
    for v in (
        replace(ev, recent=replace(ev.recent, worst_s=95.0)),
        replace(ev, recent=replace(ev.recent, worst_polls=50)),
        replace(ev, recent=replace(ev.recent, worst_at=NOW - timedelta(hours=1))),
        replace(ev, oldest=replace(ev.oldest, worst_s=90.0, worst_polls=5, worst_at=NOW)),
    ):
        assert health.shadow_hold_line(v) != ev_line
    for v in variants:
        assert health.shadow_hold_line(v) != line


def test_one_long_hold_is_named_and_does_not_read_as_a_climb() -> None:
    """Mistakes #198's reading: `0.752 s/poll, x1.57 = CLIMB` at 11-15 02:15Z
    was one 64.6 s hold under the host's OOM inside a flat 0.56 s span."""
    old = _run(NOW - timedelta(hours=48), 6, 0.48, 798)
    recent = []
    for i in range(73):  # one line per 300 s, 5 polls a line (the 60 s poll)
        n = 100_000 + 5 * i
        held = 48_000.0 + 5 * i * 0.56 + (64.0 if i >= 20 else 0.0)
        recent.append(_hold(NOW - timedelta(hours=6) + timedelta(seconds=300 * i), 798, n, held))
    hold = health.parse_shadow_hold(old + recent, [], NOW)
    assert hold is not None and hold.recent.event
    assert hold.recent.per_poll_s == pytest.approx((360 * 0.56 + 64.0) / 360)  # 0.738
    assert hold.recent.steady_per_poll_s == pytest.approx(0.56)
    assert hold.recent.worst_at == NOW - timedelta(hours=6) + timedelta(seconds=300 * 20)
    assert hold.climb == pytest.approx(0.56 / 0.48)
    line = health.shadow_hold_line(hold)
    assert "0.738 s/poll over 360 polls" in line
    assert (
        "; one line carried 66.8 s of the 265.6 s (11-09 15:55Z), 0.560 s/poll without it" in line
    )
    assert "x1.17 = flat" in line


def test_a_uniform_span_names_no_event() -> None:
    hold = health.parse_shadow_hold(_run(NOW - timedelta(hours=6), 6, 0.458, 798), [], NOW)
    assert hold is not None and not hold.recent.event
    assert hold.recent.steady_per_poll_s == hold.recent.per_poll_s
    assert "one line carried" not in health.shadow_hold_line(hold)


# --- the climb against the archive's own growth (2026-11-16) -----------------

GB = 10**9


def _base(archive: health.ArchiveGrowth | None, recent_held: float = 400.0) -> health.ShadowHold:
    return health.ShadowHold(
        health.HoldReading(NOW - timedelta(hours=6), NOW, 1000, recent_held),
        health.HoldReading(NOW - timedelta(hours=168), NOW - timedelta(hours=162), 1000, 300.0),
        frozenset({798}),
        10,
        2,
        archive,
    )


def test_archive_size_is_interpolated_and_extrapolated_only_within_reach() -> None:
    t0 = NOW - timedelta(days=6)
    pts = [(t0 + timedelta(days=1), 110 * GB), (t0, 100 * GB), (NOW, 160 * GB)]
    assert health.archive_size_at(pts, t0 + timedelta(hours=12)) == pytest.approx(105 * GB)
    assert health.archive_size_at(pts, t0) == pytest.approx(100 * GB)
    assert health.archive_size_at(pts, NOW) == pytest.approx(160 * GB)
    # before the oldest slot: along the first two points, 10 GB/day
    assert health.archive_size_at(pts, t0 - timedelta(hours=12)) == pytest.approx(95 * GB)
    assert health.archive_size_at(pts, t0 - timedelta(hours=25)) is None
    assert health.archive_size_at(pts, NOW + timedelta(hours=25)) is None
    assert health.archive_size_at(pts[:1], t0) is None
    assert health.archive_size_at([], t0) is None


def test_growth_is_priced_at_each_span_midpoint() -> None:
    hold = _base(None)
    t0 = NOW - timedelta(hours=168)
    pts = [(t0, 100 * GB), (NOW, 268 * GB)]  # 1 GB/h
    g = health.archive_growth(pts, hold.oldest, hold.recent)
    assert g is not None
    assert g.then_b == pytest.approx(103 * GB)
    assert g.now_b == pytest.approx(265 * GB)
    assert health.archive_growth(pts, None, hold.recent) is None
    assert health.archive_growth(pts[:1], hold.oldest, hold.recent) is None


def test_a_climb_the_file_matches_tracks_the_archive_and_one_it_does_not_is_beyond() -> None:
    # hold x1.33 on both; the file x1.30 in one and flat in the other
    tracks = health.shadow_hold_line(_base(health.ArchiveGrowth(270 * GB, 351 * GB)))
    assert "x1.33 = CLIMB" in tracks
    assert "; tracks the archive: file 270 -> 351 GB x1.30, hold per GB x1.03 |" in tracks
    assert "BEYOND" not in tracks
    beyond = health.shadow_hold_line(_base(health.ArchiveGrowth(270 * GB, 272 * GB)))
    assert "; BEYOND THE ARCHIVE: file 270 -> 272 GB x1.01, hold per GB x1.32 |" in beyond
    # the boundary is the constant, not a literal
    edge = 4.0 / 3.0 / health.SHADOWHOLD_BEYOND_RATIO
    assert "BEYOND" in health.shadow_hold_line(
        _base(health.ArchiveGrowth(270 * GB, 270 * GB * edge * 0.999))
    )
    assert "tracks" in health.shadow_hold_line(
        _base(health.ArchiveGrowth(270 * GB, 270 * GB * edge * 1.001))
    )


def test_an_unread_size_says_so_and_a_flat_hold_is_never_beyond() -> None:
    assert "; archive size over the same spans unread |" in health.shadow_hold_line(_base(None))
    # hold flat while the file SHRANK (a retention): per GB is up, but nothing climbed
    flat = health.shadow_hold_line(_base(health.ArchiveGrowth(400 * GB, 200 * GB), 300.0))
    assert "= flat" in flat and "BEYOND" not in flat and "tracks" not in flat
    assert "; archive: file 400 -> 200 GB x0.50, hold per GB x2.00 |" in flat


def test_archive_fields_reach_the_rendered_line() -> None:
    from dataclasses import replace

    base = _base(health.ArchiveGrowth(270 * GB, 351 * GB))
    line = health.shadow_hold_line(base)
    for v in (
        replace(base, archive=health.ArchiveGrowth(280 * GB, 351 * GB)),
        replace(base, archive=health.ArchiveGrowth(270 * GB, 361 * GB)),
        replace(base, archive=None),
    ):
        assert health.shadow_hold_line(v) != line


def test_sizes_are_stat_of_the_live_file_now_and_each_slot_at_its_mtime(tmp_path) -> None:
    import os

    live = tmp_path / "hyxstream.duckdb"
    live.write_bytes(b"x" * 500)
    slots = tmp_path / "slots"
    slots.mkdir()
    for day, size, age_d in (("Mon", 400, 1), ("Sun", 300, 2)):
        p = slots / f"hyxstream.{day}.duckdb"
        p.write_bytes(b"x" * size)
        ts = (NOW - timedelta(days=age_d)).timestamp()
        os.utime(p, (ts, ts))
    (slots / "hyxlab.Mon.duckdb").write_bytes(b"x" * 999)  # another archive's slot
    (slots / "hyxstream.Mon.duckdb.tmp").mkdir()  # a spill directory, not a slot
    pts = sorted(health.read_archive_sizes(NOW, live, slots))
    assert pts == [
        (NOW - timedelta(days=2), 300),
        (NOW - timedelta(days=1), 400),
        (NOW, 500),
    ]
    # a missing live file leaves the slots; a missing directory leaves nothing
    assert len(health.read_archive_sizes(NOW, tmp_path / "absent.duckdb", slots)) == 0
    assert health.read_archive_sizes(NOW, live, tmp_path / "nowhere") == [(NOW, 500)]


def test_the_slot_directory_falls_back_to_the_backup_units_own_setting(monkeypatch) -> None:
    """The digest is run by hand and by the autoloop, neither with the
    variable set; the unit file is the one place the directory is written."""
    import re

    text = (health.REPO / health.SHADOWHOLD_BACKUP_UNIT).read_text()
    assert re.search(r"^Environment=HYXLAB_BACKUP_DIR=\S+", text, re.M)
    monkeypatch.delenv("HYXLAB_BACKUP_DIR", raising=False)
    seen: list[str] = []
    real_glob = health.Path.glob

    def glob(self, pattern):
        seen.append(str(self))
        return real_glob(self, pattern)

    monkeypatch.setattr(health.Path, "glob", glob)
    health.read_archive_sizes(NOW, "data/absent-for-test.duckdb")
    want = re.search(r"^Environment=HYXLAB_BACKUP_DIR=(\S+)", text, re.M).group(1)
    assert seen == [want]
