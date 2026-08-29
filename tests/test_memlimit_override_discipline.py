"""A site that moves `memory_limit` after the chokepoint re-derives the cap.

EXP-1377, the rung above `test_spillcap_discipline.py`. That guard put
`spill_cap` at the connect chokepoint and proved, by AST, that every
kernel attach calls it AFTER `cgroup_memory_limit` — the cap is a
multiple of the limit, so deriving it from a limit about to be replaced
gives a number ~90x too large on exactly the capped services it exists
for.

**WHAT THAT GUARD CANNOT SEE IS A CALLER.** It reads
`hyxlab/store.py` only, and the order it enforces is an order between
two statements INSIDE the chokepoint. A module outside the kernel that
attaches through `connect_retry` and then issues its own
`SET memory_limit` has passed the chokepoint already: the cap it carries
is a multiple of the limit it had on the way in, and nothing reddens.
That is not hypothetical — `simulator.shadow.stream_conn` did exactly
this, and rung-11 measured the result outside a cgroup:
`max_temp_directory_size` **344.1 GiB** (the free-disk term, off the
host-RAM default) on a connection whose engine was pinned to 512 MiB and
had earned **4.0 GiB**. It was fixed there, by hand, with a
shadow-specific assertion in `test_hyxlab_shadow.py`.

**A FIX AT ONE SITE IS NOT A RULE.** Today that override is unique;
mistake #37 is that "unique today" is a discovery waiting to be made
again in two days. So this rung sweeps for the ROLE — any statement in
any package that sets `memory_limit` — and pins the answer, the way
EXP-1373 and EXP-1376 pin theirs.

  DERIVED — every site outside the kernel that executes
            `SET memory_limit` calls `spill_cap` AFTER it, in the same
            function, by AST; and the set of such sites is ENUMERATED,
            so a second override is a red suite rather than a silently
            unbounded spill. The kernel's own `cgroup_memory_limit` is
            the exempt one: it IS the statement the chokepoint's order
            rule is written about, and `test_spillcap_discipline.py`
            holds that line.
  CLAIMED — verified by RUNNING, not by reading: a real `connect_retry`
            connection that lowers its limit keeps its OLD cap, byte for
            byte, and re-deriving takes it to the multiple the new limit
            earns. The stale cap is asserted to be the larger one, so a
            green here cannot come from two numbers that happened to
            agree.

The direction of the override does not matter and is deliberately not
checked. Lowering the limit leaves the cap too large, which is the
measured hazard; RAISING it leaves the cap too small, which rations a
query that fits. Both are "the cap no longer describes this connection",
and re-deriving is the same one-line answer to both.
"""

from __future__ import annotations

import ast
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from hyxlab.spillcap import SPILL_MULTIPLE, duck_spill_limit, parse_size

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("collector", "simulator", "strategies", "hyxlab")

CAP_CALL = "spill_cap"

#: `SET memory_limit`, in any of the spellings DuckDB accepts — with or
#: without `=`, any case, any whitespace.
_SET_MEM = re.compile(r"\bSET\s+memory_limit\b", re.IGNORECASE)

#: The one site allowed to set the limit without re-deriving after it:
#: the chokepoint statement the cap's ORDER rule is written about.
#: `test_spillcap_discipline.py` proves `spill_cap` runs after it.
KERNEL_SITE = "hyxlab/store.py::cgroup_memory_limit"

#: Every site outside the kernel that moves the limit after attaching.
#: ENUMERATED, not counted: rung-11 found the third copy of one walk by
#: sweeping for the role, and the lesson (mistake #37) was to pin the
#: answer so the fourth is a red test instead of a discovery.
OVERRIDE_SITES = {"simulator/shadow.py::stream_conn"}


# -- derived half ---------------------------------------------------------


def _sql_strings(node: ast.AST) -> list[str]:
    """Every literal SQL fragment in `node`, f-strings included.

    An f-string is a `JoinedStr`; its literal parts carry the verb, and
    the interpolated part is the value being set. `SET memory_limit` is
    always in the literal part, because a statement assembled entirely
    out of variables is not something this repo writes.
    """
    out: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            out.append(
                "".join(
                    v.value
                    for v in n.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            )
    return out


_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _own_nodes(scope: ast.AST):
    """Every node in `scope` EXCLUDING nested function and class bodies.

    A nested scope is enumerated in its own right, so attributing its
    statements to the enclosing one would both double-count the override
    and let an outer `spill_cap` vouch for an inner `SET`.
    """
    body = scope.body[1:] if ast.get_docstring(scope) else scope.body
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _SCOPE):
                stack.append(child)


def _limit_setters() -> dict[str, tuple[int, list[tuple[int, str]]]]:
    """`rel::qualname` -> (line of the SET, the calls in that function).

    Calls are carried with their line numbers because this rung is about
    ORDER: a `spill_cap` before the override re-derives from the limit
    that is about to be replaced, which is the bug, not the fix.
    """
    found: dict[str, tuple[int, list[tuple[int, str]]]] = {}
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text())
            stack: list[str] = []

            class V(ast.NodeVisitor):
                def _scoped(self, node):
                    stack.append(node.name)
                    own = list(_own_nodes(node))
                    # A docstring that NAMES the hazard is how the hazard
                    # stays understood; only executed strings are code.
                    lines = [
                        n.lineno
                        for n in own
                        if any(_SET_MEM.search(f) for f in _sql_strings(n))
                    ]
                    if lines:
                        calls = [
                            (n.lineno, n.func.id)
                            for n in own
                            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        ]
                        found[f"{rel}::{'.'.join(stack)}"] = (max(lines), calls)
                    self.generic_visit(node)
                    stack.pop()

                visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

            V().visit(tree)
    return found


