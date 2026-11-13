"""`archive disk headroom` — the check that replaced `stream disk under 20 GB`.

The predecessor was a constant ceiling on a monotonically increasing quantity
(mistakes #29's family, #45): it crossed at 20.31 GB on 2026-09-09 and could
never go green again, it watched 8% of the footprint it was named for because
the 7-slot backup rotation multiplies every byte, and it lived inside
`qa_stream`, which returns early when the stream archive is locked. Each of
those three is pinned below.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from collector import backup, health, qa

GB = 10**9
# Captured before the autouse fixture replaces it, for the parser's own test.
_REAL_SLOT_DU = qa._slot_exclusive_bytes


class _Usage:
    """`shutil.disk_usage`'s free and total fields, pinned, so a horizon test
    does not depend on how much room the machine running it happens to have."""

    def __init__(self, free: int, total: int = 2000 * 10**9) -> None:
        self.free = free
        self.total = total


def _rotation(backup_dir: Path, stem: str, sizes: list[int], day0: float = 1.0e9) -> None:
    """One slot per day, oldest first, mtimes exactly 1 day apart."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    for i, size in enumerate(sizes):
        p = backup_dir / f"{stem}.d{i}.duckdb"
        p.write_bytes(b"")
        os.truncate(p, size)
        os.utime(p, (day0 + i * 86400, day0 + i * 86400))


def _live(tmp_path: Path, stem: str, size: int) -> str:
    p = tmp_path / f"{stem}.duckdb"
    p.write_bytes(b"")
    os.truncate(p, size)
    return str(p)


def _run(**kw) -> tuple[set, list[str]]:
    qa._failures.clear()
    lines: list[str] = []
    printed = []
    import builtins

    real = builtins.print

    def cap(*a, **k):
        printed.append(" ".join(str(x) for x in a))

    builtins.print = cap
    try:
        qa.qa_disk_headroom(**kw)
    finally:
        builtins.print = real
    lines = printed
    failed = set(qa._failures)
    qa._failures.clear()
    return failed, lines


def test_rate_is_measured_from_the_rotation_and_multiplied_by_the_slots(tmp_path):
    """1 GB/day of live growth on a shared filesystem burns 8 GB/day, not 1."""
    db = _live(tmp_path, "a", 7 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB, 3 * GB, 4 * GB, 5 * GB, 6 * GB, 7 * GB])
    rate, span = qa._rotation_growth([Path(db)], bdir)
    assert rate == pytest.approx(1 * GB)
    assert span == pytest.approx(6.0)

    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=1.0)
    detail = "\n".join(lines)
    assert "1.00 live + 7.00 backups (x7 full copies, sharing unmeasured)" in detail, detail
    assert "burn 8.00 GB/day" in detail, detail


BTRFS_DU = (
    "     Total   Exclusive  Set shared  Filename\n"
    "534520524800  3030818816  95962836992  data/backups\n"
)


def test_reflinked_slots_cost_their_exclusive_bytes_not_a_full_copy(tmp_path, monkeypatch):
    """MEASURED 2026-10-25 on this box: 14 slots, 534.5 GB nominal, 3.03 GB
    exclusive (0.57%) -- `copy_file_range` reflinks on btrfs. x8 on every live
    byte was the model's premise and was 8x wrong; the rotation's real cost is
    its exclusive bytes spread over the span they accrued in."""
    db = _live(tmp_path, "a", 7 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB, 3 * GB, 4 * GB, 5 * GB, 6 * GB, 7 * GB])
    monkeypatch.setattr(qa, "_slot_exclusive_bytes", lambda _d: (3 * GB, 534 * GB))

    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=1.0)
    detail = "\n".join(lines)
    assert "1.00 live + 0.50 backups (3.0 GB exclusive of 534 GB nominal, reflinked" in detail, (
        detail
    )
    assert "burn 1.50 GB/day" in detail, detail


