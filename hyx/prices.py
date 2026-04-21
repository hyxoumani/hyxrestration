"""OHLCV fetch via yfinance.

Free, account-free, 10+ years of daily bars on US-listed equities. Returns
both raw Close and Adj Close — architecture.md §3.6 and phase0_data_sources.md
§2.1 both call out adjusted-close as load-bearing for multi-year return math.

Caveats left to higher layers:
- yfinance returns NaN for pre-existence windows (e.g. CTVA pre-2019 spin).
  We drop NaN rows here and let the ingest loop continue.
- Index is a naive DatetimeIndex at session close. We normalize to a plain
  `datetime.date`; the schema stores DATE anyway.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as dt_date
from datetime import datetime

import yfinance as yf

from hyx.retry import with_retry


@dataclass(frozen=True)
class OhlcvRow:
    ticker: str
    date: dt_date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: int


def _to_rows(df, tickers: Sequence[str]) -> list[OhlcvRow]:
    """Convert a yfinance multi-ticker DataFrame to a flat list of OhlcvRow."""
    if df is None or df.empty:
        return []

    rows: list[OhlcvRow] = []
    for ticker in tickers:
        try:
            # yfinance always returns MultiIndex columns (field, ticker) even for one
            # ticker; .xs selects the sub-frame for this ticker with flat columns.
            sub = df.xs(ticker, axis=1, level=1)
        except KeyError:
            # Ticker not in response (e.g. delisted with no bars over window)
            continue
        sub = sub.dropna(how="any")
        for idx, r in sub.iterrows():
            # Index entries may be Timestamp, datetime, or date depending on version.
            if hasattr(idx, "date"):
                bar_date = idx.date()
            elif isinstance(idx, dt_date):
                bar_date = idx
            else:
                bar_date = datetime.fromisoformat(str(idx)).date()
            rows.append(
                OhlcvRow(
                    ticker=ticker,
                    date=bar_date,
                    open=float(r["Open"]),
                    high=float(r["High"]),
                    low=float(r["Low"]),
                    close=float(r["Close"]),
                    adj_close=float(r["Adj Close"]),
                    volume=int(r["Volume"]),
                )
            )
    return rows


def fetch_daily_bars(
    tickers: Sequence[str],
    start: dt_date,
    end: dt_date | None = None,
) -> list[OhlcvRow]:
    """Daily OHLCV for `tickers` over [start, end). end defaults to today."""
    if not tickers:
        return []
    end = end or dt_date.today()
    # Zero-range or inverted window: yfinance returns empty and prints a
    # misleading "possibly delisted" warning. Short-circuit instead.
    if start >= end:
        return []

    def _download():
        return yf.download(
            list(tickers),
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=False,  # keep raw Close + separate Adj Close
            progress=False,
            threads=True,
            group_by="column",  # (field, ticker) MultiIndex
        )

    df = with_retry(_download, what="yfinance.download")
    return _to_rows(df, tickers)
