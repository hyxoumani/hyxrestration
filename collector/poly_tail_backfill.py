"""One-shot: recover the pre-recorder `trades_tail` early stops from the
journal into `poly_tail_stops`.

`qa.qa_poly_tail_absorbed` tests the absorber that licenses `trades_tail`'s
missing retry ladder, and it starts with an EMPTY table: the recorder shipped
2026-09-24 20:45Z, after that day's sweep, so the check's whole history begins
at the next run. The population it would have judged is not gone — it is in
`journalctl --user -u hyxlab-poly-sweep`, one line per stop, 1,358 of them
over 2026-09-06..24. It is gone SOON, though: the journal had already rotated
away everything before 09-06 when this was written, so this recovery has an
expiry date and runs once.

WHY A BACKFILL AND NOT A WAIT. The check's failure mode is a market that
stopped early and was never re-visited, and that is decided by history the
archive holds for ~60 days. Starting empty means the first ~48h of the check
are vacuous and every earlier hole is invisible forever; the journal turns
24 days of real population into the check's opening reading.

WHAT IT REFUSES TO GUESS. The log line carries `condition_id[:16]`, not the id
— the ids are resolved against `markets` and a prefix matching zero or more
than one polymarket market is REPORTED AND DROPPED, never guessed (1,299 of
1,299 resolved uniquely on 2026-09-25). A line containing "stopped early at"
that the parser cannot read is a hard refusal for the whole run rather than a
silently skipped row: a parser that quietly drops what it does not understand
is the defect that cost 78 days of poly delta capture (mistakes #82).

WHAT IT WILL NOT OVERWRITE. The journal is a PREFIX of the history; the live
recorder owns everything from its first row onward. So stops at or after the
earliest `stopped_at` already in the table are dropped: journald stamps to the
second and the recorder to the microsecond, so the same stop seen both ways
would not dedup on the (market_id, stopped_at) key and would show up as two.

Run (writes nothing without --commit):
    python -m collector.poly_tail_backfill
    python -m collector.poly_tail_backfill --commit
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta

from collector.sweep import writer_burst
from hyxlab.store import open_retry

DEFAULT_UNIT = "hyxlab-poly-sweep"
MARKER = "stopped early at"

#: The line `collector.venues.polymarket.trades_tail` prints at a stop, as
#: journald renders it with `-o short-iso`. `status` is captured as text and
#: converted separately so a non-numeric status is a parse refusal, not a 0.
STOP_LINE = re.compile(
    r"^(?P<ts>\S+) \S+ \S+: \[poly\] trades_tail (?P<prefix>[0-9a-fx]+) "
    r"stopped early at (?P<prints>\d+) prints "
    r"\(non-list body, status (?P<status>\d+)\)\s*$"
)


def read_journal(unit: str = DEFAULT_UNIT) -> str:
    return subprocess.run(
        ["journalctl", "--user", "-u", unit, "-o", "short-iso", "--no-pager"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def parse_stops(text: str) -> list[tuple[str, datetime, int, int]]:
    """(prefix, stopped_at UTC-naive, prints, status) per logged stop.

    Raises on any `MARKER` line the regex cannot read — see the module
    docstring. `stopped_at` is normalised off the line's own offset rather
    than assumed UTC: journald renders the BOX's local time, and reading
    -05:00 as UTC would shift every stop five hours forward, past the grace
    the check excludes on.
    """
    out: list[tuple[str, datetime, int, int]] = []
    for line in text.splitlines():
        if MARKER not in line:
            continue
        m = STOP_LINE.match(line)
        if m is None:
            raise ValueError(f"unreadable stop line: {line[:200]!r}")
        at = datetime.fromisoformat(m["ts"])
        out.append(
            (
                m["prefix"],
                at.replace(tzinfo=None) - (at.utcoffset() or timedelta()),
                int(m["prints"]),
                int(m["status"]),
            )
        )
    return out


def resolve_prefixes(store, prefixes: list[str]) -> dict[str, str]:
    """prefix -> polymarket market_id, for prefixes matching EXACTLY one.

    Ambiguous and unmatched prefixes are absent from the result; the caller
    reports them. Batched through a temp table because a LIKE per prefix is
    1,299 scans of `markets`.
    """
    conn = store.conn
    conn.execute("CREATE OR REPLACE TEMP TABLE _pfx(p VARCHAR)")
    conn.executemany("INSERT INTO _pfx VALUES (?)", [(p,) for p in prefixes])
    rows = conn.execute(
        """
        SELECT p.p, min(m.market_id)
        FROM _pfx p JOIN markets m
          ON m.venue = 'polymarket' AND m.market_id LIKE p.p || '%'
        GROUP BY p.p HAVING count(*) = 1
        """
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--unit", default=DEFAULT_UNIT)
    ap.add_argument("--commit", action="store_true", help="write the rows (default: report only)")
    args = ap.parse_args()

    stops = parse_stops(read_journal(args.unit))
    print(f"[tailfill] {len(stops)} stop line(s) parsed from {args.unit}")
    if not stops:
        return 0

    store = open_retry(args.db, read_only=True)
    try:
        floor = store.conn.execute("SELECT min(stopped_at) FROM poly_tail_stops").fetchone()[0]
        prefixes = sorted({s[0] for s in stops})
        ids = resolve_prefixes(store, prefixes)
    finally:
        store.close()

    unresolved = [p for p in prefixes if p not in ids]
    if unresolved:
        print(
            f"[tailfill] DROPPED {len(unresolved)} prefix(es) with no unique polymarket "
            f"market: {', '.join(unresolved[:6])}"
        )

    rows, skipped_live, skipped_id = [], 0, 0
    for prefix, at, prints, status in stops:
        mid = ids.get(prefix)
        if mid is None:
            skipped_id += 1
            continue
        if floor is not None and at >= floor:
            skipped_live += 1
            continue
        rows.append((mid, at, prints, status))

    print(
        f"[tailfill] {len(rows)} row(s) to write; {skipped_id} unresolved, "
        f"{skipped_live} at/after the live recorder's first row"
        + (f" ({floor:%Y-%m-%d %H:%M}Z)" if floor is not None else " (table empty)")
    )
    if not args.commit:
        print("[tailfill] dry run — pass --commit to write")
        return 0

    with writer_burst(args.db) as store:
        n = store.insert_poly_tail_stops(rows)
    print(f"[tailfill] inserted {n} new row(s) ({len(rows) - n} already present)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
