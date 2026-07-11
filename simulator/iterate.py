"""Iteration machinery (B5): the anti-p-hacking core.

Every parameter sweep is a multiple-testing family; the machinery makes
the family size impossible to omit:

- `deflated_sharpe` — Bailey & López de Prado (2014): the probability
  that the candidate's Sharpe exceeds the Sharpe you'd expect from the
  BEST of n_trials worthless strategies, given the non-normality of the
  returns. A pre-reg PASS on a swept strategy must quote DSR, not SR.
- `expected_max_sr` — E[max SR] of n_trials zero-skill trials whose SRs
  vary with variance sr_var (the deflation benchmark SR0).
- `purged_folds` — walk-forward partitions on market CLOSE dates with
  an embargo: markets closing within embargo_days of the test fold's
  span appear in NEITHER train nor test (adjacent-day weather/econ
  regimes leak through naive splits).

Normal CDF via math.erf; inverse CDF via Acklam's rational
approximation (|ε| < 1.15e-9) — no scipy dependency.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import NamedTuple

EULER_GAMMA = 0.5772156649015329


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def inv_norm(p: float) -> float:
    """Acklam's inverse normal CDF approximation."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0,1), got {p}")
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > p_high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


def sharpe(returns: list[float]) -> float:
    """Per-period (non-annualized) Sharpe; 0 for degenerate input."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return mean / math.sqrt(var) if var > 0 else 0.0


def moments(returns: list[float]) -> tuple[float, float]:
    """(skewness, kurtosis) — kurtosis is Pearson (normal = 3)."""
    n = len(returns)
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    if m2 == 0:
        return 0.0, 3.0
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    return m3 / m2**1.5, m4 / m2**2


def expected_max_sr(n_trials: int, sr_var: float) -> float:
    """E[max SR] across n_trials zero-skill strategies (SR0 benchmark)."""
    if n_trials <= 1 or sr_var <= 0:
        return 0.0
    return math.sqrt(sr_var) * (
        (1 - EULER_GAMMA) * inv_norm(1 - 1 / n_trials)
        + EULER_GAMMA * inv_norm(1 - 1 / (n_trials * math.e))
    )


class DSRResult(NamedTuple):
    sr: float  # candidate per-period Sharpe
    sr0: float  # expected max SR of the trial family (deflation benchmark)
    dsr: float  # P[true SR > SR0] given non-normal returns
    n_returns: int
    n_trials: int
    skew: float
    kurt: float


def deflated_sharpe(returns: list[float], n_trials: int, sr_var: float | None = None) -> DSRResult:
    """Deflated Sharpe of a candidate given its trial-family size.

    sr_var: variance of SR estimates across the family's trials; if the
    sweep is available pass the actual variance, else the conservative
    default 1/(T-1) (the SR estimator's own sampling variance under
    zero skill)."""
    t = len(returns)
    sr = sharpe(returns)
    skew, kurt = moments(returns)
    if t < 3:
        return DSRResult(sr, 0.0, 0.0, t, n_trials, skew, kurt)
    if sr_var is None:
        sr_var = 1.0 / (t - 1)
    sr0 = expected_max_sr(n_trials, sr_var)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:  # pathological moments: refuse to flatter
        return DSRResult(sr, sr0, 0.0, t, n_trials, skew, kurt)
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom)
    return DSRResult(sr, sr0, norm_cdf(z), t, n_trials, skew, kurt)


def purged_folds(
    close_dates: dict[str, date], n_folds: int, embargo_days: int
) -> list[tuple[list[str], list[str]]]:
    """Walk-forward folds over market close dates with an embargo.

    Markets sorted by close date are split into n_folds contiguous test
    spans; for each fold, train = markets closing STRICTLY BEFORE the
    test span minus the embargo (walk-forward: no future markets in
    train), and markets whose close falls inside the embargo belt appear
    in neither set. Returns [(train_ids, test_ids), ...]; the first fold
    has no train and is emitted only as later folds' history."""
    if n_folds < 2:
        raise ValueError("need at least 2 folds")
    ordered = sorted(close_dates, key=lambda m: (close_dates[m], m))
    fold_size = max(1, len(ordered) // n_folds)
    spans: list[list[str]] = [
        ordered[i * fold_size : (i + 1) * fold_size if i < n_folds - 1 else len(ordered)]
        for i in range(n_folds)
    ]
    out: list[tuple[list[str], list[str]]] = []
    for k in range(1, n_folds):
        test = spans[k]
        if not test:
            continue
        test_start = close_dates[test[0]]
        cutoff = test_start - timedelta(days=embargo_days)
        train = [m for span in spans[:k] for m in span if close_dates[m] < cutoff]
        out.append((train, test))
    return out


def family_report(variant_returns: dict[str, list[float]]) -> dict:
    """Sweep summary: per-variant Sharpe, best/median/worst, and the
    Deflated Sharpe of the BEST variant given the family size and the
    family's own SR variance. This is the number a pre-reg verdict may
    quote; the raw best-variant SR is not."""
    if not variant_returns:
        return {"n_trials": 0}
    srs = {k: sharpe(v) for k, v in variant_returns.items()}
    ordered = sorted(srs, key=lambda k: srs[k])
    best = ordered[-1]
    n = len(srs)
    sr_var = None
    if n >= 2:
        mean_sr = sum(srs.values()) / n
        sr_var = sum((s - mean_sr) ** 2 for s in srs.values()) / (n - 1)
    dsr = deflated_sharpe(variant_returns[best], n_trials=n, sr_var=sr_var)
    return {
        "n_trials": n,
        "best": {"variant": best, "sr": srs[best]},
        "median": {"variant": ordered[n // 2], "sr": srs[ordered[n // 2]]},
        "worst": {"variant": ordered[0], "sr": srs[ordered[0]]},
        "family_sr_var": sr_var,
        "deflated_sharpe_of_best": dsr._asdict(),
    }
