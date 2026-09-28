"""One renderer for the attach block, and it must name every field.

THE MISTAKE THIS FILE IS. `attach_wait_block` gained four fields on
2026-09-26 -- the hold half, `held_n`/`held_unknown_n`/`held_s_max`/
`held_s_total`, the whole point of that day's work. Four of the five
publishers pick a new field up for free, because they serialise the dict
(`atlas`, `divergence`, `run_l2` into JSON artifacts; `shadow` through
`json.dumps` and a ledger row). The fifth, `collector.sweep`, writes NO
artifact: its one journal line is a hand-written f-string naming seven
keys, and it was not touched.

So the 2026-09-27 06:10Z sweep -- the first and so far only run of the
widest ladder in this repo (150 x 2.0s, 7,549 attaches, two bursts per
series over ~3,709 series) to measure its own hold -- computed
`held_s_total` for the archive the 5-minute collector writes, and
dropped it at the format string. The journal has the line; it reads
`7549 attaches, 13 contended, worst sleep 2.0s ... 0 exhausted` and
stops. The number the 08-02 burst fix claimed to shrink is still
unprinted, one pass after the instrument to print it shipped, and that
run's copy is gone.

THE RULE: the block is rendered in exactly one place
(`hyxlab.store.attach_wait_line`), and that renderer names every scalar
key a real block carries. Then a field added to the block fails HERE,
in the suite, rather than going missing in a daemon's journal for a day.
"""

from __future__ import annotations

import ast
from pathlib import Path

from hyxlab import store
from hyxlab.store import (
    AttachWait,
    attach_wait_block,
    attach_wait_line,
    reset_attach_waits,
)

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("collector", "simulator", "strategies", "hyxlab")

#: The one non-scalar key: the per-attach sample, which a one-line
#: summary cannot carry and `rows=False` omits anyway.
NOT_RENDERABLE = {"attaches"}
#: Excluded from the SECOND-RENDERER scan only (the completeness test above
#: still requires it): `n` is any dict's count key, and three unrelated
#: reports subscript an `["n"]` of their own in an f-string. A line that
#: renders the block names more than its count, so the scan loses nothing
#: real by dropping the one ambiguous key rather than carrying a
#: per-module allowlist that would also hide a genuine offender.
AMBIGUOUS = {"n"}


def _block() -> dict:
    reset_attach_waits()
    rows = [
        AttachWait("a.duckdb", 1, 0.010, 30.0, True, 0.0),
        AttachWait("a.duckdb", 3, 4.100, 30.0, True, 4.0),
        AttachWait("a.duckdb", 15, 30.0, 30.0, False, 28.0),
    ]
    rows[0].held_s = 0.4
    rows[1].held_s = 2745.0
    return attach_wait_block(rows)


def _rich_block(db: str | None = None) -> dict:
    """A block whose every scalar field is non-zero, so no clause of the
    line is switched off while the test is looking. Values are OVERWRITTEN
    on a real block rather than written out as a literal: the keys have to
    come from `attach_wait_block`, or a new field would be missing from the
    fixture exactly as it is missing from the line."""
    reset_attach_waits()
    for _ in range(store._ATTACH_WAITS_MAX + 5):
        store._record_attach("a.duckdb", 1, 0.01, 30.0, True)
    block = attach_wait_block(rows=False, db=db)
    for i, (k, v) in enumerate(block.items()):
        if k in NOT_RENDERABLE:
            continue
        if isinstance(v, str):
            continue  # a label, not a magnitude; it is already distinctive
        if isinstance(v, dict):
            # `held_by_db`: a per-file mapping, so the magnitudes are one
            # level down. Flattening it to a number here would hide every
            # sub-field from the perturbation below -- which is how a
            # renderer that prints the file names and drops their seconds
            # would pass.
            block[k] = {
                name: {
                    kk: 11 + j if isinstance(vv, int) else 11.5 + j
                    for j, (kk, vv) in enumerate(sub.items())
                }
                for name, sub in (
                    v or {"a.duckdb": {"held_n": 0, "held_s_max": 0.0, "held_s_total": 0.0}}
                ).items()
            }
            continue
        block[k] = 11 + i if isinstance(v, int) else 11.5 + i
    return block


