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


# -- the Store side, which is where most of the debt was ----------------


def test_held_open_charges_the_hold_of_a_store_attach(tmp_path):
    """`open_retry` is how the 5-minute collector's own file is attached, so
    a hold here is a DROPPED capture cycle rather than a delayed report --
    and it had no instrument at all until `held_open`."""
    with store_mod.held_open(tmp_path / "s.duckdb") as store:
        assert store.conn.execute("SELECT 1").fetchone() == (1,)
        time.sleep(0.05)
    (row,) = store_mod.attach_waits()
    assert row.held_s is not None and row.held_s >= 0.05
    block = attach_wait_block(rows=False)
    assert block["held_n"] == 1 and block["held_unknown_n"] == 0


def test_held_open_closes_the_store_and_records_a_hold_that_raised(tmp_path):
    """Both halves of `held_attach`'s contract, at the twin: the file is
    released by the block, and a body that died still says how long it
    held."""
    db = tmp_path / "s2.duckdb"
    with pytest.raises(RuntimeError), store_mod.held_open(db) as store:
        time.sleep(0.02)
        raise RuntimeError("burst died")
    (row,) = store_mod.attach_waits()
    assert row.held_s is not None and row.held_s >= 0.02
    with pytest.raises(Exception):
        store.conn.execute("SELECT 1")


def test_a_wrapper_borrows_charge_hold_rather_than_reintroducing_the_debt(tmp_path):
    """`simulator.shadow.stream_conn` cannot BE `held_attach` -- it lowers
    `memory_limit` after the attach and re-derives the spill bound, so it
    owns the connection between the open and its caller. A wrapper that
    cannot borrow the seam is a wrapper that publishes
    `held_unknown_n`, which is how the 45m45s replay looked clean."""
    from simulator.shadow import held_stream_conn

    db = tmp_path / "stream.duckdb"
    store_mod.connect_retry(db, read_only=False).close()
    reset_attach_waits()
    with held_stream_conn(str(db)) as conn:
        conn.execute("SELECT 1").fetchone()
        time.sleep(0.05)
    block = attach_wait_block(rows=False)
    assert block["held_n"] == 1 and block["held_unknown_n"] == 0
    assert block["held_s_max"] >= 0.05


# -- the named holders, each of which published waits and no hold -------


def test_the_sweep_burst_publishes_what_it_cost_the_collector(tmp_path):
    """`writer_burst` exists BECAUSE the hold is the harm: the whole-run
    hold it replaced dropped 421 of 3,706 capture cycles (its docstring).
    It has published the waits since mistakes #70 and never the hold, so
    the claim that the burst is short was the one number missing from the
    only artifact that could carry it."""
    import collector.sweep as sweep

    db = str(tmp_path / "t.duckdb")
    lock = str(tmp_path / "w.lock")
    reset_attach_waits()
    for _ in range(2):
        with sweep.writer_burst(db, lock_file=lock):
            time.sleep(0.02)
    block = attach_wait_block(rows=False)
    assert block["n"] == 2
    assert block["held_n"] == 2, "a burst that does not measure its hold is the old whole-run hold"
    assert block["held_unknown_n"] == 0
    # The sum, not the max: a sweep's exclusion of the archive is ~2 bursts
    # x ~3,709 series, and one short burst says nothing about that total.
    assert block["held_s_total"] >= 0.04


def test_the_atlas_hold_is_measured_and_the_block_is_built_after_the_release(monkeypatch, tmp_path):
    """`build_atlas` scans the settled corpus of the file the collector
    writes every 5 minutes. THE ORDERING IS THE TEST: `held_s` is charged
    by the close, so a block assembled one line before the release
    publishes `held_unknown_n 1` against the very attach it describes."""
    import json

    from simulator import atlas
    from tests.test_attach_wait import EMPTY_ATLAS

    db = tmp_path / "a.duckdb"
    store_mod.connect_retry(db, read_only=False).close()

    def slow_build(conn):
        time.sleep(0.05)
        return dict(EMPTY_ATLAS)

    monkeypatch.setattr(atlas, "build_atlas", slow_build)
    monkeypatch.setattr(atlas, "tier_stability", lambda *a: None)
    monkeypatch.setattr(
        atlas, "verdict_stability", lambda *a: {"quoted_verdict": {"delta_vs_prior": None}}
    )
    monkeypatch.setattr(atlas, "annotate_quoted_looks", lambda *a: None)
    monkeypatch.setattr(atlas, "TIERS", ())
    reset_attach_waits()
    monkeypatch.setattr("sys.argv", ["atlas", "--db", str(db), "--out", str(tmp_path / "rep")])
    atlas.main()

    (written,) = (tmp_path / "rep").glob("*.json")
    block = json.loads(written.read_text())["attach_wait"]
    assert block["held_n"] == 1 and block["held_unknown_n"] == 0
    assert block["held_s_max"] >= 0.05