def test_slot_sharing_is_read_from_the_filesystem_and_admits_when_it_cannot(monkeypatch):
    """`stat` reports a reflinked slot at full size, so only `btrfs filesystem
    du` can say what it costs; the parser reads the real tool's summary row,
    and every way of not knowing is None (-> full copies), never zero."""
    calls = []

    class _Res:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _Res(0, BTRFS_DU)

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    assert _REAL_SLOT_DU(Path("data/backups")) == (3030818816, 534520524800)
    cmd, kw = calls[0]
    assert cmd[:4] == ["btrfs", "filesystem", "du", "-s"] and "--raw" in cmd
    assert kw["timeout"] == qa.SLOT_DU_TIMEOUT_S

    monkeypatch.setattr(qa.subprocess, "run", lambda *a, **k: _Res(1, ""))
    assert _REAL_SLOT_DU(Path("x")) is None
    monkeypatch.setattr(qa.subprocess, "run", lambda *a, **k: _Res(0, "garbage\n"))
    assert _REAL_SLOT_DU(Path("x")) is None

    def boom(*a, **k):
        raise FileNotFoundError("btrfs")

    monkeypatch.setattr(qa.subprocess, "run", boom)
    assert _REAL_SLOT_DU(Path("x")) is None

    def slow(*a, **k):
        raise qa.subprocess.TimeoutExpired(cmd="btrfs", timeout=1)

    monkeypatch.setattr(qa.subprocess, "run", slow)
    assert _REAL_SLOT_DU(Path("x")) is None


def _raw_day(raw: Path, day: str, size: int) -> None:
    (raw / day).mkdir(parents=True, exist_ok=True)
    f = raw / day / "00-books.jsonl.zst"
    f.write_bytes(b"")
    os.truncate(f, size)


def test_raw_frames_add_a_days_frames_per_day_until_the_window_is_full(tmp_path):
    """Measured 2026-10-25: 13-17 GB/day of wire with 3 of 14 days held --
    the model's largest term. It is the newest COMPLETE day (today's is
    partial), and once `held_day_dirs(RETAIN_DAYS)` = 15 directories exist
    the prune makes it a plateau: zero, not negative and not still growing.
    Not 14: the prune's cutoff is exclusive, so the running day stands
    beside 14 closed ones -- read live 2026-11-05 with 14 held, the model
    saying plateau and one more ~15 GB day still to land before the first
    prune (11-07)."""
    raw = tmp_path / "raw_frames"
    now = qa.datetime(2026, 10, 25, 20, 0, tzinfo=qa.UTC)
    assert qa._raw_frames_growth(raw, 14, now) == (0.0, 0, 0)
    _raw_day(raw, "2026-10-25", 5 * GB)  # only the running day: a floor
    assert qa._raw_frames_growth(raw, 14, now) == (5 * GB, 1, 0)
    _raw_day(raw, "2026-10-23", 6 * GB)
    _raw_day(raw, "2026-10-24", 17 * GB)
    assert qa._raw_frames_growth(raw, 14, now) == (17 * GB, 3, 0)
    for i in range(11):  # 10-12 .. 10-22: 14 held, still filling
        _raw_day(raw, f"2026-10-{12 + i:02d}", 10 * GB)
    assert qa._raw_frames_growth(raw, 14, now) == (17 * GB, 14, 0)
    _raw_day(raw, "2026-10-11", 10 * GB)  # the 15th: the window is full
    assert qa._raw_frames_growth(raw, 14, now) == (0.0, 15, 0)


