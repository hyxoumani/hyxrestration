"""Kalshi public market-data client (no auth required for reads).

The /markets endpoint already carries top-of-book (yes/no bid/ask in
dollars plus displayed sizes), so one paginated call per series yields
snapshots for every strike bracket — no per-market orderbook calls needed
at this fidelity. Public rate limit is ~30 req/s; the collector polls at
minutes-scale, far below it.

Weather series (KXHIGHNY, KXHIGHCHI, ...) settle on the NWS Climatological
Report (Daily) — objective, no oracle risk. Event tickers encode the
measured local date as e.g. "KXHIGHNY-26JUL07".
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
VENUE = "kalshi"

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

from hyxlab.models import MarketInfo, Snapshot  # noqa: E402


def get_markets(
    series_ticker: str | None = None,
    status: str = "open",
    limit: int = 200,
    max_pages: int = 10,
    session: requests.Session | None = None,
    **extra_params: Any,
) -> list[dict[str, Any]]:
    sess = session or requests.Session()
    out: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": limit, "status": status, **extra_params}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        resp = sess.get(f"{BASE}/markets", params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        out.extend(body.get("markets", []))
        cursor = body.get("cursor") or ""
        if not cursor or not body.get("markets"):
            break
    return out


def get_trades(
    ticker: str,
    limit: int = 1000,
    max_pages: int = 100,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """All public trade prints for one market (cursor-paginated).

    Probed 2026-07-07: same string-dollar shape as the WS trade channel
    (trade_id, created_time ISO, yes_price_dollars, count_fp, taker_side,
    is_block_trade). Retention purges prints ~64 days after close —
    markets closed ≤2026-05-01 already return empty.
    """
    sess = session or requests.Session()
    out: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(max_pages):
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        resp = sess.get(f"{BASE}/markets/trades", params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        out.extend(body.get("trades", []))
        cursor = body.get("cursor") or ""
        if not cursor or not body.get("trades"):
            break
    return out


def trade_row(t: dict[str, Any]) -> tuple:
    """Flatten one API trade into the store's trades-table column order."""
    return (
        VENUE,
        t["ticker"],
        t["trade_id"],
        _parse_ts(t.get("created_time")),
        float(t["yes_price_dollars"]),
        float(t.get("count_fp") or 0.0),
        t.get("taker_side") or None,
        bool(t.get("is_block_trade", False)),
    )


def get_series_list(session: requests.Session | None = None) -> list[dict[str, Any]]:
    """All series with category/fee metadata. Verified 2026-07-06: the
    endpoint returns the full set (~11k) in one unpaginated response."""
    sess = session or requests.Session()
    resp = sess.get(f"{BASE}/series", timeout=60)
    resp.raise_for_status()
    return resp.json().get("series", [])


def get_candlesticks(
    series_ticker: str,
    market_ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int = 60,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Historical candles (price + yes_bid/yes_ask OHLC) for one market.

    period_interval is in minutes (1, 60, or 1440). Available for settled
    markets — this is what makes Tier-1 historical backtesting possible.
    """
    sess = session or requests.Session()
    resp = sess.get(
        f"{BASE}/series/{series_ticker}/markets/{market_ticker}/candlesticks",
        params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("candlesticks", [])


def candle_row(series: str, m: dict[str, Any], c: dict[str, Any], period_s: int) -> tuple:
    """Flatten one API candle into the store's candles-table column order."""

    def d(block: dict[str, Any] | None, key: str) -> float | None:
        if not block:
            return None
        v = block.get(key)
        return None if v in (None, "") else float(v)

    price, bid, ask = c.get("price"), c.get("yes_bid"), c.get("yes_ask")
    return (
        VENUE,
        m["ticker"],
        datetime.fromtimestamp(c["end_period_ts"], tz=UTC),
        period_s,
        d(price, "open_dollars"),
        d(price, "high_dollars"),
        d(price, "low_dollars"),
        d(price, "close_dollars"),
        d(bid, "close_dollars"),
        d(ask, "close_dollars"),
        d(bid, "high_dollars"),
        d(ask, "low_dollars"),
        float(c.get("volume_fp") or 0.0),
        float(c.get("open_interest_fp") or 0.0),
    )


def parse_event_date(event_ticker: str) -> date | None:
    """'KXHIGHNY-26JUL07' → date(2026, 7, 7); None if no date suffix."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    tail = parts[1]
    if len(tail) < 7:
        return None
    month = _MONTHS.get(tail[2:5].upper())
    if month is None:
        return None
    try:
        return date(2000 + int(tail[:2]), month, int(tail[5:7]))
    except ValueError:
        return None


def _dollars(m: dict[str, Any], key: str) -> float | None:
    v = m.get(key)
    if v in (None, ""):
        return None
    return float(v)


def _fp(m: dict[str, Any], key: str) -> float:
    v = m.get(key)
    if v in (None, ""):
        return 0.0
    return float(v)


def _parse_ts(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def to_market_info(m: dict[str, Any]) -> MarketInfo:
    event = m.get("event_ticker", "")
    return MarketInfo(
        venue=VENUE,
        market_id=m["ticker"],
        title=m.get("title", ""),
        series=event.split("-")[0] if event else "",
        close_time=_parse_ts(m.get("close_time")),
        strike_type=m.get("strike_type", "") or "",
        floor_strike=m.get("floor_strike"),
        cap_strike=m.get("cap_strike"),
        result=m.get("result", "") or "",
        target_date=parse_event_date(event) if event else None,
    )


def to_snapshot(m: dict[str, Any], ts: datetime | None = None) -> Snapshot:
    # Kalshi's NO book is the mirror of the YES book and the API only
    # reports YES-side sizes: buying NO at the no_ask consumes the yes_bid,
    # so the NO ask size IS the yes_bid size (and vice versa).
    yes_bid_size = _fp(m, "yes_bid_size_fp")
    yes_ask_size = _fp(m, "yes_ask_size_fp")
    return Snapshot(
        venue=VENUE,
        market_id=m["ticker"],
        ts=ts or datetime.now(UTC),
        yes_bid=_dollars(m, "yes_bid_dollars"),
        yes_ask=_dollars(m, "yes_ask_dollars"),
        no_bid=_dollars(m, "no_bid_dollars"),
        no_ask=_dollars(m, "no_ask_dollars"),
        yes_bid_size=yes_bid_size,
        yes_ask_size=yes_ask_size,
        no_bid_size=_fp(m, "no_bid_size_fp") or yes_ask_size,
        no_ask_size=_fp(m, "no_ask_size_fp") or yes_bid_size,
        last_price=_dollars(m, "last_price_dollars"),
        volume=_fp(m, "volume_fp"),
        open_interest=_fp(m, "open_interest_fp"),
    )
