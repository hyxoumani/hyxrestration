"""Phase 0 — Test 1: Naive top-5 momentum baseline vs SPY.

Spec: phase0_testing.md §1.

Hypothesis: equal-weight portfolio of the top-5 trailing-6-month momentum
names in the 10-equity ag universe, rebalanced monthly at 20% per name,
produces OOS Sharpe ≥ SPY Sharpe + 0.2 over 2014-2024, with positive excess
CAGR in ≥ 3 of 4 regime sub-periods.

Pass criterion:
  1. Full-period annualized Sharpe ≥ SPY Sharpe + 0.2
  2. Positive excess CAGR vs SPY in ≥ 3 of 4 regime sub-periods

Run:
    python -m phase0.test1_naive_baseline
Outputs:
    phase0/results/test1_{today}.md
    phase0/results/test1_{today}_equity.png
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from phase0.data_loaders import PHASE0_DIR, load_adj_close

# ---------------------------------------------------------------- pre-registered

UNIVERSE = ("NTR", "MOS", "CF", "CTVA", "FMC", "ADM", "BG", "DE", "AGCO", "CNH")
BENCHMARK = "SPY"
START = "2014-01-01"
END = "2025-01-01"  # exclusive; yields calendar 2024 inclusive

MOMENTUM_LOOKBACK_DAYS = 126  # ~6 trading months
TOP_N = 5
WEIGHT_PER_NAME = 0.20  # matches T02 production concentration cap
COST_BPS_PER_SIDE = 5  # 5 bps per side per rebalance
TRADING_DAYS_PER_YEAR = 252

REGIMES: tuple[tuple[str, str, str], ...] = (
    ("2014-2016 commodity bust", "2014-01-01", "2016-12-31"),
    ("2017-2019 stable", "2017-01-01", "2019-12-31"),
    ("2020-2021 pandemic+infl.", "2020-01-01", "2021-12-31"),
    ("2022-2024 rates+Ukraine", "2022-01-01", "2024-12-31"),
)

RESULTS_DIR = PHASE0_DIR / "results"


# ---------------------------------------------------------------- signal + portfolio


def compute_momentum_ranks(adj_close: pd.DataFrame) -> pd.DataFrame:
    """126-day return per ticker, sampled on month-end trading days."""
    mom = adj_close.pct_change(MOMENTUM_LOOKBACK_DAYS)
    # Month-end snapshot: last observation of each month. 'ME' = month-end frequency.
    return mom.resample("ME").last()


def select_top_n(month_end_mom: pd.Series, n: int) -> list[str]:
    """Return up to n tickers with the highest 126-day return. NaN tickers dropped."""
    valid = month_end_mom.dropna()
    return valid.sort_values(ascending=False).head(n).index.tolist()


def build_daily_weights(
    adj_close: pd.DataFrame,
    mom_me: pd.DataFrame,
) -> pd.DataFrame:
    """Daily-indexed weights matrix. At each month-end, select top N from
    last month's 126-day momentum and hold for the next month.

    Positions are realized on the *first trading day after* each signal month-end
    (so the signal date is strictly before the rebalance date — no look-ahead).
    Unselected tickers are 0; cash is implicit (weights sum to ≤ 1.0).
    """
    daily_index = adj_close.index
    weights = pd.DataFrame(0.0, index=daily_index, columns=adj_close.columns)

    # Iterate signal dates (month-ends). Use the next-available trading day as
    # the rebalance date.
    for signal_date in mom_me.index:
        picks = select_top_n(mom_me.loc[signal_date], TOP_N)
        if not picks:
            continue
        after = daily_index[daily_index > signal_date]
        if len(after) == 0:
            continue
        rebalance_date = after[0]
        weights.loc[rebalance_date:, :] = 0.0
        for t in picks:
            weights.loc[rebalance_date:, t] = WEIGHT_PER_NAME
    return weights


def portfolio_returns(
    adj_close: pd.DataFrame,
    weights: pd.DataFrame,
    cost_bps_per_side: float = COST_BPS_PER_SIDE,
) -> pd.Series:
    """Daily portfolio returns net of rebalance cost.

    Returns on day t = sum(weights[t-1, i] × asset_return[t, i]). Cost applied
    on any day whose weight vector differs from the prior day — proportional
    to |Δweight|.
    """
    asset_returns = adj_close.pct_change().fillna(0.0)
    # Weight used for day t's return is the prior day's weight (held overnight)
    lagged = weights.shift(1).fillna(0.0)
    # Align columns that might be missing (NaN adj_close → 0 return)
    gross = (lagged * asset_returns).sum(axis=1)
    # Rebalance cost: fire on days where weights change.
    dw = weights.diff().abs().fillna(0.0)
    turnover = dw.sum(axis=1)
    cost = turnover * (cost_bps_per_side / 10_000)
    # Cost applied on the day of rebalance (day of weight change).
    return gross - cost


def spy_returns(
    adj_close_spy: pd.Series, cost_bps_per_side: float = COST_BPS_PER_SIDE
) -> pd.Series:
    """Buy-and-hold SPY: full cost on first trading day, zero thereafter."""
    rets = adj_close_spy.pct_change().fillna(0.0)
    # Charge bps on initial buy.
    if len(rets) > 0:
        rets = rets.copy()
        rets.iloc[0] = rets.iloc[0] - (cost_bps_per_side / 10_000)
    return rets


# ---------------------------------------------------------------- metrics


def annualized_sharpe(daily_returns: pd.Series) -> float:
    rets = daily_returns.dropna()
    if rets.std() == 0 or len(rets) == 0:
        return float("nan")
    return float((rets.mean() / rets.std()) * np.sqrt(TRADING_DAYS_PER_YEAR))


def cagr(daily_returns: pd.Series) -> float:
    rets = daily_returns.dropna()
    if len(rets) == 0:
        return float("nan")
    total = (1 + rets).prod()
    years = len(rets) / TRADING_DAYS_PER_YEAR
    if years <= 0 or total <= 0:
        return float("nan")
    return float(total ** (1 / years) - 1)


def max_drawdown(daily_returns: pd.Series) -> float:
    rets = daily_returns.dropna()
    if len(rets) == 0:
        return float("nan")
    equity = (1 + rets).cumprod()
    running_max = equity.cummax()
    dd = equity / running_max - 1
    return float(dd.min())


# ---------------------------------------------------------------- evaluation


@dataclass
class RegimeResult:
    name: str
    start: str
    end: str
    port_cagr: float
    spy_cagr: float

    @property
    def excess_cagr(self) -> float:
        return self.port_cagr - self.spy_cagr

    @property
    def beats_spy(self) -> bool:
        return self.excess_cagr > 0


@dataclass
class TestResult:
    port_sharpe: float
    spy_sharpe: float
    port_cagr: float
    spy_cagr: float
    port_maxdd: float
    spy_maxdd: float
    regimes: list[RegimeResult]
    n_regimes_beating_spy: int

    @property
    def sharpe_criterion_met(self) -> bool:
        return self.port_sharpe >= self.spy_sharpe + 0.2

    @property
    def regime_criterion_met(self) -> bool:
        return self.n_regimes_beating_spy >= 3

    @property
    def passes(self) -> bool:
        return self.sharpe_criterion_met and self.regime_criterion_met

    @property
    def verdict(self) -> str:
        """Pass-classification per §1.5 outcome interpretation."""
        if not self.passes:
            return "fail"
        if self.port_sharpe >= self.spy_sharpe + 0.3 and self.n_regimes_beating_spy == 4:
            return "strong pass"
        return "marginal pass"


def evaluate(port_rets: pd.Series, spy_rets: pd.Series) -> TestResult:
    regimes = []
    n_beats = 0
    for name, start, end in REGIMES:
        pc = cagr(port_rets.loc[start:end])
        sc = cagr(spy_rets.loc[start:end])
        r = RegimeResult(name=name, start=start, end=end, port_cagr=pc, spy_cagr=sc)
        regimes.append(r)
        if r.beats_spy:
            n_beats += 1
    return TestResult(
        port_sharpe=annualized_sharpe(port_rets),
        spy_sharpe=annualized_sharpe(spy_rets),
        port_cagr=cagr(port_rets),
        spy_cagr=cagr(spy_rets),
        port_maxdd=max_drawdown(port_rets),
        spy_maxdd=max_drawdown(spy_rets),
        regimes=regimes,
        n_regimes_beating_spy=n_beats,
    )


# ---------------------------------------------------------------- reporting


def write_equity_curve(
    port_rets: pd.Series,
    spy_rets: pd.Series,
    path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(10, 6))
    (1 + port_rets).cumprod().plot(ax=ax, label="top-5 momentum (20% each)")
    (1 + spy_rets).cumprod().plot(ax=ax, label="SPY buy-and-hold")
    ax.set_title("Phase 0 — Test 1: naive top-5 momentum vs SPY (ag universe, 2014-2024)")
    ax.set_ylabel("equity (start = 1.0)")
    ax.set_xlabel("")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def write_report(
    result: TestResult,
    run_date: date,
    equity_png_name: str,
    report_path: Path,
) -> None:
    lines: list[str] = []
    lines.append("# Phase 0 — Test 1: Naive top-5 momentum baseline")
    lines.append("")
    lines.append(f"**Run date:** {run_date.isoformat()}")
    lines.append(f"**Universe:** {', '.join(UNIVERSE)} (n={len(UNIVERSE)}); benchmark {BENCHMARK}")
    lines.append(f"**Window:** {START} → {END} (exclusive end)")
    lines.append(
        f"**Rule:** hold top-{TOP_N} by trailing "
        f"{MOMENTUM_LOOKBACK_DAYS}-day return at {WEIGHT_PER_NAME:.0%} each; "
        f"rebalance monthly; {COST_BPS_PER_SIDE} bps/side cost."
    )
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    lines.append(f"- **Outcome: {result.verdict.upper()}**")
    lines.append(
        f"- Sharpe criterion (≥ SPY + 0.2): "
        f"{'✅' if result.sharpe_criterion_met else '❌'}  "
        f"({result.port_sharpe:.3f} vs SPY {result.spy_sharpe:.3f}, "
        f"Δ={result.port_sharpe - result.spy_sharpe:+.3f})"
    )
    lines.append(
        f"- Regime criterion (≥ 3/4 beat SPY on CAGR): "
        f"{'✅' if result.regime_criterion_met else '❌'}  "
        f"({result.n_regimes_beating_spy}/4)"
    )
    lines.append("")

    lines.append("## Full-period summary")
    lines.append("")
    lines.append("| Metric | Portfolio | SPY |")
    lines.append("|---|---:|---:|")
    lines.append(f"| Annualized Sharpe | {result.port_sharpe:.3f} | {result.spy_sharpe:.3f} |")
    lines.append(f"| CAGR | {result.port_cagr:.2%} | {result.spy_cagr:.2%} |")
    lines.append(f"| Max drawdown | {result.port_maxdd:.2%} | {result.spy_maxdd:.2%} |")
    lines.append("")

    lines.append("## Regime breakdown (CAGR)")
    lines.append("")
    lines.append("| Period | Portfolio | SPY | Excess | Beats SPY |")
    lines.append("|---|---:|---:|---:|:--:|")
    for r in result.regimes:
        lines.append(
            f"| {r.name} ({r.start}→{r.end}) | {r.port_cagr:.2%} | "
            f"{r.spy_cagr:.2%} | {r.excess_cagr:+.2%} | "
            f"{'✅' if r.beats_spy else '❌'} |"
        )
    lines.append("")

    lines.append("## Equity curve")
    lines.append("")
    lines.append(f"![equity curve]({equity_png_name})")
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("- **Survivorship bias:** the 10 equities were chosen in 2026, applied to 2014. ")
    lines.append(
        "  Reported result is ex-post on a universe picked with foreknowledge of which names "
    )
    lines.append(
        "  still traded. Sensitivity to dropping the two most ex-post-obvious picks not yet run."
    )
    lines.append("- **CTVA gap:** Corteva spun from DowDuPont 2019-06-03. Pre-spin rows are NaN; ")
    lines.append("  the selector simply picks from the n<10 tickers available in those months.")
    lines.append(
        "- **Costs:** 5 bps/side is conservative-to-realistic for large-cap ag at retail-scale "
    )
    lines.append("  commissions. Slippage is implicit (buy-at-close assumed to fill flat).")
    lines.append(
        "- **yfinance adj close:** dividend and split adjusted; survivorship-free for bars that exist."
    )
    lines.append("")

    lines.append("## Interpretation (per phase0_testing.md §1.5)")
    lines.append("")
    if result.verdict == "strong pass":
        lines.append(
            "**Strong pass.** Passive ag momentum is the edge. A monthly-rebalance rule "
            "captures most of what hyxrestration is trying to capture. Architecture is "
            "over-engineered — seriously consider shipping a ~200-line monthly rebalancer as "
            "the actual 'system' and reframing the complex build as a learning project only."
        )
    elif result.verdict == "marginal pass":
        lines.append(
            "**Marginal pass.** Ag universe has capturable structure; LLM system needs to deliver "
            "edge *above* what momentum already gives. Quantified target for what slices 1–9 must add: "
            f"Sharpe > {result.port_sharpe:.3f} with comparable or lower drawdown."
        )
    else:
        lines.append(
            "**Fail.** No inherent structural tilt in this universe. All alpha must come from active "
            "security selection. Raises the bar for everything else and is a strong signal the universe "
            "is wrong."
        )
    lines.append("")

    report_path.write_text("\n".join(lines))


# ---------------------------------------------------------------- entrypoint


def run(refresh: bool = False) -> TestResult:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()

    all_tickers = [*UNIVERSE, BENCHMARK]
    adj = load_adj_close(all_tickers, START, END, refresh=refresh)

    # Universe adj-close (10 ag tickers), benchmark adj-close (SPY).
    universe_adj = adj[list(UNIVERSE)]
    spy_adj = adj[BENCHMARK]

    mom_me = compute_momentum_ranks(universe_adj)
    weights = build_daily_weights(universe_adj, mom_me)
    port_rets = portfolio_returns(universe_adj, weights)
    spy_rets = spy_returns(spy_adj)

    # Align both series to the common evaluation window (drop warm-up period
    # before the first real rebalance so Sharpe isn't pulled toward zero by
    # 126 days of flat cash).
    first_live = weights.sum(axis=1).gt(0).idxmax()
    port_rets = port_rets.loc[first_live:]
    spy_rets = spy_rets.loc[first_live:]

    result = evaluate(port_rets, spy_rets)

    png_name = f"test1_{today.isoformat()}_equity.png"
    write_equity_curve(port_rets, spy_rets, RESULTS_DIR / png_name)
    report_path = RESULTS_DIR / f"test1_{today.isoformat()}.md"
    write_report(result, today, png_name, report_path)

    print(f"\nVerdict: {result.verdict.upper()}")
    print(
        f"  Sharpe: portfolio {result.port_sharpe:.3f} vs SPY {result.spy_sharpe:.3f} "
        f"(Δ={result.port_sharpe - result.spy_sharpe:+.3f})"
    )
    print(f"  Regimes beaten: {result.n_regimes_beating_spy}/4")
    print(f"  Report: {report_path}")
    return result


if __name__ == "__main__":
    run()
