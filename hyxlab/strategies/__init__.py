"""Baseline strategies. Each is a hypothesis to falsify, not advice."""

from hyxlab.strategies.cross_venue import CrossVenueArb
from hyxlab.strategies.rebalance import IntramarketRebalance
from hyxlab.strategies.weather import WeatherNWS

__all__ = ["CrossVenueArb", "IntramarketRebalance", "WeatherNWS"]