def test_an_l2_replay_publishes_the_hold_it_took_on_both_files(tmp_path):
    """The ad-hoc replay is the holder nobody watches: same seed-then-trade
    shape, same daemon-owned file, as the divergence run that held
    `hyxstream.duckdb` 45m45s -- and it wrote no attach block at all. Two
    attaches, both instrumented; `held_unknown_n` is what a third,
    forgotten one would print."""
    import json
    from datetime import timedelta

    from simulator.run_l2 import run_l2
    from tests.test_hyxlab_run_l2 import T0, _build_archives

    archive_db, stream_db = _build_archives(tmp_path)
    reset_attach_waits()
    manifest, _, _ = run_l2(
        ["hylshi_fade"],
        (T0 - timedelta(minutes=10)).replace(tzinfo=None),
        (T0 + timedelta(minutes=10)).replace(tzinfo=None),
        stream_db=str(stream_db),
        archive_db=str(archive_db),
        prefix="KXLOWT",
        latency=0.0,
        runs_dir=str(tmp_path / "runs"),
    )
    block = json.loads(manifest.read_text())["data"]["attach_wait"]
    assert block["n"] == 2
    assert block["held_n"] == 2 and block["held_unknown_n"] == 0
    # The stream hold spans the seed replay AND the trading window, so it is
    # the larger of the two by construction -- the shape the fix has to keep
    # visible, since it is the one that starves `collector.streamd`.
    assert block["held_s_max"] > 0.0


def test_a_refused_attach_is_not_counted_as_an_unmeasured_hold(monkeypatch):
    """`held_unknown_n` means "holds nobody measured", not "attaches with
    no hold", and an exhausted attach is the second without being the
    first: `connect_retry` records its row from the `except` on the last
    attempt, so the file was never opened and the hold is a MEASURED zero.

    Invisible while every publisher was a batch report, because those
    raise out of the run a refusal happens in and never reach a block.
    `simulator.shadow` is the first publisher that SURVIVES one --
    `poll_once` swallows `duckdb.Error` and polls again ~20s later, for
    the daemon's whole life -- so conflating the two would give it a
    forever-rising `held_unknown_n` that reads as instrumentation debt
    and is really contention, already reported one field to the left.
    """
    import duckdb

    from hyxlab.store import attach_waits, connect_retry

    reset_attach_waits()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(
        store_mod.duckdb, "connect", lambda *a, **k: (_ for _ in ()).throw(duckdb.Error("locked"))
    )
    with pytest.raises(duckdb.Error):
        connect_retry("nope.duckdb", retries=2, delay=0.0)
    (w,) = attach_waits()
    assert w.ok is False and w.held_s is None
    block = attach_wait_block()
    assert block["n"] == 1 and block["exhausted_n"] == 1
    # The refusal is reported, once, as what it is.
    assert block["held_n"] == 0 and block["held_unknown_n"] == 0


def test_an_unmeasured_hold_is_still_counted_when_the_attach_succeeded(tmp_path):
    """The other arm, so the exclusion above cannot be read as a licence:
    an attach that GOT IN and took no `charge_hold` is exactly the debt the
    field names, and it still prints."""
    from hyxlab.store import connect_retry

    reset_attach_waits()
    connect_retry(tmp_path / "ok.duckdb", read_only=False).close()
    block = attach_wait_block()
    assert block["exhausted_n"] == 0
    assert block["held_n"] == 0 and block["held_unknown_n"] == 1
