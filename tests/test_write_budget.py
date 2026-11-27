"""The per-test write budget (tests/conftest.py, mistakes #252).

One file's fixtures were 86.3 of the suite's 91.6 GB of writes for 18 days
because nothing counted them. These pin the counter the budget stands on --
it sees real bytes, it does not see a hole -- and the verdict's three arms.
"""

from __future__ import annotations

from pathlib import Path

from tests import conftest


def test_the_counter_sees_bytes_written_and_not_a_hole(tmp_path: Path) -> None:
    start = conftest.proc_write_bytes()
    assert start is not None, "/proc/self/io unreadable: the budget is blind here"
    with (tmp_path / "hole").open("wb") as fh:
        fh.truncate(10**9)
    assert (tmp_path / "hole").stat().st_size == 10**9
    after_hole = conftest.proc_write_bytes()
    assert after_hole - start < 10**6
    (tmp_path / "real").write_bytes(b"\0" * (8 * 10**6))
    assert conftest.proc_write_bytes() - after_hole >= 8 * 10**6


def test_a_test_over_the_budget_is_named_with_its_bytes() -> None:
    msg = conftest.write_budget_breach(0, 3 * 10**9, 2 * 10**9)
    assert msg is not None and "wrote 3.00 GB" in msg and "budget of 2.00 GB" in msg


def test_at_the_budget_or_unreadable_is_no_breach() -> None:
    assert conftest.write_budget_breach(0, 2 * 10**9, 2 * 10**9) is None
    assert conftest.write_budget_breach(None, 5 * 10**9, 1) is None
    assert conftest.write_budget_breach(0, None, 1) is None


def test_the_budget_is_wired_to_every_test_and_sits_above_the_largest_writer() -> None:
    import inspect

    assert "autouse=True" in inspect.getsource(conftest).split("def _write_budget")[0][-40:]
    # 0.74 GB measured for the EXP-1373 shared-spill test; 34.1 GB for the worst before.
    assert 10**9 <= conftest.WRITE_BUDGET_BYTES <= 4 * 10**9
