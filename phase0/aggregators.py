"""Daily sentiment aggregators for §2.10 expanded grid.

Three pre-registered aggregation schemas operating on the (news, scores)
joined frame. Each returns a DataFrame with columns (ticker, date, sentiment,
total) — the same schema `events.daily_ticker_sentiment` returns, so it
drops into build_event_panel / build_daily_panel unchanged.

Pre-registered in phase0_testing.md §2.10.3. Any change to the formulas
requires a [pre-registration-violation] commit per §0.1.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd


def _prep(news: pd.DataFrame, scores: pd.DataFrame, extra_cols: list[str]) -> pd.DataFrame:
    """Inner-join news ↔ scores on news_id, with dtype-safe coercion + date column.

    `extra_cols` lists the score columns to bring across (varies by aggregator).
    """
    news = news.copy()
    scores = scores.copy()
    news["news_id"] = news["news_id"].astype(str)
    scores["news_id"] = scores["news_id"].astype(str)
    merged = news.merge(scores[["news_id"] + extra_cols], on="news_id", how="inner")
    merged["date"] = pd.to_datetime(merged["timestamp"]).dt.tz_convert("UTC").dt.date
    merged["date"] = pd.to_datetime(merged["date"])
    return merged


def aggregate_mean_label(news: pd.DataFrame, scores: pd.DataFrame) -> pd.DataFrame:
    """A1 — (pos_count − neg_count) / total_count.

    Same as phase0.events.daily_ticker_sentiment — argmax label counts,
    divided by total daily headline count per (ticker, date).
    """
    merged = _prep(news, scores, ["label"])
    grp = merged.groupby(["ticker", "date"])["label"]
    counts = grp.value_counts().unstack(fill_value=0)
    counts = counts.reindex(columns=["positive", "negative", "neutral"], fill_value=0)
    total = counts.sum(axis=1).rename("total")
    sentiment = ((counts["positive"] - counts["negative"]) / total).rename("sentiment")
    out = pd.concat([sentiment, total], axis=1).reset_index()
    return out


def aggregate_conf_weighted(news: pd.DataFrame, scores: pd.DataFrame) -> pd.DataFrame:
    """A2 — mean(P_pos − P_neg) across day's headlines.

    Uses the softmax probabilities directly. Picks up "confidently positive"
    vs "marginally positive" distinctions that A1's argmax-count loses.
    """
    merged = _prep(news, scores, ["pos", "neg"])
    merged["diff"] = merged["pos"] - merged["neg"]
    grp = merged.groupby(["ticker", "date"])
    sentiment = grp["diff"].mean().rename("sentiment")
    total = grp.size().rename("total")
    out = pd.concat([sentiment, total], axis=1).reset_index()
    return out


def aggregate_volume_normalized(news: pd.DataFrame, scores: pd.DataFrame) -> pd.DataFrame:
    """A3 — (pos_count − neg_count) / log(1 + total_count).

    Same numerator as A1, log-dampened denominator. Smooths over heavy-news
    days that otherwise spike A1's sentiment magnitude.
    """
    merged = _prep(news, scores, ["label"])
    grp = merged.groupby(["ticker", "date"])["label"]
    counts = grp.value_counts().unstack(fill_value=0)
    counts = counts.reindex(columns=["positive", "negative", "neutral"], fill_value=0)
    total = counts.sum(axis=1).rename("total")
    sentiment = ((counts["positive"] - counts["negative"]) / np.log1p(total)).rename("sentiment")
    out = pd.concat([sentiment, total], axis=1).reset_index()
    return out


AGGREGATORS: dict[str, Callable[[pd.DataFrame, pd.DataFrame], pd.DataFrame]] = {
    "A1_mean_label": aggregate_mean_label,
    "A2_conf_weighted": aggregate_conf_weighted,
    "A3_volume_normalized": aggregate_volume_normalized,
}
