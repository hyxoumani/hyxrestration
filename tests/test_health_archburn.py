"""The ARCHBURN digest line: how much of the disk's burn is our own archives.

HOSTFS reads the filesystem's net fall and cannot say whose it is. On
2026-11-22 a pass read free space going flat after the raw-frame copy
yielded and called the raw copy the whole burn; a sibling job was handing
space back while the stream archive kept growing 0.4 GB/h (mistakes #234).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from collector import health

NOW = datetime(2026, 11, 22, 14, 40, tzinfo=UTC)
GB = 10**9


def _pts(live: int, *slots: tuple[float, int]) -> list[tuple[datetime, int]]:
    return [(NOW, live)] + [(NOW - timedelta(hours=h), size) for h, size in slots]


def test_growth_is_read_against_the_newest_slot() -> None:
    # THE MUTANT THIS KILLS: min(slots) -- a week of growth over a week is
    # the average, and the day the rate changes is the day it is read.
    b = health.archive_burn("a.duckdb", _pts(430 * GB, (11.0, 426 * GB), (35.0, 410 * GB)), NOW)
    assert b.hours == pytest.approx(11.0)
    assert b.grew_b == 4 * GB


def test_a_slot_too_young_or_absent_is_unresolved_not_zero() -> None:
    young = health.archive_burn("a.duckdb", _pts(430 * GB, (0.5, 429 * GB)), NOW)
    assert young.grew_b is None and young.hours == pytest.approx(0.5)
    assert health.archive_burn("a.duckdb", _pts(430 * GB), NOW) == health.ArchiveBurn("a.duckdb")
    no_live = [(NOW - timedelta(hours=5), 400 * GB)]
    assert health.archive_burn("a.duckdb", no_live, NOW) == health.ArchiveBurn("a.duckdb")
    line = health.archive_burn_line([young, health.ArchiveBurn("b.duckdb")], 4 * GB)
    assert "a.duckdb unresolved (newest slot only 30 min old)" in line
    assert "b.duckdb unresolved (no backup slot or live file readable)" in line
    assert "ours" not in line and "fills" not in line  # nothing measured, nothing summed


def test_the_line_sums_the_rates_and_prices_the_free_space() -> None:
    burns = [
        health.ArchiveBurn("hyxstream.duckdb", 10.0, 4 * GB),
        health.ArchiveBurn("hyxlab.duckdb", 10.0, 1 * GB),
    ]
    line = health.archive_burn_line(burns, 5 * GB)
    assert "hyxstream.duckdb +4.00 GB in 10.0 h (+0.40 GB/h)" in line
    assert "hyxlab.duckdb +1.00 GB in 10.0 h (+0.10 GB/h)" in line
    assert "ours 0.50 GB/h = 12.0 GB/day" in line
    assert "alone it fills the 5.0 GB free in 10.0 h" in line
    assert "lower bound" in line
    # #95: every input moves the rendered line.
    seen = {line}
    for i in range(2):
        for bump in ({"hours": 20.0}, {"grew_b": 8 * GB}, {"name": "x.duckdb"}):
            changed = list(burns)
            changed[i] = health.ArchiveBurn(**{**burns[i].__dict__, **bump})
            seen.add(health.archive_burn_line(changed, 5 * GB))
    seen.add(health.archive_burn_line(burns, 6 * GB))
    assert len(seen) == 8
    assert health.archive_burn_line(burns, None).endswith("free space unread")


def test_an_unread_archive_is_named_and_the_sum_says_it_is_partial() -> None:
    burns = [
        health.ArchiveBurn("hyxstream.duckdb", 10.0, 4 * GB),
        health.ArchiveBurn("hyxlab.duckdb"),
    ]
    line = health.archive_burn_line(burns, 4 * GB)
    assert "ours 0.40 GB/h = 9.6 GB/day (1 archive(s) left out)" in line
    assert "fills the 4.0 GB free in 10.0 h" in line


def test_a_shrinking_archive_prints_no_time_to_full() -> None:
    line = health.archive_burn_line([health.ArchiveBurn("a.duckdb", 10.0, -2 * GB)], 4 * GB)
    assert "a.duckdb -2.00 GB in 10.0 h (-0.20 GB/h)" in line
    assert line.endswith("not growing") and "fills" not in line


def test_each_archive_is_read_against_its_own_slots(tmp_path) -> None:
    slots = tmp_path / "slots"
    slots.mkdir()
    sizes = {"hyxstream": (900, 700, 400), "hyxlab": (300, 250, 100)}
    dbs = []
    for stem, (live, sun, sat) in sizes.items():
        p = tmp_path / f"{stem}.duckdb"
        p.write_bytes(b"x" * live)
        dbs.append(str(p))
        for day, size, age_h in (("Sun", sun, 10), ("Sat", sat, 34)):
            s = slots / f"{stem}.{day}.duckdb"
            s.write_bytes(b"x" * size)
            ts = (NOW - timedelta(hours=age_h)).timestamp()
            os.utime(s, (ts, ts))
    got = health.read_archive_burns(NOW, tuple(dbs), slots)
    assert [(b.name, b.grew_b) for b in got] == [("hyxstream.duckdb", 200), ("hyxlab.duckdb", 50)]
    assert all(b.hours == pytest.approx(10.0) for b in got)


def test_the_digest_prints_the_row_from_the_one_renderer(capsys, monkeypatch) -> None:
    # The live reader on the real tree (or its absence) never raises.
    assert len(health.read_archive_burns(NOW)) == len(health.ARCHBURN_DBS)
    burns = [health.ArchiveBurn("hyxstream.duckdb", 10.0, 4 * GB)]
    monkeypatch.setattr(health, "read_archive_burns", lambda now: burns)
    monkeypatch.setattr(health, "read_kernel_oom_window", lambda: None)
    monkeypatch.setattr(health, "read_hostfs", lambda: health.HostFs(8 * GB, 2000 * GB))
    monkeypatch.setattr(health, "read_kubelet_window", lambda: None)
    health.main()
    rows = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    names = [ln.split()[0] for ln in rows]
    assert names.count("ARCHBURN") == 1
    i = names.index("ARCHBURN")
    assert (names[i - 1], names[i + 1]) == ("ENOSPC", "RAWFRAMES")
    assert rows[i].startswith("ARCHBURN   (host)")
    # The free space is the HOSTFS reading of the same run, not a second stat.
    assert health.archive_burn_line(burns, 8 * GB) in rows[i]
    assert "fills the 8.0 GB free in 20.0 h" in rows[i]
