"""ALFRED point-in-time economic data (St. Louis Fed archival FRED).

Verified 2026-07-06: the keyless CSV endpoint
`alfred.stlouisfed.org/graph/alfredgraph.csv?id=<SERIES>&vintage_date=<D>`
returns the series exactly as it existed on that date (checked: the
2024-01-15 CPIAUCSL vintage ends at 2023-12 — December CPI, released
2024-01-11). One request per (series, vintage date); fine for monthly/
weekly series. A free FRED API key (env `FRED_API_KEY`) unlocks bulk
realtime-range queries later; the keyless path removes the dependency now.

knowable_at: vintages are date-granular here, so we stamp the pessimistic
23:59 US/Eastern (≈ 03:59+1d UTC) — a strategy can never see a vintage
earlier than a live trader could have. Exact release datetimes (08:30 ET
prints) can tighten this later via the FRED release-calendar endpoint.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

import requests

from hyxlab.models import EconVintage

BASE = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"

# Kalshi-relevant starting set (proposal §C1).
SERIES = ["CPIAUCSL", "CPILFESL", "ICSA", "PAYEMS", "UNRATE"]


def pessimistic_knowable_at(vintage_date: date) -> datetime:
    """23:59 US/Eastern on the vintage date, as naive UTC (+4h ≈ EDT).

    Deliberately late: never lets a backtest see data before a live
    trader could have. Uses fixed UTC−4; the ≤1h DST slack is dwarfed by
    the end-of-day pessimism margin anyway.
    """
    return datetime(vintage_date.year, vintage_date.month, vintage_date.day, 23, 59) + timedelta(
        hours=4
    )


def parse_vintage_csv(text: str, series_id: str, vintage_date: date) -> list[EconVintage]:
    """Parse an alfredgraph CSV. Header: observation_date,<SERIES>_<YYYYMMDD>."""
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    expected_prefix = f"{series_id}_"
    if len(header) != 2 or not header[1].startswith(expected_prefix):
        raise ValueError(f"unexpected ALFRED header {header!r} for {series_id}")
    knowable = pessimistic_knowable_at(vintage_date)
    out: list[EconVintage] = []
    for row in reader:
        if len(row) != 2 or row[1] in ("", "."):  # '.' = missing in FRED CSVs
            continue
        out.append(
            EconVintage(
                series_id=series_id,
                obs_date=date.fromisoformat(row[0]),
                value=float(row[1]),
                knowable_at=knowable,
            )
        )
    return out


def get_vintage(
    series_id: str, vintage_date: date, session: requests.Session | None = None
) -> list[EconVintage]:
    sess = session or requests.Session()
    resp = sess.get(
        BASE,
        params={"id": series_id, "vintage_date": vintage_date.isoformat()},
        timeout=60,
    )
    resp.raise_for_status()
    return parse_vintage_csv(resp.text, series_id, vintage_date)