def test_a_hard_linked_oldest_day_keeps_raw_frames_a_burn(tmp_path):
    """Measured 2026-11-06: the sibling's lake hard-links every raw file
    (`st_nlink` 2 on all 48 of `2026-10-23`), so the prune's unlink drops a
    link count and frees 0 B. A full window is a plateau only if the day the
    prune drops next is NOT linked; linked, the newest complete day's bytes
    stay the rate, and the count of linked files is published so the label
    can say why (mistakes #164)."""
    raw = tmp_path / "raw_frames"
    now = qa.datetime(2026, 11, 6, 20, 0, tzinfo=qa.UTC)
    first = qa.datetime(2026, 10, 23, tzinfo=qa.UTC)
    for i in range(15):  # 10-23 .. 11-06: the window is full
        day = (first + qa.timedelta(days=i)).strftime("%Y-%m-%d")
        _raw_day(raw, day, 10 * GB)
    _raw_day(raw, "2026-11-05", 17 * GB)  # the newest COMPLETE day
    assert qa._raw_frames_growth(raw, 14, now) == (0.0, 15, 0)
    oldest = sorted(p for p in raw.iterdir())[0]
    assert oldest.name == "2026-10-23"
    lake = tmp_path / "lake"
    lake.mkdir()
    os.link(oldest / "00-books.jsonl.zst", lake / "2026-10-23-books.zst")
    assert qa._raw_frames_growth(raw, 14, now) == (17 * GB, 15, 1)
    # A linked day that is NOT the oldest is not the prune's next victim.
    os.unlink(lake / "2026-10-23-books.zst")
    os.link(raw / "2026-11-01" / "00-books.jsonl.zst", lake / "x.zst")
    assert qa._raw_frames_growth(raw, 14, now) == (0.0, 15, 0)


def test_raw_frames_are_a_term_of_the_model(tmp_path, monkeypatch):
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    raw = tmp_path / "raw_frames"
    _raw_day(raw, "2000-01-01", 4 * GB)
    _raw_day(raw, "2999-01-01", 1 * GB)  # a day that has not closed: ignored

    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=1.0)
    detail = "\n".join(lines)
    assert "+ 4.00 raw_frames (2 of 15 day dirs held, filling)" in detail, detail
    assert "burn 12.00 GB/day" in detail, detail  # 1 live + 7 copies + 4 raw


def test_a_hard_linked_raw_window_is_labelled_in_the_model(tmp_path, monkeypatch):
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    raw = tmp_path / "raw_frames"
    for i in range(15):
        _raw_day(raw, f"2000-01-{i + 1:02d}", 4 * GB)
    _raw_day(raw, "2999-01-01", 1 * GB)
    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=1.0)
    assert "+ 0.00 raw_frames (16 of 15 day dirs held, plateau)" in "\n".join(lines)
    os.link(raw / "2000-01-01" / "00-books.jsonl.zst", tmp_path / "linked.zst")
    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=1.0)
    detail = "\n".join(lines)
    assert (
        "+ 4.00 raw_frames (16 of 15 day dirs held, oldest day has 1 hard-linked file(s),"
        " the prune frees nothing)"
    ) in detail, detail
    assert "burn 12.00 GB/day" in detail, detail


