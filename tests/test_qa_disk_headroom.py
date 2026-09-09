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

from collector import backup, qa

GB = 10**9


class _Usage:
    """`shutil.disk_usage`'s free field, pinned, so a horizon test does not
    depend on how much room the machine running it happens to have."""

    def __init__(self, free: int) -> None:
        self.free = free


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
    assert "1.00 live x8" in detail, detail
    assert "burn 8.00 GB/day" in detail, detail


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
