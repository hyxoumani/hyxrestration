"""Phase 0 — Test 2-standalone: daily FinBERT sentiment predicts forward returns?

Spec: phase0_testing.md §2.8. Pre-registration locked in the commit that introduces
this file.

Diagnostic to resolve the ambiguity in iter 3's `WASDE-only pass` verdict:
is sentiment adding nothing because the interaction is zero, or because the
sentiment arm contains no signal at all?

Regression per (category, horizon):
    excess_return[t, t+h] ~ α + β · sentiment[t] + ε   (Newey-West HAC, maxlags=h)

9 regressions total (3 × 3). BH-FDR correction at q=0.10 on 9 β p-values.
Pass criteria per §2.8.5.

Run:
    python -m phase0.test2_sentiment_standalone
Outputs:
    phase0/results/test2_{today}.md
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.stats.multitest import multipletests

from phase0.data_loaders import PHASE0_DIR, load_adj_close
from phase0.events import daily_ticker_sentiment, rolling_beta
from phase0.news_loader import NEWS_CSV
from phase0.sentiment import SCORES_CSV

# -------------------------------------------- pre-registered (§2.8)

TICKER_CATEGORIES: dict[str, tuple[str, ...]] = {
    "fertilizer": ("NTR", "MOS", "CF"),
    "equipment": ("DE", "AGCO"),  # CNH excluded via §7.1 depth check
    "processors": ("ADM", "BG"),
}
HORIZONS: tuple[int, ...] = (1, 5, 10)

FDR_Q = 0.10
ECONOMIC_MAGNITUDE_BPS = 30

RESULTS_DIR = PHASE0_DIR / "results"
BETA_LOOKBACK_DAYS = 252
WINDOW_START = "2021-01-01"
WINDOW_END = "2024-12-31"

ALL_TICKERS: tuple[str, ...] = tuple(t for grp in TICKER_CATEGORIES.values() for t in grp)


# -------------------------------------------- daily panel


def build_daily_panel(
    daily_sent: pd.DataFrame,
    prices: pd.DataFrame,
    tickers: tuple[str, ...],
    horizons: tuple[int, ...] = HORIZONS,
    beta_lookback: int = BETA_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """Daily (ticker, date, horizon) → beta-adjusted forward excess return + sentiment.

    For each ticker, each trading day `t` with defined sentiment, compute
    forward return over [t, t+h] and beta-adjust with strictly-trailing β[t].
    """
    if "SPY" not in prices.columns:
        raise ValueError("prices must include SPY for beta adjustment")

    trading_index = prices.index
    asset_returns = prices.pct_change()
    spy_rets = asset_returns["SPY"]
    betas: dict[str, pd.Series] = {
        t: rolling_beta(asset_returns[t], spy_rets, beta_lookback) for t in tickers
    }

    sent_wide = daily_sent.pivot_table(
        index="date", columns="ticker", values="sentiment", aggfunc="mean"
    ).reindex(trading_index)

    rows: list[dict[str, object]] = []
    for tkr in tickers:
        if tkr not in sent_wide.columns:
            continue
        sent_series = sent_wide[tkr].dropna()
        for d, sent_val in sent_series.items():
            if d not in trading_index:
                continue
            pos = trading_index.get_loc(d)
            beta_at_t = betas[tkr].loc[d] if d in betas[tkr].index else np.nan
            if np.isnan(beta_at_t):
                continue
            for h in horizons:
                target_pos = pos + h
                if target_pos >= len(trading_index):
                    continue
                d_plus_h = trading_index[target_pos]
                try:
                    t_ret = float(prices[tkr].loc[d_plus_h] / prices[tkr].loc[d] - 1)
                    s_ret = float(prices["SPY"].loc[d_plus_h] / prices["SPY"].loc[d] - 1)
                except KeyError:
                    continue
                if np.isnan(t_ret) or np.isnan(s_ret):
                    continue
                excess = t_ret - float(beta_at_t) * s_ret
                rows.append(
                    {
                        "ticker": tkr,
                        "date": d,
                        "sentiment": float(sent_val),
                        "horizon": h,
                        "forward_return": t_ret,
                        "spy_return": s_ret,
                        "beta": float(beta_at_t),
                        "excess_return": excess,
                    }
                )
    return pd.DataFrame(rows)


# -------------------------------------------- regression


@dataclass
class CellResult:
    category: str
    horizon: int
    n_obs: int
    beta: float
    se: float
    t_stat: float
    p_value: float
    implied_bps_at_1sigma: float
    p_fdr: float = float("nan")
    survives_fdr: bool = False


def _empty_coeffs() -> dict[str, float]:
    return {
        "beta": float("nan"),
        "se": float("nan"),
        "t_stat": float("nan"),
        "p_value": float("nan"),
        "implied_bps_at_1sigma": float("nan"),
    }


def _run_one_regression(sub: pd.DataFrame, horizon: int) -> tuple[dict[str, float], int]:
    """OLS with Newey-West HAC SEs (maxlags=horizon) on excess_return ~ sentiment + ε."""
    if len(sub) < 30:
        return _empty_coeffs(), len(sub)
    df = sub.dropna(subset=["excess_return", "sentiment"]).copy()
    if len(df) < 30:
        return _empty_coeffs(), len(df)
    y = df["excess_return"].to_numpy()
    X = df[["sentiment"]].to_numpy()
    X = sm.add_constant(X, prepend=True, has_constant="add")
    try:
        model = sm.OLS(y, X).fit(
            cov_type="HAC",
            cov_kwds={"maxlags": max(1, horizon)},
        )
    except Exception:
        return _empty_coeffs(), len(df)
    sigma_sent = df["sentiment"].std(ddof=0) or 1e-12
    implied_bps = model.params[1] * sigma_sent * 10_000
    return (
        {
            "beta": float(model.params[1]),
            "se": float(model.bse[1]),
            "t_stat": float(model.tvalues[1]),
            "p_value": float(model.pvalues[1]),
            "implied_bps_at_1sigma": float(implied_bps),
        },
        len(df),
    )


def run_all_regressions(panel: pd.DataFrame) -> list[CellResult]:
    results: list[CellResult] = []
    for cat, tickers in TICKER_CATEGORIES.items():
        for horizon in HORIZONS:
            sub = panel[(panel["ticker"].isin(tickers)) & (panel["horizon"] == horizon)]
            coeffs, n = _run_one_regression(sub, horizon)
            results.append(
                CellResult(
                    category=cat,
                    horizon=horizon,
                    n_obs=n,
                    **coeffs,
                )
            )
    return results


# -------------------------------------------- FDR + verdict


def apply_fdr(results: list[CellResult], q: float = FDR_Q) -> list[CellResult]:
    idx_valid = [i for i, r in enumerate(results) if not np.isnan(r.p_value)]
    if not idx_valid:
        return results
    pvals = [results[i].p_value for i in idx_valid]
    rejected, pvals_corrected, _, _ = multipletests(pvals, alpha=q, method="fdr_bh")
    for local_idx, global_idx in enumerate(idx_valid):
        results[global_idx].p_fdr = float(pvals_corrected[local_idx])
        results[global_idx].survives_fdr = bool(rejected[local_idx])
    return results


@dataclass
class TestVerdict:
    n_surviving: int
    n_economic: int
    directionally_consistent: bool
    passes_c1: bool
    passes_c2: bool
    passes_c3: bool
    verdict: str = ""
    surviving: list[CellResult] = field(default_factory=list)
    inconsistent_categories: list[str] = field(default_factory=list)


def evaluate_verdict(results: list[CellResult]) -> TestVerdict:
    surviving = [r for r in results if r.survives_fdr]
    n_surviving = len(surviving)

    # Directional consistency §2.8.5 criterion 2: within a category, surviving β
    # across horizons must have matching signs.
    inconsistent: list[str] = []
    by_cat: dict[str, list[CellResult]] = {}
    for r in surviving:
        by_cat.setdefault(r.category, []).append(r)
    for cat, cells in by_cat.items():
        if len(cells) >= 2:
            signs = {int(np.sign(c.beta)) for c in cells if c.beta != 0}
            if len(signs) > 1:
                inconsistent.append(cat)
    directionally_consistent = len(inconsistent) == 0

    n_economic = sum(1 for r in surviving if abs(r.implied_bps_at_1sigma) >= ECONOMIC_MAGNITUDE_BPS)

    c1 = n_surviving >= 1
    c2 = directionally_consistent
    c3 = n_economic >= 1

    if c1 and c2 and c3:
        verdict = "pass"
    elif n_surviving >= 1:
        verdict = "partial"
    else:
        verdict = "fail"

    return TestVerdict(
        n_surviving=n_surviving,
        n_economic=n_economic,
        directionally_consistent=directionally_consistent,
        passes_c1=c1,
        passes_c2=c2,
        passes_c3=c3,
        verdict=verdict,
        surviving=surviving,
        inconsistent_categories=inconsistent,
    )


# -------------------------------------------- report


_VERDICT_LABELS = {
    "pass": "PASS",
    "fail": "FAIL",
    "partial": "PARTIAL (explicitly NOT a pass per §2.8.5)",
}

_VERDICT_INTERP = {
    "pass": (
        "FinBERT-on-ag-news sentiment captures real predictive signal at daily swing "
        "horizons. The iter 3 `WASDE-only pass` finding is **robust**: the interaction "
        "specifically is zero, not the sentiment arm broadly. Per §2.8.6, §2.5's "
        "architectural prescription stands: demote sentiment agent to context-only, "
        "delete Sentiment LoRA (slice 5b). Qwen re-scoring becomes a lower-priority "
        "second-order sensitivity check, not a pivot-blocker."
    ),
    "fail": (
        "FinBERT-on-ag-news sentiment shows no standalone predictive signal either. "
        "FinBERT is the live candidate for 'bottleneck model, not bottleneck thesis.' "
        "Per §2.8.6, **Qwen zero-shot re-scoring becomes load-bearing** before executing "
        "§2.5's pivot. The cross-modal claim cannot be cleanly falsified on FinBERT "
        "data alone; we may have been measuring FinBERT, not the thesis."
    ),
    "partial": (
        "1-2 surviving cells with sign-inconsistency or below 30bps economic magnitude. "
        "Pre-registration (§2.8.5) explicitly treats this as NOT a pass — the easiest "
        "outcome to rationalize post-hoc. Treat as fail for decision purposes."
    ),
}


def _fmt(v: float, spec: str) -> str:
    return spec.format(v) if not (v is None or np.isnan(v)) else "—"


def _current_git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PHASE0_DIR.parent,
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def write_report(
    results: list[CellResult],
    verdict: TestVerdict,
    panel_info: dict,
    run_date: date,
    report_path: Path,
) -> None:
    lines: list[str] = []
    lines.append("# Phase 0 — Test 2 standalone: daily FinBERT sentiment → forward returns")
    lines.append("")
    lines.append(f"**Run date:** {run_date.isoformat()}")
    lines.append(f"**Current commit SHA:** `{_current_git_sha()}`")
    lines.append("**Pre-registration:** locked in the commit that introduced §2.8 and this file.")
    lines.append(
        f"**Regressions:** {len(results)} "
        f"(3 categories × 3 horizons, Newey-West HAC SEs with maxlags=horizon)"
    )
    lines.append(f"**FDR method:** Benjamini-Hochberg at q={FDR_Q}")
    lines.append(
        f"**Economic-magnitude threshold:** |implied bps at 1σ| ≥ {ECONOMIC_MAGNITUDE_BPS}"
    )
    lines.append("")
    lines.append("## Data")
    lines.append("")
    for k, v in panel_info.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"- **Outcome: {_VERDICT_LABELS[verdict.verdict]}**")
    lines.append(
        f"- Criterion 1 (≥1 surviving BH-FDR): "
        f"{'✅' if verdict.passes_c1 else '❌'} ({verdict.n_surviving} surviving)"
    )
    lines.append(
        f"- Criterion 2 (directional consistency): "
        f"{'✅' if verdict.passes_c2 else '❌'}"
        + (
            f" (inconsistent: {verdict.inconsistent_categories})"
            if verdict.inconsistent_categories
            else ""
        )
    )
    lines.append(
        f"- Criterion 3 (≥1 economic |bps|≥{ECONOMIC_MAGNITUDE_BPS}): "
        f"{'✅' if verdict.passes_c3 else '❌'} ({verdict.n_economic} economic)"
    )
    lines.append("")
    lines.append("## Interpretation (per phase0_testing.md §2.8.6)")
    lines.append("")
    lines.append(_VERDICT_INTERP[verdict.verdict])
    lines.append("")

    if verdict.surviving:
        lines.append("## Surviving (category × horizon)")
        lines.append("")
        lines.append("| category | horizon | β | t | p (FDR) | implied bps | economic? |")
        lines.append("|---|---:|---:|---:|---:|---:|:--:|")
        for r in verdict.surviving:
            econ = "✅" if abs(r.implied_bps_at_1sigma) >= ECONOMIC_MAGNITUDE_BPS else "❌"
            lines.append(
                f"| {r.category} | {r.horizon}d | {r.beta:+.5f} | {r.t_stat:+.2f} | "
                f"{r.p_fdr:.4f} | {r.implied_bps_at_1sigma:+.1f} | {econ} |"
            )
        lines.append("")

    lines.append("## Full regression table (all 9)")
    lines.append("")
    lines.append("| category | horizon | n | β | t | p_raw | p_fdr | implied bps |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in results:
        lines.append(
            f"| {r.category} | {r.horizon}d | {r.n_obs} | "
            f"{_fmt(r.beta, '{:+.5f}')} | {_fmt(r.t_stat, '{:+.2f}')} | "
            f"{_fmt(r.p_value, '{:.3f}')} | {_fmt(r.p_fdr, '{:.3f}')} | "
            f"{_fmt(r.implied_bps_at_1sigma, '{:+.1f}')} |"
        )
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- **Newey-West HAC with maxlags=horizon.** At h=5 and h=10, overlapping "
        "forward-return windows create autocorrelation; HAC corrects the SEs. At h=1 "
        "overlap is nil but maxlags=1 is a conservative default."
    )
    lines.append(
        "- **Sample is non-random in the time dimension.** Days with news are selected-on — "
        "more-covered tickers contribute more observations, and tickers in high-news "
        "periods (earnings season, M&A events) dominate those periods. Interpret β as "
        "a conditional-on-news-existing effect, not an always-on daily alpha."
    )
    lines.append(
        "- **FinBERT trained pre-2020.** Calibration on 2021+ ag-specific headlines "
        "is unverified. A null here is the premise for pulling Qwen zero-shot forward."
    )
    lines.append(
        "- **CNH excluded via §7.1** depth check (0.6 articles/mo over the window). "
        "Equipment category is (DE, AGCO) for this test."
    )

    report_path.write_text("\n".join(lines))


# -------------------------------------------- entrypoint


def run() -> TestVerdict:
    # News corpus + FinBERT scores (cached from iter 3)
    if not NEWS_CSV.exists():
        raise SystemExit(f"{NEWS_CSV} missing — run phase0.test23_real_driver first")
    if not SCORES_CSV.exists():
        raise SystemExit(f"{SCORES_CSV} missing — run phase0.test23_real_driver first")
    news = pd.read_csv(NEWS_CSV, parse_dates=["timestamp"])
    scores = pd.read_csv(SCORES_CSV)
    print(f"[test2] news corpus: {len(news)} rows, {news['news_id'].nunique()} articles")
    print(f"[test2] finbert scores: {len(scores)} rows")

    daily_sent = daily_ticker_sentiment(news, scores)
    print(f"[test2] daily_ticker_sentiment: {len(daily_sent)} (ticker,date) pairs")

    # Prices covering the test window + 252-day beta lookback
    all_needed = tuple({*ALL_TICKERS, "SPY"})
    prices = load_adj_close(all_needed, start="2020-01-01", end="2025-01-01").sort_index()
    prices = prices[(prices.index >= "2020-01-01") & (prices.index <= "2024-12-31")]
    print(
        f"[test2] prices: {len(prices)} trading days ({prices.index.min().date()} → {prices.index.max().date()})"
    )

    panel = build_daily_panel(daily_sent, prices, ALL_TICKERS)
    # Filter to the pre-reg test window
    panel = panel[(panel["date"] >= WINDOW_START) & (panel["date"] <= WINDOW_END)]
    print(
        f"[test2] daily panel: {len(panel)} rows  "
        f"({panel['ticker'].nunique()} tickers × {panel['horizon'].nunique()} horizons)"
    )

    # Per-category sample sizes
    print("[test2] rows per (category, horizon):")
    for cat, tks in TICKER_CATEGORIES.items():
        for h in HORIZONS:
            n = len(panel[(panel["ticker"].isin(tks)) & (panel["horizon"] == h)])
            print(f"  {cat:<11} h={h:>2}d  n={n}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = run_all_regressions(panel)
    apply_fdr(results)
    verdict = evaluate_verdict(results)

    today = date.today()
    report_path = RESULTS_DIR / f"test2_{today.isoformat()}.md"
    panel_info = {
        "Window": f"{WINDOW_START} → {WINDOW_END}",
        "News articles": f"{news['news_id'].nunique()}",
        "FinBERT scores": f"{len(scores)}",
        "Daily panel rows": f"{len(panel)}",
        "Tickers": ", ".join(ALL_TICKERS),
        "Horizons": ", ".join(f"{h}d" for h in HORIZONS),
    }
    write_report(results, verdict, panel_info, today, report_path)

    print(f"\n[test2] verdict: {_VERDICT_LABELS[verdict.verdict]}")
    print(f"[test2]   surviving FDR: {verdict.n_surviving}/{len(results)}")
    print(f"[test2]   economic: {verdict.n_economic}")
    print(f"[test2]   directionally consistent: {verdict.directionally_consistent}")
    print(f"[test2]   report: {report_path}")
    return verdict


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