def test_headroom_is_a_horizon_so_it_can_fail_and_recover(tmp_path):
    """The defect that retired the constant: a level check on a growing
    quantity is one-way. The same archive passes or fails on the FLOOR, and a
    larger floor is the only thing that changes -- no size threshold moves."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    free_days = qa.shutil.disk_usage(tmp_path).free / (1 * GB * 8)

    failed, _ = _run(dbs=[db], backup_dir=bdir, min_days=free_days / 2)
    assert failed == set()
    failed, _ = _run(dbs=[db], backup_dir=bdir, min_days=free_days * 2)
    assert failed == {"archive disk headroom"}


def test_a_partial_series_is_unprojected_never_an_optimistic_sum(tmp_path):
    """Summing only the archives that HAVE history understates the burn, and
    understating it is the reassuring direction. One unmeasurable archive
    makes the whole projection unmeasurable."""
    a = _live(tmp_path, "a", 1 * GB)
    b = _live(tmp_path, "b", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])  # 'b' has no slots at all

    assert qa._rotation_growth([Path(a), Path(b)], bdir)[0] is None
    failed, lines = _run(dbs=[a, b], backup_dir=bdir)
    assert failed == set()  # free disk still holds another copy of 2 GB
    assert "UNPROJECTED" in "\n".join(lines)


def test_unprojected_still_fails_when_one_more_copy_would_not_fit(tmp_path, monkeypatch):
    """The fallback is a real bound, not a shrug: with no history the disk must
    at least be able to hold another copy of what it already carries."""
    db = _live(tmp_path, "a", 4 * GB)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(3 * GB))

    failed, lines = _run(dbs=[db], backup_dir=bdir)
    assert failed == {"archive disk headroom"}
    assert "UNPROJECTED" in "\n".join(lines)

    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(5 * GB))
    failed, _ = _run(dbs=[db], backup_dir=bdir)
    assert failed == set()


def test_the_horizon_is_free_over_burn(tmp_path, monkeypatch):
    """End to end, with both sides pinned: 1 GB/day live x8 slots against
    800 GB free is 100 days, so a 90-day floor passes and a 120-day one does
    not -- and nothing about it depends on the archive's absolute size."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(800 * GB))

    failed, lines = _run(dbs=[db], backup_dir=bdir, min_days=90.0)
    assert failed == set()
    assert "100 d to full" in "\n".join(lines)
    failed, _ = _run(dbs=[db], backup_dir=bdir, min_days=120.0)
    assert failed == {"archive disk headroom"}


def test_a_shrinking_archive_is_not_an_infinite_or_negative_horizon(tmp_path):
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [2 * GB, 1 * GB])
    failed, lines = _run(dbs=[db], backup_dir=bdir, min_days=1e9)
    assert failed == set()
    assert "not growing" in "\n".join(lines)


def test_the_archive_list_is_backups_own_so_a_fourth_db_cannot_be_missed():
    """The rotation and the headroom check must never disagree about WHICH
    files exist -- a db added to `backup.DBS` is copied 7 times whether or not
    anyone remembers to widen this check."""
    assert qa.qa_disk_headroom.__defaults__[0] is None
    src = inspect.getsource(qa.qa_disk_headroom)
    assert "DBS if dbs is None" in src
    assert qa.DBS is backup.DBS


def test_the_check_is_not_gated_by_an_archive_lock():
    """It lived inside `qa_stream`, which returns early when the stream
    archive is unreachable -- so the disk signal disappeared exactly when the
    box was in trouble. It takes no connection and main() calls it first."""
    assert "conn" not in inspect.signature(qa.qa_disk_headroom).parameters
    main = inspect.getsource(qa.main)
    assert main.index("qa_disk_headroom()") < main.index("qa_stream(")
    assert "disk_usage" not in inspect.getsource(qa.qa_stream)


def test_no_constant_size_ceiling_survives_anywhere_in_qa():
    """The retired defect, by name. A check that names a fixed GB bound on a
    file that only grows is one-way by construction."""
    for line in Path(qa.__file__).read_text().splitlines():
        code = line.split("#")[0]
        if "check(" not in code:
            continue
        assert "stream disk under 20 GB" not in code
        if "GB" in code and "under" in code:
            raise AssertionError(f"constant size ceiling reintroduced: {line.strip()}")


# ---------------------------------------------------------------------------
# The filesystem's own free-space series (2026-10-24). The rotation rate is a
# model of one tenant's files; the series is the measurement of the disk.


@pytest.fixture(autouse=True)
def _series_in_tmp(tmp_path, monkeypatch):
    """Never let a test append to the repo's own `reports/qa/disk_free.jsonl`."""
    monkeypatch.setattr(qa, "DISK_SERIES", tmp_path / "series" / "disk_free.jsonl")
    # Nor read this box's kubelet config: no kubelet is the default, and a
    # test that wants one writes its own file and passes `kubelet_cfg`.
    monkeypatch.setattr(health, "KUBELET_CONFIG_PATH", str(tmp_path / "no-kubelet.yaml"))
    # The repo's own `data/raw_frames` and `btrfs filesystem du` must never
    # reach a test: an empty raw dir and an unmeasured sharing (full copies)
    # are the defaults; tests that want the measured arm say so.
    monkeypatch.setattr(qa, "RAW_FRAMES_DIR", str(tmp_path / "raw_frames"))
    monkeypatch.setattr(qa, "_slot_exclusive_bytes", lambda _d: None)


