"""A QA check that can stop being PRINTED is weaker than one that fails, and
nothing derived which of collector/qa.py's checks can vanish.

WHY THIS EXISTS ON TOP OF test_qa_table_coverage AND test_qa_staleness_coverage
(2026-09-05). Those two derive, in order, "every archive table is READ by QA"
and "every table's ingest stamp is asked its AGE". Both stop at the same
place, and that place was written down when the second one shipped: coverage
proves the QUESTION is asked, not that the ANSWER is loud. A check sitting
behind a guard is not asked at all when the guard is false, and a guard
computed from the data it watches can be false for exactly the reason the
check exists.

So this derives the third comparison:

  every named check in qa.py  ->  the guards that gate it
                              ->  can that guard be false on a LIVE archive?

and the classification is derived from the guard's own SQL, not declared:

  MONOTONE   the guard's names come only from an UNWINDOWED whole-table
             count. Nothing in this repo deletes from an archive table
             (test_no_archive_table_is_ever_deleted_from derives that, so
             the monotonicity is a fact about the code and not a hope), so
             such a guard is false only on a deployment that never enabled
             the writer, and it never reverts. `n_breadth`, `n_nws`,
             `n_poly`, `n_news` are all of this shape: they mean "was this
             feed ever turned on", which is a question about a deliberate
             choice, and answering "quiet" is right.

  WINDOWED   any other guard — in practice one whose SQL carries a time
             restriction. It empties on a dead writer, which is when the
             check it gates matters most. These must be DECLARED, and the
             declaration has to name what stays loud: either an emitting
             else branch (derived — the `orelse` must really emit), or
             another check that reds on the same input.

THE FINDING ON ITS FIRST RUN was `poly swept universe not shrinking`. Its
guard reads the last 10 days of poly_market_stats, so the stats half of the
poly walk going inert emptied `runs` and the tripwire stopped being printed
at all, while `poly prices fresh` stayed green off the CLOB half of the same
sweep. The check written because a universe shrink went unnoticed could
itself go missing unnoticed. It now has an else branch (bounded SKIP,
escalating to FAIL against the prices witness).

WHAT THIS DOES NOT CLAIM. That an emitted check is CORRECT, or that its
budget is right — only that no named check can disappear from the output
without a declared reason. Whether the answer is loud enough is what the
mutant tests in tests/test_hyxlab_qa.py are for.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QA = REPO / "collector" / "qa.py"
PACKAGES = ("collector", "hyxlab", "simulator", "strategies")

EMITTERS = {"check", "_check_freshness", "_check_continuity", "_reachable", "print"}
# Calls that emit a check LINE (print is an emitter for the else-branch test
# but does not itself name a check).
NAMED = {"check": 0, "_check_freshness": 1, "_check_continuity": 1}

# Guards that are NOT monotone and therefore gate a check that can vanish.
# Each names what stays loud in its place. Keyed by the guard source exactly
# as qa.py writes it, so editing the guard forces the reason to be re-read.
DECLARED: dict[str, str] = {
    "ok_sweeps": (
        "Nested under `sweep ran in last 36h`, which counts ok entries in the "
        "SAME 36h window and FAILS at zero. With no sweep in the window there "
        "is no candle to expect, so `candle ingest landing (36h)` has nothing "
        "to measure and its cause is already named once — two red lines for "
        "one cause is noise, not coverage."
    ),
    "len(runs) >= 2 and prior > POLY_UNIVERSE_MIN_PRIOR": (
        "EXP-1381, the finding this file was written to produce. Now has an "
        "emitting else branch (asserted below, not trusted): a bounded SKIP "
        "that escalates to FAIL after SKIP_MAX_AGE_H, decided against the "
        "independent witness `poly prices fresh (< 30h old)` — the same sweep "
        "writes both tables, so fresh prices with no settled stats run means "
        "the stats writer is inert, and stale prices mean the sweep itself is "
        "down and is already reported above."
    ),
}


def _tree(source: str | None = None) -> ast.AST:
    return ast.parse(QA.read_text() if source is None else source)


def _emits(nodes: list[ast.stmt]) -> bool:
    for st in nodes:
        for n in ast.walk(st):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in EMITTERS:
                return True
    return False


def _sql_of(node: ast.AST) -> str | None:
    """The first SQL literal inside an assignment's value, or None."""
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "SELECT" in n.value:
            return " ".join(n.value.split())
    return None


