"""Rotated consistent snapshots of the DuckDB archives.

    python -m collector.backup [--dest DIR]

Copies each archive to <dest>/<name>.<weekday>.duckdb (7-slot
rotation). The copy must be taken while HOLDING a read-only
attachment: DuckDB is one writer XOR many readers across processes,
so the held reader excludes writers for the copy's duration and the
file on disk is transactionally consistent.

THE HOLD IS THE COST, AND IT IS NOT WHAT THIS FILE USED TO CLAIM.
The old docstring said "writers tolerate the pause" and pointed at
the journal, where six of seven nights read 0.2s for a 26 GB file.
0.2s is not a copy — it is a btrfs reflink, which `shutil.copyfile`
reaches through `copy_file_range` only because src and dest are on
the SAME filesystem. Measured 2026-09-26 on `hyxstream.Fri.duckdb`,
26,313,502,720 bytes: `--reflink=always` **0.199s**, `--reflink=never`
onto the same NVMe **20.5s** (~1.29 GB/s). A real copy is 100x the
hold, and that is the FASTEST target there is. The standing user item
— "point HYXLAB_BACKUP_DIR at an off-box mount to make it a real
backup" — is precisely the change that removes the reflink: 26 GB at
a gigabit link's ~110 MB/s is ~240s, and hyxlab.duckdb behind it
another ~190s, every night, with `hyxlab-stream` excluded throughout.
The shape of that damage is measured too (2026-09-25, a 3,057s reader
hold): 400,154 rows held in streamd's buffer, 387,856 pushed to the
spill sidecar. So the assumption was true, and true for a reason
nobody had written down, that the documented next step deletes.

Hence two legs. Under the lock, the archive is copied to a STAGE file
beside the source, on the source's own filesystem — the cheapest copy
available and the floor on the hold no matter where dest points. The
lock is then RELEASED, and the transfer to dest runs against that
private copy at whatever speed the destination has. Where dest is on
the same device the transfer is a rename, so the current setup pays
exactly what it paid before (one reflink, measured 0.2-0.3s). Each
per-database line reports `hold` apart from `total`: what excluded
the writers, measured rather than assumed (mistakes #88 — nothing in
this repo measured a HOLD until it cost a capture day).

AND THE HOLD HAD TO BE MADE REAL BEFORE IT COULD BE SHORTENED
(mistakes #90). DuckDB takes a POSIX `fcntl` record lock, and POSIX
drops EVERY record lock a process holds on a file the moment that
process closes ANY descriptor on it — not just the one the lock was
taken through. `shutil.copyfile` opens the source and closes it, so
the old code released the archive at the instant its copy finished
while believing it held it until `conn.close()`. Measured 2026-09-26:
holder attached read-only, an outside process is refused; the holder
opens and closes a plain read fd; the outside process now takes the
file read-write with the holder still attached. The main snapshot
survived only because that close is the LAST thing `copyfile` does
— the WAL copy that followed it ran unprotected, and any future line
added inside the hold would have too. So the locked leg copies from a
descriptor the caller opens ONCE and closes only after the connection
is gone, via `os.copy_file_range` (measured 26.3 GB in 0.119s on the
real archive, reflink intact — fractionally faster than `copyfile`).

Default destination is data/backups (guards corruption and
fat-finger deletion, not disk loss).
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from hyxlab.store import duck_connect

DBS = ["data/hyxlab.duckdb", "data/hyxstream.duckdb", "data/hyxshadow.duckdb"]

#: A hold past this many seconds is worth a line of its own: it is long
#: enough for streamd's flush to have queued behind it. Not a refusal —
#: an interrupted backup is worse than a slow one — but the journal now
#: says so instead of leaving it to be reconstructed from the victim.
HOLD_WARN_S = 30.0


@dataclass(frozen=True)
class BackupResult:
    """One archive's outcome. `hold_s` is the writer-excluding part."""

    path: Path
    hold_s: float
    total_s: float
    off_device: bool


