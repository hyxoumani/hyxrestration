"""Synthetic end-to-end smoke test for Test 2+3's pipeline.

Validates the plumbing before real WASDE / sentiment data arrive:

- WASDE: synthetic release values (AR(1) around realistic baselines) from
  wasde_loader.generate_synthetic_wasde. Surprises computed via the real
  trend-residual proxy.
- Prices: REAL yfinance data for the 8 regression tickers + SPY + 14-ticker
  universe, cached from Test 1. Uses the real beta-rolling code path.
- Sentiment: synthetic per (ticker, trading day) with two modes:
    - `null_mode=True`  → random noise, expected verdict = joint_fail
    - `null_mode=False` → inject a real interaction effect for a subset of
       (category, direction, line_item) combinations, expected verdict
       = joint_pass. Sanity check that the test can DETECT signal, not just
       reject noise.

Run:
    python -m phase0.test23_synthetic_smoke
    python -m phase0.test23_synthetic_smoke --mode positive
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from phase0.data_loaders import load_adj_close
from phase0.events import build_event_panel
from phase0.surprise import compute_trend_residual
from phase0.test23_wasde_sentiment import TICKER_CATEGORIES, run
from phase0.wasde_loader import WASDE_CSV, generate_synthetic_wasde, load_wasde

REGRESSION_TICKERS: tuple[str, ...] = tuple(
    t for group in TICKER_CATEGORIES.values() for t in group
)  # NTR, MOS, CF, DE, AGCO, CNH, ADM, BG


def _synthetic_sentiment(
    trading_days: pd.DatetimeIndex,
    tickers: tuple[str, ...],
    mode: str,
    surprises: pd.DataFrame,
    seed: int = 7,
) -> pd.DataFrame:
    """Generate a (ticker, date) sentiment panel.

    mode='null': uniform random noise in [-1, +1].
    mode='positive': null baseline + injected interaction effect for the
        fertilizer × corn × production × upside cell — sentiment scales
        positively with surprise in the [D-1, D+2] window, so
        β_interaction should be large and positive for that cell if the
        regression engine works.
    """
    rng = np.random.default_rng(seed)
    n_days = len(trading_days)
    n_tickers = len(tickers)
    vals = rng.uniform(-0.5, 0.5, size=(n_days, n_tickers))

    df = pd.DataFrame(vals, index=trading_days, columns=list(tickers))

    if mode == "positive":
        # Inject: for fertilizer tickers on days in the [D-1, D+2] window of a
        # corn-production release, add sentiment that's *proportional to* the
        # surprise value. This gives β_interaction > 0 on fertilizer×upside×corn
        # production but noise elsewhere.
        fert = ["NTR", "MOS", "CF"]
        corn_prod = surprises[
            (surprises["crop"] == "corn") & (surprises["line_item"] == "production")
        ][["release_date", "surprise"]].dropna()
        for _, r in corn_prod.iterrows():
            D = pd.Timestamp(r["release_date"]).normalize()
            pos = trading_days.searchsorted(D)
            for t in fert:
                if t not in df.columns:
                    continue
                for offset in (-1, 0, 1, 2):
                    p = pos + offset
                    if 0 <= p < n_days:
                        df.iloc[p, df.columns.get_loc(t)] += 0.35 * r["surprise"]

    # Convert wide → long format expected by build_event_panel
    long = df.reset_index(names="date").melt(
        id_vars="date", var_name="ticker", value_name="sentiment"
    )
    long["sentiment"] = long["sentiment"].clip(-1.0, 1.0)
    long["total"] = 10  # pretend each day has 10 headlines
    return long


def _inject_return_signal(panel: pd.DataFrame, seed: int = 11) -> pd.DataFrame:
    """For positive-mode smoke: overwrite `excess_return` so that
    fertilizer × corn × production × upside events at horizon=5 have
    excess_return ≈ 0.06 × (surprise × sentiment) + noise.

    This is the only place signal is injected. Every other cell gets pure
    noise. A working regression should light up that one cell via
    β_interaction and nothing else should survive FDR.
    """
    rng = np.random.default_rng(seed)
    panel = panel.copy()
    # Noise baseline (std ≈ 3% weekly, typical for individual equities)
    panel["excess_return"] = rng.normal(0.0, 0.03, size=len(panel))

    mask = (
        panel["ticker"].isin(["NTR", "MOS", "CF"])
        & (panel["crop"] == "corn")
        & (panel["line_item"] == "production")
        & (panel["direction"] == "upside")
        & (panel["horizon"] == 5)
    )
    sig = 0.06 * panel.loc[mask, "surprise"] * panel.loc[mask, "event_sentiment"]
    panel.loc[mask, "excess_return"] = panel.loc[mask, "excess_return"] + sig.fillna(0.0)
    return panel


def run_synthetic(mode: str = "null", wasde_source: str = "auto") -> None:
    print(f"[smoke] mode = {mode!r}  wasde = {wasde_source!r}")

    # --- real prices (cached from Test 1)
    all_needed = tuple({*REGRESSION_TICKERS, "SPY"})
    prices = load_adj_close(all_needed, start="2014-01-01", end="2025-01-01")
    prices = prices.sort_index()

    # --- WASDE: real scraped data if CSV exists or explicitly requested, else synthetic
    use_real = wasde_source == "real" or (wasde_source == "auto" and WASDE_CSV.exists())
    if use_real:
        wasde = load_wasde()
        print(
            f"[smoke] using REAL WASDE ({len(wasde)} rows, "
            f"{wasde['release_date'].nunique()} releases, "
            f"{wasde['release_date'].min().date()} → {wasde['release_date'].max().date()})"
        )
    else:
        wasde = generate_synthetic_wasde(start="2014-01-10", end="2024-12-31")
        print(f"[smoke] using SYNTHETIC WASDE ({len(wasde)} rows)")
    surprises = compute_trend_residual(wasde)

    # --- synthetic sentiment on the real trading-day calendar
    sentiment_panel = _synthetic_sentiment(prices.index, REGRESSION_TICKERS, mode, surprises)

    # --- build event panel (uses real prices for beta + returns)
    panel = build_event_panel(
        wasde_surprises=surprises.dropna(subset=["surprise"]),
        prices=prices,
        daily_sentiment=sentiment_panel,
        tickers=REGRESSION_TICKERS,
    )
    print(
        f"[smoke] event panel: {len(panel)} rows  "
        f"({panel['ticker'].nunique()} tickers × {panel['release_date'].nunique()} events)"
    )

    # --- for positive-mode, overwrite excess_return with injected signal
    if mode == "positive":
        panel = _inject_return_signal(panel)

    # --- run
    verdict = run(
        panel,
        caveats=[
            "**Synthetic inputs.** This run used generated WASDE values and "
            f"{'random-noise' if mode == 'null' else 'signal-injected'} sentiment. "
            + (
                "Excess returns were synthetic with signal injected into fertilizer × corn × "
                "production × upside × h=5 only. "
                if mode == "positive"
                else "Excess returns are real (yfinance). "
            )
            + "Real verdict requires WASDE scrape (iter 2) + Alpaca news + FinBERT (iter 3).",
            "Pre-registration lock is NOT in place (user decision 2026-04-22).",
        ],
    )

    expected_map = {
        "null": {"partial_pass", "joint_fail"},  # either is within FDR tolerance
        "positive": {"joint_pass", "partial_pass"},
    }
    actual = verdict.verdict
    marker = "✅ within expected" if actual in expected_map[mode] else "⚠️  unexpected"
    print(f"[smoke] expected verdict ∈ {sorted(expected_map[mode])}, actual = {actual}  {marker}")


def main() -> int:
    p = argparse.ArgumentParser(prog="test23_synthetic_smoke")
    p.add_argument("--mode", choices=["null", "positive"], default="null")
    p.add_argument(
        "--wasde",
        choices=["auto", "real", "synthetic"],
        default="auto",
        help="auto = real if phase0/data/wasde_releases.csv exists, else synthetic.",
    )
    args = p.parse_args()
    run_synthetic(args.mode, args.wasde)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
