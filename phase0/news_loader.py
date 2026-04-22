"""Alpaca Benzinga news loader for Phase 0.

Parallel to hyx/news.py (separation rule §0 forbids cross-imports). Pulls
ag-tagged articles, paginates, caches to phase0/data/alpaca_news.csv.

Schema per phase0_data_sources.md §3.4 — one row per (news_id, tagged_ticker)
so a single article tagging NTR+MOS+CF becomes three rows. This is the
"count once per tagged ticker" aggregation pattern; the alternative
(count-once-total with weight distribution) is documented but not chosen.

Requires Alpaca credentials (ALPACA_KEY / ALPACA_SECRET in .env). The
load_news() function short-circuits to the cached CSV if it exists, so
downstream phase 0 scripts don't re-hit the API.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from phase0.data_loaders import DATA_DIR

NEWS_CSV = DATA_DIR / "alpaca_news.csv"

# Phase 0 universe per §2.3 — 10 ag equities, ETFs excluded.
PHASE0_UNIVERSE: tuple[str, ...] = (
    "NTR",
    "MOS",
    "CF",  # fertilizer
    "DE",
    "AGCO",
    "CNH",  # equipment
    "ADM",
    "BG",  # processors
    "CTVA",
    "FMC",  # others (tracked but not in regression)
)


def _client():
    from alpaca.data.historical.news import NewsClient

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    key = os.environ.get("ALPACA_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError(
            "ALPACA_KEY / ALPACA_SECRET not set. Copy .env.example to .env "
            "and populate with free paper-trading credentials from alpaca.markets."
        )
    return NewsClient(key, secret)


def fetch_news(
    tickers: tuple[str, ...] = PHASE0_UNIVERSE,
    start: datetime | None = None,
    end: datetime | None = None,
    page_size: int = 50,
) -> pd.DataFrame:
    """Pull Alpaca news in [start, end), return a DataFrame shaped per §3.4.

    One row per (news_id, tagged_ticker). Articles with N symbols become N rows.
    """
    from alpaca.data.requests import NewsRequest

    start = start or datetime(2021, 1, 1, tzinfo=UTC)
    end = end or datetime.now(tz=UTC)

    client = _client()
    rows: list[dict[str, object]] = []
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
        news_set = client.get_news(req)
        for article in news_set.data.get("news", []):
            for sym in article.symbols or ():
                if sym not in tickers:
                    continue
                rows.append(
                    {
                        "news_id": str(article.id),
                        "ticker": sym,
                        "timestamp": article.created_at,
                        "headline": article.headline or "",
                        "summary": article.summary or "",
                        "source": article.source or "",
                    }
                )
        page_token = news_set.next_page_token
        if not page_token:
            break

    return pd.DataFrame(rows)


def load_news(
    tickers: tuple[str, ...] = PHASE0_UNIVERSE,
    refresh: bool = False,
) -> pd.DataFrame:
    """Return cached news DataFrame, fetching once if cache is absent or stale."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if NEWS_CSV.exists() and not refresh:
        df = pd.read_csv(NEWS_CSV, parse_dates=["timestamp"])
        if set(tickers).issubset(set(df["ticker"].unique()) | set(tickers)):
            return df
    df = fetch_news(tickers)
    df.to_csv(NEWS_CSV, index=False)
    return df


if __name__ == "__main__":
    df = load_news()
    print(
        f"alpaca_news.csv: {len(df)} rows, "
        f"{df['news_id'].nunique()} unique articles, "
        f"{df['ticker'].nunique()} tickers, "
        f"range {df['timestamp'].min()} to {df['timestamp'].max()}"
    )