def _assignments(fn: ast.FunctionDef) -> dict[str, tuple[str | None, set[str]]]:
    """name -> (its SQL if any, the names it was computed from)."""
    out: dict[str, tuple[str | None, set[str]]] = {}
    for node in ast.walk(fn):
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets]
            value = node.value
        elif isinstance(node, ast.NamedExpr):
            targets, value = [node.target], node.value
        else:
            continue
        deps = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
        sql = _sql_of(value)
        for t in targets:
            for name in (n.id for n in ast.walk(t) if isinstance(n, ast.Name)):
                out[name] = (sql, deps)
    return out


def _guard_sql(test: ast.expr, assigns: dict[str, tuple[str | None, set[str]]]) -> set[str] | None:
    """Every SQL the guard's value depends on, or None if any name in it
    cannot be traced to one (an untraceable guard is never monotone)."""
    seen: set[str] = set()
    todo = [n.id for n in ast.walk(test) if isinstance(n, ast.Name)]
    sql: set[str] = set()
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


VERB = re.compile(r"^(SELECT|INSERT|DELETE|UPDATE|TRUNCATE|CREATE|ALTER|WITH)\b", re.I)

_COUNT_ONLY = re.compile(r"^SELECT count\(\*\) FROM [a-z_]+(?: WHERE [^?]*)?$", re.I)


def _is_monotone(sql: set[str] | None) -> bool:
    """A whole-table population count: no time window, no bound parameter,
    so it can only go from 0 to non-zero and never back."""
    if not sql:
        return False
    return all(_COUNT_ONLY.match(q) and "INTERVAL" not in q.upper() for q in sql)


def guarded_checks(source: str | None = None) -> list[dict]:
    """One record per (named check, enclosing guard) pair."""
    out: list[dict] = []
    tree = _tree(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        assigns = _assignments(fn)

        def visit(body: list[ast.stmt], guards: list[dict]) -> None:
            for st in body:
                if isinstance(st, ast.If):
                    g = {
                        "src": ast.unparse(st.test),
                        "sql": _guard_sql(st.test, assigns),
                        "else_emits": _emits(st.orelse),
                    }
                    visit(st.body, guards + [g])
                    visit(st.orelse, guards)
                    continue
                if isinstance(st, ast.Try):
                    visit(st.body, guards)
                    for h in st.handlers:
                        visit(h.body, guards)
                    visit(st.orelse + st.finalbody, guards)
                    continue
                if isinstance(st, ast.For | ast.While | ast.With):
                    visit(st.body, guards)
                    continue
                for n in ast.walk(st):
                    if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)):
                        continue
                    idx = NAMED.get(n.func.id)
                    if idx is None or len(n.args) <= idx:
                        continue
                    arg = n.args[idx]
                    if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                        continue  # a `name` parameter: the helper emits on every path
                    for g in guards:
                        out.append({"check": arg.value, "fn": fn.name, **g})

        visit(fn.body, [])
    return out


def test_no_named_check_can_vanish_without_a_declared_reason():
    """The property: a check that stops being PRINTED leaves no trace to
    miss, so every guard over one is either monotone (false only on a
    deployment that never enabled the writer) or declared."""
    undeclared = sorted(
        {
            (r["check"], r["src"])
            for r in guarded_checks()
            if not _is_monotone(r["sql"]) and r["src"] not in DECLARED
        }
    )
    assert not undeclared, (
        f"check(s) {undeclared} sit behind a guard that a LIVE archive can turn false — they "
        "stop being printed on the input they exist to notice, and nothing in the output says "
        "so. Give the guard an emitting else branch, or declare it in DECLARED with the check "
        "that goes red on the same input."
    )


def test_a_declared_guard_still_has_to_name_something():
    """A declaration that names no witness is the omission wearing a hat —
    the bar UNREAD and WITNESS are held to in the sibling coverage tests."""
    live = {r["src"] for r in guarded_checks()}
    for src, reason in DECLARED.items():
        assert src in live, f"DECLARED names a guard qa.py no longer has: {src!r}"
        assert len(reason) > 150, f"{src}: name the check that stays loud when this goes quiet"


def test_the_poly_tripwire_really_has_an_emitting_else():
    """Its declaration CLAIMS an else branch. Derive it rather than trust it:
    a reason is a comment, and this one would go stale the moment the branch
    is dropped — which is the exact state the archive was in until
    2026-09-05."""
    poly = [
        r
        for r in guarded_checks()
        if r["check"] == "poly swept universe not shrinking"
        and r["src"].startswith("len(runs) >= 2")
    ]
    assert poly, "the poly enumeration tripwire's guard is no longer where this test looks"
    assert all(r["else_emits"] for r in poly), (
        "the poly swept-universe tripwire has lost its else branch: it now emits NOTHING when "
        "poly_market_stats holds fewer than two settled runs, which is exactly what an inert "
        "stats writer produces"
    )


