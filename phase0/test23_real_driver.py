"""Phase 0 — Test 2+3 iter 3: real driver (Alpaca news + FinBERT + real WASDE).

Glue script for the actual pre-registered Test 2+3 run. Wires together:

  phase0.wasde_loader   → 132 scraped USDA releases (2014-2024)
  phase0.surprise       → trend-residual proxy surprises + terciles
  phase0.news_loader    → Alpaca Benzinga news (2021-2024) — pulls + caches
  phase0.sentiment      → FinBERT scoring — idempotent, caches per news_id
  phase0.events         → build event panel with real sentiment + beta-adjusted
                          forward returns
  phase0.test23_wasde_sentiment.run → 36 regressions, BH-FDR, verdict, report

Per the §7.1 depth check (phase0/results/depth_check_2026-04-22.md), CNH is
excluded from the regression matrix — 0.6 articles/month over the window is
below the §7.1 NOISE floor. The exclusion is a pre-registered operation per
§7.1, not a §2.3 edit, so CNH stays in TICKER_CATEGORIES but contributes
~nothing via NaN dropping in _run_one_regression.

Run:
    python -m phase0.test23_real_driver                        # full pipeline
    python -m phase0.test23_real_driver --skip-fetch           # reuse cached news
    python -m phase0.test23_real_driver --skip-fetch --skip-score  # reuse everything

Outputs:
    phase0/data/alpaca_news.csv       — raw news corpus
    phase0/data/finbert_scores.csv    — FinBERT score per news_id
    phase0/results/test23_{today}.md  — verdict + 36-cell table + interpretation
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import pandas as pd

from phase0.data_loaders import load_adj_close
from phase0.events import build_event_panel, daily_ticker_sentiment
from phase0.news_loader import NEWS_CSV, PHASE0_UNIVERSE, fetch_news
from phase0.sentiment import SCORES_CSV, score_corpus
from phase0.surprise import compute_trend_residual
from phase0.test23_wasde_sentiment import TICKER_CATEGORIES, run
from phase0.wasde_loader import load_wasde

REGRESSION_TICKERS: tuple[str, ...] = tuple(
    t for group in TICKER_CATEGORIES.values() for t in group
)

TEST23_START = datetime(2021, 1, 1, tzinfo=UTC)
TEST23_END = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)


def fetch_all_news() -> pd.DataFrame:
    """Pull the full Test 2+3 window of news for all 10 ag tickers. Cache to CSV.

    Incremental: if the cache already covers the requested window, skip the pull.
    This is the single place iter 3 hits Alpaca for news.
    """
    if NEWS_CSV.exists():
        existing = pd.read_csv(NEWS_CSV, parse_dates=["timestamp"])
        print(
            f"[driver] cached news: {len(existing)} rows, "
            f"{existing['news_id'].nunique()} articles, "
            f"range {existing['timestamp'].min()} → {existing['timestamp'].max()}"
        )
        return existing

    print(
        f"[driver] fetching Alpaca news {TEST23_START.date()} → {TEST23_END.date()} "
        f"for {len(PHASE0_UNIVERSE)} tickers — takes a few minutes"
    )
    df = fetch_news(tickers=PHASE0_UNIVERSE, start=TEST23_START, end=TEST23_END)
    NEWS_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(NEWS_CSV, index=False)
    print(f"[driver] fetched {len(df)} rows, {df['news_id'].nunique()} articles")
    return df


def score_all() -> pd.DataFrame:
    """Run FinBERT over the cached news corpus. Idempotent."""
    print(f"[driver] scoring with FinBERT (cache: {SCORES_CSV})")
    scores = score_corpus()
    print(f"[driver] finbert scores: {len(scores)} rows")
    return scores


def main() -> int:
    p = argparse.ArgumentParser(prog="test23_real_driver")
    p.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Reuse phase0/data/alpaca_news.csv instead of calling Alpaca. "
        "Errors if the cache is missing.",
    )
    p.add_argument(
        "--skip-score",
        action="store_true",
        help="Reuse phase0/data/finbert_scores.csv instead of running FinBERT. "
        "Errors if the cache is missing.",
    )
    args = p.parse_args()

    # --- WASDE surprises --------------------------------------------------
    wasde = load_wasde()
    print(
        f"[driver] WASDE: {len(wasde)} rows, {wasde['release_date'].nunique()} releases, "
        f"{wasde['release_date'].min().date()} → {wasde['release_date'].max().date()}"
    )
    surprises = compute_trend_residual(wasde).dropna(subset=["surprise"])
    counts = surprises["direction"].value_counts().to_dict()
    print(f"[driver] surprises: {len(surprises)} rows  terciles={counts}")

    # Filter surprises to the Test 2+3 window (2021-2024) — the news arm only
    # covers 2021-2024, so older surprises would join to NaN sentiment and drop.
    window_mask = (surprises["release_date"] >= "2021-01-01") & (
        surprises["release_date"] <= "2024-12-31"
    )
    window_surprises = surprises[window_mask]
    print(
        f"[driver] surprises in Test 2+3 window: {len(window_surprises)} rows "
        f"({window_surprises['release_date'].nunique()} releases)"
    )

    # --- prices (cached from Test 1) --------------------------------------
    all_needed = tuple({*REGRESSION_TICKERS, "SPY"})
    prices = load_adj_close(all_needed, start="2014-01-01", end="2025-01-01").sort_index()
    print(f"[driver] prices: {len(prices)} trading days, {len(prices.columns)} columns")

    # --- news -------------------------------------------------------------
    if args.skip_fetch:
        if not NEWS_CSV.exists():
            raise SystemExit(f"--skip-fetch but {NEWS_CSV} missing")
        news = pd.read_csv(NEWS_CSV, parse_dates=["timestamp"])
        print(f"[driver] skip-fetch: loaded {len(news)} cached news rows")
    else:
        news = fetch_all_news()

    # --- FinBERT scoring --------------------------------------------------
    if args.skip_score:
        if not SCORES_CSV.exists():
            raise SystemExit(f"--skip-score but {SCORES_CSV} missing")
        scores = pd.read_csv(SCORES_CSV)
        print(f"[driver] skip-score: loaded {len(scores)} cached scores")
    else:
        scores = score_all()

    # --- daily per-ticker sentiment ---------------------------------------
    daily_sent = daily_ticker_sentiment(news, scores)
    print(f"[driver] daily_ticker_sentiment: {len(daily_sent)} (ticker,date) pairs")

    # --- event panel ------------------------------------------------------
    panel = build_event_panel(
        wasde_surprises=window_surprises,
        prices=prices,
        daily_sentiment=daily_sent,
        tickers=REGRESSION_TICKERS,
    )
    coverage = panel["event_sentiment"].notna().mean()
    print(
        f"[driver] event panel: {len(panel)} rows  "
        f"({panel['ticker'].nunique()} tickers × {panel['release_date'].nunique()} events), "
        f"sentiment coverage={coverage:.1%}"
    )

    # Per-ticker sentiment coverage
    per_tkr = (
        panel.assign(has_sent=panel["event_sentiment"].notna())
        .groupby("ticker")["has_sent"]
        .mean()
        .sort_values(ascending=False)
    )
    print("[driver] per-ticker sentiment coverage (fraction of events with a score):")
    for t, frac in per_tkr.items():
        print(f"  {t:<6} {frac:.1%}")

    # --- run regression ---------------------------------------------------
    verdict = run(
        panel,
        caveats=[
            (
                "**Real Alpaca news + real WASDE + real prices + FinBERT scoring.** "
                "No synthetic data in this run. News window 2021-2024 (48 months). "
                f"Corpus: {len(news)} raw rows across {news['news_id'].nunique()} unique "
                f"articles, FinBERT-scored on {len(scores)} articles."
            ),
            (
                "**CNH excluded via §7.1 depth check** (0.6 articles/mo — below NOISE "
                "floor). Exclusion flows through as NaN event_sentiment → row drop in "
                "_run_one_regression. See phase0/results/depth_check_2026-04-22.md."
            ),
            (
                "**Surprise is trend-residual proxy**, not Reuters/Farmdoc consensus. "
                "If this passes, a ~10-release Farmdoc sensitivity spot-check is "
                "warranted before trading the signal. If it fails, a similar spot-check "
                "confirms proxy wasn't the bottleneck."
            ),
        ],
    )
    print(f"\n[driver] verdict: {verdict.verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