def test_the_sites_that_move_the_limit_are_enumerated():
    """A new override is a red suite, not a discovery two days later."""
    got = set(_limit_setters()) - {KERNEL_SITE}
    assert got == OVERRIDE_SITES, (
        f"sites setting memory_limit outside the kernel changed: {sorted(got)}"
        f" != {sorted(OVERRIDE_SITES)}. Each must re-derive its spill cap"
        " (see this module's docstring); then add it here."
    )


def test_the_kernel_site_is_still_the_chokepoint_statement():
    """The exemption is load-bearing, so it is asserted, not assumed."""
    assert KERNEL_SITE in _limit_setters(), (
        f"{KERNEL_SITE} no longer sets memory_limit — the cap's whole"
        " derivation moved; re-read hyxlab/store.py and this exemption"
    )


def test_every_override_re_derives_the_spill_cap_after_it():
    """The rule, not the site: past the chokepoint, you own the cap."""
    for site, (lineno, calls) in _limit_setters().items():
        if site == KERNEL_SITE:
            continue
        after = [ln for ln, name in calls if name == CAP_CALL and ln > lineno]
        assert after, (
            f"{site} sets memory_limit at line {lineno} without calling"
            f" {CAP_CALL}() after it. The connection's"
            " max_temp_directory_size is a multiple of the limit it had"
            " at the chokepoint — measured 344.1 GiB against the 4.0 GiB"
            " a 512 MiB engine earns (EXP-1376)."
        )


# -- claimed half: verified by RUNNING ------------------------------------


def _cap(conn) -> int | None:
    raw = conn.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0]
    return parse_size(str(raw))


@pytest.fixture
def spill_tmp():
    """A temp dir on a volume where the MULTIPLE binds, not the disk term.

    `tmp_path` is under `/tmp`, which on this host is a tmpfs sized off
    RAM (31 GiB, 8.3 GiB free 2026-08-29). `DISK_SHARE * free` is then
    ~2.2 GB -- BELOW the 4.0 GiB a 512 MiB engine earns -- so the disk
    term binds every cap on the volume and the three claims below become
    two equal numbers that agree for the wrong reason. The failing
    assertion said so in its own message ("this box cannot show the
    hazard") and then failed rather than declining to measure.

    So the dir is placed on the repo volume, where free space is a
    thousand times the multiple. If that volume is ALSO too small, the
    box genuinely cannot demonstrate the rung and the tests SKIP: an
    unmeasurable claim is not a refuted one (mistakes #32).
    """
    root = Path(__file__).resolve().parent.parent / ".pytest-spill"
    root.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=root))
    earned = duck_spill_limit(512 * 2**20, d)
    if earned < SPILL_MULTIPLE * 512 * 2**20:
        shutil.rmtree(d, ignore_errors=True)
        pytest.skip(
            f"the disk term binds on {root}: a 512 MiB engine earns {earned} B,"
            f" under the {SPILL_MULTIPLE}x multiple this rung measures"
        )
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_lowering_the_limit_does_not_move_the_cap(spill_tmp):
    """The hazard itself, on a real kernel connection.

    Not "the cap is large" — that depends on the box — but the exact
    claim the rule rests on: DuckDB does not recompute
    `max_temp_directory_size` when `memory_limit` changes, so a caller
    past the chokepoint keeps a bound describing a connection it no
    longer is.
    """
    from hyxlab.store import connect_retry

    conn = connect_retry(spill_tmp / "override.duckdb", read_only=False)
    before = _cap(conn)
    conn.execute("SET memory_limit = '512MiB'")
    after = _cap(conn)
    earned = duck_spill_limit(512 * 2**20, spill_tmp)
    conn.close()

    assert before is not None and after is not None
    assert after == before, (
        "DuckDB now re-derives its spill cap when the limit moves"
        f" ({before} -> {after}) — this rung may have outlived its reason"
    )
    # Non-vacuous: the stale cap must be the WRONG one, or a green here
    # would just be two numbers that agreed. On a volume small enough
    # that the disk term binds both, they legitimately do agree.
    assert before > earned, (
        f"stale cap {before} B is not larger than the {earned} B a"
        " 512 MiB engine earns — the disk term binds both on this"
        " volume, so this box cannot show the hazard"
    )


def test_re_deriving_after_the_override_earns_the_new_limit(spill_tmp):
    """The fix, run: one call, and the bound describes the connection."""
    from hyxlab.store import connect_retry, spill_cap

    db = spill_tmp / "rederive.duckdb"
    conn = connect_retry(db, read_only=False)
    conn.execute("SET memory_limit = '512MiB'")
    spill_cap(conn, db)
    got = _cap(conn)
    conn.close()

    assert got is not None
    assert got <= SPILL_MULTIPLE * 512 * 2**20
    assert got == duck_spill_limit(512 * 2**20, db)


def test_the_shipped_override_site_carries_the_rule(spill_tmp):
    """End to end through the site the rule was written from.

    `test_hyxlab_shadow.py` asserts shadow's own cap; this asserts that
    it is the RULE that puts it there, by driving the real helper and
    checking the same invariant the AST half enforces.
    """
    from hyxlab.streamstore import StreamStore
    from simulator.shadow import DUCK_MEM, stream_conn

    db = spill_tmp / "stream.duckdb"
    StreamStore(db)
    with stream_conn(str(db)) as conn:
        got = _cap(conn)
        mem = parse_size(conn.execute("SELECT current_setting('memory_limit')").fetchone()[0])

    assert got is not None and mem == parse_size(DUCK_MEM)
    assert got == duck_spill_limit(mem, str(db)), (
        "shadow's cap is not the one its OWN limit earns — the override"
        " re-derivation regressed"
    )
