"""Rotated consistent snapshots of the DuckDB archives.

    python -m collector.backup [--dest DIR]

Copies each archive to <dest>/<name>.<weekday>.duckdb (7-slot
rotation) while HOLDING a read-only attachment: DuckDB is one
writer XOR many readers across processes, so the held reader
excludes writers for the copy's duration and the file on disk is
transactionally consistent. Writers tolerate the pause — streamd
holds its flush batch for retry, collect/sweeps use open_retry.

Default destination is data/backups (guards corruption and
fat-finger deletion, not disk loss). Point HYXLAB_BACKUP_DIR at an
off-box mount to make it a real backup — the standing user item.
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

DBS = ["data/hyxlab.duckdb", "data/hyxstream.duckdb", "data/hyxshadow.duckdb"]


def backup_one(src: str | Path, dest_dir: Path, retries: int = 30) -> Path | None:
    src = Path(src)
    if not src.exists():
        return None
    conn = None
    for attempt in range(retries):
        try:
            conn = duckdb.connect(str(src), read_only=True)
            break
        except duckdb.Error:
            if attempt == retries - 1:
                raise
            time.sleep(2)
    try:
        out = dest_dir / f"{src.stem}.{datetime.now(UTC):%a}.duckdb"
        tmp = out.with_suffix(".tmp")
        shutil.copyfile(src, tmp)
        wal = src.with_suffix(src.suffix + ".wal")
        if wal.exists():  # only after an unclean writer shutdown
            shutil.copyfile(wal, Path(str(out) + ".wal"))
        os.replace(tmp, out)
        return out
    finally:
        if conn is not None:
            conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="rotated consistent DuckDB backups")
    ap.add_argument("--dest", default=os.environ.get("HYXLAB_BACKUP_DIR", "data/backups"))
    args = ap.parse_args()
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    for db in DBS:
        t0 = time.monotonic()
        out = backup_one(db, dest)
        if out is None:
            print(f"[backup] {db}: missing, skipped", flush=True)
        else:
            mb = out.stat().st_size / 1e6
            print(f"[backup] {db} -> {out} ({mb:.0f} MB, {time.monotonic() - t0:.1f}s)", flush=True)


if __name__ == "__main__":
    main()
