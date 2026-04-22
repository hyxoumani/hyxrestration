"""Phase 0 — Test 2+3: WASDE-conditional sentiment predicts forward returns.

Spec: phase0_testing.md §2. Combined test replacing the originally-separate
Test 2 (sentiment alone) and Test 3 (WASDE alone). The load-bearing claim
is the `β_interaction` coefficient on (surprise × sentiment) — not main-effect
sentiment, which would be indistinguishable from generic sentiment-trading.

Regression per (category, direction, horizon, line_item):
    excess_return ~ α + β_surprise·|surprise|
                      + β_sentiment·sentiment
                      + β_interaction·(surprise · sentiment)
                      + ε

36 regressions total (3 × 2 × 2 × 3). BH-FDR correction at q=0.10 on the
36 β_interaction p-values. Pass criteria per §2.4.

Run:
    python -m phase0.test23_wasde_sentiment
Outputs:
    phase0/results/test23_{today}.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.stats.multitest import multipletests

from phase0.data_loaders import PHASE0_DIR

# ---------------------------------------------------------------- pre-registered

TICKER_CATEGORIES: dict[str, tuple[str, ...]] = {
    "fertilizer": ("NTR", "MOS", "CF"),
    "equipment": ("DE", "AGCO", "CNH"),
    "processors": ("ADM", "BG"),
}
DIRECTIONS: tuple[str, ...] = ("upside", "downside")  # neutral excluded
HORIZONS: tuple[int, ...] = (5, 10)
LINE_ITEMS: tuple[str, ...] = ("production", "ending_stocks", "yield")

FDR_Q = 0.10
ECONOMIC_MAGNITUDE_BPS = 30  # min tradeable effect (1σ×1σ)

RESULTS_DIR = PHASE0_DIR / "results"


# ---------------------------------------------------------------- regression


@dataclass
class CellResult:
    category: str
    direction: str
    horizon: int
    line_item: str

    n_obs: int
    beta_interaction: float
    se_interaction: float
    t_interaction: float
    p_interaction: float
    beta_sentiment: float
    p_sentiment: float
    beta_surprise: float
    p_surprise: float

    # Economic magnitude in bps: β_interaction × 1σ(surprise) × 1σ(sentiment)
    implied_bps_at_1sigma: float

    # FDR outcome, populated later
    p_fdr: float = float("nan")
    survives_fdr: bool = False


def _run_one_regression(sub: pd.DataFrame) -> tuple[dict[str, float], int]:
    """OLS with HC3 robust SEs on excess_return ~ |surprise| + sentiment + surprise×sentiment.

    Returns (coefficients dict, n_observations). NaN everywhere if <10 obs
    or degenerate design matrix.
    """
    if len(sub) < 10:
        return _empty_coeffs(), len(sub)

    df = sub.dropna(subset=["excess_return", "surprise", "event_sentiment"]).copy()
    if len(df) < 10:
        return _empty_coeffs(), len(df)

    df["abs_surprise"] = df["surprise"].abs()
    df["interaction"] = df["surprise"] * df["event_sentiment"]

    y = df["excess_return"].to_numpy()
    X = df[["abs_surprise", "event_sentiment", "interaction"]].to_numpy()
    X = sm.add_constant(X, prepend=True, has_constant="add")

    try:
        model = sm.OLS(y, X).fit(cov_type="HC3")
    except Exception:
        return _empty_coeffs(), len(df)

    # Params: [const, abs_surprise, sentiment, interaction]
    sigma_interaction = df["interaction"].std(ddof=0) or 1e-12
    sigma_inputs_1x1 = df["surprise"].std(ddof=0) * df["event_sentiment"].std(ddof=0)
    implied_bps = model.params[3] * sigma_inputs_1x1 * 10_000  # bps

    return (
        {
            "beta_surprise": float(model.params[1]),
            "p_surprise": float(model.pvalues[1]),
            "beta_sentiment": float(model.params[2]),
            "p_sentiment": float(model.pvalues[2]),
            "beta_interaction": float(model.params[3]),
            "se_interaction": float(model.bse[3]),
            "t_interaction": float(model.tvalues[3]),
            "p_interaction": float(model.pvalues[3]),
            "implied_bps_at_1sigma": float(implied_bps),
            "_sigma_interaction": float(sigma_interaction),
        },
        len(df),
    )


def _empty_coeffs() -> dict[str, float]:
    return {
        "beta_surprise": float("nan"),
        "p_surprise": float("nan"),
        "beta_sentiment": float("nan"),
        "p_sentiment": float("nan"),
        "beta_interaction": float("nan"),
        "se_interaction": float("nan"),
        "t_interaction": float("nan"),
        "p_interaction": float("nan"),
        "implied_bps_at_1sigma": float("nan"),
        "_sigma_interaction": float("nan"),
    }


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
                            beta_interaction=coeffs["beta_interaction"],
                            se_interaction=coeffs["se_interaction"],
                            t_interaction=coeffs["t_interaction"],
                            p_interaction=coeffs["p_interaction"],
                            beta_sentiment=coeffs["beta_sentiment"],
                            p_sentiment=coeffs["p_sentiment"],
                            beta_surprise=coeffs["beta_surprise"],
                            p_surprise=coeffs["p_surprise"],
                            implied_bps_at_1sigma=coeffs["implied_bps_at_1sigma"],
                        )
                    )
    return results


# ---------------------------------------------------------------- FDR + verdict


def apply_fdr(results: list[CellResult], q: float = FDR_Q) -> list[CellResult]:
    """BH-FDR correction on β_interaction p-values. Mutates results in-place."""
    idx_valid = [i for i, r in enumerate(results) if not np.isnan(r.p_interaction)]
    if not idx_valid:
        return results
    pvals = [results[i].p_interaction for i in idx_valid]
    rejected, pvals_corrected, _, _ = multipletests(pvals, alpha=q, method="fdr_bh")
    for local_idx, global_idx in enumerate(idx_valid):
        results[global_idx].p_fdr = float(pvals_corrected[local_idx])
        results[global_idx].survives_fdr = bool(rejected[local_idx])
    return results


@dataclass
class TestVerdict:
    n_surviving: int
    n_economic: int  # surviving AND |implied_bps| >= threshold
    directionally_consistent: bool
    passes_criterion_1: bool  # ≥ 3 surviving FDR
    passes_criterion_2: bool  # directional consistency
    passes_criterion_3: bool  # ≥ 1 survivor with |implied_bps| ≥ 30
    verdict: str = ""  # joint_pass | sentiment_only_pass | wasde_only_pass | joint_fail | partial
    surviving: list[CellResult] = field(default_factory=list)


def evaluate_verdict(results: list[CellResult]) -> TestVerdict:
    surviving = [r for r in results if r.survives_fdr]
    n_surviving = len(surviving)

    # Directional consistency per §2.4: within the same (category, horizon, line_item),
    # the upside vs downside surviving interaction coefficients must not disagree in
    # sign about the underlying mechanism. Concretely: for a (cat, horizon, line) triple
    # where both upside+downside survive, the implied-direction of the trade shouldn't
    # flip inconsistently. We check that no (cat, horizon, line) has surviving
    # coefficients of strictly opposite sign for upside vs downside.
    inconsistent = set()
    by_triple: dict[tuple[str, int, str], dict[str, CellResult]] = {}
    for r in surviving:
        key = (r.category, r.horizon, r.line_item)
        by_triple.setdefault(key, {})[r.direction] = r
    for key, ds in by_triple.items():
        if (
            "upside" in ds
            and "downside" in ds
            and np.sign(ds["upside"].beta_interaction) == -np.sign(ds["downside"].beta_interaction)
        ):
            # Opposite-signed interactions on the same (cat, horizon, line_item)
            # triple flag an inconsistent mechanism — pre-registration strict.
            inconsistent.add(key)
    directionally_consistent = len(inconsistent) == 0

    n_economic = sum(1 for r in surviving if abs(r.implied_bps_at_1sigma) >= ECONOMIC_MAGNITUDE_BPS)

    c1 = n_surviving >= 3
    c2 = directionally_consistent
    c3 = n_economic >= 1

    # Diagnose the failure mode (for §2.5 interpretation).
    # We check main effects too, to separate sentiment-only / wasde-only passes.
    significant_sentiment = any(
        not np.isnan(r.p_sentiment) and r.p_sentiment < 0.05 for r in results
    )
    significant_surprise = any(not np.isnan(r.p_surprise) and r.p_surprise < 0.05 for r in results)

    if c1 and c2 and c3:
        verdict = "joint_pass"
    elif n_surviving >= 1:
        verdict = "partial_pass"  # explicitly NOT a pass per §2.4
    elif significant_sentiment and not significant_surprise:
        verdict = "sentiment_only_pass"
    elif significant_surprise and not significant_sentiment:
        verdict = "wasde_only_pass"
    else:
        verdict = "joint_fail"

    return TestVerdict(
        n_surviving=n_surviving,
        n_economic=n_economic,
        directionally_consistent=directionally_consistent,
        passes_criterion_1=c1,
        passes_criterion_2=c2,
        passes_criterion_3=c3,
        verdict=verdict,
        surviving=surviving,
    )


# ---------------------------------------------------------------- report


_VERDICT_LABELS = {
    "joint_pass": "JOINT PASS",
    "sentiment_only_pass": "SENTIMENT-ONLY PASS (NOT an L01 validation)",
    "wasde_only_pass": "WASDE-ONLY PASS (sentiment adds nothing)",
    "joint_fail": "JOINT FAIL",
    "partial_pass": "PARTIAL PASS (explicitly NOT a pass per §2.4)",
}

_VERDICT_INTERPRETATION = {
    "joint_pass": (
        "L01 validated in a specific, mechanistic, testable form. Architecture should "
        "collapse around WASDE-window event trading — narrower universe, simpler agent "
        "roster, stronger portfolio story. Rewrite L01 concretely around the mechanism."
    ),
    "sentiment_only_pass": (
        "Unexpected. Suggests sentiment effect is general, not event-driven. Warrants "
        "follow-up Test 2 isolating non-WASDE-window sentiment. This is NOT an L01 "
        "pass — pre-registration makes main-effect sentiment descriptive, not counting."
    ),
    "wasde_only_pass": (
        "Surprise predictive, sentiment adds nothing. Demote sentiment agent to "
        "context-only, not a weighted Quant Meta input. Sentiment LoRA (slice 5b) deleted. "
        "System becomes 'LLM-assisted WASDE surprise trading' — most of the LLM stack "
        "becomes decorative."
    ),
    "joint_fail": (
        "L01 falsified on this universe with this instrument. Architecture's central "
        "claim has no empirical basis. Per §4 decision rubric combined with Test 1's "
        "fail: KILL the trading-P&L goal. Reframe project as research / portfolio piece. "
        "Write up the negative result honestly — this is a stronger interview story "
        "than handwaved positive claims."
    ),
    "partial_pass": (
        "1–2 surviving combinations or magnitude below 30 bps threshold. Pre-registration "
        "explicitly treats this as NOT a pass (§2.4). Highest-risk outcome because it's "
        "the easiest to rationalize. Redesign or treat as fail."
    ),
}


def write_report(
    results: list[CellResult],
    verdict: TestVerdict,
    run_date: date,
    report_path: Path,
    caveats: list[str] | None = None,
) -> None:
    caveats = caveats or []
    lines: list[str] = []
    lines.append("# Phase 0 — Test 2+3: WASDE-conditional sentiment")
    lines.append("")
    lines.append(f"**Run date:** {run_date.isoformat()}")
    lines.append(
        f"**Regressions:** {len(results)} (3 categories × 2 directions × 2 horizons × 3 line items)"
    )
    lines.append(f"**FDR method:** Benjamini-Hochberg at q={FDR_Q}")
    lines.append(
        f"**Economic-magnitude threshold:** |implied bps at 1σ×1σ| ≥ {ECONOMIC_MAGNITUDE_BPS}"
    )
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    label = _VERDICT_LABELS[verdict.verdict]
    lines.append(f"- **Outcome: {label}**")
    lines.append(
        f"- Criterion 1 (≥ 3 surviving BH-FDR): "
        f"{'✅' if verdict.passes_criterion_1 else '❌'} "
        f"({verdict.n_surviving} surviving)"
    )
    lines.append(
        f"- Criterion 2 (directional consistency): {'✅' if verdict.passes_criterion_2 else '❌'}"
    )
    lines.append(
        f"- Criterion 3 (≥ 1 economic magnitude ≥ {ECONOMIC_MAGNITUDE_BPS} bps): "
        f"{'✅' if verdict.passes_criterion_3 else '❌'} "
        f"({verdict.n_economic} economic)"
    )
    lines.append("")

    lines.append("## Interpretation (per phase0_testing.md §2.5)")
    lines.append("")
    lines.append(_VERDICT_INTERPRETATION[verdict.verdict])
    lines.append("")

    if verdict.surviving:
        lines.append("## Surviving (category × direction × horizon × line_item)")
        lines.append("")
        lines.append(
            "| category | direction | horizon | line_item | β_int | t | p (FDR) | implied bps | economic? |"
        )
        lines.append("|---|---|---:|---|---:|---:|---:|---:|:--:|")
        for r in verdict.surviving:
            econ = "✅" if abs(r.implied_bps_at_1sigma) >= ECONOMIC_MAGNITUDE_BPS else "❌"
            lines.append(
                f"| {r.category} | {r.direction} | {r.horizon}d | {r.line_item} | "
                f"{r.beta_interaction:+.4f} | {r.t_interaction:+.2f} | "
                f"{r.p_fdr:.4f} | {r.implied_bps_at_1sigma:+.1f} | {econ} |"
            )
        lines.append("")

    lines.append("## Full regression table (all 36)")
    lines.append("")
    lines.append(
        "| category | direction | horizon | line_item | n | β_int | p_raw | p_fdr | β_sent | p_sent | β_surp | p_surp |"
    )
    lines.append("|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in results:
        lines.append(
            f"| {r.category} | {r.direction} | {r.horizon}d | {r.line_item} | {r.n_obs} | "
            f"{_fmt(r.beta_interaction, '{:+.4f}')} | {_fmt(r.p_interaction, '{:.3f}')} | "
            f"{_fmt(r.p_fdr, '{:.3f}')} | "
            f"{_fmt(r.beta_sentiment, '{:+.4f}')} | {_fmt(r.p_sentiment, '{:.3f}')} | "
            f"{_fmt(r.beta_surprise, '{:+.4f}')} | {_fmt(r.p_surprise, '{:.3f}')} |"
        )
    lines.append("")

    if caveats:
        lines.append("## Caveats")
        lines.append("")
        for c in caveats:
            lines.append(f"- {c}")
        lines.append("")

    report_path.write_text("\n".join(lines))


def _fmt(v: float, spec: str) -> str:
    return spec.format(v) if not (v is None or np.isnan(v)) else "—"


# ---------------------------------------------------------------- entrypoint


def run(event_panel: pd.DataFrame, caveats: list[str] | None = None) -> TestVerdict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = run_all_regressions(event_panel)
    apply_fdr(results)
    verdict = evaluate_verdict(results)

    today = date.today()
    report_path = RESULTS_DIR / f"test23_{today.isoformat()}.md"
    write_report(results, verdict, today, report_path, caveats=caveats)

    print(f"\nVerdict: {_VERDICT_LABELS[verdict.verdict]}")
    print(f"  Surviving FDR: {verdict.n_surviving}/{len(results)}")
    print(f"  Economic (≥{ECONOMIC_MAGNITUDE_BPS} bps): {verdict.n_economic}")
    print(f"  Directionally consistent: {verdict.directionally_consistent}")
    print(f"  Report: {report_path}")
    return verdict


if __name__ == "__main__":
    # For the real run, glue together wasde_loader → surprise → events → here.
    raise SystemExit(
        "phase0.test23_wasde_sentiment: invoke via run() with a prepared event_panel. "
        "See phase0/test23_driver.py for the full pipeline wire-up."
    )
