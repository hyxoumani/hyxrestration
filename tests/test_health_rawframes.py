"""The RAWFRAMES digest line: the raw-frame burn by CHANNEL against its own past.

The 2026-11-07 ticker-channel doubling (5.6-6.0 -> 10.6 / 11.8 GB/day) was
read by hand -- a `find -printf` over `data/raw_frames` -- on four consecutive
autoloop passes before the channel was named, while QA's headroom model
carried the day as one number and HOSTFS carried the filesystem's net. These
tests pin the line's two halves (newest complete day vs the median of the
prior week; the running day's CLOSED hours vs the same hour files one day
and one week back), every unread state, and -- by perturbation, #95 -- that
every byte the reader stats moves the rendered line.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from collector import health

NOW = datetime(2026, 11, 9, 8, 15, tzinfo=UTC)
CHANNELS = ("books", "ticker", "trades", "lifecycle")


def _day(root: Path, day: str, per_hour: dict[str, int], hours: int = 24) -> None:
    """Write `hours` hour files per channel, each `per_hour[channel]` bytes."""
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    for hh in range(hours):
        for ch, n in per_hour.items():
            (d / f"{hh:02d}-{ch}.jsonl.zst").write_bytes(b"\0" * n)


def _week(root: Path, ticker_mb: int = 10, books_mb: int = 20) -> None:
    """Seven complete days before 11-08 at a flat per-hour rate (GB-scale,
    written as MB-scale files: 1 MB/h x 24 h = 24 MB/day per channel)."""
    for back in range(2, 9):  # 11-01 .. 11-07
        day = (NOW - timedelta(days=back)).strftime("%Y-%m-%d")
        _day(
            root, day, {"books": books_mb * 10**6, "ticker": ticker_mb * 10**6, "trades": 3 * 10**6}
        )


def test_the_reader_stats_every_frame_file_and_ignores_strangers(tmp_path: Path) -> None:
    _day(tmp_path, "2026-11-08", {"books": 100, "ticker": 50}, hours=3)
    (tmp_path / "2026-11-08" / "notes.txt").write_bytes(b"x" * 999)
    (tmp_path / "2026-11-08" / "07-books.jsonl").write_bytes(b"x" * 999)  # not .zst
    day = health.read_raw_day(tmp_path / "2026-11-08")
    assert day is not None and day.day == "2026-11-08"
    assert day.by_channel == {"books": 300, "ticker": 150}
    assert day.by_hour[("02", "ticker")] == 50 and day.total_b == 450
    assert day.hours_subset({"00", "01"}) == {"books": 200, "ticker": 100}
    assert health.read_raw_day(tmp_path / "missing") is None


def test_an_unreadable_tree_says_unread_not_zero(tmp_path: Path) -> None:
    assert health.read_raw_frames(tmp_path / "nope") is None
    line = health.raw_frames_line(None, NOW)
    assert line.startswith(f"{health.RAW_FRAMES_DIR} UNREAD") and "0.0" not in line
    assert health.read_raw_frames(tmp_path) == []
    assert (
        health.raw_frames_line([], NOW)
        == "no complete day held (0 day dir(s)) | today 11-09: no hour file yet"
    )


def test_a_doubled_channel_is_named_drift_and_a_flat_one_is_not(tmp_path: Path) -> None:
    _week(tmp_path)
    # 11-08: ticker doubled, books +10%, trades flat.
    _day(tmp_path, "2026-11-08", {"books": 22 * 10**6, "ticker": 20 * 10**6, "trades": 3 * 10**6})
    line = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW)
    head, tail = line.split(" | ")
    assert head.startswith("11-08: 1.1 GB (books 0.5, ticker 0.5, trades 0.1)")
    assert "vs median of 7 prior day(s) 0.8 GB (books 0.5, ticker 0.2, trades 0.1)" in head
    assert head.endswith("-- STEP over +50%: ticker +100%")
    assert "books +" not in head
    assert tail == "today 11-09: no hour file yet"
    # Flat: no channel named.
    _day(tmp_path, "2026-11-08", {"books": 20 * 10**6, "ticker": 10 * 10**6, "trades": 3 * 10**6})
    flat = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW)
    assert flat.split(" | ")[0].endswith("-- no channel over +50%")


def test_the_baseline_median_straddles_a_weekend(tmp_path: Path) -> None:
    """Seven prior days hold two weekend days; the median is a weekday, so a
    weekend-sized newest day reads as drift against the WEEK, not against a
    weekend it happens to follow."""
    _week(tmp_path)
    for day in ("2026-11-01", "2026-11-07"):  # Sun, Sat: ticker 2x
        _day(tmp_path, day, {"books": 20 * 10**6, "ticker": 20 * 10**6, "trades": 3 * 10**6})
    _day(tmp_path, "2026-11-08", {"books": 20 * 10**6, "ticker": 20 * 10**6, "trades": 3 * 10**6})
    line = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW)
    assert "ticker 0.2" in line.split(" -- ")[0].split(" vs ")[1]
    assert "STEP over +50%: ticker +100%" in line


def test_a_single_complete_day_has_nothing_to_compare_to(tmp_path: Path) -> None:
    _day(tmp_path, "2026-11-08", {"books": 10**6}, hours=2)
    line = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW)
    assert (
        line.split(" | ")[0]
        == "11-08: 0.0 GB (no channel over 0.05 GB) -- no earlier complete day to compare"
    )


def test_the_running_day_compares_closed_hours_against_the_same_hour_files(tmp_path: Path) -> None:
    _week(tmp_path)
    _day(tmp_path, "2026-11-08", {"books": 20 * 10**6, "ticker": 20 * 10**6, "trades": 3 * 10**6})
    # Today: hours 00..08, 08 still open (tiny); ticker 2x the week, books flat.
    _day(
        tmp_path,
        "2026-11-09",
        {"books": 20 * 10**6, "ticker": 20 * 10**6, "trades": 3 * 10**6},
        hours=8,
    )
    (tmp_path / "2026-11-09" / "08-books.jsonl.zst").write_bytes(b"\0" * 1000)
    (tmp_path / "2026-11-09" / "08-ticker.jsonl.zst").write_bytes(b"\0" * 1000)
    line = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW)
    tail = line.split(" | ")[1]
    assert tail.startswith("today through 07Z: 0.3 GB (books 0.2, ticker 0.2)")
    # 1 d ago (11-08) ran at the same per-hour rate: flat. 7 d ago (11-02): ticker doubled.
    assert "same hours 1 d ago 0.3 GB: total +0% (books +0%, ticker +0%)" in tail
    assert "same hours 7 d ago 0.3 GB: total +30% (books +0%, ticker +100%)" in tail
    # Drop the week-ago day: the clause says so rather than comparing to nothing.
    for f in (tmp_path / "2026-11-02").iterdir():
        f.unlink()
    (tmp_path / "2026-11-02").rmdir()
    assert "same hours 7 d ago: not held" in health.raw_frames_line(
        health.read_raw_frames(tmp_path), NOW
    )
    # Only the open hour exists: no closed hour, no comparison.
    for f in (tmp_path / "2026-11-09").iterdir():
        f.unlink()
    (tmp_path / "2026-11-09" / "08-books.jsonl.zst").write_bytes(b"\0" * 1000)
    assert health.raw_frames_line(health.read_raw_frames(tmp_path), NOW).endswith(
        "| today 11-09: no closed hour yet"
    )


def test_every_byte_the_reader_stats_moves_the_line(tmp_path: Path) -> None:
    """Perturbation (#95): the renderer may drop no term it was handed. One
    hour file of every channel of the newest complete day and of the running
    day is grown by a GB and the rendered line must change. The baseline days
    enter only through a MEDIAN, which one day cannot move by construction,
    so they are bumped together -- a perturbation a single-day bump would
    pass against a renderer that dropped the baseline entirely."""
    _week(tmp_path)
    _day(tmp_path, "2026-11-08", {"books": 20 * 10**6, "ticker": 10 * 10**6, "trades": 3 * 10**6})
    _day(
        tmp_path,
        "2026-11-09",
        {"books": 20 * 10**6, "ticker": 10 * 10**6, "trades": 3 * 10**6},
        hours=5,
    )
    base = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW)
    baseline = [d for d in sorted(tmp_path.iterdir()) if d.name < "2026-11-08"]
    assert len(baseline) == 7
    for group in ([tmp_path / "2026-11-08"], [tmp_path / "2026-11-09"], baseline):
        for ch in ("books", "ticker", "trades"):
            files = [d / f"01-{ch}.jsonl.zst" for d in group]
            originals = [f.read_bytes() for f in files]
            for f, o in zip(files, originals, strict=True):
                f.write_bytes(o + b"\0" * 10**9)
            bumped = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW)
            assert bumped != base, ([d.name for d in group], ch)
            for f, o in zip(files, originals, strict=True):
                f.write_bytes(o)
    assert health.raw_frames_line(health.read_raw_frames(tmp_path), NOW) == base
    # The window and the drift fraction are terms too.
    assert health.raw_frames_line(health.read_raw_frames(tmp_path), NOW, baseline_days=3) != base
    assert health.raw_frames_line(health.read_raw_frames(tmp_path), NOW, drift_frac=0.25) != base


def test_the_digest_prints_the_line_from_the_one_renderer(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The live reader on the real tree (or its absence) never raises.
    live = health.read_raw_frames()
    assert live is None or isinstance(live, list)
    _week(tmp_path)
    _day(tmp_path, "2026-11-08", {"books": 22 * 10**6, "ticker": 20 * 10**6, "trades": 3 * 10**6})
    days = health.read_raw_frames(tmp_path)
    monkeypatch.setattr(health, "read_raw_frames", lambda: days)
    monkeypatch.setattr(health, "read_kernel_oom_window", lambda: None)
    monkeypatch.setattr(health, "read_hostfs", lambda: health.HostFs(None, None))
    monkeypatch.setattr(health, "read_kubelet_window", lambda: None)
    health.main()
    out = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("RAWFRAMES")]
    assert len(out) == 1 and out[0].startswith("RAWFRAMES  (host)")
    expect = health.raw_frames_line(days, datetime.now(UTC)).split(" | ")[0]
    assert expect in out[0] and "STEP over +50%: ticker +100%" in out[0]