def _perturbations(block: dict):
    """Every renderable scalar a block carries, as (label, mutated block) --
    descending into the per-file hold mapping, whose sub-fields are the
    only place the hold numbers live once the holds span more than one
    lock (2026-09-28)."""
    for k, v in block.items():
        if k in NOT_RENDERABLE:
            continue
        if v is None:
            # Not a magnitude. `held_s_max`/`held_s_total` are None exactly
            # when the holds span several locks, and that None is what
            # SELECTS the per-file clause -- its role is covered by the
            # multi-file test below, not by bumping it into a string the
            # renderer would then try to format.
            continue
        if isinstance(v, dict):
            if block.get("held_s_total") is not None:
                # One file held, so `held_by_db[f]` is the scalars again
                # under another name; the scalars are perturbed directly
                # above. Bumping a sub-field alone here builds a block that
                # cannot occur, and the line is right to ignore it.
                continue
            for name, sub in v.items():
                for kk, vv in sub.items():
                    bumped = {
                        **sub,
                        kk: vv + 100 if isinstance(vv, int | float) else f"{vv}-bumped",
                    }
                    yield f"{k}[{name}].{kk}", {**block, k: {**v, name: bumped}}
            continue
        # Strings get perturbed too. `v if not a number` would compare a
        # block against ITSELF and report every non-numeric field as deaf
        # -- or, with the field skipped instead, wave `db` through: a
        # scope label is exactly the kind of field a line can omit while
        # every number in it stays right.
        yield k, {**block, k: v + 100 if isinstance(v, int | float) else f"{v}-bumped"}


def test_every_field_a_db_scoped_block_carries_changes_the_line():
    """The same perturbation over the block shape that names ONE file.

    `db` is only present when the publisher asked for a scope, so the
    unscoped run below cannot see it -- and a scope label dropped at the
    format string is a line that reports the stream archive's holds under
    the shared archive's name."""
    base = _rich_block(db="a.duckdb")
    assert base["db"] == "a.duckdb"
    line = attach_wait_line(base, prefix="[t]", budget_s=30.0)
    deaf = [
        label
        for label, bumped in _perturbations(base)
        if attach_wait_line(bumped, prefix="[t]", budget_s=30.0) == line
    ]
    assert not deaf, f"a db-scoped block publishes {deaf} and the line drops it"


def test_every_field_the_block_carries_changes_the_line():
    """THE REGRESSION, mechanically -- by PERTURBATION, not by reading the
    renderer's source. A source scan passes against a renderer that reads a
    field into a local and then drops the local, which is one edit away from
    the defect itself; this asks the only question that matters, which is
    whether the number can reach the operator."""
    base = _rich_block()
    line = attach_wait_line(base, prefix="[t]", budget_s=30.0)
    deaf = [
        label
        for label, bumped in _perturbations(base)
        if attach_wait_line(bumped, prefix="[t]", budget_s=30.0) == line
    ]
    assert not deaf, (
        f"attach_wait_block publishes {deaf} and attach_wait_line does not"
        " print it — a measurement taken and thrown away at the format"
        " string, which is how the 2026-09-27 06:10Z sweep lost the first"
        " held_s_total ever measured for the writer burst"
    )


def test_the_hold_half_reaches_the_line():
    line = attach_wait_line(_block(), prefix="[t]", budget_s=30.0)
    # The file is NAMED even on a single-file block: this line is the
    # sweep's whole artifact, and "held 2 for 2745.4s" does not say what
    # was excluded for that time (2026-09-28).
    assert "held 2 on a.duckdb for 2745.4s in total, worst 2745.0s" in line
    # The uninstrumented attach is NAMED, never folded in as 0.0. Here it is
    # zero because the third row was REFUSED (it opened nothing, so it held
    # nothing -- measured, not unknown; mistakes #94).
    assert "0 unmeasured" in line
    assert "3 attaches" in line and "1 exhausted" in line


def test_an_unmeasured_hold_is_named_rather_than_averaged_in():
    reset_attach_waits()
    rows = [AttachWait("a.duckdb", 1, 0.01, 30.0, True, 0.0) for _ in range(3)]
    rows[0].held_s = 1.0
    line = attach_wait_line(attach_wait_block(rows), prefix="[t]")
    assert "held 1 on a.duckdb for 1.0s in total" in line
    assert "2 unmeasured" in line
    # no budget passed -> the share prints without a denominator it would
    # otherwise have to invent (#71's atlas literal).
    assert "budget" not in line


def test_no_hold_measured_is_not_a_zero():
    reset_attach_waits()
    line = attach_wait_line(
        attach_wait_block([AttachWait("a.duckdb", 1, 0.01, 30.0, True, 0.0)]), prefix="[t]"
    )
    assert "no hold measured, 1 unmeasured" in line
    assert "0.0s in total" not in line


