"""Which rotated backup slot can stand in for a live archive on a read.

    from hyxlab.backupslot import covering_slot
    slot = covering_slot("data/hyxstream.duckdb", covers=run_end)

Why this exists (2026-11-16). `collector.backup` leaves seven day-named
snapshots of each archive in `HYXLAB_BACKUP_DIR` (`<stem>.<Day>.duckdb`),
reflinked on this box so they cost ~0.2 GB exclusive each. A report whose
subject is a CLOSED window -- a finished shadow run -- needs nothing the
live file gained after the window closed, and a read of a slot holds
nothing the stream daemon writes. Measured: the divergence replay of run
20261114T145455 over 36.5 h read `hyxstream.Mon.duckdb` for 33.7 s and
matched 10,083 of 10,083 fills both ways, with zero seconds on
`data/hyxstream.duckdb`; the same report's nightly holds on the live file
were 14.3 s and 81.0 s on 2026-11-16 (the second one 82.5 s stall of
streamd, 816,633 rows pending with no disk copy -- ops.md, mistakes #119).

What "covers" means, and what it does not prove. A slot is the archive as
of its copy, and its mtime is when that copy was written (the copy runs
inside the backup's read hold; an off-device destination stamps the
transfer's end, later by the transfer's length). Everything a reader of
the live file had seen by wall-clock T is in any slot taken after T. What
a slot can lack is a row with an old `recv_ts` that LANDED late -- a
sidecar drain after a long flush stall, a backfilled gap row. `margin`
is the allowance for that: the slot must be taken at least that long
after the instant to cover. It is an allowance, not a proof, so a caller
records which source it read in its artifact.

A slot with a `.wal` beside it is declined: it is a consistent pair, but
a read-only attach of it is unrehearsed here. Unset env, no directory, no
slot late enough -- all `None`, and the caller reads the live file as
before.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Where `collector.backup` rotates its slots; set in the unit files.
BACKUP_DIR_ENV = "HYXLAB_BACKUP_DIR"

#: How long after the covered instant a slot must have been taken.
SLOT_MARGIN = timedelta(hours=1)


@dataclass(frozen=True)
class Slot:
    path: Path
    taken: datetime  # naive UTC, the slot file's mtime


def covering_slot(
    live: str | Path,
    covers: datetime | None,
    *,
    backup_dir: str | Path | None = None,
    margin: timedelta = SLOT_MARGIN,
) -> Slot | None:
    """The NEWEST slot of `live` taken at least `margin` after `covers`.

    `covers` is naive UTC, like every timestamp in the archives. The
    newest covering slot, not the oldest: it holds the most late-landed
    rows and so is the closest to what a live read would return.
    """
    root = backup_dir if backup_dir is not None else os.environ.get(BACKUP_DIR_ENV)
    if not root or covers is None:
        return None
    best: Slot | None = None
    for p in Path(root).glob(f"{Path(live).stem}.*.duckdb"):
        try:
            if not p.is_file() or Path(f"{p}.wal").exists():
                continue
            taken = datetime.fromtimestamp(p.stat().st_mtime, UTC).replace(tzinfo=None)
        except OSError:
            continue
        if taken < covers + margin:
            continue
        if best is None or taken > best.taken:
            best = Slot(p, taken)
    return best
