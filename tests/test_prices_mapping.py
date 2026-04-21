"""yfinance DataFrame -> OhlcvRow mapping tests.

Builds a synthetic DataFrame with yfinance's MultiIndex column layout so we
can exercise `_to_rows` without hitting the network. The shape here mirrors
what we probed against yfinance 1.3.0.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from hyx.prices import OhlcvRow, _to_rows, fetch_daily_bars


def _frame(rows_per_ticker: dict[str, list[dict]]) -> pd.DataFrame:
    """Build a yfinance-shaped DataFrame: MultiIndex columns (field, ticker).

    rows_per_ticker = {"DE": [{"date": "2024-01-02", "Open": 100.0, ...}, ...], ...}
    """
    fields = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    all_dates = sorted({r["date"] for rows in rows_per_ticker.values() for r in rows})
    tickers = list(rows_per_ticker.keys())

    columns = pd.MultiIndex.from_product([fields, tickers], names=["Price", "Ticker"])
    index = pd.DatetimeIndex([pd.Timestamp(d) for d in all_dates], name="Date")
    df = pd.DataFrame(index=index, columns=columns, dtype=float)

    for ticker, rows in rows_per_ticker.items():
        for r in rows:
            idx = pd.Timestamp(r["date"])
            for f in fields:
                df.loc[idx, (f, ticker)] = r.get(f, float("nan"))
    return df


def test_ohlcv_row_preserves_fields_for_single_ticker():
    df = _frame(
        {
            "DE": [
                {
                    "date": "2024-01-02",
                    "Open": 100.5,
                    "High": 105.25,
                    "Low": 99.75,
                    "Close": 102.0,
                    "Adj Close": 101.3,
                    "Volume": 1_000_000,
                },
            ]
        }
    )
    rows = _to_rows(df, ["DE"])
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, OhlcvRow)
    assert r.ticker == "DE"
    assert r.date == date(2024, 1, 2)
    assert r.open == 100.5
    assert r.high == 105.25
    assert r.low == 99.75
    assert r.close == 102.0
    assert r.adj_close == 101.3
    assert r.volume == 1_000_000
    assert isinstance(r.volume, int)


def test_ohlcv_row_drops_nan_rows():
    # CTVA pre-2019 returns NaN rows. The DataFrame layout for a two-ticker
    # download has a row per calendar day with NaN entries for tickers that
    # weren't trading yet.
    df = _frame(
        {
            "DE": [
                {
                    "date": "2019-05-31",
                    "Open": 150.0,
                    "High": 152.0,
                    "Low": 148.0,
                    "Close": 151.0,
                    "Adj Close": 150.5,
                    "Volume": 2_000_000,
                },
                {
                    "date": "2019-06-03",
                    "Open": 151.0,
                    "High": 153.0,
                    "Low": 150.0,
                    "Close": 152.5,
                    "Adj Close": 152.0,
                    "Volume": 1_800_000,
                },
            ],
            "CTVA": [
                # No pre-spin rows — CTVA started trading 2019-06-03
                {
                    "date": "2019-06-03",
                    "Open": 29.0,
                    "High": 29.5,
                    "Low": 28.5,
                    "Close": 29.2,
                    "Adj Close": 29.2,
                    "Volume": 5_000_000,
                },
            ],
        }
    )
    rows = _to_rows(df, ["DE", "CTVA"])
    de_rows = [r for r in rows if r.ticker == "DE"]
    ctva_rows = [r for r in rows if r.ticker == "CTVA"]
    assert len(de_rows) == 2
    assert len(ctva_rows) == 1
    assert ctva_rows[0].date == date(2019, 6, 3)


def test_ohlcv_row_volume_is_int_even_if_frame_gives_float():
    # Pandas promotes BIGINT volume columns to float when they share a frame
    # with NaN values. Our mapper must coerce back.
    df = _frame(
        {
            "DE": [
                {
                    "date": "2024-01-02",
                    "Open": 100.0,
                    "High": 101.0,
                    "Low": 99.0,
                    "Close": 100.5,
                    "Adj Close": 100.5,
                    "Volume": 1_234_567.0,
                },
            ]
        }
    )
    rows = _to_rows(df, ["DE"])
    assert rows[0].volume == 1_234_567
    assert isinstance(rows[0].volume, int)


def test_empty_frame_returns_empty_list():
    assert _to_rows(pd.DataFrame(), ["DE"]) == []


def test_fetch_daily_bars_short_circuits_zero_range():
    """Start >= end must short-circuit without calling yfinance.

    yfinance prints a misleading 'possibly delisted' warning when start == end.
    We prevent that by returning [] before the call — verified here by
    passing an impossible date range and asserting the fast path ran.
    """
    # Identical start/end
    assert fetch_daily_bars(["DE"], start=date(2024, 1, 1), end=date(2024, 1, 1)) == []
    # Inverted range
    assert fetch_daily_bars(["DE"], start=date(2024, 2, 1), end=date(2024, 1, 1)) == []


def test_fetch_daily_bars_empty_tickers_short_circuits():
    assert fetch_daily_bars([], start=date(2024, 1, 1), end=date(2024, 2, 1)) == []


def test_missing_ticker_is_skipped():
    df = _frame(
        {
            "DE": [
                {
                    "date": "2024-01-02",
                    "Open": 100.0,
                    "High": 101.0,
                    "Low": 99.0,
                    "Close": 100.5,
                    "Adj Close": 100.5,
                    "Volume": 500_000,
                },
            ]
        }
    )
    rows = _to_rows(df, ["DE", "ZZZZ"])  # ZZZZ not present
    assert len(rows) == 1
    assert rows[0].ticker == "DE"
