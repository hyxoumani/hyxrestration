"""WASDE surprise computation — stage-1 trend-residual proxy.

Spec: phase0_data_sources.md §5.3. Without real consensus estimates (Reuters/
Bloomberg paid, Farmdoc scrape-heavy), the defensible free fallback is a
trend-residual:

    surprise = (value - trailing_12mo_mean) / trailing_12mo_std

computed per (crop, line_item). This approximates consensus as the rolling
average of recent prints — noisy but directionally aligned with real
consensus when actual analysts extrapolate from recent history.

Known bias: the proxy over-weights seasonal patterns (e.g. corn production
always rises into August regardless of "true" surprise). Stage-2 sensitivity
check against Farmdoc is run only if stage-1 surprises produce signal in
Test 2+3 — don't pay the scrape cost until it's necessary.

Terciles: bucket the standardized surprise into upside / neutral / downside
at 33/67 percentiles of the distribution. Terciles matter because Test 2+3
runs the regression restricted to upside-only and downside-only subsets.
"""

from __future__ import annotations

import pandas as pd

DIRECTIONS = ("upside", "neutral", "downside")


def compute_trend_residual(
    wasde: pd.DataFrame,
    lookback_months: int = 12,
) -> pd.DataFrame:
    """Append `surprise` and `direction` columns to the WASDE panel.

    `surprise` is (value - rolling_mean) / rolling_std per (crop, line_item).
    `direction` is the tercile assignment across the full history.

    The rolling window is strictly trailing — the surprise at release_date D
    uses only releases strictly before D. No look-ahead.
    """
    df = wasde.sort_values(["crop", "line_item", "release_date"]).copy()

    # Rolling mean / std per (crop, line_item) series, strictly trailing.
    grp = df.groupby(["crop", "line_item"])["value_reported"]
    rolling = grp.apply(lambda s: _trailing_stats(s, lookback_months))
    # `rolling` is a DataFrame indexed by [crop, line_item, original_index]
    # with 'mean' and 'std' columns. Restore flat form.
    rolling = rolling.reset_index(level=[0, 1], drop=True)
    df = df.join(rolling)

    df["surprise"] = (df["value_reported"] - df["trailing_mean"]) / df["trailing_std"]
    df = df.drop(columns=["trailing_mean", "trailing_std"])

    # Assign terciles per (crop, line_item) over the set of defined surprises.
    df["direction"] = "neutral"
    for (_crop, _line), sub in df.groupby(["crop", "line_item"]):
        mask = sub["surprise"].notna()
        if mask.sum() < 10:
            continue
        qs = sub.loc[mask, "surprise"].quantile([1 / 3, 2 / 3])
        lo, hi = qs.iloc[0], qs.iloc[1]
        df.loc[sub.index[mask & (sub["surprise"] <= lo)], "direction"] = "downside"
        df.loc[sub.index[mask & (sub["surprise"] >= hi)], "direction"] = "upside"

    return df.sort_values(["release_date", "crop", "line_item"]).reset_index(drop=True)


def _trailing_stats(series: pd.Series, lookback: int) -> pd.DataFrame:
    """Rolling mean + std over `lookback` prior observations (strictly trailing).

    Returns a DataFrame aligned to `series.index` with columns trailing_mean
    and trailing_std. Uses shift(1) to make the window strictly exclude the
    current row.
    """
    lagged = series.shift(1)
    mean = lagged.rolling(window=lookback, min_periods=lookback).mean()
    std = lagged.rolling(window=lookback, min_periods=lookback).std()
    return pd.DataFrame({"trailing_mean": mean, "trailing_std": std})


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """Descriptive table: per (crop, line_item), surprise count + tercile counts."""
    out = (
        df.dropna(subset=["surprise"])
        .groupby(["crop", "line_item", "direction"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=list(DIRECTIONS), fill_value=0)
    )
    total = df.groupby(["crop", "line_item"]).size().rename("total")
    return out.join(total)


if __name__ == "__main__":
    from phase0.wasde_loader import load_wasde

    w = load_wasde()
    s = compute_trend_residual(w)
    print(summarize(s))
