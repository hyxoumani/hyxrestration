"""`scripts/extprice.py`: a deletion candidate priced by the extents nothing
else maps. The arithmetic is tested on fixtures; the claim itself -- a reflink
copy prices at zero against its source and at full size against nothing -- is
tested on a real reflink in the repo's on-disk tmp (skipped off btrfs)."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("extprice", REPO / "scripts" / "extprice.py")
extprice = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402

sys.modules["extprice"] = extprice  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(extprice)

FILEFRAG = """Filesystem type is: 9123683e
File size of x is 20983808 (5123 blocks of 4096 bytes)
 ext:     logical_offset:        physical_offset: length:   expected: flags:
   0:        0..       0:   35171613..  35171613:      1:             encoded,shared
   1:        1..       1:  956767897.. 956767897:      1:   35171614:
   2:        3..      34:  211740583.. 211740614:     32:  957226247: encoded
   3:       35..     134:       1000..      1099:    100:  211740615: last,shared,eof
   4:      135..     140:          0..         5:      6:             unknown,delalloc
x: 5 extents found
"""


def test_parse_separates_plain_encoded_and_unallocated():
    e = extprice.parse_filefrag(FILEFRAG)
    assert e.plain == [(956767897, 956767898), (1000, 1100)]
    assert e.encoded == {35171613: 1, 211740583: 32}


def test_parse_refuses_another_block_size():
    with pytest.raises(ValueError):
        extprice.parse_filefrag(FILEFRAG.replace("of 4096 bytes", "of 1024 bytes"))


def test_union_and_subtract():
    assert extprice.union([(5, 9), (0, 3), (2, 4), (9, 12)]) == [(0, 4), (5, 12)]
    a = [(0, 10), (20, 30), (40, 50)]
    b = [(2, 4), (8, 22), (29, 41), (60, 70)]
    assert extprice.subtract(a, b) == [(0, 2), (4, 8), (22, 29), (41, 50)]
    assert extprice.subtract(a, []) == a
    assert extprice.subtract(a, [(0, 100)]) == []


def test_price_counts_a_partial_overlap_and_matches_encoded_by_start():
    mine = extprice.Extents(plain=[(0, 100), (50, 150)], encoded={7: 32, 9: 32})
    theirs = extprice.Extents(plain=[(140, 400)], encoded={9: 32, 11: 32})
    p = extprice.price(mine, theirs, files=2, other_files=1)
    assert p.plain_b == 150 * extprice.BLOCK  # the two files' overlap counted once
    assert p.plain_only_b == 140 * extprice.BLOCK
    assert p.encoded_logical_b == 64 * extprice.BLOCK
    assert p.encoded_only_logical_b == 32 * extprice.BLOCK
    line = extprice.render("cand", p)
    assert "UPPER bound" in line and "2 file(s)" in line and "1 file(s) named" in line


def _reflink(src: Path, dst: Path) -> bool:
    return (
        subprocess.run(
            ["cp", "--reflink=always", str(src), str(dst)], capture_output=True
        ).returncode
        == 0
    )


@pytest.mark.skipif(shutil.which("filefrag") is None, reason="filefrag not installed")
def test_a_reflink_pair_inside_the_candidate_is_freeable_and_a_shared_one_is_not(tmp_path):
    """The case `btrfs filesystem du -s` cannot tell apart: two files in the
    candidate sharing extents with each other. Against nothing they are
    freeable (once, not twice); against an outside reflink they are not."""
    cand = tmp_path / "cand"
    cand.mkdir()
    n = 1 << 20
    a = cand / "a.bin"
    with open(a, "wb") as fh:
        fh.write(os.urandom(n))  # incompressible: stays a plain extent under compress=zstd
        fh.flush()
        os.fsync(fh.fileno())
    if not _reflink(a, cand / "b.bin"):
        pytest.skip("filesystem does not reflink")
    outside = tmp_path / "outside.bin"
    assert _reflink(a, outside)
    unrelated = tmp_path / "unrelated.bin"
    with open(unrelated, "wb") as fh:
        fh.write(os.urandom(n))
        fh.flush()
        os.fsync(fh.fileno())

    files = frozenset(os.path.realpath(f) for f in (a, cand / "b.bin"))
    mine, k = extprice.read_extents([str(cand)])
    assert k == 2
    alone, m = extprice.read_extents([str(unrelated)])
    p = extprice.price(mine, alone, k, m)
    assert p.plain_b == n, "a reflink pair maps its bytes once"
    assert p.plain_only_b == n, "nothing outside maps them: the delete frees them"

    shared, _ = extprice.read_extents([str(outside)])
    assert extprice.price(mine, shared).plain_only_b == 0

    # An OTHER that contains the candidate must not excuse the candidate by itself.
    whole, m2 = extprice.read_extents([str(tmp_path)], skip=files)
    assert m2 == 2
    assert extprice.price(mine, whole).plain_only_b == 0
    outside.unlink()
    whole, _ = extprice.read_extents([str(tmp_path)], skip=files)
    assert extprice.price(mine, whole).plain_only_b == n
