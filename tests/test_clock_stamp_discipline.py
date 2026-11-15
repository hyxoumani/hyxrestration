"""No test module may stamp the wall clock at IMPORT.

pytest imports every test module at collection, before the first test runs,
so a module-level `NOW = datetime.now(UTC)` is as old as the suite's progress
by the time a test reads it. `tests/test_hyxlab_qa.py` built "fresh" fixtures
from such a stamp and handed them to checks that read the real clock with
budgets of 300 s and 20 min. On 2026-11-15 the suite ran ~3x slow under the
sibling's lake nightly (PSI io full 36%), reached the file 943 s after
collection, and 15 tests failed with `age 943s`: the promote gate went red
on the host's speed, not on the code.

The module set is DISCOVERED (every tracked `tests/*.py`), never listed.
"""

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: (object, attribute) pairs that read the wall clock.
WALL_CLOCK = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
    ("date", "today"),
    ("time", "time"),
    ("time", "time_ns"),
}


def _test_modules() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "tests/*.py"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return [ROOT / p for p in out]


def import_time_stamps(source: str) -> list[tuple[int, str]]:
    """(line, call) for each wall-clock read that runs when the module is
    imported: anything outside a function or lambda body. Class bodies and
    module-level `if`/`with`/`try` blocks all execute at import."""
    found: list[tuple[int, str]] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                # Decorators and argument defaults DO run at import.
                for early in (
                    *getattr(child, "decorator_list", []),
                    *child.args.defaults,
                    *(d for d in child.args.kw_defaults if d is not None),
                ):
                    walk(ast.Expression(early))
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and (child.func.value.id, child.func.attr) in WALL_CLOCK
            ):
                found.append((child.lineno, f"{child.func.value.id}.{child.func.attr}()"))
            walk(child)

    walk(ast.parse(source))
    return found


def test_the_scanner_sees_the_shape_that_failed():
    """Vacuity guard: the exact line the two QA test modules carried, and the
    places an import-time read can hide."""
    assert import_time_stamps("NOW = datetime.now(UTC)\n") == [(1, "datetime.now()")]
    assert import_time_stamps("class C:\n    T0 = time.time()\n") == [(2, "time.time()")]
    assert import_time_stamps("def f(t=datetime.now()):\n    pass\n") == [(1, "datetime.now()")]
    assert import_time_stamps(
        "@pytest.mark.parametrize('d', [date.today()])\ndef t(d):\n    pass\n"
    ) == [(1, "date.today()")]
    assert import_time_stamps("if True:\n    X = datetime.utcnow()\n") == [(2, "datetime.utcnow()")]


def test_the_scanner_leaves_per_test_stamps_alone():
    src = (
        "NOW: datetime\n"
        "@pytest.fixture(autouse=True)\n"
        "def _fresh_now():\n"
        "    global NOW\n"
        "    NOW = datetime.now(UTC)\n"
        "def test_x():\n"
        "    assert time.time() > 0\n"
        "f = lambda: datetime.now()\n"
    )
    assert import_time_stamps(src) == []


def test_no_test_module_stamps_the_clock_at_import():
    modules = _test_modules()
    assert len(modules) > 50, "discovery found too few test modules to be the suite"
    offenders = {
        str(p.relative_to(ROOT)): hits
        for p in modules
        if (hits := import_time_stamps(p.read_text()))
    }
    assert not offenders, (
        "wall clock read at import -- it ages with the suite; stamp it per test "
        f"in an autouse fixture instead: {offenders}"
    )
