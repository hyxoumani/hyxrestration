"""As-written sizes of raw frame files, ledgered before anyone rewrites them.

The files under `data/raw_frames` are hard-linked into the sibling's lake,
and its hourly `hylshi-raw-frames-recompress-d1` (EXP-2248) rewrites every
closed day IN PLACE at `zstd -19 --long=27`: both names onto one new inode,
mtime preserved, about half the bytes (mistakes #188). After that the file's
size says what the recompressor made of the wire, and the size the writer
left -- the only cheap per-channel measure of the wire this repo has -- is
gone. So the 5-minute collector records it first: one JSON line per closed
hour file, the first time it is seen as the writer left it. The digest's
`RAWFRAMES` line reads a rewritten file's bytes from here.

The job starts a day no earlier than 00:35Z of the NEXT day, so an hour file
has between 35 minutes and a day to be seen by a 5-minute cycle; a file that
was rewritten before it was ever ledgered is simply absent, and the digest
says NOT COMPARABLE for it rather than guessing.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

LEDGER = "data/raw_frames_sizes.jsonl"
RAW_FRAMES_DIR = "data/raw_frames"
RAW_FILE_RE = re.compile(r"^(\d\d)-([a-z]+)\.jsonl\.zst$")
#: A frame file whose first zstd frame declares a window this large is NOT as
#: the writer left it: the recompressor's `--long=27` declares 2^27, the
#: writer's level 9 declares 2^21..2^22 (measured 2026-11-13 on both kinds;
#: `tests/test_health_rawframes.py` pins the writer's side by running its own
#: level, so a retune that crosses this line fails there).
REWRITTEN_WLOG = 27
#: An hour file is final this long after its hour ends (the writer closes it
#: at the first frame of the next hour; mtimes read HH:59:59..HH+1:00:00).
SETTLE_S = 120
#: Day directories scanned per call, newest first. Bounds the per-cycle cost
#: (a stat and six bytes per unledgered file) and still backfills a few days
#: of collector outage.
SCAN_DAYS = 5
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

Key = tuple[str, str, str]  # (YYYY-MM-DD, HH, channel)


def zstd_window_log(path: Path) -> int | None:
    """Window log declared by the file's first zstd frame; None when the file
    is unreadable, not zstd, or a single-segment frame (which declares none).
    Six bytes read, nothing decompressed."""
    try:
        with open(path, "rb") as fh:
            b = fh.read(6)
    except OSError:
        return None
    if len(b) < 6 or b[:4] != _ZSTD_MAGIC or (b[4] >> 5) & 1:
        return None
    return 10 + (b[5] >> 3)


def is_rewritten(path: Path) -> bool:
    return (zstd_window_log(path) or 0) >= REWRITTEN_WLOG


def load(ledger: str | Path | None = None) -> dict[Key, int]:
    """Every ledgered size; the FIRST line for a key wins (it was written
    while the file was as the writer left it). A missing ledger is empty and
    a torn line is skipped."""
    out: dict[Key, int] = {}
    try:
        with open(LEDGER if ledger is None else ledger) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    out.setdefault((r["day"], r["hh"], r["ch"]), int(r["b"]))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return out


def record_closed(
    raw_dir: str | Path | None = None,
    ledger: str | Path | None = None,
    now: datetime | None = None,
) -> int:
    """Ledger every closed, settled, not-yet-ledgered hour file of the last
    `SCAN_DAYS` day directories that is still as the writer left it. Returns
    the number of lines appended. Never raises on a missing tree."""
    now = now or datetime.now(UTC)
    path = Path(LEDGER if ledger is None else ledger)
    root = Path(RAW_FRAMES_DIR if raw_dir is None else raw_dir)
    try:
        days = sorted(p for p in root.iterdir() if p.is_dir())[-SCAN_DAYS:]
    except OSError:
        return 0
    have = load(path)
    rows: list[dict] = []
    for d in days:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for f in entries:
            m = RAW_FILE_RE.match(f.name)
            if m is None or (d.name, m.group(1), m.group(2)) in have:
                continue
            try:
                hour_end = datetime.strptime(f"{d.name} {m.group(1)}", "%Y-%m-%d %H").replace(
                    tzinfo=UTC
                ) + timedelta(hours=1)
            except ValueError:
                continue
            if (now - hour_end).total_seconds() < SETTLE_S or is_rewritten(f):
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            rows.append({"day": d.name, "hh": m.group(1), "ch": m.group(2), "b": size})
    if rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))
    return len(rows)
