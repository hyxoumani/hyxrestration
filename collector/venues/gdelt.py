"""GDELT bulk GKG ingestion — filter-and-discard, never archive raw.

High-volume path per proposal §C1: 15-minute GKG 2.1 files from
`data.gdeltproject.org/gdeltv2/` (no rate limit, ~2.5 MB zipped each).
We grep theme codes against the per-category templates in
`collector/queries/gdelt.json` (prefix-matched), keep matches as
NewsItem rows, and drop the rest (~480 MB/day raw otherwise).

knowable_at = the file's batch timestamp (V2.1DATE, UTC) — the moment
GDELT's monitors saw the article; ±15 min honesty, so daily-horizon
strategies only (enforced downstream by capability checks, not here).

Format probed live 2026-07-11 (20260711191500.gkg.csv): 27 tab fields;
[1] batch ts yyyymmddhhmmss, [4] document URL, [7] V1THEMES ;-list,
[15] V1.5TONE comma-list (first = avg tone), [26] EXTRASXML with
optional <PAGE_TITLE>. The DOC 2.0 API is NOT used here (hard 5s/IP
limit, throttling signalled via plain-text bodies; artlist carries no
tone) — reserve it for narrow ad-hoc queries on a slow lane.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from contextlib import suppress
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

import requests

from hyxlab.models import NewsItem

BULK = "http://data.gdeltproject.org/gdeltv2"
TEMPLATES_PATH = Path(__file__).parent.parent / "queries" / "gdelt.json"
_TITLE_RE = re.compile(r"<PAGE_TITLE>([^<]*)</PAGE_TITLE>")


def load_templates(path: str | Path = TEMPLATES_PATH) -> dict[str, list[str]]:
    data = json.loads(Path(path).read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def url_hash(url: str) -> str:
    return sha256(url.strip().encode()).hexdigest()[:16]


def gkg_urls(start: datetime, end: datetime) -> list[str]:
    """15-minute grid of GKG file URLs covering [start, end) (UTC)."""
    t = start.replace(minute=start.minute - start.minute % 15, second=0, microsecond=0)
    out = []
    while t < end:
        out.append(f"{BULK}/{t:%Y%m%d%H%M%S}.gkg.csv.zip")
        t += timedelta(minutes=15)
    return out


def fetch_gkg(url: str, session: requests.Session | None = None) -> str | None:
    """Download + unzip one GKG file; None for a missing quarter-hour
    (404s happen — GDELT occasionally skips a batch)."""
    sess = session or requests.Session()
    resp = sess.get(url, timeout=120)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = zf.namelist()[0]
        return zf.read(name).decode("utf-8", errors="replace")


def parse_gkg(text: str, templates: dict[str, list[str]]) -> list[NewsItem]:
    """Filter-and-discard: keep only rows whose V1THEMES prefix-match a
    template; tag with every matching template key."""
    out: list[NewsItem] = []
    seen: set[str] = set()
    for line in text.split("\n"):
        f = line.split("\t")
        if len(f) < 16:
            continue
        themes = f[7]
        if not themes:
            continue
        theme_list = themes.split(";")
        topics = [
            tag
            for tag, codes in templates.items()
            if any(t.startswith(c) for c in codes for t in theme_list)
        ]
        if not topics:
            continue
        url = f[4].strip()
        if not url.startswith("http"):
            continue
        h = url_hash(url)
        if h in seen:  # same URL can appear twice within one batch
            continue
        seen.add(h)
        try:
            knowable = datetime.strptime(f[1], "%Y%m%d%H%M%S")
        except ValueError:
            continue
        tone = None
        if f[15]:
            with suppress(ValueError):
                tone = float(f[15].split(",")[0])
        title = ""
        if len(f) >= 27:
            m = _TITLE_RE.search(f[26])
            if m:
                title = m.group(1)[:300]
        out.append(
            NewsItem(
                source="gdelt",
                url_hash=h,
                published_at=None,
                knowable_at=knowable,
                title=title,
                tone=tone,
                topics=",".join(sorted(topics)),
            )
        )
    return out
