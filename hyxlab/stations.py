"""Kalshi weather series ↔ NWS climate station mapping (shared kernel).

Static reference data used by BOTH sides of the import boundary: the NWS
collector resolves gridpoints from it, and the weather strategy maps a
market's series to its station. Per the series rules: NYC=Central Park,
CHI=Midway, MIA=Miami Intl, AUS=Camp Mabry, DEN=Denver Intl.
"""

from __future__ import annotations

STATIONS: dict[str, dict] = {
    "NYC": {"lat": 40.783, "lon": -73.967, "series": "KXHIGHNY"},
    "CHI": {"lat": 41.786, "lon": -87.752, "series": "KXHIGHCHI"},
    "MIA": {"lat": 25.788, "lon": -80.317, "series": "KXHIGHMIA"},
    "AUS": {"lat": 30.321, "lon": -97.760, "series": "KXHIGHAUS"},
    "DEN": {"lat": 39.847, "lon": -104.656, "series": "KXHIGHDEN"},
}

SERIES_TO_STATION: dict[str, str] = {v["series"]: k for k, v in STATIONS.items()}
