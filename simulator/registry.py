"""Shared strategy registry: one name → Strategy-class map for every
runner that composes strategies by name (run_l2, shadow, divergence).

Extracted from simulator.divergence (which previously held a private
{"probe": ...} map) so a strategy registered once is runnable everywhere
— including simulator/run_l2.py's one-command L2 backtest.

Only default-constructible strategies belong here: registry entries are
instantiated with no arguments (`build(names)`), exactly as divergence
always did. Strategies needing mandatory arguments (e.g. CrossVenueArb's
pair list) are wired by their dedicated runners instead.
"""

from __future__ import annotations

from simulator.strategy import Strategy
from strategies.fav_long import FavoriteLongshot
from strategies.hylshi_fade import HylshiFade
from strategies.probe import TightSpreadProbe
from strategies.weather import WeatherNWS

STRATEGIES: dict[str, type[Strategy]] = {
    "probe": TightSpreadProbe,
    "hylshi_fade": HylshiFade,
    "fav_long": FavoriteLongshot,
    "weather_nws": WeatherNWS,
}


def build(names: list[str]) -> list[Strategy]:
    """Instantiate strategies (defaults) for a list of registry names."""
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        raise KeyError(f"unknown strategy {unknown}; registered: {sorted(STRATEGIES)}")
    return [STRATEGIES[n]() for n in names]
