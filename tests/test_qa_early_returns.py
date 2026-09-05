"""A QA section that RETURNS early takes every check below it with it, and
nothing derived which of collector/qa.py's early exits can leave silently.

WHY THIS EXISTS ON TOP OF THE THREE SIBLING DERIVATIONS (2026-09-05).
test_qa_table_coverage, test_qa_staleness_coverage and test_qa_silent_guards
derive, in order, "every archive table is READ by QA", "every table's ingest
stamp is asked its AGE", and "no named check can stop being PRINTED without a
declared reason". The third stops at a place it named when it shipped: it
covers a check gated by an `if`, and says nothing about a check whose SECTION
returns before reaching it. Those are different failure shapes — a guard
skips one check, an early exit drops the rest of the function.

So this derives the fourth comparison:

  every `return` in a qa_* section  ->  does an EMITTER stand on its path?

where an emitter is `check`/`_check_freshness`/`_check_continuity`/`_reachable`
/`print`, and "on its path" means a statement that precedes the return in its
own block or in any enclosing block, or the TEST of an enclosing `if`. The
last clause is what credits the reachability shape:

    if not _reachable(conn, "main archive reachable", "archive", now):
        return None

`_reachable` emits from inside the condition — a bounded SKIP that escalates
to FAIL after SKIP_MAX_AGE_H, or a plain FAIL when no live writer holds the
lock. A derivation that only looked at preceding STATEMENTS would call the
two oldest, most carefully bounded exits in the file unbounded, which is how
a derivation gets switched off for noise.

THE FINDING ON ITS FIRST RUN was `qa_econ_pull_live`'s empty-table return.
Every other early exit in qa.py emits; that one returned None with no line
and no clock. Its guard IS monotone in the sibling classification (nothing
deletes archive rows), so on a deployment that never enabled the econ pull
silence is right — but monotone is an argument about the GUARD, not about
what else can produce an empty table. `collector.signals.main` calls
`record_fetch` BEFORE the locked DuckDB write, so a working fetch and a
failing write leave ok series in the sidecar and nothing in the table; and
`diff_vintages` cannot explain that away, because on an empty table every
fetched observation is new. That state is now decided against the sidecar
(EXP-1382), and the remaining silence is DECLARED below.

WHAT THIS DOES NOT CLAIM. That the emitted line is the RIGHT line, or that
its budget is right — only that no section can leave without saying so.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_qa_silent_guards import _assignments, _is_monotone  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
QA = REPO / "collector" / "qa.py"

EMITTERS = {"check", "_check_freshness", "_check_continuity", "_reachable", "print"}

# Early exits that emit nothing, keyed by "<section>:<enclosing guard>" so
# editing the guard forces the reason to be re-read. Each must name what
# stays loud in its place.
DECLARED: dict[str, str] = {
    "qa_econ_pull_live:not conn.execute('SELECT count(*) FROM econ_vintages').fetchone()[0]": (
        "EXP-1382, the finding this file was written to produce. The guard is MONOTONE — "
        "nothing in this repo deletes archive rows, so an empty econ_vintages means the pull "
        "was never enabled on this deployment, a deliberate choice that silence answers "
        "correctly (asserted below against the sibling classifier, not trusted). The other way "
        "into an empty table — the fetch working while the archive write does not, which the "
        "sidecar's ok run reveals because record_fetch() runs BEFORE the DuckDB insert — now "
        "FAILS above this return instead of taking it. And a pull that is not running at all "
        "is reported once by qa_signals_fetch's UNVERIFIED skip, which this return leaves in "
        "place: two red lines for one cause is noise, not coverage."
    ),
}


def _tree(source: str | None = None) -> ast.AST:
    return ast.parse(QA.read_text() if source is None else source)


def _emits(node: ast.AST) -> bool:
    """Does this statement (or expression) emit on EVERY path through it?

    An emitter buried in a nested `if` is not an emitter for the code after
    it — that is precisely the shape of qa_econ_pull_live's new FAIL, which
    fires only when the sidecar witness says the pull is running and leaves
    the return silent otherwise. Crediting it would have declared the finding
    fixed by the fix that only covers half of it.
    """
    if isinstance(node, ast.If):
        return bool(node.orelse) and _emits_block(node.body) and _emits_block(node.orelse)
    if isinstance(node, ast.Try):
        return _emits_block(node.body + node.orelse) and all(
            _emits_block(h.body) for h in node.handlers
        )
    if isinstance(node, ast.For | ast.While):
        return False  # a loop that runs zero times emits nothing
    if isinstance(node, ast.With):
        return _emits_block(node.body)
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return False  # defining a helper does not call it
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in EMITTERS:
            return True
    return False


def _emits_block(body: list[ast.stmt]) -> bool:
    return any(_emits(st) for st in body)


def _emits_somewhere(node: ast.AST) -> bool:
    """Does an emitter appear anywhere inside, on any path? Not enough to bound
    a return — it is what a DECLARED exit's witness branch looks like."""
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in EMITTERS
        for n in ast.walk(node)
    )


