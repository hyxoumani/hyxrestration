"""Phase 0 §2.10 — expanded grid driver (horizons × aggregators on Qwen scores).

Final sensitivity test pre-registered in phase0_testing.md §2.10. Two
expansions run with family-wide BH-FDR and an elevated 50 bps economic bar:

  §2.10-A Test 2-standalone expanded:
    3 categories × 5 horizons (1/3/5/10/20d) × 3 aggregators = 45 cells.

  §2.10-B Test 2+3 combined expanded:
    36 base cells × 3 aggregators = 108 cells.

Pre-registered commitment (§2.10.2): a null outcome binds the architectural
decision to the §2.5 pivot (or Options B/C). No further sensitivity tests
after this one without a [pre-registration-violation] commit.

Run:
    python -m phase0.test210_expanded_grid

Outputs:
    phase0/results/test210_expanded_{today}.md
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.stats.multitest import multipletests

from phase0 import test2_sentiment_standalone as t2
from phase0 import test23_wasde_sentiment as t23
from phase0.aggregators import AGGREGATORS
from phase0.data_loaders import PHASE0_DIR, load_adj_close
from phase0.events import build_event_panel
from phase0.news_loader import NEWS_CSV
from phase0.sentiment_qwen import SCORES_CSV as QWEN_SCORES_CSV
from phase0.surprise import compute_trend_residual
from phase0.wasde_loader import load_wasde

# -------------------------------------------- pre-registered (§2.10)

T2A_HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20)
T2A_CATEGORIES = t2.TICKER_CATEGORIES

T23B_CATEGORIES = t23.TICKER_CATEGORIES
T23B_HORIZONS = t23.HORIZONS  # 5, 10
T23B_DIRECTIONS = t23.DIRECTIONS
T23B_LINE_ITEMS = t23.LINE_ITEMS

FDR_Q = 0.10
ECONOMIC_MAGNITUDE_BPS = 50  # elevated from 30 in §2.10.5

RESULTS_DIR = PHASE0_DIR / "results"
BETA_LOOKBACK_DAYS = 252
WINDOW_START = "2021-01-01"
WINDOW_END = "2024-12-31"

T2_ALL_TICKERS = tuple(t for grp in T2A_CATEGORIES.values() for t in grp)
T23_ALL_TICKERS = tuple(t for grp in T23B_CATEGORIES.values() for t in grp)


# -------------------------------------------- §2.10-A: Test 2-standalone expanded


@dataclass
class T2ACell:
    aggregator: str
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


def _run_t2a_cell(sub: pd.DataFrame, horizon: int) -> tuple[dict, int]:
    if len(sub) < 30:
        return _empty_t2a(), len(sub)
    df = sub.dropna(subset=["excess_return", "sentiment"]).copy()
    if len(df) < 30:
        return _empty_t2a(), len(df)
    y = df["excess_return"].to_numpy()
    X = df[["sentiment"]].to_numpy()
    X = sm.add_constant(X, prepend=True, has_constant="add")
    try:
        model = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": max(1, horizon)})
    except Exception:
        return _empty_t2a(), len(df)
    sigma = df["sentiment"].std(ddof=0) or 1e-12
    return (
        {
            "beta": float(model.params[1]),
            "se": float(model.bse[1]),
            "t_stat": float(model.tvalues[1]),
            "p_value": float(model.pvalues[1]),
            "implied_bps_at_1sigma": float(model.params[1] * sigma * 10_000),
        },
        len(df),
    )


def _empty_t2a() -> dict:
    return {
        "beta": float("nan"),
        "se": float("nan"),
        "t_stat": float("nan"),
        "p_value": float("nan"),
        "implied_bps_at_1sigma": float("nan"),
    }


def run_t2a_family(news: pd.DataFrame, scores: pd.DataFrame, prices: pd.DataFrame) -> list[T2ACell]:
    results: list[T2ACell] = []
    for agg_name, agg_fn in AGGREGATORS.items():
        daily_sent = agg_fn(news, scores)
        panel = t2.build_daily_panel(daily_sent, prices, T2_ALL_TICKERS, T2A_HORIZONS)
        panel = panel[(panel["date"] >= WINDOW_START) & (panel["date"] <= WINDOW_END)]
        for cat, tickers in T2A_CATEGORIES.items():
            for h in T2A_HORIZONS:
                sub = panel[(panel["ticker"].isin(tickers)) & (panel["horizon"] == h)]
                coeffs, n = _run_t2a_cell(sub, h)
                results.append(
                    T2ACell(aggregator=agg_name, category=cat, horizon=h, n_obs=n, **coeffs)
                )
    return results


# -------------------------------------------- §2.10-B: Test 2+3 combined expanded


@dataclass
class T23BCell:
    aggregator: str
    category: str
    direction: str
    horizon: int
    line_item: str
    n_obs: int
    beta_interaction: float
    t_interaction: float
    p_interaction: float
    beta_surprise: float
    p_surprise: float
    beta_sentiment: float
    p_sentiment: float
    implied_bps_at_1sigma: float
    p_fdr: float = float("nan")
    survives_fdr: bool = False


def run_t23b_family(
    news: pd.DataFrame,
    scores: pd.DataFrame,
    prices: pd.DataFrame,
    surprises: pd.DataFrame,
) -> list[T23BCell]:
    results: list[T23BCell] = []
    for agg_name, agg_fn in AGGREGATORS.items():
        daily_sent = agg_fn(news, scores)
        event_panel = build_event_panel(
            wasde_surprises=surprises,
            prices=prices,
            daily_sentiment=daily_sent,
            tickers=T23_ALL_TICKERS,
        )
        for cat, tickers in T23B_CATEGORIES.items():
            for direction in T23B_DIRECTIONS:
                for horizon in T23B_HORIZONS:
                    for line in T23B_LINE_ITEMS:
                        sub = event_panel[
                            (event_panel["ticker"].isin(tickers))
                            & (event_panel["direction"] == direction)
                            & (event_panel["horizon"] == horizon)
                            & (event_panel["line_item"] == line)
                        ]
                        coeffs, n = t23._run_one_regression(sub)
                        results.append(
                            T23BCell(
                                aggregator=agg_name,
                                category=cat,
                                direction=direction,
                                horizon=horizon,
                                line_item=line,
                                n_obs=n,
                                beta_interaction=coeffs["beta_interaction"],
                                t_interaction=coeffs["t_interaction"],
                                p_interaction=coeffs["p_interaction"],
                                beta_surprise=coeffs["beta_surprise"],
                                p_surprise=coeffs["p_surprise"],
                                beta_sentiment=coeffs["beta_sentiment"],
                                p_sentiment=coeffs["p_sentiment"],
                                implied_bps_at_1sigma=coeffs["implied_bps_at_1sigma"],
                            )
                        )
    return results


# -------------------------------------------- FDR + verdict


def apply_fdr(results: list, pval_attr: str, q: float = FDR_Q) -> None:
    idx_valid = [i for i, r in enumerate(results) if not np.isnan(getattr(r, pval_attr))]
    if not idx_valid:
        return
    pvals = [getattr(results[i], pval_attr) for i in idx_valid]
    rejected, pvals_corrected, _, _ = multipletests(pvals, alpha=q, method="fdr_bh")
    for local_idx, global_idx in enumerate(idx_valid):
        results[global_idx].p_fdr = float(pvals_corrected[local_idx])
        results[global_idx].survives_fdr = bool(rejected[local_idx])


@dataclass
class FamilyVerdict:
    family: str
    n_cells: int
    n_surviving: int
    n_economic: int
    directionally_consistent: bool
    passes_c1: bool
    passes_c2: bool
    passes_c3: bool
    verdict: str = ""
    surviving: list = field(default_factory=list)
    inconsistent_keys: list = field(default_factory=list)


def evaluate_t2a(results: list[T2ACell]) -> FamilyVerdict:
    surviving = [r for r in results if r.survives_fdr]
    n_surviving = len(surviving)

    # §2.10.5 criterion 2: signs within a category must agree across all surviving cells.
    inconsistent: list[str] = []
    by_cat: dict[str, list[T2ACell]] = {}
    for r in surviving:
        by_cat.setdefault(r.category, []).append(r)
    for cat, cells in by_cat.items():
        if len(cells) >= 2:
            signs = {int(np.sign(c.beta)) for c in cells if c.beta != 0}
            if len(signs) > 1:
                inconsistent.append(cat)

    n_economic = sum(1 for r in surviving if abs(r.implied_bps_at_1sigma) >= ECONOMIC_MAGNITUDE_BPS)

    c1 = n_surviving >= 1
    c2 = len(inconsistent) == 0
    c3 = n_economic >= 1
    verdict = "pass" if (c1 and c2 and c3) else ("partial" if n_surviving >= 1 else "fail")

    return FamilyVerdict(
        family="§2.10-A",
        n_cells=len(results),
        n_surviving=n_surviving,
        n_economic=n_economic,
        directionally_consistent=c2,
        passes_c1=c1,
        passes_c2=c2,
        passes_c3=c3,
        verdict=verdict,
        surviving=surviving,
        inconsistent_keys=inconsistent,
    )


def evaluate_t23b(results: list[T23BCell]) -> FamilyVerdict:
    surviving = [r for r in results if r.survives_fdr]
    n_surviving = len(surviving)

    # Directional consistency: within a (category, horizon, line_item), upside and
    # downside surviving β_interaction should not have opposite signs — matches §2.4.
    inconsistent: list[str] = []
    by_triple: dict[tuple, dict] = {}
    for r in surviving:
        by_triple.setdefault((r.category, r.horizon, r.line_item, r.aggregator), {})[
            r.direction
        ] = r
    for key, ds in by_triple.items():
        if "upside" in ds and "downside" in ds:
            if np.sign(ds["upside"].beta_interaction) == -np.sign(ds["downside"].beta_interaction):
                inconsistent.append(str(key))

    n_economic = sum(1 for r in surviving if abs(r.implied_bps_at_1sigma) >= ECONOMIC_MAGNITUDE_BPS)

    c1 = n_surviving >= 1
    c2 = len(inconsistent) == 0
    c3 = n_economic >= 1
    verdict = "pass" if (c1 and c2 and c3) else ("partial" if n_surviving >= 1 else "fail")

    return FamilyVerdict(
        family="§2.10-B",
        n_cells=len(results),
        n_surviving=n_surviving,
        n_economic=n_economic,
        directionally_consistent=c2,
        passes_c1=c1,
        passes_c2=c2,
        passes_c3=c3,
        verdict=verdict,
        surviving=surviving,
        inconsistent_keys=inconsistent,
    )


# -------------------------------------------- report


def _fmt(v: float, spec: str) -> str:
    return spec.format(v) if not (v is None or np.isnan(v)) else "—"


def _current_git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PHASE0_DIR.parent, stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def write_report(
    t2a_results: list[T2ACell],
    t23b_results: list[T23BCell],
    t2a_verdict: FamilyVerdict,
    t23b_verdict: FamilyVerdict,
    run_date: date_cls,
    report_path: Path,
) -> None:
    lines: list[str] = []
    lines.append("# Phase 0 §2.10 — expanded grid results")
    lines.append("")
    lines.append(f"**Run date:** {run_date.isoformat()}")
    lines.append(f"**Current commit SHA:** `{_current_git_sha()}`")
    lines.append("**Pre-registration:** locked in the commit that introduced §2.10 and this file.")
    lines.append("**Scorer:** Qwen 2.5 7B Instruct zero-shot (§2.9).")
    lines.append(f"**FDR:** Benjamini-Hochberg at q={FDR_Q}, family-wide.")
    lines.append(
        f"**Economic-magnitude threshold:** |bps at 1σ| ≥ {ECONOMIC_MAGNITUDE_BPS} (elevated)."
    )
    lines.append("")

    lines.append("## Joint outcome")
    lines.append("")
    lines.append(
        f"- §2.10-A Test 2-standalone expanded: **{t2a_verdict.verdict.upper()}** "
        f"({t2a_verdict.n_surviving}/{t2a_verdict.n_cells} surviving)"
    )
    lines.append(
        f"- §2.10-B Test 2+3 combined expanded: **{t23b_verdict.verdict.upper()}** "
        f"({t23b_verdict.n_surviving}/{t23b_verdict.n_cells} surviving)"
    )
    lines.append("")

    both_fail = t2a_verdict.verdict == "fail" and t23b_verdict.verdict == "fail"
    if both_fail:
        lines.append("### §2.10.2 commitment binds")
        lines.append("")
        lines.append(
            "Both expanded families null under Qwen × 3 aggregators × expanded horizons. "
            "Per the §2.10.2 pre-registered commitment: **the §2.5 architectural pivot "
            "(or Options B / C) executes immediately.** No further Test 2 / Test 2+3 "
            "sensitivity configurations will be added without a "
            "[pre-registration-violation] commit."
        )
    else:
        lines.append("### Non-null outcome")
        lines.append("")
        lines.append(
            "Surviving cells detected. Architectural-decision discussion required before "
            "locking the §2.5 pivot or alternatives. Review surviving cells below."
        )
    lines.append("")

    # ---------------- §2.10-A table ----------------
    lines.append(f"## §2.10-A — Test 2-standalone expanded (n={len(t2a_results)} cells)")
    lines.append("")
    lines.append(
        f"- Criterion 1 (≥1 surviving BH-FDR): {'✅' if t2a_verdict.passes_c1 else '❌'} "
        f"({t2a_verdict.n_surviving} surviving)"
    )
    lines.append(
        f"- Criterion 2 (directional consistency): {'✅' if t2a_verdict.passes_c2 else '❌'}"
    )
    lines.append(
        f"- Criterion 3 (≥1 economic ≥{ECONOMIC_MAGNITUDE_BPS} bps): "
        f"{'✅' if t2a_verdict.passes_c3 else '❌'} ({t2a_verdict.n_economic} economic)"
    )
    lines.append("")
    if t2a_verdict.surviving:
        lines.append("### §2.10-A surviving cells")
        lines.append("")
        lines.append("| aggregator | category | horizon | β | t | p_fdr | bps @ 1σ |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for r in t2a_verdict.surviving:
            lines.append(
                f"| {r.aggregator} | {r.category} | {r.horizon}d | "
                f"{r.beta:+.5f} | {r.t_stat:+.2f} | {r.p_fdr:.4f} | "
                f"{r.implied_bps_at_1sigma:+.1f} |"
            )
        lines.append("")

    lines.append("### §2.10-A full table (45 cells)")
    lines.append("")
    lines.append("| aggregator | category | horizon | n | β | t | p_raw | p_fdr | bps @ 1σ |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in t2a_results:
        lines.append(
            f"| {r.aggregator} | {r.category} | {r.horizon}d | {r.n_obs} | "
            f"{_fmt(r.beta, '{:+.5f}')} | {_fmt(r.t_stat, '{:+.2f}')} | "
            f"{_fmt(r.p_value, '{:.3f}')} | {_fmt(r.p_fdr, '{:.3f}')} | "
            f"{_fmt(r.implied_bps_at_1sigma, '{:+.1f}')} |"
        )
    lines.append("")

    # ---------------- §2.10-B table ----------------
    lines.append(f"## §2.10-B — Test 2+3 combined expanded (n={len(t23b_results)} cells)")
    lines.append("")
    lines.append(
        f"- Criterion 1 (≥1 surviving BH-FDR): {'✅' if t23b_verdict.passes_c1 else '❌'} "
        f"({t23b_verdict.n_surviving} surviving)"
    )
    lines.append(
        f"- Criterion 2 (directional consistency): {'✅' if t23b_verdict.passes_c2 else '❌'}"
    )
    lines.append(
        f"- Criterion 3 (≥1 economic ≥{ECONOMIC_MAGNITUDE_BPS} bps): "
        f"{'✅' if t23b_verdict.passes_c3 else '❌'} ({t23b_verdict.n_economic} economic)"
    )
    lines.append("")
    if t23b_verdict.surviving:
        lines.append("### §2.10-B surviving cells")
        lines.append("")
        lines.append(
            "| aggregator | category | direction | horizon | line_item | β_int | t | p_fdr | bps @ 1σ |"
        )
        lines.append("|---|---|---|---:|---|---:|---:|---:|---:|")
        for r in t23b_verdict.surviving:
            lines.append(
                f"| {r.aggregator} | {r.category} | {r.direction} | {r.horizon}d | "
                f"{r.line_item} | {r.beta_interaction:+.4f} | {r.t_interaction:+.2f} | "
                f"{r.p_fdr:.4f} | {r.implied_bps_at_1sigma:+.1f} |"
            )
        lines.append("")

    # Top-10 by raw p_interaction (diagnostic, not verdict-binding)
    top10 = sorted(
        [r for r in t23b_results if not np.isnan(r.p_interaction)],
        key=lambda r: r.p_interaction,
    )[:10]
    if top10:
        lines.append("### §2.10-B top-10 cells by raw p_interaction (diagnostic)")
        lines.append("")
        lines.append(
            "| aggregator | category | direction | horizon | line_item | β_int | p_raw | p_fdr | bps @ 1σ |"
        )
        lines.append("|---|---|---|---:|---|---:|---:|---:|---:|")
        for r in top10:
            lines.append(
                f"| {r.aggregator} | {r.category} | {r.direction} | {r.horizon}d | "
                f"{r.line_item} | {_fmt(r.beta_interaction, '{:+.4f}')} | "
                f"{_fmt(r.p_interaction, '{:.3f}')} | {_fmt(r.p_fdr, '{:.3f}')} | "
                f"{_fmt(r.implied_bps_at_1sigma, '{:+.1f}')} |"
            )
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- **Single scorer (Qwen).** §2.10.1 deliberately omits FinBERT — the §2.9 "
        "Qwen A/B already established instrument-level agreement on the null."
    )
    lines.append("- **CNH excluded via §7.1** (same as §2.9).")
    lines.append(
        "- **Family-wide FDR.** BH correction is applied to each family (§2.10-A: 45 "
        "cells; §2.10-B: 108 cells) separately, not across both jointly. Each family "
        "tests a different hypothesis (main-effect sentiment vs cross-modal interaction) "
        "so joint correction would mis-represent the test structure."
    )
    lines.append(
        "- **Economic bar elevated to 50 bps** from §2.4/§2.8.5's 30 bps, per §2.10.5. "
        "Deliberately stricter in multiple-testing territory."
    )

    report_path.write_text("\n".join(lines))


# -------------------------------------------- entrypoint


def run() -> tuple[FamilyVerdict, FamilyVerdict]:
    if not NEWS_CSV.exists():
        raise SystemExit(f"{NEWS_CSV} missing — run phase0.test23_real_driver first")
    if not QWEN_SCORES_CSV.exists():
        raise SystemExit(f"{QWEN_SCORES_CSV} missing — run phase0.test29_qwen_ab first")

    news = pd.read_csv(NEWS_CSV, parse_dates=["timestamp"])
    scores = pd.read_csv(QWEN_SCORES_CSV)
    print(f"[t210] news: {len(news)} rows ({news['news_id'].nunique()} articles)")
    print(f"[t210] Qwen scores: {len(scores)} rows")

    all_needed = tuple({*T2_ALL_TICKERS, *T23_ALL_TICKERS, "SPY"})
    prices = load_adj_close(all_needed, start="2014-01-01", end="2025-01-01").sort_index()
    print(f"[t210] prices: {len(prices)} trading days")

    # WASDE + surprises — same window as §2.9
    wasde = load_wasde()
    surprises = compute_trend_residual(wasde).dropna(subset=["surprise"])
    surprises = surprises[
        (surprises["release_date"] >= "2021-01-01") & (surprises["release_date"] <= "2024-12-31")
    ]
    print(f"[t210] WASDE surprises in window: {len(surprises)}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print("\n=== §2.10-A Test 2-standalone expanded (45 cells) ===")
    t2a_results = run_t2a_family(news, scores, prices)
    apply_fdr(t2a_results, "p_value")
    t2a_verdict = evaluate_t2a(t2a_results)
    print(
        f"[t2a] verdict={t2a_verdict.verdict}  surviving={t2a_verdict.n_surviving}/{t2a_verdict.n_cells}"
    )

    print("\n=== §2.10-B Test 2+3 combined expanded (108 cells) ===")
    t23b_results = run_t23b_family(news, scores, prices, surprises)
    apply_fdr(t23b_results, "p_interaction")
    t23b_verdict = evaluate_t23b(t23b_results)
    print(
        f"[t23b] verdict={t23b_verdict.verdict}  surviving={t23b_verdict.n_surviving}/{t23b_verdict.n_cells}"
    )

    today = date_cls.today()
    report_path = RESULTS_DIR / f"test210_expanded_{today.isoformat()}.md"
    write_report(t2a_results, t23b_results, t2a_verdict, t23b_verdict, today, report_path)
    print(f"\n[t210] report: {report_path}")
    return t2a_verdict, t23b_verdict


def main() -> int:
    t2a_verdict, t23b_verdict = run()
    both_fail = t2a_verdict.verdict == "fail" and t23b_verdict.verdict == "fail"
    print()
    if both_fail:
        print("=== §2.10.2 commitment binds: §2.5 pivot (or B/C) executes. ===")
    else:
        print("=== Non-null outcome — architectural discussion required. ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