def _copy_out(src_fd: int, dst: Path) -> None:
    """The LOCKED leg: archive -> stage, on the archive's own filesystem.

    Takes a DESCRIPTOR, never a path. Opening the source again here
    would be harmless; closing it is what drops the read lock this copy
    exists to be protected by, and a path argument is an invitation to
    do both. `copy_file_range` reflinks where the filesystem can.
    """
    size = os.fstat(src_fd).st_size
    out_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        moved = 0
        while moved < size:
            step = os.copy_file_range(src_fd, out_fd, size - moved, moved, moved)
            if step == 0:  # source shrank under us; never seen, not assumed
                break
            moved += step
    finally:
        os.close(out_fd)


def _transfer(src: Path, dst: Path) -> None:
    """The UNLOCKED leg: stage -> destination, at the destination's speed."""
    shutil.copyfile(src, dst)


def _same_device(a: Path, b: Path) -> bool:
    return a.stat().st_dev == b.stat().st_dev


def backup_one(src: str | Path, dest_dir: Path, retries: int = 30) -> BackupResult | None:
    src = Path(src)
    if not src.exists():
        return None
    t0 = time.monotonic()
    out = dest_dir / f"{src.stem}.{datetime.now(UTC):%a}.duckdb"
    stage = src.parent / f".{src.stem}.backup-stage"
    stage_wal = Path(f"{stage}.wal")
    wal = src.with_suffix(src.suffix + ".wal")

    conn = None
    for attempt in range(retries):
        try:
            conn = duck_connect(str(src), read_only=True)
            break
        except duckdb.Error:
            if attempt == retries - 1:
                raise
            time.sleep(2)

    fds: list[int] = []
    try:
        hold0 = time.monotonic()
        try:
            src_fd = os.open(src, os.O_RDONLY)
            fds.append(src_fd)
            _copy_out(src_fd, stage)
            if wal.exists():  # only after an unclean writer shutdown
                wal_fd = os.open(wal, os.O_RDONLY)
                fds.append(wal_fd)
                _copy_out(wal_fd, stage_wal)
            hold_s = time.monotonic() - hold0
        except BaseException:
            stage.unlink(missing_ok=True)
            stage_wal.unlink(missing_ok=True)
            raise
    finally:
        if conn is not None:
            conn.close()
        for fd in fds:  # only now: closing one of these drops the lock
            os.close(fd)

    # The archive is free from here down: everything below reads the stage.
    off_device = not _same_device(stage, dest_dir)
    out_wal = Path(f"{out}.wal")
    try:
        if off_device:
            tmp = out.with_suffix(".tmp")
            try:
                _transfer(stage, tmp)
                if stage_wal.exists():
                    tmp_wal = out.with_suffix(".tmp.wal")
                    _transfer(stage_wal, tmp_wal)
                    os.replace(tmp_wal, out_wal)
                os.replace(tmp, out)
            except BaseException:
                tmp.unlink(missing_ok=True)
                out.with_suffix(".tmp.wal").unlink(missing_ok=True)
                raise
        else:
            if stage_wal.exists():
                os.replace(stage_wal, out_wal)
            os.replace(stage, out)
    finally:
        stage.unlink(missing_ok=True)
        stage_wal.unlink(missing_ok=True)

    return BackupResult(
        path=out, hold_s=hold_s, total_s=time.monotonic() - t0, off_device=off_device
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="rotated consistent DuckDB backups")
    ap.add_argument("--dest", default=os.environ.get("HYXLAB_BACKUP_DIR", "data/backups"))
    args = ap.parse_args()
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    for db in DBS:
        res = backup_one(db, dest)
        if res is None:
            print(f"[backup] {db}: missing, skipped", flush=True)
            continue
        mb = res.path.stat().st_size / 1e6
        leg = "off-device transfer" if res.off_device else "same-device rename"
        print(
            f"[backup] {db} -> {res.path} ({mb:.0f} MB,"
            f" hold {res.hold_s:.1f}s, total {res.total_s:.1f}s, {leg})",
            flush=True,
        )
        if res.hold_s > HOLD_WARN_S:
            print(
                f"[backup] WARNING: {db} writers were excluded for"
                f" {res.hold_s:.1f}s (> {HOLD_WARN_S:.0f}s) — the staging copy"
                f" is not reflinking on this filesystem",
                flush=True,
            )


if __name__ == "__main__":
    main()
