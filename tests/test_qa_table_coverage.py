"""Every table in the archive must be READ by QA, or be declared unread.

WHY THIS IS DERIVED AND NOT A LIST (2026-09-04). `breadth_snapshots` was
written every 5 min from 2026-08-03 and QA did not name it once until
2026-09-04 — 8.26M rows of the archive's only exchange-wide quote history
with nothing watching them. The fix that pass was to add two checks. That
fix is not this one: adding a check is the same hand-kept coverage with
one more entry, and the NEXT table lands the same way. The defect is that
nothing compares the archive's tables against QA's readers.

So the comparison is derived from both sides:

  the archive's tables  <- the CREATE TABLE set in hyxlab/store.py
  QA's readers          <- the SQL in collector/qa.py, parsed

and the moment a table exists that QA never reads, this fails and names
it. Run against the state of 2026-09-04 it immediately found the SECOND
such table, `nws_forecasts` (580,270 rows, written in the same cycle as
`snapshots`, zero mentions in qa.py), which no one had noticed in the
month since breadth.

WHAT THIS DOES NOT CLAIM. "Read" is weaker than "checked for freshness":
`markets` is read only through `close_time`, a business column, so this
test would not notice `markets` going stale. It catches the defect that
has actually happened twice — a table nothing reads at all — and states
the stronger property it does not reach rather than implying it.

WHY THE READER CANNOT BE ATTRIBUTED TO A WRITING UNIT. The obvious
stronger test is "every table with a LIVE writer has a freshness reader",
per unit. It is not derivable here, by measurement rather than by
assumption: every archive write goes through hyxlab/store.py by
discipline (tests/test_writer_lock_discipline.py), so the import closure
of every unit contains store.py and closure-based attribution assigns
every table to every unit. test_unit_closures_cannot_attribute_tables
pins that measurement — the day it fails, a unit stopped going through
the store, and the stronger test becomes possible.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STORE = REPO / "hyxlab" / "store.py"
QA = REPO / "collector" / "qa.py"
UNITS = REPO / "scripts" / "systemd"

# Tables the archive holds that QA deliberately does not read. Each entry is
# a decision with a measured reason, not an omission — the whole point of the
# derivation is that omissions cannot get in here silently.
UNREAD: dict[str, str] = {
    "series": (
        "Dimension table, upserted whole by the 06:10Z sweep (sweep.py "
        "upsert_series). Its liveness IS the sweep's: measured 2026-09-04, "
        "max(updated_at) = 06:10:01Z, the sweep timer to the second, so "
        "'sweep ran in last 36h' cannot pass while this table is stale."
    ),
    "watermarks": (
        "Sweep bookkeeping (set_watermark), one row per series per sweep. "
        "Same witness as `series`: it is written by, and only by, the run "
        "that 'sweep ran in last 36h' already watches."
    ),
    "trades": (
        "326M rows, written by tradepass/sweep/poly_sweep — and watched "
        "harder than a max(ts) could: 'trade tape covers retention window' "
        "judges PER-MARKET persistence via trades_swept, and "
        "qa_batch_run_budget watches the backfill's 4.0h wall clock. A "
        "table-level freshness check would be strictly weaker and would read "
        "green off any one market still landing."
    ),
    "observations": (
        "NO LIVE WRITER. Only collector/backfill.py writes it and that is a "
        "hand-run tool; measured 2026-09-04 the newest row is 2026-07-06 "
        "(1,827 rows total). A freshness check here would fail forever about "
        "a table nothing is supposed to be filling."
    ),
}

_RECENCY_HELPERS = ("_check_freshness", "_check_continuity")
# _check_freshness(conn, name, table, col, now, noun) — same index in both.
_TABLE_ARG = 2


def _archive_tables() -> set[str]:
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", STORE.read_text()))


def _sql_strings(tree: ast.AST) -> list[str]:
    """Every string constant in the module, with f-string holes rendered as
    ` ? ` so an interpolated query still reads as SQL."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append(
                "".join(
                    p.value if isinstance(p, ast.Constant) else " ? " for p in node.values
                )
            )
    return out


