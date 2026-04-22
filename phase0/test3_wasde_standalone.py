"""Phase 0 — Test 3 standalone: does WASDE surprise alone predict forward returns?

Spec: phase0_testing.md §2.7. Pre-registration locked in the commit that introduces
this file. Tests ONE component of L01 — the surprise-predictability of public USDA
data at swing horizons — WITHOUT the sentiment arm. Does NOT test cross-modal
integration; that still requires the combined Test 2+3 once news unblocks.

Regression per (category, direction, horizon, line_item):
    excess_return ~ α + β_surprise · |surprise| + ε   (HC3 SEs)

36 regressions total (3 × 2 × 2 × 3). BH-FDR correction at q=0.10 on the 36
β_surprise p-values. Pass criteria per §2.7.5.

Run:
    python -m phase0.test3_wasde_standalone            # real WASDE + real prices
    python -m phase0.test3_wasde_standalone --dry-run  # load data + print diagnostics, no regression

Outputs:
    phase0/results/test3_{today}.md
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.stats.multitest import multipletests

from phase0.data_loaders import PHASE0_DIR, load_adj_close
from phase0.events import (
    _next_trading_day,
    _offset_trading_day,
    rolling_beta,
)
from phase0.surprise import compute_trend_residual
from phase0.wasde_loader import load_wasde

# -------------------------------------------- pre-registered (§2.7)

TICKER_CATEGORIES: dict[str, tuple[str, ...]] = {
    "fertilizer": ("NTR", "MOS", "CF"),
    "equipment": ("DE", "AGCO", "CNH"),
    "processors": ("ADM", "BG"),
}
DIRECTIONS: tuple[str, ...] = ("upside", "downside")  # neutral excluded
HORIZONS: tuple[int, ...] = (5, 10)
LINE_ITEMS: tuple[str, ...] = ("production", "ending_stocks", "yield")

FDR_Q = 0.10
ECONOMIC_MAGNITUDE_BPS = 30

RESULTS_DIR = PHASE0_DIR / "results"
BETA_LOOKBACK_DAYS = 252

ALL_TICKERS: tuple[str, ...] = tuple(t for grp in TICKER_CATEGORIES.values() for t in grp)


# -------------------------------------------- event panel (no sentiment)


def build_event_panel_no_sentiment(
    wasde_surprises: pd.DataFrame,
    prices: pd.DataFrame,
    tickers: tuple[str, ...] = ALL_TICKERS,
    horizons: tuple[int, ...] = HORIZONS,
    beta_lookback: int = BETA_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """Event panel WITHOUT sentiment columns.

    Mirrors events.build_event_panel but drops all sentiment machinery. Same
    beta alignment, same non-look-ahead guarantees — the sentiment-free
    construction is the ONLY difference.
    """
    if "SPY" not in prices.columns:
        raise ValueError("prices panel must include SPY for beta adjustment")

    trading_index = prices.index
    asset_returns = prices.pct_change()
    spy_rets = asset_returns["SPY"]
    betas: dict[str, pd.Series] = {
        t: rolling_beta(asset_returns[t], spy_rets, beta_lookback) for t in tickers
    }

    rows: list[dict[str, object]] = []
    for _, w in wasde_surprises.iterrows():
        release = pd.Timestamp(w["release_date"]).normalize()
        D = _next_trading_day(release, trading_index)
        if D is None:
            continue
        for tkr in tickers:
            if tkr not in betas:
                continue
            beta_at_D = betas[tkr].loc[D] if D in betas[tkr].index else np.nan
            for h in horizons:
                D_plus_h = _offset_trading_day(D, h, trading_index)
                if D_plus_h is None:
                    continue
                try:
                    t_ret = float(prices[tkr].loc[D_plus_h] / prices[tkr].loc[D] - 1)
                    s_ret = float(prices["SPY"].loc[D_plus_h] / prices["SPY"].loc[D] - 1)
                except KeyError:
                    continue
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
                        "horizon": h,
                        "forward_return": t_ret,
                        "spy_return": s_ret,
                        "beta": float(beta_at_D) if not np.isnan(beta_at_D) else np.nan,
                        "excess_return": excess,
                    }
                )
    return pd.DataFrame(rows)


# -------------------------------------------- regression


@dataclass
class CellResult:
    category: str
    direction: str
    horizon: int
    line_item: str
    n_obs: int
    beta_surprise: float
    se_surprise: float
    t_surprise: float
    p_surprise: float
    implied_bps_at_1sigma: float
    p_fdr: float = float("nan")
    survives_fdr: bool = False


def _empty_coeffs() -> dict[str, float]:
    return {
        "beta_surprise": float("nan"),
        "se_surprise": float("nan"),
        "t_surprise": float("nan"),
        "p_surprise": float("nan"),
        "implied_bps_at_1sigma": float("nan"),
    }


def _run_one_regression(sub: pd.DataFrame) -> tuple[dict[str, float], int]:
    """OLS with HC3 robust SEs on excess_return ~ |surprise| + ε."""
    if len(sub) < 10:
        return _empty_coeffs(), len(sub)
    df = sub.dropna(subset=["excess_return", "surprise"]).copy()
    if len(df) < 10:
        return _empty_coeffs(), len(df)
    df["abs_surprise"] = df["surprise"].abs()
    y = df["excess_return"].to_numpy()
    X = df[["abs_surprise"]].to_numpy()
    X = sm.add_constant(X, prepend=True, has_constant="add")
    try:
        model = sm.OLS(y, X).fit(cov_type="HC3")
    except Exception:
        return _empty_coeffs(), len(df)
    sigma_abs = df["abs_surprise"].std(ddof=0) or 1e-12
    implied_bps = model.params[1] * sigma_abs * 10_000
    return (
        {
            "beta_surprise": float(model.params[1]),
            "se_surprise": float(model.bse[1]),
            "t_surprise": float(model.tvalues[1]),
            "p_surprise": float(model.pvalues[1]),
            "implied_bps_at_1sigma": float(implied_bps),
        },
        len(df),
    )


def run_all_regressions(event_panel: pd.DataFrame) -> list[CellResult]:
    results: list[CellResult] = []
    for cat, tickers in TICKER_CATEGORIES.items():
        for direction in DIRECTIONS:
            for horizon in HORIZONS:
                for line in LINE_ITEMS:
                    sub = event_panel[
                        (event_panel["ticker"].isin(tickers))
                        & (event_panel["direction"] == direction)
                        & (event_panel["horizon"] == horizon)
                        & (event_panel["line_item"] == line)
                    ]
                    coeffs, n = _run_one_regression(sub)
                    results.append(
                        CellResult(
                            category=cat,
                            direction=direction,
                            horizon=horizon,
                            line_item=line,
                            n_obs=n,
                            **coeffs,
                        )
                    )
    return results


# -------------------------------------------- FDR + verdict


def apply_fdr(results: list[CellResult], q: float = FDR_Q) -> list[CellResult]:
    idx_valid = [i for i, r in enumerate(results) if not np.isnan(r.p_surprise)]
    if not idx_valid:
        return results
    pvals = [results[i].p_surprise for i in idx_valid]
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
    inconsistent_triples: list[tuple[str, int, str]] = field(default_factory=list)


def evaluate_verdict(results: list[CellResult]) -> TestVerdict:
    surviving = [r for r in results if r.survives_fdr]
    n_surviving = len(surviving)

    # §2.7.5 criterion 2: within the same (category, horizon, line_item), if both
    # upside AND downside directions produce surviving β_surprise, they must have
    # OPPOSITE signs on |surprise|. Same-signed βs imply a volatility response
    # (any surprise → returns same direction), not a directional mechanism.
    inconsistent: list[tuple[str, int, str]] = []
    by_triple: dict[tuple[str, int, str], dict[str, CellResult]] = {}
    for r in surviving:
        by_triple.setdefault((r.category, r.horizon, r.line_item), {})[r.direction] = r
    for key, ds in by_triple.items():
        if "upside" in ds and "downside" in ds:
            if np.sign(ds["upside"].beta_surprise) == np.sign(ds["downside"].beta_surprise):
                inconsistent.append(key)
    directionally_consistent = len(inconsistent) == 0

    n_economic = sum(1 for r in surviving if abs(r.implied_bps_at_1sigma) >= ECONOMIC_MAGNITUDE_BPS)

    c1 = n_surviving >= 3
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
        inconsistent_triples=inconsistent,
    )


# -------------------------------------------- report


_VERDICT_LABELS = {
    "pass": "PASS",
    "fail": "FAIL",
    "partial": "PARTIAL (explicitly NOT a pass per §2.7.5)",
}

_VERDICT_INTERP = {
    "pass": (
        "WASDE surprises are unpriced at 5d/10d ag-equity swing horizons — a tradable "
        "single-modal signal exists without text/sentiment. **This does NOT validate L01's "
        "cross-modal claim**; it only shows one of L01's inputs contributes on its own. "
        "Two decision branches, to be discussed before locking:\n\n"
        "  (a) **Pivot.** Ship the simpler WASDE-event capture system. The LLM/sentiment "
        "layer becomes a secondary 'does it add marginal value?' question, not the core "
        "edge. Architecture collapses.\n\n"
        "  (b) **Still run Test 2+3 later** when a news source unblocks, to answer whether "
        "cross-modal adds anything beyond the main effect. L01 validation still requires "
        "that answer."
    ),
    "fail": (
        "WASDE surprises are fully arbitraged at swing horizons — no standalone signal in "
        "public USDA releases at 5d/10d. This **weakens** one pillar of L01 but does NOT "
        "conclusively falsify the cross-modal claim; a sentiment × surprise interaction "
        "could still exist even when main effects don't. Combined with Test 1's fail, "
        "this is §4's 'Test 1 fail + Test 2+3 fail → kill trading-P&L goal' branch, "
        "with the caveat that Test 2+3-proper hasn't actually been run — so technically "
        "this is a stronger case for the kill path but not a full §4 verdict."
    ),
    "partial": (
        "1–2 surviving combinations, or magnitude below 30 bps, or sign-inconsistent on "
        "criterion 2. Pre-registration (§2.7.5) explicitly treats this as NOT a pass — "
        "it's the easiest outcome to rationalize post-hoc. Treat as fail for decision "
        "purposes. Do not soft-pass, do not cherry-pick surviving cells."
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
    lines.append("# Phase 0 — Test 3 standalone: WASDE surprise → forward returns")
    lines.append("")
    lines.append(f"**Run date:** {run_date.isoformat()}")
    lines.append(f"**Current commit SHA:** `{_current_git_sha()}`")
    lines.append("**Pre-registration:** locked in the commit that introduced §2.7 and this file.")
    lines.append(
        f"**Regressions:** {len(results)} (3 categories × 2 directions × 2 horizons × 3 line items)"
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
        f"- Criterion 1 (≥3 surviving BH-FDR): "
        f"{'✅' if verdict.passes_c1 else '❌'} ({verdict.n_surviving} surviving)"
    )
    lines.append(
        f"- Criterion 2 (directional consistency): "
        f"{'✅' if verdict.passes_c2 else '❌'}"
        + (
            f" ({len(verdict.inconsistent_triples)} inconsistent triples)"
            if verdict.inconsistent_triples
            else ""
        )
    )
    lines.append(
        f"- Criterion 3 (≥1 survivor with |bps| ≥ {ECONOMIC_MAGNITUDE_BPS}): "
        f"{'✅' if verdict.passes_c3 else '❌'} ({verdict.n_economic} economic)"
    )
    lines.append("")

    lines.append("## Interpretation (per phase0_testing.md §2.7.6)")
    lines.append("")
    lines.append(_VERDICT_INTERP[verdict.verdict])
    lines.append("")

    if verdict.inconsistent_triples:
        lines.append("## Directional-inconsistency flags")
        lines.append("")
        lines.append("| category | horizon | line_item |")
        lines.append("|---|---:|---|")
        for cat, h, line in verdict.inconsistent_triples:
            lines.append(f"| {cat} | {h}d | {line} |")
        lines.append("")

    if verdict.surviving:
        lines.append("## Surviving (category × direction × horizon × line_item)")
        lines.append("")
        lines.append(
            "| category | direction | horizon | line_item | β_surprise | t | p (FDR) | "
            "implied bps | economic? |"
        )
        lines.append("|---|---|---:|---|---:|---:|---:|---:|:--:|")
        for r in verdict.surviving:
            econ = "✅" if abs(r.implied_bps_at_1sigma) >= ECONOMIC_MAGNITUDE_BPS else "❌"
            lines.append(
                f"| {r.category} | {r.direction} | {r.horizon}d | {r.line_item} | "
                f"{r.beta_surprise:+.4f} | {r.t_surprise:+.2f} | "
                f"{r.p_fdr:.4f} | {r.implied_bps_at_1sigma:+.1f} | {econ} |"
            )
        lines.append("")

    lines.append("## Full regression table (all 36)")
    lines.append("")
    lines.append(
        "| category | direction | horizon | line_item | n | β_surprise | t | p_raw | "
        "p_fdr | implied bps |"
    )
    lines.append("|---|---|---:|---|---:|---:|---:|---:|---:|---:|")
    for r in results:
        lines.append(
            f"| {r.category} | {r.direction} | {r.horizon}d | {r.line_item} | {r.n_obs} | "
            f"{_fmt(r.beta_surprise, '{:+.4f}')} | {_fmt(r.t_surprise, '{:+.2f}')} | "
            f"{_fmt(r.p_surprise, '{:.3f}')} | {_fmt(r.p_fdr, '{:.3f}')} | "
            f"{_fmt(r.implied_bps_at_1sigma, '{:+.1f}')} |"
        )
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- **Surprise is a trend-residual proxy**, not true consensus (per "
        "phase0_data_sources.md §5.3). If the test passes, a Farmdoc-consensus "
        "sensitivity check is warranted before trading it; if it fails, a Farmdoc "
        "spot-check on ~10 releases confirms whether the proxy was the bottleneck."
    )
    lines.append(
        "- **Directional tercile boundaries are ex-post** (33/67 percentiles over the "
        "full 2014-2024 surprise distribution). A real-time deployment would use rolling "
        "tercile cutoffs."
    )
    lines.append(
        "- **Cross-modal claim is not tested here.** A fail here weakens but does NOT "
        "falsify L01. A pass here validates one main effect, not the LLM-specific "
        "integration claim."
    )

    report_path.write_text("\n".join(lines))


# -------------------------------------------- entrypoint


def run(dry_run: bool = False) -> TestVerdict | None:
    # Load WASDE + prices
    wasde = load_wasde()
    print(
        f"[test3] WASDE: {len(wasde)} rows, {wasde['release_date'].nunique()} releases, "
        f"{wasde['release_date'].min().date()} → {wasde['release_date'].max().date()}"
    )
    surprises = compute_trend_residual(wasde).dropna(subset=["surprise"])
    print(
        f"[test3] surprises: {len(surprises)} rows defined "
        f"(terciles: {surprises['direction'].value_counts().to_dict()})"
    )

    all_needed = tuple({*ALL_TICKERS, "SPY"})
    prices = load_adj_close(all_needed, start="2014-01-01", end="2025-01-01").sort_index()
    print(
        f"[test3] prices: {len(prices)} trading days, "
        f"{prices.index.min().date()} → {prices.index.max().date()}, "
        f"{len(prices.columns)} columns"
    )

    panel = build_event_panel_no_sentiment(surprises, prices)
    print(
        f"[test3] event panel: {len(panel)} rows "
        f"({panel['ticker'].nunique()} tickers × {panel['release_date'].nunique()} events × "
        f"{panel['horizon'].nunique()} horizons)"
    )
    valid = panel.dropna(subset=["excess_return"])
    print(f"[test3] panel rows with excess_return defined: {len(valid)} / {len(panel)}")

    # Cell-size diagnostic
    sizes = panel.groupby(["direction", "horizon", "line_item"]).size().unstack("line_item")
    print("[test3] rows per (direction, horizon, line_item), across 8 regression tickers:")
    print(sizes.to_string())

    if dry_run:
        print("[test3] dry-run — skipping regression")
        return None

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = run_all_regressions(panel)
    apply_fdr(results)
    verdict = evaluate_verdict(results)

    today = date.today()
    report_path = RESULTS_DIR / f"test3_{today.isoformat()}.md"
    panel_info = {
        "WASDE releases": (
            f"{wasde['release_date'].nunique()} "
            f"({wasde['release_date'].min().date()} → {wasde['release_date'].max().date()})"
        ),
        "Defined surprises": str(len(surprises)),
        "Event-panel rows": str(len(panel)),
        "Rows with excess_return": str(len(valid)),
        "Tickers (regression)": ", ".join(ALL_TICKERS),
        "Horizons": ", ".join(f"{h}d" for h in HORIZONS),
    }
    write_report(results, verdict, panel_info, today, report_path)

    print(f"\n[test3] verdict: {_VERDICT_LABELS[verdict.verdict]}")
    print(f"[test3]   surviving FDR: {verdict.n_surviving}/{len(results)}")
    print(f"[test3]   economic: {verdict.n_economic}")
    print(f"[test3]   directionally consistent: {verdict.directionally_consistent}")
    if verdict.inconsistent_triples:
        print(f"[test3]   inconsistent triples: {verdict.inconsistent_triples}")
    print(f"[test3]   report: {report_path}")
    return verdict


def main() -> int:
    p = argparse.ArgumentParser(prog="test3_wasde_standalone")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Load + diagnose data but skip the regression",
    )
    args = p.parse_args()
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