def _sample(series: Path, days_ago: float, free: int) -> None:
    series.parent.mkdir(parents=True, exist_ok=True)
    ts = qa.datetime.now(qa.UTC) - qa.timedelta(days=days_ago)
    with series.open("a") as fh:
        fh.write(qa.json.dumps({"ts": ts.isoformat(), "free": free}) + "\n")


def test_each_run_records_the_filesystems_free_space(tmp_path, monkeypatch):
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(800 * GB))
    _run(dbs=[db], backup_dir=bdir)
    _run(dbs=[db], backup_dir=bdir)
    lines = qa.DISK_SERIES.read_text().splitlines()
    assert len(lines) == 2
    assert all(qa.json.loads(x)["free"] == 800 * GB for x in lines)


def test_a_day_old_sample_makes_the_filesystem_the_burn(tmp_path, monkeypatch):
    """Model: 1 GB/day live x8 = 8 GB/day, 100 d. Filesystem: 820 -> 800 GB in
    two days = 10 GB/day, 80 d. The horizon is the filesystem's, and the model
    is printed beside it rather than replacing it."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(800 * GB))
    _sample(qa.DISK_SERIES, 2.0, 820 * GB)

    failed, lines = _run(dbs=[db], backup_dir=bdir, min_days=90.0)
    detail = "\n".join(lines)
    assert failed == {"archive disk headroom"}, detail
    assert "80 d to full" in detail and "burn 10.00 GB/day measured on the filesystem" in detail
    assert "rotation model 8.00 GB/day = 1.00 live + 7.00 backups" in detail
    failed, _ = _run(dbs=[db], backup_dir=bdir, min_days=70.0)
    assert failed == set()


def test_a_step_in_the_burn_shows_from_its_first_day_not_the_window_average(tmp_path):
    """Ten quiet days at ~1 GB/day, then 20 GB/day for the last 1.5 days: the
    window average is ~4 GB/day and would hide the step for two weeks. The rate
    is the worst over any sample >= 1 day old."""
    series = tmp_path / "s.jsonl"
    now = qa.datetime.now(qa.UTC)
    _sample(series, 10.0, 900 * GB)
    _sample(series, 1.5, 890 * GB)
    rate, span = qa._fs_burn(series, now, 860 * GB)
    assert rate == pytest.approx(20 * GB, rel=0.01)
    assert span == pytest.approx(10.0, abs=0.01)


def test_the_live_pace_is_printed_beside_the_worst_day(tmp_path, monkeypatch):
    """A 60 GB step three days ago, then 20 GB/day: the verdict's burn is the
    worst day-old sample (max, 33.33 GB/day over 3.0d) and stays so until the
    step leaves the window -- but the line also says the youngest day-old
    sample's pace (20.00 GB/day over 1.0d), so nobody differences the series
    by hand to learn whether the step is still the current burn."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(800 * GB))
    _sample(qa.DISK_SERIES, 3.0, 900 * GB)
    _sample(qa.DISK_SERIES, 2.0, 840 * GB)
    _sample(qa.DISK_SERIES, 1.0, 820 * GB)
    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=90.0)
    detail = "\n".join(lines)
    assert "burn 33.33 GB/day measured on the filesystem over 3.0d" in detail
    assert "(latest 20.00 GB/day over 1.0d)" in detail
    rate, age = qa._fs_burn_latest(qa.DISK_SERIES, qa.datetime.now(qa.UTC), 800 * GB)
    assert rate == pytest.approx(20 * GB, rel=0.01) and age == pytest.approx(1.0, abs=0.01)