def test_a_trimmed_sample_says_so_and_a_whole_one_stays_quiet():
    """The statistics come from the untrimmed totals (mistakes #72); the
    caveat is about the ROWS, so it prints only when rows were dropped."""
    reset_attach_waits()
    for _ in range(store._ATTACH_WAITS_MAX + 5):
        store._record_attach("a.duckdb", 1, 0.01, 30.0, True)
    line = attach_wait_line(attach_wait_block(rows=False), prefix="[t]")
    assert f"statistics over all {store._ATTACH_WAITS_MAX + 5}" in line
    assert "5 dropped" in line
    assert "dropped" not in attach_wait_line(_block(), prefix="[t]")


def test_an_empty_block_says_nothing_attached_rather_than_zeros():
    assert "nothing attached" in attach_wait_line(None, prefix="[t]")


def _fstring_subscripts(path: Path) -> set[str]:
    """String keys subscripted inside an f-string in `path`."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.JoinedStr):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Subscript)
                and isinstance(sub.slice, ast.Constant)
                and isinstance(sub.slice.value, str)
            ):
                out.add(sub.slice.value)
    return out


def test_nobody_hand_formats_the_block_any_more():
    """Set membership, over the whole repo rather than the publisher list:
    a module that formats these keys into a line of its own is a second
    renderer, and the second renderer is the one that misses the next
    field. Serialising the dict whole (`json.dumps`) and reading single
    keys for a DB row are both fine -- neither can drop a field silently.
    """
    keys = set(_block()) - NOT_RENDERABLE - AMBIGUOUS
    offenders = {}
    for pkg in PACKAGES:
        for p in sorted((ROOT / pkg).rglob("*.py")):
            if p.name == "store.py":
                continue  # the renderer itself
            hit = keys & _fstring_subscripts(p)
            if hit:
                offenders[p.relative_to(ROOT).as_posix()] = sorted(hit)
    assert not offenders, (
        f"attach-block fields formatted outside attach_wait_line: {offenders}"
        " — use hyxlab.store.attach_wait_line so a new field cannot be"
        " dropped by one publisher"
    )


def _rich_multi_db_block() -> dict:
    """A block whose holds span THREE files -- the shape
    `simulator.divergence` publishes, and the only shape in which the
    per-file numbers are the sole carrier of the hold reading."""
    reset_attach_waits()
    # Through the LIVE ledger, not an explicit population: an explicit list
    # IS the population, so `dropped_n` is 0 by definition and the #72
    # sample caveat -- which is where `retained_n` reaches the line -- never
    # prints. A fixture that switches a clause off hides every field in it.
    names = ("x.duckdb", "y.duckdb", "z.duckdb")
    for i in range(store._ATTACH_WAITS_MAX + 5):
        w = store._record_attach(names[i % 3], 1, 0.01, 30.0, True, 0.5)
        store._ATTACH_TOTALS.add_hold(1.5 + i % 3)
        store._ATTACH_TOTALS_BY_DB[w.db].add_hold(1.5 + i % 3)
    block = attach_wait_block(rows=False)
    assert block["held_s_total"] is None, "fixture must span several locks"
    assert block["dropped_n"], "fixture must trim, or retained_n has no clause"
    return block


def test_every_per_file_hold_reaches_the_line():
    """The hold half, once it is per file, by the same PERTURBATION the
    scalars get.

    `simulator.divergence` published `held_s_max: 7.213` over three
    archives, and that max was `hyxstream.duckdb` -- the file mistakes #91
    is about -- wearing the report's own name. Splitting by file only
    helps if every file's seconds actually reach the operator, so bump
    each one and require the line to move.
    """
    base = _rich_multi_db_block()
    line = attach_wait_line(base, prefix="[t]", budget_s=30.0)
    deaf = [
        label
        for label, bumped in _perturbations(base)
        if attach_wait_line(bumped, prefix="[t]", budget_s=30.0) == line
    ]
    assert not deaf, f"a multi-file block publishes {deaf} and the line drops it"


def test_a_file_that_held_is_named_in_the_line():
    """A per-file total nobody can attribute is the union again."""
    line = attach_wait_line(_rich_multi_db_block(), prefix="[t]")
    for name in ("x.duckdb", "y.duckdb", "z.duckdb"):
        assert name in line
