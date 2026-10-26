"""The stream unit pins glibc's mmap threshold; the daemon publishes the split.

Measured 2026-10-26 02:22Z on the live `hyxlab-stream` (pid 89373, 11.8h
up): anon RSS 987 MiB, of which 753 MiB in 76 glibc malloc arena heaps --
the 64 MiB-aligned anonymous regions glibc reserves per non-main arena.
Regressed over the run's 136 stats lines, anon tracked UPTIME, not load
(50.2 MiB/h with event rate controlled; R2 0.90 vs 0.54 for rate alone),
so `rss_anon_mb` had been read as "the expanded universe is bigger" for
two days while it was allocator retention. Rehearsed on the real
`StreamStore.flush` (20 x 150k rows): the default allocator retains
+139 MiB, `MALLOC_MMAP_THRESHOLD_=131072` +44 with the arenas holding
2 MiB -- glibc's DYNAMIC threshold is the mechanism: a freed mmapped
chunk raises it, the next large chunk comes from an arena, and stays.

The precondition lives in the unit file, not in code the tests import
(#87's rule: test the precondition where it lives). The daemon's own
stats line carries `arena_mb=<MiB>/<heaps>` so the production verdict --
flat across a day -- is readable from the journal.
"""

import re
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "scripts/systemd/hyxlab-stream.service"

#: 128 KiB: glibc's default INITIAL threshold, pinned so it stops moving.
MMAP_THRESHOLD = 131072


def test_stream_unit_pins_glibc_mmap_threshold():
    text = UNIT.read_text()
    lines = [ln for ln in text.splitlines() if ln.startswith("Environment=MALLOC_")]
    assert lines == [f"Environment=MALLOC_MMAP_THRESHOLD_={MMAP_THRESHOLD}"], lines
    # The number is a measured choice; the measurement must sit beside it.
    assert re.search(r"^# .*arenas at 2 MiB", text, re.M), "the rehearsal that picked 128 KiB"


def test_env_line_is_under_the_cap_it_protects():
    # Order is documentation: the threshold sits with MemoryMax, whose
    # headroom it preserves, not loose at the bottom of [Service].
    text = UNIT.read_text()
    assert text.index("MemoryMax=4G") < text.index("Environment=MALLOC_MMAP_THRESHOLD_")
    assert text.index("Environment=MALLOC_MMAP_THRESHOLD_") < text.index("ExecStopPost=")
