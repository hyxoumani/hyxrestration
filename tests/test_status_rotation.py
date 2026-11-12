"""The status page stays readable at cold start, and rotating it loses nothing.

`docs/wiki/status.md` grows ~45 KB a day and reached 1 MB twice (rotated by
hand 2026-10-18 and 2026-11-12); past a few hundred KB a cold-start reads
only the first entry. The second hand rotation also wrote a size bound into
the page's pointer block that the month-by-month rule could not meet -- a
limit in prose. This is the limit as an assertion, and the rotation as a
function with a conservation test.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS = ROOT / "docs" / "wiki" / "status.md"
MAX_BYTES = 600_000

_spec = importlib.util.spec_from_file_location(
    "rotate_status", ROOT / "scripts" / "rotate_status.py"
)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

PAGE = "\n".join(
    [
        "# Status",
        "",
        "Updated: **2026-11-12 20:00Z (c3 ** / **USER x**)** / Updated: **2026-11-11 08:00Z (c2)**"
        " / Updated: **2026-11-03 08:00Z (c1 says Updated: maybe)**",
        "",
        "**2026-11-02 10:00Z PASS -- p3.** body",
        "",
        "continuation of p3",
        "",
        "**2026-10-30 10:00Z PASS -- p2.**",
        "",
        "**2026-10-29 10:00Z PASS -- p1.**",
        "",
        "",
        "---",
        "",
        "**Older dated entries were rotated.**",
        "",
        "## Where the project is",
        "standing text",
        "",
    ]
)


def test_the_living_page_is_under_its_bound():
    size = STATUS.stat().st_size
    assert size < MAX_BYTES, (
        f"status.md is {size:,} B (bound {MAX_BYTES:,}): run"
        " `.venv/bin/python scripts/rotate_status.py` and commit both files"
    )


def test_rotation_moves_only_old_entries_and_conserves_every_one():
    new, moved = rs.rotate(PAGE, keep_days=5)
    # Cutoff is 11-07: c3 and c2 stay; c1, p3, p2, p1 move, by month.
    assert list(moved) == ["2026-11", "2026-10"]
    assert [e[:30] for e in moved["2026-11"]] == [
        "Updated: **2026-11-03 08:00Z (",
        "**2026-11-02 10:00Z PASS -- p3",
    ]
    assert moved["2026-11"][1].endswith("continuation of p3")  # owns its undated lines
    assert [e[:12] for e in moved["2026-10"]] == ["**2026-10-30", "**2026-10-29"]
    # The ` / ` inside c3 is not a chain boundary.
    assert "Updated: **2026-11-12 20:00Z (c3 ** / **USER x**)** / Updated: **2026-11-11" in new
    for gone in ("c1 says", "p3", "p2", "p1"):
        assert gone not in new
    # Head and standing sections are byte-identical.
    assert new.startswith("# Status\n\nUpdated: **2026-11-12")
    tail = PAGE[PAGE.index("---\n") :]
    assert new.endswith(tail)
    # Conservation: every entry is in exactly one place, unaltered.
    everything = new + "\n".join(e for es in moved.values() for e in es)
    for piece in (
        "(c3 ** / **USER x**)**",
        "(c2)**",
        "(c1 says Updated: maybe)**",
        "continuation of p3",
    ):
        assert everything.count(piece) == 1


def test_nothing_moves_inside_the_window_and_the_page_is_unchanged():
    new, moved = rs.rotate(PAGE, keep_days=60)
    assert moved == {} and new == PAGE


def test_archive_gets_newer_entries_above_older_ones(tmp_path):
    rs.write_archive("2026-11", ["**2026-11-02 a**", "**2026-11-01 b**"], wiki=tmp_path)
    rs.write_archive("2026-11", ["**2026-11-05 c**"], wiki=tmp_path)
    text = (tmp_path / "status-archive-2026-11.md").read_text()
    assert text.index("2026-11-05 c") < text.index("2026-11-02 a") < text.index("2026-11-01 b")
    assert text.count("\n---\n") == 1


def test_a_page_without_the_pointer_block_is_refused():
    import pytest

    with pytest.raises(SystemExit):
        rs.rotate("# Status\n\n**2026-11-02 x**\n", keep_days=5)
