"""Replace a small state file whole, or leave the one that is there.

`Path.write_text` TRUNCATES first and writes second. On a filesystem that
cannot take the write, the truncate has already succeeded: the record that
was there is gone and the one meant to replace it is absent or torn. A
reader that degrades on an unreadable record (`collector.qa._load_state`
returns `{}`) then reads a full disk as "no history" without a word.

Rehearsed 2026-11-17 on a real full filesystem (`unshare -Urm`, 4 MB tmpfs,
`tests/test_state_enospc.py`), with the shared disk ~2 d from its first
ENOSPC (docs/wiki/mistakes.md #206):

* a rewrite that fits the block the truncate just freed SUCCEEDS on tmpfs --
  the truncate is its own reservation, so a mocked errno and a naive
  rehearsal both read clean;
* a rewrite that needs ONE block more lands the first block, fails the
  second and leaves torn JSON: the record AND its history are gone;
* a copy-on-write filesystem (btrfs, the production one) does not hand the
  truncated extent back before the write asks, so there every rewrite is
  the second case. NOT measured -- no unprivileged way to fill a btrfs here.

So: write a sibling temporary, `fsync` it (delayed allocation may report
ENOSPC at writeback rather than at `write`), and only then `os.replace`. A
failure at any step raises `OSError` with the old file untouched and the
temporary removed; the caller decides whether losing the UPDATE matters.
What it cannot do is write the new record on a full disk -- nothing can.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def write_text_atomic(path: Path | str, text: str) -> None:
    """Make `path` read `text`, or raise `OSError` leaving `path` as it was."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
