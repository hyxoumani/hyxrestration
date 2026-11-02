"""Every reader that feeds `stream_gaps` rows into `replay_snapshots`
selects the `shard` column (2026-11-02): a two-tuple read is the
pre-11-02 channel-wide wipe, and a replay that wipes channel-wide while
the shadow wipes per-shard turns gap semantics into a false divergence
in the one report whose job is shadow-vs-replay.

Source-scraping, so it pins the SELECT text, not the behaviour; the
behaviour is `tests/test_hyxlab_bookreplay.py`'s three-tuple test."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
READERS = [
    "simulator/shadow.py",
    "simulator/divergence.py",
    "simulator/run_l2.py",
    "simulator/queuescore.py",
    "simulator/simui/session.py",
]
TWO_TUPLE = re.compile(r"SELECT started_at, ended_at FROM stream_gaps")
THREE_TUPLE = re.compile(r"SELECT started_at, ended_at, shard FROM stream_gaps")


def test_every_replay_feeding_gap_read_selects_the_shard():
    for rel in READERS:
        src = (ROOT / rel).read_text()
        assert THREE_TUPLE.search(src), rel
        assert not TWO_TUPLE.search(src), rel
