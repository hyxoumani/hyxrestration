"""A hold excludes ONE file, so a max and a sum over holds need one lock.

THE MISTAKE THIS FILE IS. The hold half of `attach_wait_block`
(`held_n`/`held_unknown_n`/`held_s_max`/`held_s_total`) was built in
answer to mistakes #91: `simulator.divergence` held `hyxstream.duckdb`
for 45m45s and cost `streamd` 387,856 rows to the torn-append sidecar,
while publishing an `attach_wait` block that called the event perfect
because every field in it measured what contention cost the READER.

The `db` scope that narrows a block to one file shipped the pass after
(#96) and reached exactly one publisher -- `simulator.shadow`, whose
artifact is named after a file (`shadow_stream_holds`). The other four
kept calling `attach_wait_block()` with no scope, and an unscoped block
is not "no claim about a file": it is a claim about every file the
process touched, summed.

MEASURED, from the production artifact
`reports/shadow_divergence/20260912T023431.json`::

    "held_s_max": 7.213, "held_s_total": 7.398
    hyxshadow.duckdb held 0.083s
    hyxstream.duckdb held 7.213s
    hyxlab.duckdb    held 0.102s

The headline max is the STREAM archive's hold wearing the report's own
name, and the total adds seconds spent excluding three different
writers -- two of them 24/7 daemons -- as though they were one duration.
The module publishing it is the module #91 is about.

`simulator.run_l2` is the worse arm: its archive read is nested INSIDE
the stream hold (the call site's own comment says "nested INSIDE the
stream hold above ... both files"), so the inner seconds are inside the
outer ones and `held_s_total` is not a duration at all. Splitting by
file settles that too, because every nesting here is cross-file.

THE RULE: the scalars are published when the quantity exists -- holds on
a single file -- and `None` when the holds span several, never a number
summing different locks. `held_by_db` always carries the per-file
reading, and the line renders it.
"""

from __future__ import annotations

from hyxlab.store import (
    AttachWait,
    attach_wait_block,
    attach_wait_line,
    reset_attach_waits,
)

#: The 2026-09-12 divergence report, verbatim: three files, one block.
DIVERGENCE_0912 = (
    ("hyxshadow.duckdb", 28.0, 0.083),
    ("hyxstream.duckdb", 638.7, 7.213),
    ("hyxlab.duckdb", 58.0, 0.102),
)


def _rows(spec) -> list[AttachWait]:
    out = []
    for name, budget, held in spec:
        w = AttachWait(name, 1, 0.01, budget, True, 0.0)
        w.held_s = held
        out.append(w)
    return out


def test_the_divergence_block_no_longer_sums_three_archives():
    """The regression, on the population that produced it."""
    reset_attach_waits()
    block = attach_wait_block(_rows(DIVERGENCE_0912))
    # The old block said 7.398 / 7.213 here. Both are gone, because
    # neither is a quantity: they span three locks.
    assert block["held_s_total"] is None
    assert block["held_s_max"] is None
    # ... and the reading is not lost, only attributed.
    assert block["held_by_db"] == {
        "hyxlab.duckdb": {"held_n": 1, "held_s_max": 0.102, "held_s_total": 0.102},
        "hyxshadow.duckdb": {"held_n": 1, "held_s_max": 0.083, "held_s_total": 0.083},
        "hyxstream.duckdb": {"held_n": 1, "held_s_max": 7.213, "held_s_total": 7.213},
    }
    # The count IS summable across locks -- "how many holds did this
    # process take" has one answer -- so it stays a scalar.
    assert block["held_n"] == 3


def test_the_stream_holds_stop_being_the_reports_headline_number():
    """#91's own file, named in the line rather than anonymous in a max.

    A reader of the old line could not tell 7.2s on the daemon-owned
    stream archive from 7.2s on a file nothing else wants.
    """
    reset_attach_waits()
    line = attach_wait_line(attach_wait_block(_rows(DIVERGENCE_0912)), prefix="[divergence]")
    assert "held 3 across 3 files" in line
    assert "hyxstream.duckdb 1 for 7.2s (worst 7.2s)" in line
    # The bare sum must not appear anywhere in the line.
    assert "7.4s" not in line


