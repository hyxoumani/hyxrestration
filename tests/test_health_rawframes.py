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


# -- files rewritten in place by the sibling's recompressor (mistakes #188) --

#: First six bytes of a zstd frame declaring window 2^27 (descriptor 0x88),
#: as `zstd -19 --long=27` writes, and 2^22 (0x60), as the writer's level does.
_HDR_LONG27 = b"\x28\xb5\x2f\xfd\x04\x88"
_HDR_WLOG22 = b"\x28\xb5\x2f\xfd\x00\x60"


def _rewrite(root: Path, day: str, shrink: float = 0.5, hours: range = range(24)) -> None:
    """What the recompressor does to a day: same names, smaller, window 2^27."""
    for f in (root / day).iterdir():
        if int(f.name[:2]) in hours:
            n = int(f.stat().st_size * shrink)
            f.write_bytes(_HDR_LONG27 + b"\0" * (n - 6))


def test_the_window_log_is_read_from_the_frame_header(tmp_path: Path) -> None:
    from compression import zstd

    from collector import rawframes

    big, small, junk = tmp_path / "a.zst", tmp_path / "b.zst", tmp_path / "c.zst"
    big.write_bytes(_HDR_LONG27 + b"\0" * 10)
    small.write_bytes(_HDR_WLOG22 + b"\0" * 10)
    junk.write_bytes(b"\0" * 16)
    assert health.zstd_window_log(big) == 27
    assert health.zstd_window_log(small) == 22
    assert health.zstd_window_log(junk) is None
    assert health.zstd_window_log(tmp_path / "absent.zst") is None
    # The premise the threshold stands on: what the WRITER's own level emits
    # for a stream reads below it, and a real long-window frame reads at it.
    # A retune of ZSTD_LEVEL that crosses the line fails here, not in the digest.
    payload = (b'{"k": 1, "v": "abcdefgh"}\n' * 4096) * 64
    ours = tmp_path / "ours.zst"
    with zstd.ZstdFile(ours, "a", level=rawframes.ZSTD_LEVEL) as fh:
        fh.write(payload)
    theirs = tmp_path / "theirs.zst"
    opts = {
        zstd.CompressionParameter.compression_level: 19,
        zstd.CompressionParameter.window_log: 27,
    }
    with zstd.ZstdFile(theirs, "w", options=opts) as fh:
        fh.write(payload)
    assert health.zstd_window_log(ours) < health.RAW_REWRITTEN_WLOG
    assert health.zstd_window_log(theirs) >= health.RAW_REWRITTEN_WLOG


def test_a_rewritten_prior_day_is_left_out_of_the_baseline(tmp_path: Path) -> None:
    _week(tmp_path)
    _day(tmp_path, "2026-11-08", {"books": 20 * 10**6, "ticker": 10 * 10**6, "trades": 3 * 10**6})
    flat = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW)
    assert "no channel over +50%" in flat and "left out" not in flat
    # Halve four of the seven prior days in place. Unfixed, the median lands on
    # a halved day and a flat 11-08 reads as a +100% STEP on every channel.
    for day in ("2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"):
        _rewrite(tmp_path, day)
    line = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW)
    head = line.split(" | ")[0]
    assert "STEP" not in head, head
    assert "vs median of 3 prior day(s)" in head, head
    assert "no channel over +50% (4 rewritten prior day(s) left out)" in head, head
    # Every prior day rewritten: no verdict, and the reason.
    for day in ("2026-11-05", "2026-11-06", "2026-11-07"):
        _rewrite(tmp_path, day)
    head = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW).split(" | ")[0]
    assert "NOT COMPARABLE: all 7 prior day(s) rewritten in place" in head, head
    assert "STEP" not in head and "no channel over" not in head, head


def test_a_rewritten_newest_day_gets_no_verdict(tmp_path: Path) -> None:
    _week(tmp_path)
    _day(tmp_path, "2026-11-08", {"books": 20 * 10**6, "ticker": 10 * 10**6, "trades": 3 * 10**6})
    # The recompressor is partway through yesterday: hours 00..05 done.
    _rewrite(tmp_path, "2026-11-08", hours=range(6))
    head = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW).split(" | ")[0]
    assert "NOT COMPARABLE: 18 of 72 file(s) rewritten in place" in head, head
    assert "vs median" not in head and "no channel over" not in head, head


def test_a_rewritten_reference_hour_is_not_compared(tmp_path: Path) -> None:
    _week(tmp_path)
    rate = {"books": 20 * 10**6, "ticker": 10 * 10**6, "trades": 3 * 10**6}
    _day(tmp_path, "2026-11-08", rate)
    _day(tmp_path, "2026-11-09", rate, hours=9)
    _rewrite(tmp_path, "2026-11-02")
    tail = health.raw_frames_line(health.read_raw_frames(tmp_path), NOW).split(" | ")[1]
    assert "same hours 1 d ago 0.3 GB: total +0%" in tail, tail
    # Unfixed: `same hours 7 d ago 0.1 GB: total +100%` -- the live 11-13
    # digest's `+124% (ticker +234%)` against a rewritten 11-06.
    assert "same hours 7 d ago: not comparable (24 of 24 file(s) rewritten in place)" in tail, tail
