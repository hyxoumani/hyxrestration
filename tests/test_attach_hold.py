"""What an attach cost EVERYONE ELSE, which the ledger never recorded.

MEASURED INCIDENT, 2026-09-25. `hyxlab-divergence` attached the live
`hyxstream.duckdb` and held it for 45m45s across its replay, which cost
`collector.streamd` 22 stall episodes, a peak of 400,154 rows held in
RAM, and 387,856 rows pushed past `SPILL_CAP` into the JSONL sidecar
whose torn-append path is a known archive-hole class.

THE ATTACH LEDGER CALLED THAT ATTACH PERFECT, AND WAS RIGHT TO. Every
field it had -- `attempts`, `waited_s`, `slept_s`, `open_s`,
`budget_frac` -- measures the cost of getting IN, which is the harm a
contended reader SUFFERS. That attach got in on its first attempt in
about 10ms and spent 0.0s of its 30s budget. The harm it DID had no
field, so the instrument built to make lock contention legible was
structurally blind to the worst lock event this box has recorded.

The consequence was not only that the incident went unseen. The copy-out
that fixed it (hold 2,745s -> 6.2s, measured on a backup) could only be
confirmed a pass later and only INDIRECTLY, by noticing that streamd's
stall ledger stayed silent across the first run of the fixed code
(2026-09-26 02:34-03:20Z, zero episodes, against 3,002.8s and 258,556
rows spilled for the OLD closure's 01:20Z run the same morning). That
inference is sound only while the victim happens to be flushing into the
window; it is not a measurement of the holder, and the holder is what
changed.

So the hold is recorded at the holder, by `held_attach`, and published
beside the waits. `held_s` is None and never 0.0 when unmeasured, and
the block counts the unmeasured separately: an uninstrumented
45-minute reader must not be able to publish a flawless maximum.
"""

from __future__ import annotations

import time

import pytest

from hyxlab import store as store_mod
from hyxlab.store import attach_wait_block, held_attach, reset_attach_waits


@pytest.fixture(autouse=True)
def _clean():
    reset_attach_waits()
    yield
    reset_attach_waits()


def test_the_hold_is_charged_to_the_attach_that_took_it(tmp_path):
    """The whole point: after the block, the ledger says how long the file
    was held, not merely how cheaply it was opened."""
    db = tmp_path / "a.duckdb"
    with held_attach(db, read_only=False):
        time.sleep(0.05)
    (row,) = store_mod.attach_waits()
    assert row.held_s is not None and row.held_s >= 0.05
    # And the acquisition fields still describe the acquisition -- an attach
    # that opened instantly and held forever must read as exactly that.
    assert row.slept_s == 0.0
    assert row.attempts == 1


def test_an_unmeasured_hold_is_none_and_never_zero(tmp_path):
    """`connect_retry` without the context manager measures no hold. 0.0
    there would mean "held no time at all", which is the opposite finding
    and the flattering one."""
    db = tmp_path / "b.duckdb"
    store_mod.connect_retry(db, read_only=False).close()
    (row,) = store_mod.attach_waits()
    assert row.held_s is None
    assert row.as_dict()["held_s"] is None


def test_the_block_counts_the_unmeasured_rather_than_averaging_them_away(tmp_path):
    """A run that instruments one of its two attaches has measured HALF its
    exposure, and the block has to say so. Folding the unmeasured in as 0.0
    is how a report certifies a hold nobody watched."""
    with held_attach(tmp_path / "c.duckdb", read_only=False):
        time.sleep(0.02)
    store_mod.connect_retry(tmp_path / "d.duckdb", read_only=False).close()
    block = attach_wait_block(rows=False)
    assert block["n"] == 2
    assert block["held_n"] == 1
    assert block["held_unknown_n"] == 1
    assert block["held_s_max"] >= 0.02


def test_the_hold_is_recorded_even_when_the_body_raises(tmp_path):
    """A replay that dies at minute 40 held the file for forty minutes. The
    reading a failed run leaves behind is the one its next pass starts from,
    and `finally` is the only place it can be written."""
    db = tmp_path / "e.duckdb"
    with pytest.raises(RuntimeError), held_attach(db, read_only=False):
        time.sleep(0.02)
        raise RuntimeError("replay died")
    (row,) = store_mod.attach_waits()
    assert row.held_s is not None and row.held_s >= 0.02


def test_the_hold_survives_the_trim_that_evicts_its_row(tmp_path):
    """The sample-vs-population trap (mistakes #72), one field later: the
    hold arrives at CLOSE, by which time a busy run may already have
    trimmed the row away. A max that lives only in the retained rows is
    the statistic a drop destroys first."""
    with held_attach(tmp_path / "f.duckdb", read_only=False):
        time.sleep(0.05)
    for i in range(store_mod._ATTACH_WAITS_MAX + 10):
        store_mod._record_attach(f"flood{i}.duckdb", 1, 0.001, 30.0, True, 0.0)
    assert all(w.held_s is None for w in store_mod.attach_waits())
    block = attach_wait_block(rows=False)
    assert block["held_n"] == 1
    assert block["held_s_max"] >= 0.05


def test_the_connection_is_closed_by_the_block(tmp_path):
    """The hold ENDS at the close, so a context manager that measured the
    hold but leaked the handle would report a number while the harm
    continued."""
    db = tmp_path / "g.duckdb"
    with held_attach(db, read_only=False) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(Exception):
        conn.execute("SELECT 1")


def test_a_reset_clears_the_hold_totals_too(tmp_path):
    """`reset_attach_waits` scopes a block to one run. A hold surviving it
    would haunt the next report in the same process with the last one's
    worst number."""
    with held_attach(tmp_path / "h.duckdb", read_only=False):
        time.sleep(0.02)
    reset_attach_waits()
    assert attach_wait_block(rows=False) is None


def test_an_explicit_population_carries_its_holds(tmp_path):
    """`attach_wait_block(waits=...)` rebuilds the totals from the rows, so
    a row that already knows its hold must be counted once -- not zero
    times, and not twice."""
    rows = [
        store_mod.AttachWait("a.duckdb", 1, 0.01, 30.0, True, 0.0, 12.0),
        store_mod.AttachWait("b.duckdb", 1, 0.01, 30.0, True, 0.0),
    ]
    block = attach_wait_block(rows)
    assert block["held_n"] == 1
    assert block["held_unknown_n"] == 1
    assert block["held_s_max"] == 12.0
    assert block["held_s_total"] == 12.0
