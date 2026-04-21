"""Alpaca data client wrapper.

Responsibilities: pull OHLCV bars and news, with 3x exponential backoff on
transient failures, crashing loudly on persistent failure per architecture.md
§3.4. Callers are responsible for writing to DuckDB — this module stays at the
API boundary.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import Bar
from alpaca.data.models.news import News
from alpaca.data.requests import NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

T = TypeVar("T")

# 1s / 2s / 4s then crash. Matches architecture.md §3.4.
_RETRY_DELAYS = (1.0, 2.0, 4.0)


def _with_retry(fn: Callable[[], T], *, what: str) -> T:
    """Run fn with 3x exponential backoff. Raises the final exception on exhaustion."""
    last_exc: Exception | None = None
    for attempt, delay in enumerate((0.0,) + _RETRY_DELAYS):
        if delay > 0:
            time.sleep(delay)
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            # No retry on bad-credential / 4xx — those won't fix themselves.
            msg = str(exc).lower()
            if any(
                s in msg
                for s in ("unauthorized", "forbidden", "invalid api key", "400 bad request")
            ):
                raise
            if attempt == len(_RETRY_DELAYS):
                break
    assert last_exc is not None
    raise RuntimeError(f"{what} failed after {len(_RETRY_DELAYS) + 1} attempts") from last_exc


@dataclass(frozen=True)
class OhlcvRow:
    ticker: str
    date: datetime  # UTC timestamp at bar start
    open: float
    high: float
    low: float
    close: float
    volume: int

    @classmethod
    def from_bar(cls, bar: Bar) -> OhlcvRow:
        return cls(
            ticker=bar.symbol,
            date=bar.timestamp,
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=int(bar.volume),
        )


@dataclass(frozen=True)
class NewsRow:
    news_id: str
    published_at: datetime
    headline: str
    summary: str | None
    url: str | None
    source: str | None
    symbols: tuple[str, ...]

    @classmethod
    def from_article(cls, n: News) -> NewsRow:
        return cls(
            news_id=str(n.id),
            published_at=n.created_at,
            headline=n.headline or "",
            summary=n.summary,
            url=n.url,
            source=n.source,
            symbols=tuple(n.symbols or ()),
        )


class AlpacaDataClient:
    """Thin wrapper around alpaca-py's historical data + news clients."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._bars = StockHistoricalDataClient(api_key, api_secret)
        self._news = NewsClient(api_key, api_secret)

    def fetch_ohlcv_daily(
        self,
        tickers: Sequence[str],
        start: datetime,
        end: datetime | None = None,
    ) -> list[OhlcvRow]:
        """Daily OHLCV for tickers over [start, end). Paginates transparently."""
        if not tickers:
            return []
        end = end or datetime.now(tz=UTC)
        req = StockBarsRequest(
            symbol_or_symbols=list(tickers),
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bar_set = _with_retry(lambda: self._bars.get_stock_bars(req), what="get_stock_bars")

        rows: list[OhlcvRow] = []
        # BarSet.data is Dict[str, List[Bar]]
        for _symbol, bars in bar_set.data.items():
            rows.extend(OhlcvRow.from_bar(b) for b in bars)
        return rows

    def fetch_news(
        self,
        tickers: Sequence[str],
        start: datetime,
        end: datetime | None = None,
        page_size: int = 50,
    ) -> Iterator[NewsRow]:
        """Yield news articles tagged for any of `tickers` in [start, end).

        Streams pages to keep memory flat. Callers can enumerate + checkpoint.
        """
        if not tickers:
            return
        end = end or datetime.now(tz=UTC)
        page_token: str | None = None

        while True:
            req = NewsRequest(
                symbols=list(tickers),
                start=start,
                end=end,
                limit=page_size,
                include_content=False,
                page_token=page_token,
            )
            # Bind req into the lambda's default args so ruff B023 doesn't flag
            # the loop-variable closure (and so the retry always sees the
            # current-iteration request even if we ever went async).
            news_set = _with_retry(lambda r=req: self._news.get_news(r), what="get_news")
            articles = news_set.data.get("news", [])
            for article in articles:
                yield NewsRow.from_article(article)
            page_token = news_set.next_page_token
            if not page_token:
                break
