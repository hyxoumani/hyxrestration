"""Rotate old dated entries off docs/wiki/status.md into monthly archives.

    .venv/bin/python scripts/rotate_status.py [--keep-days 5] [--dry-run]

The living page grows ~45 KB a day and has twice reached 1 MB, at which
point a cold-start reads only its first entry (rotated by hand 2026-10-18
and 2026-11-12). This moves every entry dated more than `--keep-days`
before the NEWEST entry into `status-archive-YYYY-MM.md`, verbatim and
newest first; the header and the standing sections are not touched.

Two entry shapes exist on the page and both are handled:
  * the `Updated: **<stamp> (...)** / Updated: **...` chain on one line --
    split only at ` / ` followed by `Updated: **YYYY-`; an archived
    segment becomes its own paragraph, text unchanged;
  * `**YYYY-MM-DD ...` paragraphs, each owning the undated lines under it.
Entries are newest first, so what moves is a suffix of each.
"""

from __future__ import annotations

import argparse
import re
from datetime import date, timedelta
from pathlib import Path

WIKI = Path(__file__).resolve().parent.parent / "docs" / "wiki"
CHAIN_SPLIT = re.compile(r" / (?=Updated: \*\*\d{4}-\d{2}-\d{2} )")
CHAIN_DATE = re.compile(r"Updated: \*\*(\d{4}-\d{2}-\d{2}) ")
PARA_DATE = re.compile(r"\*\*(\d{4}-\d{2}-\d{2}) ")
POINTER = "**Older dated entries"

ARCHIVE_HEADER = """# Status archive — {month} dated entries

Rotated out of [status.md](status.md) by `scripts/rotate_status.py`. Every
entry below is verbatim, newest first; a segment of the page's one-line
`Updated:` chain sits here as its own paragraph. Cold-start readers: do
not start here -- read [status.md](status.md), then [mistakes.md](mistakes.md).

---

"""


def split_page(text: str) -> tuple[list[str], list[str], list[list[str]], list[str]]:
    """-> (head lines, chain segments, dated paragraphs, tail lines).

    `tail` starts at the `---` rule above the pointer block; a page without
    that block is refused rather than guessed at."""
    lines = text.split("\n")
    ptr = next((i for i, ln in enumerate(lines) if ln.startswith(POINTER)), None)
    if ptr is None or ptr < 2 or lines[ptr - 2] != "---":
        raise SystemExit(
            "rotate_status: pointer block ('---' then '**Older dated entries') not found"
        )
    sep = ptr - 2
    chain_i = next((i for i, ln in enumerate(lines[:sep]) if CHAIN_DATE.match(ln)), None)
    body_from = 0 if chain_i is None else chain_i + 1
    first_para = next(
        (i for i in range(body_from, sep) if PARA_DATE.match(lines[i])),
        sep,
    )
    head = lines[: (first_para if chain_i is None else chain_i)]
    between = [] if chain_i is None else lines[chain_i + 1 : first_para]
    chain = [] if chain_i is None else CHAIN_SPLIT.split(lines[chain_i])
    paras: list[list[str]] = []
    for ln in lines[first_para:sep]:
        if PARA_DATE.match(ln):
            paras.append([ln])
        else:
            paras[-1].append(ln)
    # `between` (blank lines under the chain) rides with the head side.
    return head, chain, [between, *paras], lines[sep:]


def _para_date(p: list[str]) -> date:
    return date.fromisoformat(PARA_DATE.match(p[0]).group(1))


def _chain_date(seg: str) -> date:
    return date.fromisoformat(CHAIN_DATE.match(seg).group(1))


def rotate(text: str, keep_days: int) -> tuple[str, dict[str, list[str]]]:
    """-> (new living text, {YYYY-MM: [entry text, ...] newest first})."""
    head, chain, body, tail = split_page(text)
    between, paras = body[0], body[1:]
    dates = [_chain_date(s) for s in chain] + [_para_date(p) for p in paras]
    if not dates:
        return text, {}
    cutoff = max(dates) - timedelta(days=keep_days)
    keep_chain = [s for s in chain if _chain_date(s) >= cutoff]
    keep_paras = [p for p in paras if _para_date(p) >= cutoff]
    # Chain segments and paragraphs interleave by day (the chain can hold an
    # older stamp than the newest paragraph); a stable sort on the DATE keeps
    # each shape's own order inside a day.
    old = [(_chain_date(s), s) for s in chain if _chain_date(s) < cutoff]
    old += [(_para_date(p), "\n".join(p).rstrip("\n")) for p in paras if _para_date(p) < cutoff]
    old.sort(key=lambda e: e[0], reverse=True)
    moved: dict[str, list[str]] = {}
    for d, entry in old:
        moved.setdefault(f"{d:%Y-%m}", []).append(entry)
    out = list(head)
    if keep_chain:
        out.append(" / ".join(keep_chain))
    out += between
    for p in keep_paras:
        out += p
    if out and out[-1] != "":
        out.append("")
    return "\n".join(out + tail), moved


def write_archive(month: str, entries: list[str], wiki: Path = WIKI) -> Path:
    """Newer entries go ABOVE what the month's page already holds."""
    path = wiki / f"status-archive-{month}.md"
    block = "\n\n".join(entries) + "\n"
    if not path.exists():
        path.write_text(ARCHIVE_HEADER.format(month=month) + block)
        return path
    old = path.read_text()
    marker = "\n---\n\n"
    at = old.find(marker)
    if at < 0:
        raise SystemExit(f"rotate_status: {path.name} has no '---' rule to insert under")
    at += len(marker)
    path.write_text(old[:at] + block + "\n" + old[at:])
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--keep-days", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    status = WIKI / "status.md"
    text = status.read_text()
    new, moved = rotate(text, args.keep_days)
    n = sum(len(v) for v in moved.values())
    print(
        f"[rotate_status] {n} entr{'y' if n == 1 else 'ies'} older than {args.keep_days} d"
        f" -> {sorted(moved)}; page {len(text.encode()):,} -> {len(new.encode()):,} B"
        + (" (dry run)" if args.dry_run else "")
    )
    if args.dry_run or not moved:
        return
    for month, entries in moved.items():
        write_archive(month, entries)
    status.write_text(new)


if __name__ == "__main__":
    main()
