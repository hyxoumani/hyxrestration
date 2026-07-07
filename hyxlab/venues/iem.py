"""Iowa Environmental Mesonet (IEM) archives — the historical half of the lab.

Two archives, both free JSON/CSV over HTTP (mesonet.agron.iastate.edu):

- CLI reports (`/json/cli.py?station=KNYC&year=YYYY`): the *exact* NWS
  Climatological Report product Kalshi weather markets settle on, back
  decades. Observed daily highs → `observations` table (settlement truth
  and forecast-error calibration).

- MOS archive (`/cgi-bin/request/mos.py` bulk CSV): archived model
  forecasts as-issued, per model runtime. We use GFS extended MOS ("MEX",
  00Z/12Z runs). Convention (validated against CLI highs via the MAE
  diagnostic in run_backtest): the `n_x` value at an ftime of 00Z is the
  daytime MAX for the previous local day, so target_date =
  (ftime − 6h).date(). Rows with 12Z ftimes are overnight MINs — skipped.

Forecasts load into the same `nws_forecasts` table the live collector
writes, with fetched_at = model runtime, so the simulator's no-lookahead
as-of logic works identically for backtests and forward replay.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

import requests

from hyxlab.models import Forecast

BASE = "https://mesonet.agron.iastate.edu"

# Our station key → ICAO of the NWS climate site each Kalshi series settles on.
STATION_ICAO: dict[str, str] = {
    "NYC": "KNYC",  # Central Park
    "CHI": "KMDW",  # Midway
    "MIA": "KMIA",  # Miami Intl
    "AUS": "KATT",  # Camp Mabry
    "DEN": "KDEN",  # Denver Intl
}


def get_observed_highs(
    station: str, year: int, session: requests.Session | None = None
) -> list[tuple[str, date, int | None]]:
    """(station_key, date, observed_high) rows from archived CLI reports."""
    sess = session or requests.Session()
    resp = sess.get(
        f"{BASE}/json/cli.py",
        params={"station": STATION_ICAO[station], "year": year},
        timeout=60,
    )
    resp.raise_for_status()
    out: list[tuple[str, date, int | None]] = []
    for row in resp.json().get("results", []):
        high = row.get("high")
        out.append(
            (
                station,
                date.fromisoformat(row["valid"]),
                int(high) if isinstance(high, (int, float)) else None,
            )
        )
    return out


def get_mos_forecasts(
    station: str,
    start: date,
    end: date,
    model: str = "MEX",
    session: requests.Session | None = None,
) -> list[Forecast]:
    """Archived as-issued forecast highs for [start, end) model runtimes."""
    sess = session or requests.Session()
    resp = sess.get(
        f"{BASE}/cgi-bin/request/mos.py",
        params={
            "station": STATION_ICAO[station],
            "model": model,
            "sts": f"{start.isoformat()}T00:00Z",
            "ets": f"{end.isoformat()}T00:00Z",
            "format": "csv",
        },
        timeout=120,
    )
    resp.raise_for_status()
    out: list[Forecast] = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        n_x = row.get("n_x")
        if not n_x:
            continue
        ftime = datetime.fromisoformat(row["ftime"]).replace(tzinfo=timezone.utc)
        if ftime.hour != 0:
            continue  # 12Z ftimes carry overnight minimums
        runtime = datetime.fromisoformat(row["runtime"]).replace(tzinfo=timezone.utc)
        out.append(
            Forecast(
                station=station,
                fetched_at=runtime,
                target_date=(ftime - timedelta(hours=6)).date(),
                high_f=int(float(n_x)),
                short=f"{model} archived",
            )
        )
    return out