def _read_tables() -> set[str]:
    """Tables collector/qa.py reads, from two places — because the readers
    live in two shapes and deriving only one shape is how a table looks
    unread while being watched (or worse, the reverse).

    (a) literal SQL: `FROM x` / `JOIN x` in any string in the module.
    (b) the table argument of a recency helper, whose own SQL interpolates
        the name and so contains no literal to find.
    """
    tree = ast.parse(QA.read_text())
    blob = "\n".join(_sql_strings(tree))
    found = set(re.findall(r"(?:FROM|JOIN)\s+([a-z_]+)\b", blob))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _RECENCY_HELPERS
            and len(node.args) > _TABLE_ARG
        ):
            arg = node.args[_TABLE_ARG]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
    return found


def test_every_archive_table_is_read_by_qa_or_declared_unread():
    """The check breadth needed and never had, one level up: not "is breadth
    watched" but "is anything unwatched"."""
    missing = _archive_tables() - _read_tables() - set(UNREAD)
    assert not missing, (
        f"archive table(s) {sorted(missing)} exist and collector/qa.py never reads them. "
        "Add a check, or declare the table in UNREAD with the reason it needs none."
    )


def test_declared_unread_tables_are_really_unread():
    """Anti-rot, the lint-scope discipline: an exemption for something that
    needs no excuse stops being a decision and becomes a comment about
    nothing. If QA starts reading one of these, the declaration must go."""
    stale = sorted(set(UNREAD) & _read_tables())
    assert not stale, f"UNREAD claims {stale} are unread, but collector/qa.py reads them"


def test_declared_unread_tables_still_exist():
    unknown = sorted(set(UNREAD) - _archive_tables())
    assert not unknown, f"UNREAD names {unknown}, which are not archive tables"


def test_every_declaration_carries_a_reason():
    """A one-word reason is the omission wearing a hat."""
    for table, reason in UNREAD.items():
        assert len(reason) > 80, f"{table}: declare WHY it needs no reader"


def test_the_derivation_is_not_vacuous():
    """A parser that matched nothing would mark every table unread and fail
    loudly; a parser that matched everything would pass while seeing nothing.
    Pin both ends against tables whose status is known by hand."""
    read = _read_tables()
    for t in ("snapshots", "breadth_snapshots", "nws_forecasts", "sweep_log"):
        assert t in read, f"{t} is read by qa.py — the derivation missed it"
    assert not (read & set(UNREAD)), "the derivation reads tables declared unread"


def test_helper_argument_position_is_still_the_table():
    """_TABLE_ARG is an index into someone else's signature. If the helpers
    are reordered, the derivation silently starts reading the `name` or `col`
    argument and every helper-only table looks unread."""
    import inspect

    from collector import qa

    for fn in _RECENCY_HELPERS:
        params = list(inspect.signature(getattr(qa, fn)).parameters)
        assert params[_TABLE_ARG] == "table", f"{fn}{params}: arg {_TABLE_ARG} is not `table`"


def test_unit_closures_cannot_attribute_tables_to_a_writer():
    """The measurement that justifies deriving coverage per TABLE instead of
    per WRITING UNIT. Every archive write goes through hyxlab/store.py, so
    every unit's import closure contains it and closure-based attribution is
    uniform — it would assign all 16 tables to all 8 writers, which is no
    attribution at all.

    This is pinned rather than assumed so that the day it stops being true —
    a unit that writes the archive without going through the store, which is
    itself a defect — someone is told, and the stronger per-writer test
    becomes possible.
    """
    roots = set()
    for unit in sorted(UNITS.glob("*.service")):
        m = re.search(r"ExecStart=.*?-m\s+([\w.]+)", unit.read_text())
        if not m:
            continue  # shell entry point (autoloop), no python closure
        root = m.group(1)
        # `python -m pkg` runs pkg.__main__, and the package __init__ is
        # usually empty — taking the package would measure the wrong closure.
        if (REPO / Path(*root.split(".")) / "__main__.py").exists():
            root += ".__main__"
        roots.add(root)
    assert len(roots) >= 8, f"expected the unit set to yield the daemons, got {sorted(roots)}"
    for root in sorted(roots):
        out = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "daemon_imports.py"), "closure", root],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        assert out.returncode == 0, f"closure {root} failed: {out.stderr}"
        assert "hyxlab/store.py" in out.stdout.splitlines(), (
            f"{root} no longer imports hyxlab/store.py — archive writes may have "
            "left the store, and per-writer table attribution may now be derivable"
        )