def test_the_monotone_guards_are_the_enabling_ones():
    """Non-vacuity, one end: the four feed guards must classify monotone. If
    one stops doing so the derivation is not reading the SQL it thinks it
    is — and every one of them would then demand a declaration it does not
    need, which is how a derivation gets switched off."""
    by_src = {r["src"]: r for r in guarded_checks()}
    for src in ("n_breadth", "n_nws", "n_poly", "n_news"):
        assert src in by_src, f"qa.py no longer guards a feed check on {src}"
        assert _is_monotone(by_src[src]["sql"]), (
            f"{src} no longer reads as an unwindowed population count; if its SQL gained a time "
            "window it is now silenceable and needs a declaration"
        )


def test_the_windowed_guards_are_not_mistaken_for_enabling_ones():
    """Non-vacuity, the other end. `ok_sweeps` and the poly tripwire both
    read a window; a classifier that called either monotone would have
    reported this file's finding as fine."""
    by_src = {r["src"]: r for r in guarded_checks()}
    for src in ("ok_sweeps", "len(runs) >= 2 and prior > POLY_UNIVERSE_MIN_PRIOR"):
        assert src in by_src, f"qa.py no longer has the guard {src!r}"
        assert not _is_monotone(by_src[src]["sql"]), f"{src} was classified as monotone"


def test_a_window_added_to_an_enabling_guard_is_caught():
    """The mutation that would defeat this, exercised: the same guard shape
    with a recency filter on it. Nothing in qa.py looks like this today,
    which is why it has to be pinned against a synthetic source rather than
    left to be discovered on the day it lands."""
    windowed = guarded_checks(
        source="def qa_x(conn, now):\n"
        '    n = conn.execute("SELECT count(*) FROM breadth_snapshots'
        ' WHERE ts > ? - INTERVAL 1 DAY", [now]).fetchone()[0]\n'
        "    if n:\n"
        '        check("breadth fresh", True)\n'
    )
    assert windowed and not _is_monotone(windowed[0]["sql"]), (
        "a population count restricted to a recency window was read as monotone — every "
        "silenceable guard could then be written into a shape this derivation waves through"
    )


def test_an_untraceable_guard_is_never_monotone():
    """A guard computed from something that is not a query at all (a journal
    parse, a sidecar) cannot be proven to only go from empty to non-empty.
    Erring toward 'declare it' is the same direction the sibling derivations
    err in."""
    opaque = guarded_checks(
        source="def qa_x(p):\n"
        "    rows = read_journal(p)\n"
        "    if rows:\n"
        '        check("something", True)\n'
    )
    assert opaque and not _is_monotone(opaque[0]["sql"])


def test_no_archive_table_is_ever_deleted_from():
    """What makes the MONOTONE class sound. A population count is monotone
    only because this codebase never removes archive rows — venue retention
    purges upstream, we only ever accumulate. A DELETE landing on an archive
    table would make `n_poly` able to fall back to zero, and the feed checks
    would start disappearing on the failure they watch."""
    offenders = []
    seen_exempt = False
    pat = re.compile(r"(?:DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)\s+([a-z_]+)", re.I)
    for pkg in PACKAGES:
        for path in (REPO / pkg).rglob("*.py"):
            # Query literals only: a string whose first word is a SQL verb.
            # Prose is not a statement — a docstring in polymarket.py says a
            # bad offset cap "would silently truncate the daily sweep", and a
            # grep over source text reads that as a TRUNCATE.
            for node in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                flat = " ".join(node.value.split())
                if not VERB.match(flat):
                    continue
                for m in pat.finditer(flat):
                    if m.group(1) == "schema_meta":  # one row, rewritten by migrate
                        seen_exempt = True
                    else:
                        offenders.append(f"{path.relative_to(REPO)}: {m.group(0)}")
    assert seen_exempt, (
        "the scan found no DELETE at all, not even hyxlab/store.py's `DELETE FROM schema_meta` "
        "— it is reading nothing and would wave through a real one"
    )
    assert not offenders, (
        f"{offenders} removes archive rows, so a population count can fall back to zero and the "
        "guards this file classifies as MONOTONE are no longer monotone — reclassify them"
    )
