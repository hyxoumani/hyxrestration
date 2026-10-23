"""One-shot: un-resolve the Polymarket markets a pre-86a4414 sweep archived
AS RESOLVED on a fetch that raised, so the next run fetches them.

Until 2026-10-21 (`collector.poly_sweep.sweep`, commit 86a4414) a market
whose fetch raised KEPT its Gamma row and its stats row -- both were
appended before the fetch and survived the `except`. The Gamma row sets
`markets.result` for a closed market, and a non-empty result is exactly
what `Store.poly_resolved_complete` reads as "a prior run fetched this to
its close": the next run skips both fetches for good. A market FIRST seen
closed on an incident day was therefore archived resolved with no prices
and no tail (mistakes #131). The 10-21 04:15Z run was the last on the old
code: 330 closed-segment errors from 09:17Z (321 CLOB `500 Server Error`
on prices-history, 9 ReadTimeout), 322 of them left with `result <> ''`,
306 first seen that day.

WHAT IT DOES. Parses the sweep's `[poly] <Class> at <cond[:16]>: ...`
lines inside a UTC window, resolves the prefixes against `markets` exactly
as `collector.poly_tail_backfill` does (zero or many matches: REPORTED AND
DROPPED, never guessed), and clears `result` on the ones that carry one.
Tomorrow's run then sees them as not-yet-resolved, fetches prices from
the watermark (a token that succeeded before the raise resumes, the other
starts at the ~60-day window) and the tail from offset 0, and its Gamma
row sets `result` again. The stats row stays: it is the tail-absorption
oracle's witness, and these markets have no tail stop for it to absorb
(a raise on prices-history never reached `trades_tail`); deleting rows
from `poly_market_stats` would also move the universe-shrink tripwire.

WHY NOT A DIRECT RE-FETCH. The sweep already owns the paced fetch, the
watermarks, the flush and the hold ledger; the closed segment of the next
run is where these 330 markets belong (nothing trades on a closed market,
so a day's delay loses nothing), and their `endDate` keeps them inside the
7-day closed enumeration window on 10-22. A second fetcher would be a
second copy of `_fetch_market` against an API that was still returning
500s at the done line.

Run (writes nothing without --commit; the window bounds are UTC):
    python -m collector.poly_unmark_errors --since "2026-10-21 09:17" --until "2026-10-21 12:00"
    python -m collector.poly_unmark_errors --since ... --until ... --commit
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from collector.poly_tail_backfill import resolve_prefixes
from collector.sweep import writer_burst
from hyxlab.store import attach_wait_block, attach_wait_line, held_open, reset_attach_waits

DEFAULT_UNIT = "hyxlab-poly-sweep"
MARKER = "[poly] "

#: The sweep's per-market `except` line as `journalctl -o short-iso` renders
#: it: `[poly] HTTPError at 0x9b37b2134772e6: 500 Server Error: ...`. The
#: class is a bare identifier and the prefix is `cond[:16]`.
ERROR_LINE = re.compile(
    r"^(?P<ts>\S+) \S+ \S+: \[poly\] (?P<cls>[A-Za-z_]\w*) at (?P<prefix>0x[0-9a-f]+): "
)
#: A `[poly] ... at 0x...:` line that is not an error line is a parse
#: refusal, not a skip (mistakes #82): the sweep prints the market prefix in
#: exactly this shape only from the `except`.
MARKER_RE = re.compile(r"\[poly\] .* at 0x[0-9a-f]+: ")


def read_journal(unit: str, since: str, until: str) -> str:
    """`--since/--until` parse a bare stamp in the box's LOCAL zone; the
    ` UTC` suffix is load-bearing (ops rule, measured 2026-10-18)."""
    return subprocess.run(
        [
            "journalctl",
            "--user",
            "-u",
            unit,
            "-o",
            "short-iso",
            "--no-pager",
            "--since",
            f"{since} UTC",
            "--until",
            f"{until} UTC",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def parse_errors(text: str) -> list[tuple[str, datetime, str]]:
    """(prefix, at UTC-naive, exception class) per logged fetch error.

    Raises on any line carrying the error shape the regex cannot read. The
    stamp is normalised off the line's own offset, as the tail backfill does.
    """
    out: list[tuple[str, datetime, str]] = []
    for line in text.splitlines():
        if not MARKER_RE.search(line):
            continue
        m = ERROR_LINE.match(line)
        if m is None:
            raise ValueError(f"unreadable error line: {line[:200]!r}")
        at = datetime.fromisoformat(m["ts"])
        out.append(
            (m["prefix"], at.replace(tzinfo=None) - (at.utcoffset() or timedelta()), m["cls"])
        )
    return out


def resolved_among(store, market_ids: list[str]) -> list[str]:
    """The polymarket markets among `market_ids` whose `result` is set --
    the ones `poly_resolved_complete` would skip. Sorted, for a stable
    report."""
    if not market_ids:
        return []
    conn = store.conn
    conn.execute("CREATE OR REPLACE TEMP TABLE _ids(m VARCHAR)")
    conn.executemany("INSERT INTO _ids VALUES (?)", [(m,) for m in market_ids])
    rows = conn.execute(
        """
        SELECT m.market_id FROM markets m JOIN _ids i ON i.m = m.market_id
        WHERE m.venue = 'polymarket' AND m.result <> ''
        ORDER BY 1
        """
    ).fetchall()
    return [r[0] for r in rows]


def _attach_line(db: str) -> str:
    """The run's holds on the collector's file, through the one renderer
    (#95): the read-only resolve plus, under --commit, the writer burst."""
    return attach_wait_line(attach_wait_block(db=Path(db).name), prefix="[unmark]")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--unit", default=DEFAULT_UNIT)
    ap.add_argument("--since", required=True, help="UTC, e.g. '2026-10-21 09:17'")
    ap.add_argument("--until", required=True, help="UTC, e.g. '2026-10-21 12:00'")
    ap.add_argument("--commit", action="store_true", help="write (default: report only)")
    args = ap.parse_args(argv)
    reset_attach_waits()

    errors = parse_errors(read_journal(args.unit, args.since, args.until))
    classes: dict[str, int] = {}
    for _, _, cls in errors:
        classes[cls] = classes.get(cls, 0) + 1
    print(
        f"[unmark] {len(errors)} error line(s) in {args.unit} {args.since}..{args.until}Z: {classes}"
    )
    if not errors:
        return 0
    prefixes = sorted({e[0] for e in errors})

    # `held_open`: the 5-minute collector's file, measured (#91, #96).
    with held_open(args.db, read_only=True) as store:
        ids = resolve_prefixes(store, prefixes)
        targets = resolved_among(store, sorted(ids.values()))

    unresolved = [p for p in prefixes if p not in ids]
    if unresolved:
        print(
            f"[unmark] DROPPED {len(unresolved)} prefix(es) with no unique polymarket "
            f"market: {', '.join(unresolved[:6])}"
        )
    print(
        f"[unmark] {len(prefixes)} distinct market(s), {len(ids)} resolved to an id, "
        f"{len(targets)} archived with result set -> to clear"
        + (f": {', '.join(t[:16] for t in targets[:4])} ..." if targets else "")
    )
    if not args.commit:
        print("[unmark] dry run -- pass --commit to write")
        print(_attach_line(args.db))
        return 0
    if not targets:
        print(_attach_line(args.db))
        return 0

    with writer_burst(args.db) as store:
        n = store.poly_unset_results(targets)
    print(f"[unmark] cleared result on {n} market(s)")
    print(_attach_line(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