def test_the_live_pace_needs_a_day_old_sample_too(tmp_path):
    series = tmp_path / "s.jsonl"
    _sample(series, 0.5, 820 * GB)
    assert qa._fs_burn_latest(series, qa.datetime.now(qa.UTC), 800 * GB) == (None, 0.0)


def test_a_sample_younger_than_a_day_is_not_a_rate(tmp_path, monkeypatch):
    """A sub-day delta is the sampling period wearing the quantity's name
    (mistakes #92): the model stays the burn and the line says how young."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(800 * GB))
    _sample(qa.DISK_SERIES, 0.5, 820 * GB)
    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=90.0)
    detail = "\n".join(lines)
    assert "100 d to full" in detail and "burn 8.00 GB/day = 1.00 live + 7.00 backups" in detail
    assert "series 0.5d young, not yet a rate" in detail


def test_samples_outside_the_window_do_not_vote(tmp_path):
    series = tmp_path / "s.jsonl"
    now = qa.datetime.now(qa.UTC)
    _sample(series, 30.0, 2000 * GB)  # a disk that no longer exists
    _sample(series, 2.0, 810 * GB)
    rate, span = qa._fs_burn(series, now, 800 * GB)
    assert rate == pytest.approx(5 * GB, rel=0.01)
    assert span == pytest.approx(2.0, abs=0.01)


def test_a_filesystem_being_freed_is_not_filling(tmp_path, monkeypatch):
    """A slow drain (+2.6% over two days, under the reset share) is a disk that
    is not filling; a step larger than the share is a reset instead."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(800 * GB))
    _sample(qa.DISK_SERIES, 2.0, 780 * GB)
    failed, lines = _run(dbs=[db], backup_dir=bdir, min_days=1e9)
    assert failed == set()
    assert "filesystem not filling over 2.0d" in "\n".join(lines)


def test_a_rise_in_free_space_resets_the_series_so_a_deletion_is_not_netted_into_the_burn(
    tmp_path,
):
    """The 2026-11-05 shape: 70 GB/day for twelve days, then the user frees
    339 GB, then 70 GB/day again. The max over the whole window nets the
    deletion into every older sample and reads ~30 GB/day; samples older than
    a reset rise describe a disk that no longer exists and do not vote."""
    series = tmp_path / "s.jsonl"
    now = qa.datetime.now(qa.UTC)
    _sample(series, 4.0, 400 * GB)
    _sample(series, 3.0, 330 * GB)
    _sample(series, 2.0, 260 * GB)
    _sample(series, 1.0, 600 * GB)  # +340 GB, the reset rise
    rate, span = qa._fs_burn(series, now, 530 * GB)
    assert rate == pytest.approx(70 * GB, rel=0.01)
    assert span == pytest.approx(1.0, abs=0.01)
    assert qa._fs_reset(series, now, 530 * GB) == (340 * GB, pytest.approx(1.0, abs=0.01))


def test_a_small_rise_is_noise_and_does_not_reset(tmp_path):
    """The 11-02 -> 11-03 kubelet image GC: +24 GB on 357 GB (+6.7%) is under
    the reset share, and the worst day-old sample still carries the burn."""
    series = tmp_path / "s.jsonl"
    now = qa.datetime.now(qa.UTC)
    _sample(series, 3.0, 420 * GB)
    _sample(series, 2.0, 357 * GB)
    _sample(series, 1.0, 381 * GB)
    rate, span = qa._fs_burn(series, now, 307 * GB)
    assert qa._fs_reset(series, now, 307 * GB) is None
    assert rate == pytest.approx(74 * GB, rel=0.01) and span == pytest.approx(3.0, abs=0.01)


