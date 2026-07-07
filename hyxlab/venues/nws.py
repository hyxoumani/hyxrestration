"""NWS forecast client (api.weather.gov — free, no key, needs a User-Agent).

STATIONS maps Kalshi weather series to the NWS climate station each one
settles against (per the series rules: NYC=Central Park, CHI=Midway,
MIA=Miami Intl, AUS=Camp Mabry, DEN=Denver Intl). Gridpoints are resolved
from lat/lon once per process via /points and cached.

Forecast highs come from the daytime periods of the gridpoint forecast.
The collector stores every pull with fetched_at so the simulator can
serve forecasts as-of a snapshot's timestamp (no lookahead).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import requests

from hyxlab.models import Forecast

BASE = "https://api.weather.gov"
USER_AGENT = "hyxlab-research (educationemail123452@gmail.com)"

STATIONS: dict[str, dict] = {
    "NYC": {"lat": 40.783, "lon": -73.967, "series": "KXHIGHNY"},
    "CHI": {"lat": 41.786, "lon": -87.752, "series": "KXHIGHCHI"},
    "MIA": {"lat": 25.788, "lon": -80.317, "series": "KXHIGHMIA"},
    "AUS": {"lat": 30.321, "lon": -97.760, "series": "KXHIGHAUS"},
    "DEN": {"lat": 39.847, "lon": -104.656, "series": "KXHIGHDEN"},
}

SERIES_TO_STATION: dict[str, str] = {v["series"]: k for k, v in STATIONS.items()}

_gridpoint_cache: dict[str, str] = {}


def _session(session: requests.Session | None) -> requests.Session:
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    return sess


def forecast_url(lat: float, lon: float, session: requests.Session | None = None) -> str:
    key = f"{lat:.3f},{lon:.3f}"
    if key not in _gridpoint_cache:
        sess = _session(session)
        resp = sess.get(f"{BASE}/points/{key}", timeout=30)
        resp.raise_for_status()
        _gridpoint_cache[key] = resp.json()["properties"]["forecast"]
    return _gridpoint_cache[key]


def get_daily_highs(station: str, session: requests.Session | None = None) -> list[Forecast]:
    """Forecast daytime highs for the next ~7 days at a STATIONS key."""
    cfg = STATIONS[station]
    sess = _session(session)
    url = forecast_url(cfg["lat"], cfg["lon"], session=sess)
    resp = sess.get(url, timeout=30)
    resp.raise_for_status()
    now = datetime.now(UTC)
    out: list[Forecast] = []
    for period in resp.json()["properties"]["periods"]:
        if not period.get("isDaytime"):
            continue
        target = date.fromisoformat(period["startTime"][:10])
        out.append(
            Forecast(
                station=station,
                fetched_at=now,
                target_date=target,
                high_f=int(period["temperature"]),
                short=period.get("shortForecast", ""),
            )
        )
    return out
