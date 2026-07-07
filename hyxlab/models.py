"""Shared dataclasses for market data, forecasts, orders, and fills.

Prices are always in dollars per share/contract on [0, 1]. Sizes are in
shares/contracts (Kalshi reports fractional sizes post fractional-trading,
hence floats).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class MarketInfo:
    venue: str
    market_id: str
    title: str = ""
    series: str = ""  # e.g. "KXHIGHNY"; "" for venues without series
    close_time: datetime | None = None
    strike_type: str = ""  # "greater" | "less" | "between" | ""
    floor_strike: float | None = None
    cap_strike: float | None = None
    result: str = ""  # "" until settled, else "yes" | "no"
    target_date: date | None = None  # weather markets: local date measured


@dataclass(frozen=True)
class Snapshot:
    venue: str
    market_id: str
    ts: datetime
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    yes_bid_size: float = 0.0
    yes_ask_size: float = 0.0
    no_bid_size: float = 0.0
    no_ask_size: float = 0.0
    last_price: float | None = None
    volume: float = 0.0
    open_interest: float = 0.0

    def mid(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2.0
        return self.last_price


@dataclass(frozen=True)
class Forecast:
    station: str  # key from nws.STATIONS, e.g. "NYC"
    fetched_at: datetime
    target_date: date
    high_f: int
    short: str = ""


@dataclass(frozen=True)
class EconVintage:
    """One observation of an economic series as it existed at a vintage.

    knowable_at is the release moment of this vintage (pessimistically
    end-of-day US/Eastern when only a vintage *date* is known).
    """

    series_id: str  # e.g. "CPIAUCSL"
    obs_date: date  # the period the value describes
    value: float
    knowable_at: datetime


@dataclass(frozen=True)
class NewsItem:
    source: str  # "alpaca" | "gdelt"
    url_hash: str
    published_at: datetime | None
    knowable_at: datetime
    title: str = ""
    tone: float | None = None  # GDELT only
    topics: str = ""  # comma-separated query-template tags
    symbols: str = ""  # comma-separated tickers (alpaca)


@dataclass(frozen=True)
class Order:
    """action="open" buys `qty` of `side`; action="close" sells out of an
    existing position (capped at held qty — no shorting; buying the
    opposite side is the sanctioned equivalent). limit_price=None → taker
    at touch. tif="IOC" drops any unfilled remainder instead of resting.
    """

    venue: str
    market_id: str
    side: str  # "yes" | "no"
    qty: float
    limit_price: float | None = None
    action: str = "open"  # "open" | "close"
    tif: str = "GTC"  # "GTC" | "IOC"


@dataclass(frozen=True)
class Cancel:
    """Cancel a resting order by id (ids come from ctx.open_orders)."""

    order_id: int


@dataclass(frozen=True)
class Fill:
    strategy: str
    venue: str
    market_id: str
    side: str
    qty: float
    price: float
    fee: float  # dollars; negative = rebate
    ts: datetime
    maker: bool