def test_after_a_reset_with_no_day_old_sample_the_line_names_the_rise(tmp_path, monkeypatch):
    """The morning after the deletion: the only post-rise sample is sub-day, so
    the model is the burn and the line says why the series cannot speak."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(560 * GB))
    _sample(qa.DISK_SERIES, 2.0, 300 * GB)
    _sample(qa.DISK_SERIES, 1.0, 241 * GB)
    _sample(qa.DISK_SERIES, 0.8, 572 * GB)
    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=90.0)
    detail = "\n".join(lines)
    assert "burn 8.00 GB/day = 1.00 live + 7.00 backups" in detail
    assert "series 0.8d young, not yet a rate; series reset by a +331 GB rise 0.8d ago" in detail


def test_a_day_old_sample_is_a_day_old_to_within_the_timers_jitter(tmp_path):
    """Two 10:00Z runs a few seconds apart on the clock: the youngest sample
    is 0.9999 d old and must vote (the 11-03 line read its pace 'over 2.0d')."""
    series = tmp_path / "s.jsonl"
    now = qa.datetime.now(qa.UTC)
    _sample(series, 2.0, 820 * GB)
    _sample(series, 1.0 - 30 / 86400, 810 * GB)
    rate, age = qa._fs_burn_latest(series, now, 800 * GB)
    assert age == pytest.approx(1.0, abs=0.001)
    assert rate == pytest.approx(10 * GB, rel=0.01)


def test_a_rise_since_the_last_sample_resets_on_the_day_it_lands(tmp_path, monkeypatch):
    """The 11-06 10:00Z shape exactly: twelve falling samples, the last one
    241 GB, and the LIVE reading 565 GB. The live reading is recorded only
    after the rate is taken, so without it as the series' youngest point the
    run would read the whole window's max (~30 GB/day over 12 d) and print
    the optimistic number for a day. It is a reset: no retained sample, the
    model is the burn, and the line names the rise."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(565 * GB))
    for d, free in ((12.0, 922), (3.0, 381), (2.0, 307), (1.0, 241)):
        _sample(qa.DISK_SERIES, d, free * GB)
    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=90.0)
    detail = "\n".join(lines)
    assert "burn 8.00 GB/day = 1.00 live + 7.00 backups" in detail
    assert "series 0.0d young, not yet a rate; series reset by a +324 GB rise 0.0d ago" in detail
    assert "measured on the filesystem" not in detail


# ---------------------------------------------------------------------------
# The tightest tenant (2026-11-07, mistakes #165). The kubelet evicts pods at
# its image-GC high threshold, ~300 GB free on this 2 TB device, and crossed
# it on 11-04 while this check counted days to FULL.


def _kube_cfg(tmp_path: Path, body: str = "apiVersion: kubelet.config.k8s.io/v1beta1\n") -> Path:
    cfg = tmp_path / "kubelet.yaml"
    cfg.write_text(body)
    return cfg


def test_the_horizon_is_the_nearer_of_full_and_the_kubelet_threshold(tmp_path, monkeypatch):
    """8 GB/day against 800 GB free of 2,000 GB: 100 d to full, but the
    kubelet's 85% threshold sits at 300 GB free, 500 GB away -- 62 d. A
    90-day floor that passed to full fails to the threshold, and the line
    prints BOTH so the old headline stays comparable. The config here names
    no threshold, so the kubelet's default 85 is the one read (this box)."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(800 * GB, 2000 * GB))

    failed, lines = _run(dbs=[db], backup_dir=bdir, min_days=90.0)
    assert failed == set()
    assert "no kubelet config (horizon to full only)" in "\n".join(lines)

    failed, lines = _run(dbs=[db], backup_dir=bdir, min_days=90.0, kubelet_cfg=_kube_cfg(tmp_path))
    text = "\n".join(lines)
    assert failed == {"archive disk headroom"}
    assert "100 d to full" in text
    assert "62 d to the kubelet's 85% threshold (500 GB above)" in text


def test_a_configured_threshold_is_the_one_read(tmp_path, monkeypatch):
    """`imageGCHighThresholdPercent: 90` -> 200 GB free is the line; 800 GB
    free is 600 GB above it, 75 d at 8 GB/day."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(800 * GB, 2000 * GB))
    cfg = _kube_cfg(tmp_path, "imageGCHighThresholdPercent: 90\nimageGCLowThresholdPercent: 80\n")

    failed, lines = _run(dbs=[db], backup_dir=bdir, min_days=70.0, kubelet_cfg=cfg)
    assert failed == set()
    assert "75 d to the kubelet's 90% threshold (600 GB above)" in "\n".join(lines)


