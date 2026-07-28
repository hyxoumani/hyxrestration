"""Report timestamps and filenames must be UTC, never naive local time.

Standing reports (atlas, maker bracket, prioritycheck, divergence) are
compared against each other by hand in `docs/wiki/status.md`: "the prior
run was N hours ago, so this reading is/isn't stale". That arithmetic is
only valid if every report stamps the same clock.

It did not. `simulator/atlas.py` used `datetime.now(UTC)` while
`simulator/queuescore.py` and `simulator/prioritycheck.py` used a naive
`datetime.now()` — local time, and the box runs UTC-5. Maker-bracket
report filenames therefore read ~5h earlier than they were, so staleness
computed off a filename ran ~5h pessimistic for months of status entries
(caught 2026-07-27, 21st weather bracket pass).

Per-call-site tests would not have caught it: atlas was already correct
and queuescore was simply missed. This asserts the invariant across the
whole tree so the next report module cannot reintroduce it.

`datetime.now(UTC)` is required; `datetime.now()` with no argument is
banned outright in report-writing packages. Naive stamps derived from an
aware value (`datetime.now(UTC).replace(tzinfo=None, ...)`) are fine —
the clock is UTC, only the tzinfo label is dropped for JSON.
"""

import ast

import pytest

from tests.test_boundaries import PACKAGES, ROOT


def _module_files():
    for pkg in sorted(PACKAGES):
        yield from sorted((ROOT / pkg).rglob("*.py"))


def _naive_now_calls(tree: ast.AST) -> list[int]:
    """Line numbers of `datetime.now()` / `now()` calls with no tz argument."""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "now":
            continue
        # datetime.now(UTC) / datetime.now(tz=...) are the correct forms.
        if node.args or any(kw.arg == "tz" for kw in node.keywords):
            continue
        bad.append(node.lineno)
    return bad


@pytest.mark.parametrize("path", list(_module_files()), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_naive_datetime_now(path):
    """No module may call `datetime.now()` without an explicit timezone."""
    tree = ast.parse(path.read_text())
    lines = _naive_now_calls(tree)
    assert not lines, (
        f"{path.relative_to(ROOT)}: naive datetime.now() at line(s) "
        f"{lines} — use datetime.now(UTC). Report timestamps and filenames "
        "are compared across modules; a local-time stamp silently skews "
        "staleness arithmetic by the box's UTC offset."
    )
