"""The backup's writer-excluding hold is bounded by the LOCAL copy only.

`collector.backup` must hold a read-only attachment while it copies, or
the snapshot is not transactionally consistent. That hold excludes
`hyxlab-stream`, and the nightly journal made it look free -- 0.2s for a
26 GB file. It is free because /home is btrfs and dest sits on the same
filesystem, so `shutil.copyfile` reflinks: measured 2026-09-26 on the
real 26,313,502,720-byte archive, `--reflink=always` 0.199s against
`--reflink=never` 20.5s onto the same NVMe. Pointing HYXLAB_BACKUP_DIR
at an off-box mount -- the repo's own written next step -- deletes the
reflink and turns the hold into the transfer time.

So the module splits the work: `_copy_out` runs under the lock (stage
beside the source, on the source's filesystem), `_transfer` runs after
it is released. These tests are about WHICH LEG HOLDS THE ARCHIVE, and
they answer it from OUTSIDE the process -- a subprocess taking the file
read-write is the only witness that a DuckDB file lock is really gone.
Both arms are asserted: the unlocked leg must succeed AND the locked leg
must fail, because a test that only checks the release passes just as
well against a backup that never locked at all (mistakes #85/#86).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest

from collector import backup
from hyxlab import store

WRITER = "import duckdb,sys; duckdb.connect(sys.argv[1]).close()"


def _writable_from_outside(db: Path) -> bool:
    """True when another PROCESS can take `db` read-write right now."""
    proc = subprocess.run([sys.executable, "-c", WRITER, str(db)], capture_output=True, timeout=120)
    return proc.returncode == 0


def _archive(path: Path, rows: int = 200) -> Path:
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE t (i BIGINT, s VARCHAR)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"r{i}") for i in range(rows)])
    conn.close()
    return path


@pytest.fixture
def src(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return _archive(d / "hyxstream.duckdb")


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    d = tmp_path / "backups"
    d.mkdir()
    return d


def _force_off_device(monkeypatch) -> None:
    """tmp_path's two subdirs share a device; the off-box case does not."""
    monkeypatch.setattr(backup, "_same_device", lambda a, b: False)


def test_the_unlocked_leg_runs_with_the_archive_released(monkeypatch, src, dest):
    _force_off_device(monkeypatch)
    seen: list[bool] = []
    real = backup._transfer

    def probe(a: Path, b: Path) -> None:
        seen.append(_writable_from_outside(src))
        real(a, b)

    monkeypatch.setattr(backup, "_transfer", probe)
    res = backup.backup_one(src, dest)

    assert res is not None and res.off_device
    assert seen == [True], "the archive was still locked during the slow transfer"


def test_the_locked_leg_really_holds_the_archive(monkeypatch, src, dest):
    """Vacuity guard: without this, the release test passes on no lock."""
    seen: list[bool] = []

    real = backup._copy_out

    def probe(a: Path, b: Path) -> None:
        seen.append(_writable_from_outside(src))
        real(a, b)

    monkeypatch.setattr(backup, "_copy_out", probe)
    backup.backup_one(src, dest)

    assert seen == [False], "the staging copy ran without holding the archive"


def test_staged_backup_is_byte_identical_and_readable(monkeypatch, src, dest):
    _force_off_device(monkeypatch)
    res = backup.backup_one(src, dest)

    assert res is not None
    assert res.path.read_bytes() == src.read_bytes()
    conn = duckdb.connect(str(res.path), read_only=True)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 200
    conn.close()


def test_same_device_dest_pays_for_exactly_one_copy(monkeypatch, src, dest):
    calls: list[str] = []
    real = backup._copy_out

    def counted(fd: int, b: Path) -> None:
        calls.append("copy_out")
        real(fd, b)

    monkeypatch.setattr(backup, "_transfer", lambda a, b: calls.append("transfer"))
    monkeypatch.setattr(backup, "_copy_out", counted)
    res = backup.backup_one(src, dest)

    assert res is not None and not res.off_device
    assert calls == ["copy_out"], "the same-device path took a second copy"
    assert res.path.read_bytes() == src.read_bytes()


def test_the_stage_file_never_outlives_the_run(monkeypatch, src, dest):
    def boom(a: Path, b: Path) -> None:
        raise OSError("destination mount went away")

    _force_off_device(monkeypatch)
    monkeypatch.setattr(backup, "_transfer", boom)
    with pytest.raises(OSError):
        backup.backup_one(src, dest)

    leftovers = sorted(p.name for p in src.parent.iterdir() if "backup-stage" in p.name)
    assert leftovers == [], f"stage file survived a failed transfer: {leftovers}"
    assert sorted(p.name for p in dest.iterdir()) == [], "a partial backup was published"


def test_the_wal_is_captured_under_the_same_hold(monkeypatch, src, dest):
    wal = Path(f"{src}.wal")
    wal.write_bytes(b"unclean shutdown residue")
    locked: list[bool] = []
    real = backup._copy_out

    def probe(fd: int, b: Path) -> None:
        locked.append(not _writable_from_outside(src))
        real(fd, b)

    monkeypatch.setattr(backup, "_copy_out", probe)
    res = backup.backup_one(src, dest)

    assert res is not None
    assert locked == [True, True], "the WAL was copied outside the archive's hold"
    assert Path(f"{res.path}.wal").read_bytes() == wal.read_bytes()


