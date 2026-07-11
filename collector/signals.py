"""Daily signal-feed pull: ALFRED econ vintages + GDELT news (B4/C1).

    python -m collector.signals [--db data/hyxlab.duckdb] [--gdelt-hours 25]

Pattern: fetch everything over the network FIRST (no store open), then
write in one brief flock+open_retry burst — a signals pull must never
hold the archive lock across network I/O (review H1 discipline).

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
import time
from datetime import UTC, datetime, timedelta

import duckdb
import requests

from collector.venues import alfred, gdelt
from hyxlab.models import EconVintage, NewsItem
from hyxlab.store import Store, open_retry

LOCK_FILE = "data/writer.lock"
GKG_PAUSE_S = 0.2


def fetch_alfred(session: requests.Session, today=None) -> dict[str, list[EconVintage]]:
    today = today or datetime.now(UTC).date()
    out: dict[str, list[EconVintage]] = {}
    for series in alfred.SERIES:
        # ALFRED throttles rapid sequential fetches into read-timeouts
        # (observed 2026-07-11: all series timed out at 0.5s pacing
        # while a lone probe succeeded) — retry once with a long pause.
        for attempt in range(2):
            try:
                out[series] = alfred.get_vintage(series, today, session=session)
                break
            except Exception as exc:
                print(
                    f"[signals] alfred {series}{' retry' if attempt else ''}: {type(exc).__name__}",
                    flush=True,
                )
                time.sleep(15)
        time.sleep(3)
    return out


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

    fetched = fetch_alfred(sess)
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
        store = open_retry(args.db)
        try:
            new_vintages = diff_vintages(store, fetched)
            nv = store.insert_vintages(new_vintages)
            nn = store.insert_news(items)
        finally:
            store.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
    print(
        f"[signals] {now:%Y-%m-%d %H:%M} vintages+{nv}"
        f" news+{nn} (of {len(items)} parsed; {missing} missing files)",
        flush=True,
    )


if __name__ == "__main__":
    main()
