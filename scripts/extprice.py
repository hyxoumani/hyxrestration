"""Price a deletion candidate on btrfs by the extents nothing else references.

    .venv/bin/python scripts/extprice.py CANDIDATE --vs OTHER [OTHER ...]

`btrfs filesystem du -s` prints `Exclusive` and `Set shared` for a directory,
and neither answers "what does deleting this free": an extent referenced by two
files INSIDE the candidate and by nothing outside it is `Set shared`, not
`Exclusive`, and deleting the directory frees it. So `Exclusive` is a LOWER
bound on the bytes a delete returns and `Set shared` cannot say whether its
bytes live anywhere else (measured 2026-11-20: `~/scratch/backupcap` reads
`Exclusive 0.03 GB, Set shared 59.1 GB` -- zero or 59 GB freed, by that line).

This reads each file's physical extents (FIEMAP, via `filefrag -v`) and
subtracts every extent a file under an OTHER path also maps. What is left is an
UPPER bound on the bytes freed: a sharer nobody named (a snapshot, a copy in a
directory not listed) still pins it. Lower bound from btrfs, upper bound from
here; a candidate is priced when the two agree.

Compressed (`encoded`) extents: FIEMAP reports their LOGICAL length at the
compressed extent's physical start, so their ranges are not disk ranges and
would overlap their neighbours' if treated as intervals. They are matched by
physical start instead and reported apart, in logical bytes -- on disk they
are smaller. Extents not yet allocated (`delalloc`, `unknown`) are skipped.

Read-only: it opens no DuckDB file and takes no lock (FIEMAP is a metadata
ioctl; `filefrag` on a 400 GB archive is 0.4 s).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

BLOCK = 4096  # filefrag -v prints 4096-byte blocks on btrfs; asserted per file
_ROW = re.compile(r"\s*\d+:\s+\d+\.\.\s*\d+:\s+(\d+)\.\.\s*(\d+):\s+(\d+):(.*)$")
_BLOCKSIZE = re.compile(r"blocks? of (\d+) bytes")
_UNALLOCATED = ("delalloc", "unknown")


@dataclass
class Extents:
    """One path's physical extents: plain ones as half-open block intervals,
    compressed ones as {physical start block: logical blocks}."""

    plain: list[tuple[int, int]] = field(default_factory=list)
    encoded: dict[int, int] = field(default_factory=dict)

    def add(self, other: Extents) -> None:
        self.plain.extend(other.plain)
        for start, n in other.encoded.items():
            self.encoded[start] = max(n, self.encoded.get(start, 0))


def parse_filefrag(text: str) -> Extents:
    """Extents from one file's `filefrag -v` output."""
    m = _BLOCKSIZE.search(text)
    if m and int(m.group(1)) != BLOCK:
        raise ValueError(f"filefrag block size {m.group(1)}, expected {BLOCK}")
    out = Extents()
    for line in text.splitlines():
        row = _ROW.match(line)
        if not row:
            continue
        start, end, length, flags = (
            int(row.group(1)),
            int(row.group(2)),
            int(row.group(3)),
            row.group(4),
        )
        if any(f in flags for f in _UNALLOCATED) or "inline" in flags:
            continue
        if "encoded" in flags:
            out.encoded[start] = max(length, out.encoded.get(start, 0))
        else:
            out.plain.append((start, end + 1))
    return out


def union(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def subtract(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """`a` minus `b`; both must be `union` output (sorted, disjoint)."""
    out: list[tuple[int, int]] = []
    j = 0
    for start, end in a:
        cur = start
        while j < len(b) and b[j][1] <= cur:
            j += 1
        k = j
        while k < len(b) and b[k][0] < end:
            if b[k][0] > cur:
                out.append((cur, b[k][0]))
            cur = max(cur, b[k][1])
            k += 1
        if cur < end:
            out.append((cur, end))
    return out


def _blocks(intervals: list[tuple[int, int]]) -> int:
    return sum(b - a for a, b in intervals)


@dataclass(frozen=True)
class Price:
    files: int
    other_files: int
    plain_b: int  # distinct plain bytes the candidate maps
    plain_only_b: int  # of those, mapped by no file under an OTHER path
    encoded_logical_b: int
    encoded_only_logical_b: int


def price(candidate: Extents, others: Extents, files: int = 0, other_files: int = 0) -> Price:
    mine = union(candidate.plain)
    only = subtract(mine, union(others.plain))
    enc_only = sum(n for start, n in candidate.encoded.items() if start not in others.encoded)
    return Price(
        files=files,
        other_files=other_files,
        plain_b=_blocks(mine) * BLOCK,
        plain_only_b=_blocks(only) * BLOCK,
        encoded_logical_b=sum(candidate.encoded.values()) * BLOCK,
        encoded_only_logical_b=enc_only * BLOCK,
    )


def _files(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    out = []
    for d, _, names in os.walk(root):
        for name in names:
            p = os.path.join(d, name)
            if os.path.isfile(p) and not os.path.islink(p):
                out.append(p)
    return out


def read_extents(paths: list[str], skip: frozenset[str] = frozenset()) -> tuple[Extents, int]:
    """Every regular file under `paths`, minus real paths in `skip` -- so an
    OTHER that contains the candidate does not excuse the candidate by itself."""
    total, n = Extents(), 0
    for root in paths:
        for f in _files(root):
            if os.path.realpath(f) in skip:
                continue
            res = subprocess.run(["filefrag", "-v", f], capture_output=True, text=True, check=False)
            if res.returncode != 0:
                print(
                    f"[extprice] WARNING: filefrag failed on {f}: {res.stderr.strip()}",
                    file=sys.stderr,
                )
                continue
            total.add(parse_filefrag(res.stdout))
            n += 1
    return total, n


def render(candidate: str, p: Price) -> str:
    gb = 1e9
    line = (
        f"[extprice] {candidate}: {p.files} file(s), {p.plain_b / gb:.2f} GB of distinct plain extents, "
        f"{p.plain_only_b / gb:.2f} GB mapped by none of the {p.other_files} file(s) named with --vs "
        f"(an UPPER bound on what deleting it frees: an unnamed sharer still pins it)"
    )
    if p.encoded_logical_b:
        line += (
            f" | compressed extents: {p.encoded_logical_b / gb:.2f} GB logical, "
            f"{p.encoded_only_logical_b / gb:.2f} GB of it unshared (smaller on disk)"
        )
    return line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("candidate", help="file or directory whose deletion is being priced")
    ap.add_argument(
        "--vs", nargs="+", required=True, metavar="OTHER", help="paths that may share its extents"
    )
    args = ap.parse_args(argv)
    cand_files = frozenset(os.path.realpath(f) for f in _files(args.candidate))
    if not cand_files:
        print(f"[extprice] no regular file under {args.candidate}", file=sys.stderr)
        return 2
    mine, n = read_extents([args.candidate])
    theirs, m = read_extents(args.vs, skip=cand_files)
    print(render(args.candidate, price(mine, theirs, n, m)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