def _sql_of_guard(
    test: ast.expr, assigns: dict[str, tuple[str | None, set[str]]]
) -> set[str] | None:
    """Every SQL the guard depends on, or None if anything in it cannot be
    traced to one (an untraceable guard is never monotone).

    The sibling's `_guard_sql` traces NAMES back to the assignment that
    produced them, which is the shape every guard it classifies has. The exit
    this file found does not: `if not conn.execute("SELECT count(*) FROM
    econ_vintages").fetchone()[0]` writes its query inline, with no name to
    trace. So an inline literal counts as its own dependency, and the only
    other name in such a guard — the connection the query is sent through —
    is exempt exactly where it is used as a handle (`conn.execute`), never as
    a value the guard's truth depends on.
    """
    direct = {
        " ".join(n.value.split())
        for n in ast.walk(test)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "SELECT" in n.value
    }
    handles = {
        n.value.id
        for n in ast.walk(test)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    }
    todo = [n.id for n in ast.walk(test) if isinstance(n, ast.Name) and n.id not in handles]
    seen: set[str] = set()
    sql = set(direct)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in assigns:
            if name.isupper():  # a module constant is not data
                continue
            return None
        q, deps = assigns[name]
        if q:
            sql.add(q)
        else:
            todo.extend(deps)
    return sql or None


def section_returns(source: str | None = None) -> list[dict]:
    """One record per `return` that exits a qa_* section: whether an emitter
    stands on its path, and the innermost guard it sits under."""
    out: list[dict] = []
    tree = _tree(source)
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith("qa_")):
            continue
        assigns = _assignments(fn)

        def visit(body: list[ast.stmt], before: list[ast.AST], guard: ast.expr | None) -> None:
            for i, st in enumerate(body):
                path = before + list(body[:i])
                if isinstance(st, ast.Return):
                    out.append(
                        {
                            "fn": fn.name,  # noqa: B023 — consumed inside this iteration
                            "line": st.lineno,
                            "bounded": any(_emits(p) for p in path),
                            "witness": any(_emits_somewhere(p) for p in path),
                            "guard": None if guard is None else ast.unparse(guard),
                            "sql": None if guard is None else _sql_of_guard(guard, assigns),  # noqa: B023
                        }
                    )
                    continue
                if isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    continue  # a nested helper's return is a value, not a section exit
                if isinstance(st, ast.If):
                    # The condition runs on both branches, so an emitter inside
                    # it (`_reachable`) stands on the path of either.
                    visit(st.body, path + [st.test], st.test)
                    visit(st.orelse, path + [st.test], guard)
                    continue
                if isinstance(st, ast.Try):
                    visit(st.body, path, guard)
                    for h in st.handlers:
                        visit(h.body, path, guard)
                    visit(st.orelse + st.finalbody, path, guard)
                    continue
                if isinstance(st, ast.For | ast.While | ast.With):
                    visit(st.body, path, guard)
                    continue

        visit(fn.body, [], None)
    return out


def _key(r: dict) -> str:
    return f"{r['fn']}:{r['guard']}"


def test_no_section_can_leave_without_saying_so():
    """The property: a `return` inside a qa_* section skips every check below
    it, so it must either emit on its own path or be declared."""
    silent = sorted({_key(r) for r in section_returns() if not r["bounded"]})
    undeclared = [k for k in silent if k not in DECLARED]
    assert not undeclared, (
        f"{undeclared} leaves a QA section with no line printed — every check below the return "
        "is skipped and the output says nothing about it. Emit a check or a bounded SKIP before "
        "returning, or declare the exit in DECLARED with what stays loud in its place."
    )


def test_a_declared_exit_still_has_to_name_something():
    """A declaration that names no witness is the omission wearing a hat."""
    live = {_key(r) for r in section_returns()}
    for key, reason in DECLARED.items():
        assert key in live, f"DECLARED names an exit qa.py no longer has: {key!r}"
        assert len(reason) > 150, f"{key}: name what stays loud when this return is taken"


def test_a_declared_exit_really_has_the_witness_branch_it_claims():
    """The declaration CLAIMS the non-monotone way into this state now fails
    ABOVE the return. Derive it rather than trust it: a reason is a comment,
    and deleting the branch would leave the prose describing a check that is
    no longer there — which is exactly the state qa.py was in until
    2026-09-05."""
    for r in section_returns():
        if _key(r) in DECLARED:
            assert r["witness"], (
                f"{_key(r)} is declared silent because a witness fails above it, but nothing on "
                "the path to this return emits at all — the declaration now describes a check "
                "that does not exist"
            )