def test_hold_is_reported_apart_from_the_transfer(monkeypatch, src, dest):
    _force_off_device(monkeypatch)
    real = backup._transfer

    def slow(a: Path, b: Path) -> None:
        time.sleep(0.4)
        real(a, b)

    monkeypatch.setattr(backup, "_transfer", slow)
    res = backup.backup_one(src, dest)

    assert res is not None
    assert res.total_s - res.hold_s >= 0.4, (
        f"hold {res.hold_s:.3f}s absorbed the transfer (total {res.total_s:.3f}s)"
    )


def test_the_copy_does_not_release_the_lock_it_runs_under(monkeypatch, src, dest):
    """mistakes #90, asked directly rather than as a side effect.

    `shutil.copyfile(src, ...)` opens and closes the source, and that
    close drops every `fcntl` record lock this process holds on it --
    including DuckDB's. Measured 2026-09-26; the copy that finishes is
    therefore the moment the snapshot's guarantee ends. Reverting
    `_copy_out` to a path-based `shutil.copyfile` reddens this.
    """
    after: list[bool] = []
    real = backup._copy_out

    def probe(fd: int, dst: Path) -> None:
        real(fd, dst)
        after.append(_writable_from_outside(src))

    monkeypatch.setattr(backup, "_copy_out", probe)
    backup.backup_one(src, dest)

    assert after == [False], "the staging copy released the lock protecting it"


def test_the_descriptors_are_closed_when_the_run_ends(src, dest):
    """A retained fd is the mechanism; leaking one every night is not.

    The baseline is taken AFTER a warm-up run: `duck_connect` opens this
    process's spill-directory owner lock once and keeps it for the
    interpreter's life (EXP-1373), so a cold baseline reads that
    one-time fd as this function's leak.
    """
    backup.backup_one(src, dest)
    before = len(list(Path("/proc/self/fd").iterdir()))
    Path(f"{src}.wal").write_bytes(b"residue")  # exercise the second fd too
    for _ in range(3):
        backup.backup_one(src, dest)
    assert len(list(Path("/proc/self/fd").iterdir())) == before


# A day-named backup is overwritten weekly, and its WAL is published only
# when the source HAS one. So a WAL that last week's run copied out sits
# beside this week's clean snapshot unless the run removes it, and DuckDB
# replays a foreign WAL without a word: measured 2026-10-12 on 1.5.2, a
# 5-row snapshot opened beside an older file's WAL read 1,005 rows and a
# table that never existed in it. Built for real (a writer killed with
# commits uncheckpointed), because a byte-string WAL would fail to replay
# loudly and so could not show the silent case.
FOREIGN_WAL = """
import duckdb, os, sys
c = duckdb.connect(sys.argv[1])
c.execute("SET checkpoint_threshold='1GB'")
c.execute("CREATE TABLE t (i BIGINT, s VARCHAR)")
c.execute("CHECKPOINT")
c.execute("INSERT INTO t SELECT range, 'ghost' FROM range(1000)")
c.execute("CREATE TABLE only_in_the_old_wal (x INT)")
os._exit(9)
"""


@pytest.mark.parametrize("off_device", [False, True], ids=["same-device", "off-device"])
def test_a_clean_snapshot_never_inherits_an_older_runs_wal(monkeypatch, tmp_path, src, dest, off_device):
    if off_device:
        _force_off_device(monkeypatch)
    old = tmp_path / "old.duckdb"
    subprocess.run([sys.executable, "-c", FOREIGN_WAL, str(old)], check=False, timeout=120)
    assert Path(f"{old}.wal").stat().st_size > 0, "vacuity: no foreign WAL was built"
    res = backup.backup_one(src, dest)  # creates out; plant last week's WAL beside it
    Path(f"{res.path}.wal").write_bytes(Path(f"{old}.wal").read_bytes())
    # ...and, off-device, a killed run's staged WAL, which no except cleared
    res.path.with_suffix(".tmp.wal").write_bytes(Path(f"{old}.wal").read_bytes())

    assert not Path(f"{src}.wal").exists()
    res = backup.backup_one(src, dest)

    assert not Path(f"{res.path}.wal").exists(), "a clean snapshot was published beside a stale WAL"
    conn = duckdb.connect(str(res.path), read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (200,)
    finally:
        conn.close()


def test_the_hold_is_the_ledgers_reading_and_covers_the_release(src, dest):
    """The hold the journal prints is the ATTACH's, charged to the ledger
    row the open recorded (`held_attach`), not a hand clock. Two things the
    hand clock got wrong: it started after the open and stopped BEFORE the
    close, and DuckDB's lock spans exactly the connection's life.
    """
    store.reset_attach_waits()
    res = backup.backup_one(src, dest)
    assert res is not None
    rows = store.attach_waits()
    assert [w.db for w in rows] == [src.name]
    assert rows[0].held_s is not None
    assert res.hold_s == rows[0].held_s
    assert store.attach_wait_block(db=src.name)["held_n"] == 1


def test_a_copy_that_dies_still_charges_the_hold(monkeypatch, src, dest):
    """Recorded in `finally`: a backup killed mid-copy held the archive for
    as long as it ran, and that is the run most worth reading afterwards."""

    def boom(fd: int, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(backup, "_copy_out", boom)
    store.reset_attach_waits()
    with pytest.raises(OSError):
        backup.backup_one(src, dest)
    rows = store.attach_waits()
    assert len(rows) == 1 and rows[0].held_s is not None
    assert _writable_from_outside(src), "the failed run leaked the attach"
