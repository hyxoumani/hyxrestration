"""News fetch via Alpaca's Benzinga feed.

Free account, symbol-tagged, ~2021+ depth, unlimited query volume on the free
paper-trading tier. This module is explicitly *not* a broker client — A10
(decisions.md) decouples news-source from broker-commitment. Broker selection
is deferred to slice 8.

Articles are streamed via pagination to keep memory flat on backfills.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from alpaca.data.historical.news import NewsClient
from alpaca.data.models.news import News
from alpaca.data.requests import NewsRequest

from phase0.hyx.retry import with_retry


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


class AlpacaNewsClient:
    """Thin wrapper around alpaca-py's NewsClient, with retry + pagination."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._client = NewsClient(api_key, api_secret)

    def fetch(
        self,
        tickers: Sequence[str],
        start: datetime,
        end: datetime | None = None,
        page_size: int = 50,
    ) -> Iterator[NewsRow]:
        """Yield news articles tagged for any of `tickers` in [start, end).

        Streams pages; callers enumerate + checkpoint as they go.
        """
        if not tickers:
            return
        end = end or datetime.now(tz=UTC)
        page_token: str | None = None

        while True:
            req = NewsRequest(
                symbols=",".join(tickers),
                start=start,
                end=end,
                limit=page_size,
                include_content=False,
                page_token=page_token,
            )
            # Bind req into defaults so the retry closure captures the current
            # iteration's request (also silences ruff B023).
            news_set = with_retry(lambda r=req: self._client.get_news(r), what="alpaca.get_news")
            articles = news_set.data.get("news", [])
            for article in articles:
                yield NewsRow.from_article(article)
            page_token = news_set.next_page_token
            if not page_token:
                break