def test_past_the_threshold_fails_even_when_the_disk_is_not_filling(tmp_path, monkeypatch):
    """The kubelet evicts on the LEVEL. A shrinking archive on a disk already
    past 85% used is a disk in eviction, whatever its burn says."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [2 * GB, 1 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(200 * GB, 2000 * GB))

    failed, lines = _run(dbs=[db], backup_dir=bdir)
    assert failed == set()
    assert "not growing" in "\n".join(lines)

    failed, lines = _run(dbs=[db], backup_dir=bdir, kubelet_cfg=_kube_cfg(tmp_path))
    text = "\n".join(lines)
    assert failed == {"archive disk headroom"}
    assert "not growing" in text
    assert "100 GB PAST the kubelet's 85% eviction threshold" in text


def test_a_missing_kubelet_config_is_no_claim_not_a_default(tmp_path, monkeypatch):
    """An unreadable config path is no kubelet: the horizon is to full and
    the line says so, never a silent 85."""
    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    monkeypatch.setattr(qa.shutil, "disk_usage", lambda _p: _Usage(800 * GB, 2000 * GB))

    failed, lines = _run(
        dbs=[db], backup_dir=bdir, min_days=90.0, kubelet_cfg=tmp_path / "absent.yaml"
    )
    assert failed == set()
    assert "no kubelet config (horizon to full only)" in "\n".join(lines)


def test_the_kubelet_clause_is_rendered_in_one_place():
    """Both branches of the check (filling, not filling) go through
    `_kubelet_clause`; a second renderer is how a field goes missing from
    one arm (mistakes #95)."""
    src = inspect.getsource(qa.qa_disk_headroom)
    assert src.count("_kubelet_clause(") == 2
    assert "kubelet's" not in src.replace("kubelet's config", "").split('"""')[2]


def test_a_rewritten_newest_day_is_priced_as_written_with_the_at_rest_ratio(tmp_path):
    """Mistakes #188: the sibling's recompressor rewrites closed raw frame
    files in place at about half the bytes. The rate stays the day as the
    writer left it (from the collector's ledger) -- unfixed, a day caught
    mid-rewrite reads 2 GB here -- and the label carries the measured
    at-rest share instead of assuming one."""
    import json

    from collector import rawsizes

    db = _live(tmp_path, "a", 1 * GB)
    bdir = tmp_path / "backups"
    _rotation(bdir, "a", [1 * GB, 2 * GB])
    raw = tmp_path / "raw_frames"
    _raw_day(raw, "2000-01-01", 4 * GB)
    _raw_day(raw, "2999-01-01", 1 * GB)
    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=1.0)
    assert "at rest" not in "\n".join(lines)  # nothing rewritten: no clause

    f = raw / "2000-01-01" / "00-books.jsonl.zst"
    Path(rawsizes.LEDGER).parent.mkdir(parents=True, exist_ok=True)
    Path(rawsizes.LEDGER).write_text(
        json.dumps({"day": "2000-01-01", "hh": "00", "ch": "books", "b": 4 * GB}) + "\n"
    )
    f.write_bytes(b"\x28\xb5\x2f\xfd\x04\x88")  # zstd header, window 2^27
    os.truncate(f, 2 * GB)
    _, lines = _run(dbs=[db], backup_dir=bdir, min_days=1.0)
    detail = "\n".join(lines)
    assert (
        "+ 4.00 raw_frames (2 of 15 day dirs held, filling; as written -- 1 file(s)"
        " rewritten in place by the sibling hold x0.50 of that at rest)"
    ) in detail, detail
    assert "burn 12.00 GB/day" in detail, detail
