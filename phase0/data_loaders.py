"""Shared data loaders for Phase 0 tests.

yfinance price pulls are cached to CSV so rerunning a test doesn't re-hit the
API. The cache file lives in phase0/data/ (gitignored — reproducible from source).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pandas as pd
import yfinance as yf

PHASE0_DIR = Path(__file__).resolve().parent
DATA_DIR = PHASE0_DIR / "data"


def load_adj_close(
    tickers: Sequence[str],
    start: str,
    end: str,
    cache_name: str = "prices.csv",
    refresh: bool = False,
) -> pd.DataFrame:
    """Return a DataFrame of Adj Close keyed by date, columns = tickers.

    Pulls from yfinance on first call and caches to phase0/data/{cache_name}.
    Pass refresh=True to force a fresh pull.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = DATA_DIR / cache_name

    if cache_path.exists() and not refresh:
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        # Fast-path only when the cache already covers every requested ticker.
        if set(tickers).issubset(df.columns):
            return df[list(tickers)].sort_index()

    raw = yf.download(
        list(tickers),
        start=start,
        end=end,
        auto_adjust=False,
        progress=False,
        threads=True,
        group_by="column",
    )
    # Extract Adj Close panel and flatten MultiIndex.
    adj = raw["Adj Close"] if "Adj Close" in raw.columns.get_level_values(0) else raw
    if isinstance(adj.columns, pd.MultiIndex):
        adj.columns = adj.columns.get_level_values(-1)
    adj = adj.sort_index()
    adj.to_csv(cache_path)
    return adj[list(tickers)]
