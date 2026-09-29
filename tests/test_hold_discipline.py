"""A report that publishes an attach block must measure its own holds.

THE ESCALATION (gotcha -> rule -> hook). `attach_wait` exists to make
lock contention legible, and for eight days it made one direction of it
legible: what an attach cost the report. On 2026-09-25 the divergence
replay published `attempts 1, waited_s 0.010, contended_n 0,
budget_frac 0.0` -- a flawless block -- while holding `hyxstream.duckdb`
45m45s and costing `collector.streamd` 387,856 rows to the torn-append
sidecar (mistakes #91). `held_attach`/`held_open` fix that at the four
holders that publish the block. This file is what stops the fifth from
being written bare.

THE RULE: in a module that publishes `attach_wait_block`, every
LEDGERED attach goes through the hold-measuring helpers. A bare
`connect_retry`/`open_retry` there is a hold nobody measured inside an
artifact that claims to report holds, and `held_unknown_n` would be its
only trace -- a number nothing currently reads.

THE "LEDGERED" CARVE-OUT IS GONE, 2026-09-28. It used to read: a hold is
charged to the attach row the open produced (`_LAST_ATTACH`), and
`duck_connect` and a bare `Store(...)` produce no row, so wrapping one in
`charge_hold` would charge its seconds to whatever unrelated attach
happened to be last -- a WRONG reading, worse than a missing one. True,
and an argument about the INSTRUMENT rather than about the hold: the
four sites it excused took the same file locks as everything this rule
covers, including an hourly read of `data/hyxlab.duckdb` (the file the
5-minute collector writes) from a 24/7 daemon. So the premise was fixed
instead of the enumeration extended -- both now record a one-attempt,
no-budget row, `held_duck`/`held_store` are their hold seams, and the
enumeration of the debt was DELETED rather than emptied, because a list
that can hold zero entries outlives the debt it was written for.

WHAT PAYING IT OFF COST, named because it is the reusable half: a
process-wide block used to mean "this process's retry ladders", and the
only ledgered attaches a daemon took were the ones it took against one
file. Ledgering the direct attach silently widened every such block to
every file the process touches -- `simulator.shadow` publishes
`shadow_stream_holds`, named after `hyxstream.duckdb`, and would have
started folding in its own ledger and the shared archive. So the block
gained a `db` scope (`_ATTACH_TOTALS_BY_DB`, untrimmed, because
filtering the 256-deep sample rows would report the tail of a run under
the run's name -- mistakes #72), and the line prints it.

THE LIMIT, CLOSED 2026-09-27. This rule reaches only publishers, and
`simulator.shadow._read_new` -- which attaches the same daemon-owned file
every ~20s and holds it across the boot-time seed replay (measured
2,084,503 rows at the 2026-07-31 promote) -- published no block at all,
so no assertion here could see it. Shadow now has somewhere to publish
(`shadow_stream_holds` in its own ledger, plus the 300s journal line) and
takes `held_stream_conn`, which brings it under every test below.

It arrives as the rule's FIRST WRAPPER CASE, and that needed one
concession: `stream_conn` takes the bare `connect_retry` and cannot BE
`held_attach`, because it lowers `memory_limit` after the attach and so
owns the connection between the open and its caller. The function-scoped
walker cannot tell that wrapper from a hold nobody measured, so it is in
`ALLOWED` -- and `test_shadow_reads_the_stream_archive_only_through_the`
`_measured_seam` is what keeps that concession from covering the callers,
which are the sites that actually hold the file.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("collector", "simulator", "strategies", "hyxlab")

#: Ledgered attaches -- they record an `AttachWait`, so a hold has a row
#: to be charged to -- taken WITHOUT measuring the hold.
#: EVERY attach helper that records a row -- which since 2026-09-28 is
#: every attach helper there is. `Store`/`duck_connect` joined the list by
#: being fixed, not by being noticed.
BARE = ("connect_retry", "open_retry", "Store", "duck_connect")
#: The five ways a hold gets charged. `charge_hold` is the seam for a
#: wrapper that owns its connection between the open and its caller
#: (`simulator.shadow.stream_conn` re-tunes the engine first).
HELD = ("held_attach", "held_open", "held_duck", "held_store", "charge_hold")

#: site -> why a publisher may attach without measuring the hold. Keys are
#: `relpath::qualname`. EMPTY ON PURPOSE: the four publishers all measure
#: every attach they take, and an exemption here has to argue that a hold
#: on a shared file harms nobody -- which is the assumption #91 was.
ALLOWED: dict[str, str] = {
    # NOT the "its hold harms nobody" argument -- this hold is the worst
    # one in the repo. The argument is that this function does not HAVE a
    # hold: it hands the connection straight to its caller, so the seconds
    # belong to the caller's body, and `held_stream_conn` (right below it,
    # same module) is where they get measured. `charge_hold` is public for
    # exactly this shape. The exemption is narrow because the companion
    # test forbids every caller from using this one directly.
    "simulator/shadow.py::stream_conn": "connect_retry",
    # The SAME shape, one publisher later (2026-09-28): `_connect_ro` runs
    # QA's own retry ladder -- `RETRY_SLEEP_S` steps, degrading to `None` so
    # a live writer is a SKIP and not a failure -- and hands the connection
    # to its caller, so the hold belongs to the section's body.
    # `collector.qa._held_ro`, right below it, is where those seconds get
    # measured, and the companion test forbids every section from taking this
    # one directly.
    "collector/qa.py::_connect_ro": "duck_connect",
}


def _modules() -> list[Path]:
    return [p for pkg in PACKAGES for p in sorted((ROOT / pkg).rglob("*.py"))]


def publishers() -> list[str]:
    """Modules that emit the attach block into an artifact."""
    return [
        p.relative_to(ROOT).as_posix() for p in _modules() if "attach_wait_block(" in p.read_text()
    ]


def _qualnames(tree: ast.AST) -> dict[ast.AST, str]:
    """Every node -> the dotted name of the function it sits in."""
    out: dict[ast.AST, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                for n in ast.walk(child):
                    out.setdefault(n, name)
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def _attaches_in_publishers(names: tuple[str, ...]) -> dict[str, str]:
    """`relpath::qualname` -> the attach helper it called.

    FUNCTION-SCOPED, like every other AST walker in this suite: a function
    is clean when it takes no bare ledgered attach at all. It cannot pair
    an attach with its own `charge_hold` -- the walker does not follow
    values -- so the runtime half (`held_unknown_n` in the published block)
    stays the backstop, which is why that field exists.
    """
    found: dict[str, str] = {}
    for rel in publishers():
        if rel == "hyxlab/store.py":
            continue  # the helpers themselves; they ARE the attach
        tree = ast.parse((ROOT / rel).read_text())
        owners = _qualnames(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in names:
                found[f"{rel}::{owners.get(node, '<module>')}"] = name
    return found


def test_a_publisher_measures_every_hold_it_takes():
    """THE REGRESSION, as a rule. Each of these four reports had a bare
    attach on 2026-09-25 and published a block that could not say so."""
    bare = {k: v for k, v in _attaches_in_publishers(BARE).items() if k not in ALLOWED}
    assert not bare, (
        "a module that publishes `attach_wait` attaches without measuring the"
        f" hold: {sorted(bare)} — use hyxlab.store.held_attach/held_open (or"
        " charge_hold, if a wrapper owns the connection), or add the site to"
        " ALLOWED with the argument that its hold harms nobody"
    )


def test_shadow_reads_the_stream_archive_only_through_the_measured_seam():
    """What `ALLOWED`'s one entry does NOT excuse.

    `stream_conn` is exempt because it owns no hold -- it yields the
    connection to a caller whose body is the hold. That argument collapses
    the moment a caller takes `stream_conn` directly, which is exactly what
    `_read_new` did for the daemon's whole life: every ~20s, plus the boot
    seed replay, on a file a 24/7 writer owns. So the exemption is paired
    with this: inside `simulator/shadow.py`, the only function allowed to
    name `stream_conn` is `held_stream_conn`.
    """
    tree = ast.parse((ROOT / "simulator/shadow.py").read_text())
    owners = _qualnames(tree)
    callers = {
        owners.get(node, "<module>")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "stream_conn"
    }
    assert callers == {"held_stream_conn"}, (
        "simulator.shadow attaches the stream archive without measuring the"
        f" hold, from: {sorted(callers - {'held_stream_conn'})} — use"
        " held_stream_conn"
    )


def test_qa_reads_the_live_archives_only_through_the_measured_seam():
    """The pairing `ALLOWED`'s second entry buys, and the reason it is worth
    having twice.

    QA is a DAILY reader of two files a 24/7 writer owns -- `hyxstream.duckdb`
    (streamd) and `hyxlab.duckdb` (the 5-minute collector, where a hold is a
    dropped capture cycle, not a late report) -- and it held each one for the
    length of a whole section while publishing no block at all. The
    exemption for `_connect_ro` is that it owns no hold; that argument dies
    the moment a section calls it directly, which is what all three sections
    did until 2026-09-28. So: inside `collector/qa.py`, the only function
    allowed to name `_connect_ro` is `_held_ro`.
    """
    tree = ast.parse((ROOT / "collector/qa.py").read_text())
    owners = _qualnames(tree)
    callers = {
        owners.get(node, "<module>")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_connect_ro"
    }
    assert callers == {"_held_ro"}, (
        "collector.qa attaches a live archive without measuring the hold,"
        f" from: {sorted(callers - {'_held_ro'})} — use _held_ro"
    )


def test_the_publisher_set_is_the_one_this_rule_was_written_for():
    """Set equality, both directions (mistakes #34): a renamed report drops
    out of the rule silently, and a new one must be brought under it
    deliberately. `hyxlab/store.py` defines the block and is not a
    publisher of it."""
    assert set(publishers()) == {
        # Not a report about data either -- the daily QA run, which reads
        # both live archives and, until 2026-09-28, was the largest hold in
        # this repo with nowhere to say so (the `simui`/`backup`/`bookreplay`/
        # `streamstore` attaches are the same debt, still unpaid).
        "collector/qa.py",
        "collector/sweep.py",
        "hyxlab/store.py",
        "simulator/atlas.py",
        "simulator/divergence.py",
        "simulator/run_l2.py",
        # Not a report -- a daemon. It publishes into its own ledger table
        # and its 300s journal line, which is what brought the repo's
        # largest unmeasured hold under this rule (2026-09-27).
        "simulator/shadow.py",
    }


def test_exactly_one_place_in_the_repo_charges_a_hold():
    """`charge_hold`'s `finally` decides the ordering that makes the reading
    true -- close, THEN read the clock, and record even when the body
    raised. A second copy is a second place to get that wrong, which is why
    `held_attach` and `held_open` share it rather than each having one."""
    src = (ROOT / "hyxlab/store.py").read_text()
    assert src.count(".held_s = held") == 1
    assert src.count("_ATTACH_TOTALS.add_hold(held)") == 1
    for helper in ("held_attach", "held_open"):
        body = src.split(f"def {helper}(", 1)[1].split("\n@", 1)[0]
        assert "charge_hold(" in body, f"{helper} does not share the one finally"


def test_the_hold_helpers_are_exported_where_the_callers_look():
    """A seam nobody can reach is a seam nobody uses: the wrapper case
    (`simulator.shadow.held_stream_conn`) exists only because `charge_hold`
    is public."""
    from hyxlab import store

    for name in HELD:
        assert callable(getattr(store, name)), name