def test_the_declared_silent_exit_is_the_monotone_one():
    """Its declaration CLAIMS the guard is monotone — that an empty table means
    the feed was never enabled and cannot revert. Derive it with the sibling
    classifier rather than trust the prose: a window added to this guard would
    make the exit reachable on a LIVE deployment whose pull just died, and the
    reason written above would be quietly false."""
    silent = [r for r in section_returns() if not r["bounded"]]
    assert silent, "the derivation found no silent exit at all — it is reading nothing"
    for r in silent:
        assert _is_monotone(r["sql"]), (
            f"{_key(r)} leaves a section silently and its guard is NOT an unwindowed population "
            "count, so a live archive can take this exit on the failure the section watches"
        )


def test_the_reachability_exits_are_credited():
    """Non-vacuity, one end. `qa_stream` and `qa_archive` both return through
    `if not _reachable(...)`, where the only emitter is in the CONDITION. If
    these stop being credited the file is demanding declarations for the two
    best-bounded exits it has, and the declarations would drown the finding."""
    by_fn = {(r["fn"], r["line"]): r for r in section_returns()}
    reach = [r for r in by_fn.values() if r["guard"] and "_reachable" in r["guard"]]
    assert len(reach) == 2, f"expected qa_stream and qa_archive's reachability exits, got {reach}"
    assert all(r["bounded"] for r in reach)


def test_an_emitter_after_the_return_does_not_count():
    """Non-vacuity, the other end. Order matters: a check printed further down
    the function is precisely what the early return skips. A derivation that
    tested only 'does this function emit somewhere' would call every exit in
    qa.py bounded and find nothing, ever."""
    late = section_returns(
        source="def qa_x(conn):\n"
        "    if conn is None:\n"
        "        return\n"
        '    check("something", True)\n'
    )
    assert late and not late[0]["bounded"], (
        "a return that precedes every emitter in its function was read as bounded — the "
        "derivation is not path-sensitive and would wave through any early exit"
    )


def test_a_nested_helpers_return_is_not_a_section_exit():
    """`qa_tape_coverage` defines `age_h`, whose `return` yields a value and
    exits nothing. Counting it would put a permanent undeclared entry in the
    output that no emitter can ever clear."""
    nested = section_returns(
        source="def qa_x(conn):\n"
        '    check("something", True)\n'
        "    def age_h(m):\n"
        "        return 1.0\n"
    )
    assert nested == []


def test_the_sidecar_has_exactly_one_parser():
    """The new FAIL and the per-series check decide opposite questions against
    the same file. Two parsers would be two opinions about what it says, and
    the one that drifted would be the one nobody ran."""
    tree = _tree()
    fns = {
        f.name: f
        for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef) and f.name in ("qa_signals_fetch", "qa_econ_pull_live")
    }
    assert set(fns) == {"qa_signals_fetch", "qa_econ_pull_live"}
    for name, fn in fns.items():
        calls = {
            n.func.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "read_signals_fetch" in calls, f"{name} no longer reads the sidecar through the "
        assert "loads" not in {
            n.func.attr
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }, f"{name} parses the sidecar itself again — one file, one parser"


def test_a_conditionally_emitted_line_does_not_bound_a_return():
    """The mutation the fix itself could have hidden behind. qa.py's new FAIL
    sits inside an `if` over the sidecar witness, so it emits on ONE path and
    the return is still silent on the other. A derivation that credited any
    emitter appearing textually earlier would have marked the exit bounded and
    quietly dropped the declaration that explains the silent half."""
    half = section_returns(
        source="def qa_x(conn):\n"
        '    n = conn.execute("SELECT count(*) FROM econ_vintages").fetchone()[0]\n'
        "    if not n:\n"
        "        if witness():\n"
        '            check("something", False)\n'
        "        return None\n"
    )
    assert half and not half[0]["bounded"], (
        "an emitter reachable on only one path was read as bounding the return — every silent "
        "exit could then be papered over with a conditional line that fires on the other case"
    )


def test_an_if_that_emits_on_both_branches_does_bound_a_return():
    """Non-vacuity for the rule above: the escalating-SKIP shape used all over
    qa.py (FAIL once the clock runs out, SKIP until then) really does say
    something on every path, and must not be forced into a declaration."""
    both = section_returns(
        source="def qa_x(conn):\n"
        "    if aged_out():\n"
        '        check("something", False)\n'
        "    else:\n"
        '        print("SKIP something")\n'
        "    return None\n"
    )
    assert both and both[0]["bounded"]
