"""Daily signal-feed pull: ALFRED econ vintages + GDELT news (B4/C1).

    python -m collector.signals [--db data/hyxlab.duckdb] [--gdelt-hours 25]

Pattern: fetch everything over the network FIRST (no store open), then
write in one brief flock+open_retry burst — a signals pull must never
hold the archive lock across network I/O (review H1 discipline).

PER-SERIES FETCH RECORD (EXP-1360). `fetch_alfred` swallows every
per-series failure — three attempts, a print, and the series is simply
absent from the returned dict. That is invisible downstream: a monthly
series that ALFRED stopped serving looks exactly like a monthly series
that has not printed yet, for a month. So each run now appends one JSON
line per pull to `data/signals_fetch.jsonl` recording, per series,
whether the fetch SUCCEEDED and how many observations it returned. It is
the only witness that separates "not published yet" from "not fetched".

ALFRED subtlety: the keyless vintage endpoint stamps knowable_at with
the FETCH day's pessimistic 23:59 ET, so a naive daily insert would
re-log the entire history as fake new vintages every day. The pull
diffs against the latest stored value per (series, obs_date) and keeps
only genuinely new periods and revisions — econ_vintages stays a true
vintage log built forward from first pull. (Historical vintages need a
FRED API key; standing user item.)

GDELT: resumes from the stored news watermark (max knowable_at), else
the trailing --gdelt-hours; missing quarter-hour files are skipped.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import requests

from collector.lockid import note_holder
from collector.venues import alfred, gdelt
from hyxlab.models import EconVintage, NewsItem
from hyxlab.store import Store, open_retry

LOCK_FILE = "data/writer.lock"
GKG_PAUSE_S = 0.2
# Per-series ALFRED fetch outcomes, one JSON line per run. Deliberately a
# sidecar and not an archive table: a fetch outcome must be recordable when
# the archive is locked or unopenable, which is exactly when the pull is
# most likely to be failing. Same rationale as `data/collect_skips.jsonl`.
FETCH_LOG = "data/signals_fetch.jsonl"


def fetch_alfred(session: requests.Session, today=None) -> dict[str, list[EconVintage]]:
    """Fresh session per attempt: once a request to alfred.stlouisfed.org
    times out, the shared keep-alive connection stays wedged and every
    later request on that session times out too (observed 2026-07-11:
    whole runs failed while a fresh-session probe succeeded instantly).
    The caller's session is deliberately not used here."""
    today = today or datetime.now(UTC).date()
    out: dict[str, list[EconVintage]] = {}
    outcomes: dict[str, dict] = {}
    for series in alfred.SERIES:
        last_err: str | None = None
        for attempt in range(3):
            try:
                out[series] = alfred.get_vintage(series, today, session=requests.Session())
                last_err = None
                break
            except Exception as exc:
                last_err = type(exc).__name__
                print(
                    f"[signals] alfred {series} attempt {attempt + 1}: {last_err}",
                    flush=True,
                )
                time.sleep(10)
        # rows is the OBSERVATION count returned, not the count inserted: the
        # diff drops everything unrevised, so an insert count of 0 is the
        # normal case and says nothing about whether the fetch worked.
        outcomes[series] = (
            {"ok": False, "rows": 0, "error": last_err}
            if last_err is not None
            else {"ok": True, "rows": len(out[series]), "error": None}
        )
        time.sleep(2)
    return out, outcomes


def record_fetch(
    vintage_date: date, outcomes: dict[str, dict], at: datetime | None = None, path: str = FETCH_LOG
) -> None:
    """Append this run's per-series fetch outcomes. Never raises: losing the
    record must not fail a pull that otherwise succeeded — QA decides an
    absent sidecar against an independent witness rather than trusting it."""
    row = {
        "at": (at or datetime.now(UTC)).isoformat(),
        "vintage_date": vintage_date.isoformat(),
        "series": outcomes,
    }
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"[signals] could not record fetch outcomes: {type(exc).__name__}: {exc}", flush=True)


def diff_vintages(store: Store, fetched: dict[str, list[EconVintage]]) -> list[EconVintage]:
    """Keep only observations whose value differs from the latest stored
    vintage of that (series, period) — new periods and true revisions."""
    new: list[EconVintage] = []
    for series, vintages in fetched.items():
        latest = dict(
            store.conn.execute(
                "SELECT obs_date, arg_max(value, knowable_at) FROM econ_vintages"
                " WHERE series_id = ? GROUP BY obs_date",
                [series],
            ).fetchall()
        )
        new.extend(v for v in vintages if latest.get(v.obs_date) != v.value)
    return new


def fetch_gdelt(
    session: requests.Session, start: datetime, end: datetime
) -> tuple[list[NewsItem], int]:
    templates = gdelt.load_templates()
    items: list[NewsItem] = []
    missing = 0
    for url in gdelt.gkg_urls(start, end):
        try:
            text = gdelt.fetch_gkg(url, session=session)
        except Exception as exc:
            print(f"[signals] gdelt {url.rsplit('/', 1)[-1]}: {type(exc).__name__}", flush=True)
            continue
        if text is None:
            missing += 1
            continue
        items.extend(gdelt.parse_gkg(text, templates))
        time.sleep(GKG_PAUSE_S)
    return items, missing


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab daily signal pull (ALFRED + GDELT)")
    ap.add_argument("--db", default="data/hyxlab.duckdb")
    ap.add_argument("--gdelt-hours", type=float, default=25.0, help="cold-start window")
    ap.add_argument("--skip-gdelt", action="store_true")
    args = ap.parse_args()
    sess = requests.Session()
    sess.headers["User-Agent"] = "hyxlab-research"
    now = datetime.now(UTC).replace(tzinfo=None)

    # read the watermark in a short-lived read-only open; a read-only
    # Store doesn't create schema, so a pre-B4 archive lacks the table
    # until the first write below — treat that as a cold start.
    store = open_retry(args.db, read_only=True)
    try:
        last_news = store.conn.execute(
            "SELECT max(knowable_at) FROM news_items WHERE source='gdelt'"
        ).fetchone()[0]
    except duckdb.CatalogException:
        last_news = None
    finally:
        store.close()

    vintage_date = now.date()
    fetched, outcomes = fetch_alfred(sess, today=vintage_date)
    record_fetch(vintage_date, outcomes, at=datetime.now(UTC))
    items: list[NewsItem] = []
    missing = 0
    if not args.skip_gdelt:
        start = (
            last_news + timedelta(minutes=15)
            if last_news
            else now - timedelta(hours=args.gdelt_hours)
        )
        items, missing = fetch_gdelt(sess, start, now)

    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        note_holder(LOCK_FILE)
        store = open_retry(args.db)
        try:
            new_vintages = diff_vintages(store, fetched)
            nv = store.insert_vintages(new_vintages)
            nn = store.insert_news(items)
        finally:
            store.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
    failed = sorted(k for k, v in outcomes.items() if not v["ok"])
    print(
        f"[signals] {now:%Y-%m-%d %H:%M} vintages+{nv}"
        f" news+{nn} (of {len(items)} parsed; {missing} missing files)"
        f" alfred {len(outcomes) - len(failed)}/{len(outcomes)} fetched"
        + (f"; FAILED {', '.join(failed)}" if failed else ""),
        flush=True,
    )


if __name__ == "__main__":
    main()