def test_a_single_file_still_has_a_total_and_the_line_names_the_file():
    """The sweep's shape: 7,550 serial bursts on one archive.

    A max and a sum over one lock are real, so they stay -- but the
    line says WHICH lock, because `collector.sweep` writes no JSON and
    that line is the whole artifact.
    """
    reset_attach_waits()
    block = attach_wait_block(_rows([("hyxlab.duckdb", 298.0, 1.5)] * 3))
    assert block["held_s_total"] == 4.5
    assert block["held_s_max"] == 1.5
    assert set(block["held_by_db"]) == {"hyxlab.duckdb"}
    assert "held 3 on hyxlab.duckdb for 4.5s in total, worst 1.5s" in attach_wait_line(
        block, prefix="[sweep]"
    )


def test_a_db_scoped_block_is_unchanged_and_carries_no_breakdown():
    """`simulator.shadow` already names its file; a per-db map under a
    block that is already one db would be the same number twice."""
    reset_attach_waits()
    rows = _rows(DIVERGENCE_0912)
    block = attach_wait_block(rows, db="hyxstream.duckdb")
    assert block["db"] == "hyxstream.duckdb"
    assert block["held_s_total"] == 7.213
    assert block["held_s_max"] == 7.213
    assert "held_by_db" not in block
    assert " on hyxstream.duckdb " in attach_wait_line(block, prefix="[shadow]")


def test_a_file_that_attached_without_holding_is_not_counted_as_a_file():
    """`held_by_db` is the files this process EXCLUDED, not the files it
    opened -- otherwise an uninstrumented attach pads the count the line
    prints and the scalars vanish for a run that held exactly one lock."""
    reset_attach_waits()
    rows = _rows([("hyxlab.duckdb", 30.0, 2.0)])
    rows.append(AttachWait("hyxstream.duckdb", 1, 0.01, 30.0, True, 0.0))  # held_s None
    block = attach_wait_block(rows)
    assert set(block["held_by_db"]) == {"hyxlab.duckdb"}
    assert block["held_s_total"] == 2.0  # still a quantity: one lock held
    assert block["held_unknown_n"] == 1


def test_no_hold_at_all_keeps_zero_rather_than_none():
    """Zero holds span no locks, so there is nothing to disagree about
    and the `held_n` branch of the line is reached exactly as before."""
    reset_attach_waits()
    block = attach_wait_block([AttachWait("hyxlab.duckdb", 1, 0.01, 30.0, True, 0.0)])
    assert block["held_s_total"] == 0.0
    assert block["held_by_db"] == {}
    assert "no hold measured" in attach_wait_line(block, prefix="[t]")


def test_the_nested_hold_is_split_rather_than_double_counted():
    """`simulator.run_l2` holds the archive INSIDE the stream hold, so the
    inner seconds are also inside the outer ones: a sum over both is
    longer than the wall clock it describes. Per-file splitting is the
    whole fix, because the nesting is cross-file by construction."""
    reset_attach_waits()
    outer = AttachWait("hyxstream.duckdb", 1, 0.01, 638.7, True, 0.0)
    outer.held_s = 50.6  # the replay's whole stream hold
    inner = AttachWait("hyxlab.duckdb", 1, 0.01, 58.0, True, 0.0)
    inner.held_s = 3.0  # the markets read, entirely inside the above
    block = attach_wait_block([outer, inner])
    # 53.6s of exclusion never happened; only 50.6s of wall clock did.
    assert block["held_s_total"] is None
    assert block["held_by_db"]["hyxstream.duckdb"]["held_s_total"] == 50.6
    assert block["held_by_db"]["hyxlab.duckdb"]["held_s_total"] == 3.0


def test_the_live_ledger_path_splits_by_db_too():
    """Not just the explicit-population path: the process-wide totals are
    what every production publisher actually reads."""
    from hyxlab import store

    reset_attach_waits()
    for name, held in (("a.duckdb", 1.0), ("b.duckdb", 2.0), ("a.duckdb", 4.0)):
        w = store._record_attach(name, 1, 0.01, 30.0, True)
        store._ATTACH_TOTALS.add_hold(held)
        store._ATTACH_TOTALS_BY_DB[w.db].add_hold(held)
    block = attach_wait_block(rows=False)
    assert block["held_s_total"] is None
    assert block["held_by_db"]["a.duckdb"] == {
        "held_n": 2,
        "held_s_max": 4.0,
        "held_s_total": 5.0,
    }
    assert block["held_by_db"]["b.duckdb"]["held_s_total"] == 2.0
