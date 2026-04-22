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

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from phase0.data_loaders import DATA_DIR

NEWS_CSV = DATA_DIR / "alpaca_news.csv"
NEWS_ENDPOINT = "https://data.alpaca.markets/v1beta1/news"

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


def _auth_headers() -> dict[str, str]:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    key = os.environ.get("ALPACA_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError(
            "ALPACA_KEY / ALPACA_SECRET not set. Copy .env.example to .env "
            "and populate with free paper-trading credentials from alpaca.markets."
        )
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _fetch_page(params: dict[str, str], headers: dict[str, str]) -> dict:
    """One REST call with one retry on transient 5xx. Returns parsed JSON."""
    url = NEWS_ENDPOINT + "?" + urllib.parse.urlencode(params)
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600 and attempt == 1:
                time.sleep(1.0)
                continue
            raise RuntimeError(f"alpaca news HTTP {e.code}: {e.read().decode()[:200]}") from e
    raise AssertionError("unreachable")


def fetch_news(
    tickers: tuple[str, ...] = PHASE0_UNIVERSE,
    start: datetime | None = None,
    end: datetime | None = None,
    page_size: int = 50,
) -> pd.DataFrame:
    """Pull Alpaca news in [start, end), return a DataFrame shaped per §3.4.

    One row per (news_id, tagged_ticker). Articles with N symbols become N rows.

    Hits the REST endpoint directly rather than going through alpaca-py —
    the SDK's pagination broke at 50 articles on our version (issue hit
    during iter 3 first real run), and the REST shape is stable.
    """
    start = start or datetime(2021, 1, 1, tzinfo=UTC)
    end = end or datetime.now(tz=UTC)
    headers = _auth_headers()

    base_params = {
        "symbols": ",".join(tickers),
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": str(page_size),
    }

    rows: list[dict[str, object]] = []
    page_token: str | None = None
    pages = 0
    while True:
        params = dict(base_params)
        if page_token:
            params["page_token"] = page_token
        resp = _fetch_page(params, headers)
        articles = resp.get("news", [])
        for art in articles:
            for sym in art.get("symbols") or ():
                if sym not in tickers:
                    continue
                rows.append(
                    {
                        "news_id": str(art.get("id")),
                        "ticker": sym,
                        "timestamp": art.get("created_at"),
                        "headline": art.get("headline") or "",
                        "summary": art.get("summary") or "",
                        "source": art.get("source") or "",
                    }
                )
        pages += 1
        page_token = resp.get("next_page_token")
        if not page_token:
            break
        if pages > 5000:
            raise RuntimeError(f"runaway pagination at {pages} pages")

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
