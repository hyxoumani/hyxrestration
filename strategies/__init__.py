"""Baseline strategies. Each is a hypothesis to falsify, not advice."""

from strategies.cross_venue import CrossVenueArb
from strategies.rebalance import IntramarketRebalance
from strategies.weather import WeatherNWS

__all__ = ["CrossVenueArb", "IntramarketRebalance", "WeatherNWS"]
