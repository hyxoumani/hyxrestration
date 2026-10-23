"""`collector.venues.nws.get_daily_highs` against the shapes api.weather.gov
actually serves.

Regression (2026-10-22 18:45Z, mistakes #133): NWS published AUS's running
daytime period ("This Afternoon") with `temperature: null`. The parser did
`int(period["temperature"])` and the TypeError escaped the whole pull, so the
collector's per-station except dropped all seven AUS days on every tick for
1.5h+ (`forecasts` 35 -> 28) while the six well-formed days were sitting in
the same response. One bad period must cost one period."""

from __future__ import annotations

from datetime import date

import pytest

from collector.venues import nws


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Session:
    """Serves one forecast payload; `headers` so `_session` can set the UA."""

    def __init__(self, payload):
        self.headers = {}
        self._payload = payload

    def get(self, url, timeout=30):
        return _Resp(self._payload)


def _period(n, day, daytime, temp, name):
    return {
        "number": n,
        "name": name,
        "startTime": f"2026-10-{day:02d}T{'06' if daytime else '18'}:00:00-05:00",
        "isDaytime": daytime,
        "temperature": temp,
        "temperatureUnit": "F",
        "shortForecast": "Sunny",
    }


# The real 2026-10-22 20:16Z AUS response, abridged to three days: the
# running afternoon null, then two well-formed day/night pairs.
_AUS_WITH_NULL = {
    "properties": {
        "periods": [
            _period(1, 22, True, None, "This Afternoon"),
            _period(2, 22, False, 60, "Tonight"),
            _period(3, 23, True, 86, "Friday"),
            _period(4, 23, False, 61, "Friday Night"),
            _period(5, 24, True, 87, "Saturday"),
            _period(6, 24, False, 62, "Saturday Night"),
        ]
    }
}


@pytest.fixture(autouse=True)
def _gridpoint(monkeypatch):
    monkeypatch.setitem(nws._gridpoint_cache, "30.321,-97.760", "https://example/forecast")
    monkeypatch.setattr(
        nws, "forecast_url", lambda lat, lon, session=None: "https://example/forecast"
    )


def test_null_temperature_period_costs_only_that_period(capsys):
    out = nws.get_daily_highs("AUS", session=_Session(_AUS_WITH_NULL))
    assert [(f.target_date, f.high_f) for f in out] == [
        (date(2026, 10, 23), 86),
        (date(2026, 10, 24), 87),
    ]
    assert all(f.station == "AUS" for f in out)
    line = capsys.readouterr().out
    # The absence has to be legible in the journal: station, period, date.
    assert "[nws] AUS: This Afternoon 2026-10-22 temperature null, skipped" in line


def test_well_formed_response_is_unchanged(capsys):
    payload = {"properties": {"periods": _AUS_WITH_NULL["properties"]["periods"][2:]}}
    out = nws.get_daily_highs("AUS", session=_Session(payload))
    assert [(f.target_date, f.high_f) for f in out] == [
        (date(2026, 10, 23), 86),
        (date(2026, 10, 24), 87),
    ]
    assert capsys.readouterr().out == ""


def test_night_periods_are_never_highs():
    payload = {"properties": {"periods": [_period(2, 22, False, 60, "Tonight")]}}
    assert nws.get_daily_highs("AUS", session=_Session(payload)) == []
