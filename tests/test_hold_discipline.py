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

WHY "LEDGERED" AND NOT "EVERY": a hold is charged to the attach row the
open produced (`_LAST_ATTACH`), and `hyxlab.store.duck_connect` and a
bare `Store(...)` produce no row. Wrapping one in `charge_hold` would
charge its seconds to whatever unrelated attach happened to be last --
a WRONG reading, which is worse than a missing one. So those two sites
are outside this rule's reach by construction, and
`test_the_unledgered_attaches_in_publishers_stay_named` enumerates them
rather than letting them vanish: giving `duck_connect` a ledger row is
the open half of the question.

THE LIMIT, WRITTEN DOWN RATHER THAN DISCOVERED LATER: this rule reaches
only publishers. `simulator.shadow._read_new` attaches the same
daemon-owned file every ~20s and holds it across the boot-time seed
replay (measured 2,084,503 rows at the 2026-07-31 promote), and it
publishes no block at all, so no assertion here can see it.
`simulator.shadow.held_stream_conn` is the seam it would use; giving
shadow somewhere to publish is the open half of this work.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("collector", "simulator", "strategies", "hyxlab")

#: Ledgered attaches -- they record an `AttachWait`, so a hold has a row
#: to be charged to -- taken WITHOUT measuring the hold.
BARE = ("connect_retry", "open_retry")
#: Attaches that take the same file locks and record no row at all. Named
#: here so the debt is visible; see the module docstring.
UNLEDGERED = ("Store", "duck_connect")
#: The three ways a hold gets charged. `charge_hold` is the seam for a
#: wrapper that owns its connection between the open and its caller
#: (`simulator.shadow.stream_conn` re-tunes the engine first).
HELD = ("held_attach", "held_open", "charge_hold")

#: site -> why a publisher may attach without measuring the hold. Keys are
#: `relpath::qualname`. EMPTY ON PURPOSE: the four publishers all measure
#: every attach they take, and an exemption here has to argue that a hold
#: on a shared file harms nobody -- which is the assumption #91 was.
ALLOWED: dict[str, str] = {}


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


def test_the_unledgered_attaches_in_publishers_stay_named():
    """The rule's blind spot, enumerated so it cannot quietly grow. Both
    sites are `collector.sweep --doctor`, a branch that exits before the
    sweep and publishes nothing -- and both hold a shared file across
    full-table reads (the markets GROUP BY is 486k rows) with no row to
    charge the seconds to."""
    assert _attaches_in_publishers(UNLEDGERED) == {
        "collector/sweep.py::main": "Store",
        "collector/sweep.py::doctor": "duck_connect",
    }


def test_the_publisher_set_is_the_one_this_rule_was_written_for():
    """Set equality, both directions (mistakes #34): a renamed report drops
    out of the rule silently, and a new one must be brought under it
    deliberately. `hyxlab/store.py` defines the block and is not a
    publisher of it."""
    assert set(publishers()) == {
        "collector/sweep.py",
        "hyxlab/store.py",
        "simulator/atlas.py",
        "simulator/divergence.py",
        "simulator/run_l2.py",
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
