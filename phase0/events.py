"""Event-window sentiment aggregation + beta-adjusted forward returns.

Two jobs. For each (ticker, WASDE release_date D) pair:

1. `event_sentiment`: mean sentiment_score across trading days in [D-1, D+2].
   sentiment_score per (ticker, day) = (pos - neg) / total over FinBERT-labeled
   headlines tagged to that ticker on that day. Missing if no headlines.

2. `forward_return`: market-beta-adjusted excess return over horizons H ∈ {5, 10}:
       excess[T, D, H] = r[T, D, D+H]  −  β[T, D] · r[SPY, D, D+H]
   where β[T, D] is estimated by strictly-trailing 252-trading-day OLS of
   ticker returns on SPY returns, using only data BEFORE D (no look-ahead).

WASDE releases that land on non-trading days (rare but possible) roll to the
next trading day for return measurement. The news window is in trading days,
not calendar days, so a Friday release looks at Thurs/Fri/Mon/Tue sentiment.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FORWARD_HORIZONS: tuple[int, ...] = (5, 10)
NEWS_WINDOW_PRE = 1  # days before release
NEWS_WINDOW_POST = 2  # days after release (inclusive, so [D-1, D+2] = 4 trading days)
BETA_LOOKBACK_DAYS = 252


# ---------------------------------------------------------------- sentiment


def daily_ticker_sentiment(
    news: pd.DataFrame,  # columns: news_id, ticker, timestamp, ...
    scores: pd.DataFrame,  # columns: news_id, label, ...
) -> pd.DataFrame:
    """Return a tidy frame indexed by (ticker, date) with `sentiment` score.

    sentiment = (pos_count - neg_count) / total_count over same-day tagged
    headlines. Missing if no headlines — don't backfill to 0 (a silent day
    is not a neutral day).

    News from non-trading hours is rolled to the next trading day via
    simple calendar-date grouping; the caller aligns to its own trading
    calendar downstream.
    """
    # Inner-join news ↔ scores on news_id (some may be unscored if scorer ran partial).
    # Coerce both sides to string — news_id comes off Alpaca as a huge int but pandas
    # may read it back as int64 from one CSV and object from the other, breaking the
    # merge with "str and int64" errors.
    news = news.copy()
    scores = scores.copy()
    news["news_id"] = news["news_id"].astype(str)
    scores["news_id"] = scores["news_id"].astype(str)
    merged = news.merge(scores[["news_id", "label"]], on="news_id", how="inner")
    merged["date"] = pd.to_datetime(merged["timestamp"]).dt.tz_convert("UTC").dt.date
    merged["date"] = pd.to_datetime(merged["date"])

    grp = merged.groupby(["ticker", "date"])["label"]
    counts = grp.value_counts().unstack(fill_value=0)
    counts = counts.reindex(columns=["positive", "negative", "neutral"], fill_value=0)
    total = counts.sum(axis=1).rename("total")
    sentiment = ((counts["positive"] - counts["negative"]) / total).rename("sentiment")
    out = pd.concat([sentiment, total], axis=1).reset_index()
    return out


# ---------------------------------------------------------------- beta


def rolling_beta(
    ticker_returns: pd.Series,
    spy_returns: pd.Series,
    lookback: int = BETA_LOOKBACK_DAYS,
) -> pd.Series:
    """Strictly trailing rolling beta of ticker on SPY.

    β_t = Cov_trailing(r_T, r_SPY) / Var_trailing(r_SPY), computed on the
    prior `lookback` trading days (excludes day t itself). NaN where there
    isn't enough history.
    """
    # shift(1) makes the window strictly trailing
    t_lag = ticker_returns.shift(1)
    s_lag = spy_returns.shift(1)
    cov = t_lag.rolling(window=lookback, min_periods=lookback).cov(s_lag)
    var = s_lag.rolling(window=lookback, min_periods=lookback).var()
    return (cov / var).rename("beta")


# ---------------------------------------------------------------- event alignment


def _next_trading_day(date: pd.Timestamp, trading_index: pd.DatetimeIndex) -> pd.Timestamp | None:
    """Return the earliest trading day >= date; None if beyond the index."""
    idx = trading_index.searchsorted(date, side="left")
    if idx >= len(trading_index):
        return None
    return trading_index[idx]


def _offset_trading_day(
    anchor: pd.Timestamp,
    n: int,
    trading_index: pd.DatetimeIndex,
) -> pd.Timestamp | None:
    """Return anchor + n trading days (n can be negative). None if out of range."""
    pos = trading_index.searchsorted(anchor)
    if pos >= len(trading_index):
        return None
    # If anchor isn't itself a trading day, `pos` already points at the next
    # trading day — proceed from there.
    target = pos + n
    if target < 0 or target >= len(trading_index):
        return None
    return trading_index[target]


def build_event_panel(
    wasde_surprises: pd.DataFrame,  # release_date, crop, line_item, value_reported, surprise, direction
    prices: pd.DataFrame,  # index = date, columns = tickers (+ SPY)
    daily_sentiment: pd.DataFrame,  # ticker, date, sentiment, total
    tickers: tuple[str, ...],
    horizons: tuple[int, ...] = FORWARD_HORIZONS,
    beta_lookback: int = BETA_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """Merge surprises, sentiment, and forward returns into the event panel.

    Returns one row per (release_date, crop, line_item, ticker, horizon):
        release_date, crop, line_item, surprise, direction,
        ticker, event_sentiment, headline_count,
        horizon, forward_return, spy_return, beta, excess_return
    """
    if "SPY" not in prices.columns:
        raise ValueError("prices panel must include SPY for beta adjustment")

    trading_index = prices.index
    asset_returns = prices.pct_change()
    spy_rets = asset_returns["SPY"]

    # Precompute rolling betas per ticker (strictly trailing).
    betas: dict[str, pd.Series] = {
        t: rolling_beta(asset_returns[t], spy_rets, beta_lookback) for t in tickers
    }

    # Pivot sentiment into a (date × ticker) frame for fast lookup.
    sent_wide = daily_sentiment.pivot_table(
        index="date", columns="ticker", values="sentiment", aggfunc="mean"
    ).reindex(trading_index)
    count_wide = (
        daily_sentiment.pivot_table(index="date", columns="ticker", values="total", aggfunc="sum")
        .reindex(trading_index)
        .fillna(0.0)
    )

    rows: list[dict[str, object]] = []

    for _, w in wasde_surprises.iterrows():
        release = pd.Timestamp(w["release_date"]).normalize()
        D = _next_trading_day(release, trading_index)
        if D is None:
            continue

        pre = _offset_trading_day(D, -NEWS_WINDOW_PRE, trading_index)
        post = _offset_trading_day(D, +NEWS_WINDOW_POST, trading_index)
        if pre is None or post is None:
            continue

        window_sent = sent_wide.loc[pre:post]
        window_counts = count_wide.loc[pre:post]

        for tkr in tickers:
            sent_col = window_sent[tkr] if tkr in window_sent.columns else pd.Series(dtype=float)
            count_col = (
                window_counts[tkr] if tkr in window_counts.columns else pd.Series(dtype=float)
            )
            event_sent = float(sent_col.mean()) if sent_col.notna().any() else np.nan
            headline_count = int(count_col.sum())

            beta_at_D = betas[tkr].loc[D] if tkr in betas and D in betas[tkr].index else np.nan

            for h in horizons:
                D_plus_h = _offset_trading_day(D, h, trading_index)
                if D_plus_h is None:
                    continue
                t_ret = float(prices[tkr].loc[D_plus_h] / prices[tkr].loc[D] - 1)
                s_ret = float(prices["SPY"].loc[D_plus_h] / prices["SPY"].loc[D] - 1)
                if np.isnan(t_ret) or np.isnan(s_ret) or np.isnan(beta_at_D):
                    excess = np.nan
                else:
                    excess = t_ret - float(beta_at_D) * s_ret
                rows.append(
                    {
                        "release_date": D,
                        "crop": w["crop"],
                        "line_item": w["line_item"],
                        "surprise": w["surprise"],
                        "direction": w["direction"],
                        "ticker": tkr,
                        "event_sentiment": event_sent,
                        "headline_count": headline_count,
                        "horizon": h,
                        "forward_return": t_ret,
                        "spy_return": s_ret,
                        "beta": float(beta_at_D) if not np.isnan(beta_at_D) else np.nan,
                        "excess_return": excess,
                    }
                )

    return pd.DataFrame(rows)
