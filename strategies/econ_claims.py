"""EconClaimsRW v1 -- weekly claims print vs the Kalshi strike ladder.

Binding spec: docs/hyxpredict/prereg_econ_claims_backtest.md (2026-10-15,
written BEFORE the sample exists). A no-parameter random walk on the
prior week's FIRST print, as known at decision time through the
two-dimensional vintage table (`ctx.econ_series`), gives P(print >=
strike) per rung; the mispriced side is taken at the taker touch when the
model clears the ask by the registered edge floor. One look per market,
inside [close-60min, close-50min], 10 contracts IOC, held to settlement.

Nothing here is tuned. Every number is the registration's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from math import erf, sqrt
from statistics import stdev

from hyxlab.models import Order, Snapshot
from simulator.strategy import Context, Strategy


@dataclass(frozen=True)
class ClaimsPair:
    """One (market series, ALFRED series) pair and its fixed unit facts."""

    market_series: str
    series_id: str
    strike_unit: float  # strike -> series units (ICSA x1, CCSA x1000)
    obs_offset_days: int  # close date -> obs_date the print describes


# Binding pairs. Offsets are the published title offsets (strategy-verdicts
# item 2): an ICSA Thursday reports the week ending the prior Saturday,
# CCSA one week further back.
ICSA = ClaimsPair("KXJOBLESSCLAIMS", "ICSA", 1.0, 5)
CCSA = ClaimsPair("KXCONTCLAIMS", "CCSA", 1000.0, 12)
PAIRS = {"ICSA": ICSA, "CCSA": CCSA}

HIST_N = 53  # 53 obs -> 52 week-on-week changes for sigma
GRANULARITY = 1000.0  # prints are 1k-granular; >= is inclusive
UNIT_TOLERANCE = 2.0  # a scaled strike outside [anchor/2, anchor*2] is a UNIT failure


def _phi(z: float) -> float:
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


@dataclass(frozen=True)
class Model:
    anchor: float
    sigma: float

    def p_yes(self, strike_in_series_units: float) -> float:
        """P(first print >= K), continuity-corrected toward YES."""
        z = (strike_in_series_units - 0.5 * GRANULARITY - self.anchor) / self.sigma
        return 1.0 - _phi(z)


def fit_model(obs: list, close_date: date, pair: ClaimsPair) -> tuple[Model | None, str]:
    """The random walk as-of decision time, or (None, reason).

    `obs` is `ctx.econ_series(pair.series_id, HIST_N)`: newest first, each
    the latest revision KNOWABLE at decision time. The anchor must be the
    prior week's print exactly, and the history must be 53 consecutive
    weekly observations -- anything else is "not scorable", never a trade.
    """
    if len(obs) < HIST_N:
        return None, f"history {len(obs)} < {HIST_N}"
    want = close_date - timedelta(days=pair.obs_offset_days + 7)
    if obs[0].obs_date != want:
        return None, f"anchor obs {obs[0].obs_date} != {want}"
    dates = [o.obs_date for o in obs[:HIST_N]]
    if any((a - b).days != 7 for a, b in pairwise(dates)):
        return None, "history not consecutive weekly"
    values = [float(o.value) for o in obs[:HIST_N]]
    sigma = stdev(a - b for a, b in pairwise(values))
    if sigma <= 0.0:
        return None, "sigma 0"
    return Model(anchor=values[0], sigma=sigma), ""


class EconClaimsRW(Strategy):
    requires = frozenset()

    def __init__(
        self,
        pair: ClaimsPair = ICSA,
        edge: float = 0.10,
        qty: float = 10.0,
        window_minutes: tuple[int, int] = (60, 50),
    ) -> None:
        self.name = "econ_claims"
        self.pair = pair
        self.edge = edge
        self.qty = qty
        self.window = (timedelta(minutes=window_minutes[0]), timedelta(minutes=window_minutes[1]))
        self._done: set[tuple[str, str]] = set()
        # Scorer-visible record: why a market was looked at and not traded.
        self.skipped: dict[tuple[str, str], str] = {}
        self.looked: dict[tuple[str, str], Model] = {}

    def on_snapshot(self, snap: Snapshot, ctx: Context) -> list[Order]:
        key = (snap.venue, snap.market_id)
        if key in self._done:
            return []
        info = ctx.market(snap.venue, snap.market_id)
        if info is None or info.series != self.pair.market_series or info.close_time is None:
            return []
        if info.strike_type != "greater_or_equal" or info.floor_strike is None:
            return []
        lo, hi = info.close_time - self.window[0], info.close_time - self.window[1]
        if not (lo <= snap.ts <= hi):
            return []
        self._done.add(key)  # one look, whatever it decides

        model, why = fit_model(
            ctx.econ_series(self.pair.series_id, HIST_N), info.close_time.date(), self.pair
        )
        if model is None:
            self.skipped[key] = why
            return []
        strike = info.floor_strike * self.pair.strike_unit
        if not (model.anchor / UNIT_TOLERANCE <= strike <= model.anchor * UNIT_TOLERANCE):
            self.skipped[key] = f"UNIT: strike {strike} vs anchor {model.anchor}"
            return []
        self.looked[key] = model
        p = model.p_yes(strike)
        if snap.yes_ask is not None and p - snap.yes_ask >= self.edge:
            return [Order(snap.venue, snap.market_id, "yes", self.qty, tif="IOC")]
        if snap.no_ask is not None and (1.0 - p) - snap.no_ask >= self.edge:
            return [Order(snap.venue, snap.market_id, "no", self.qty, tif="IOC")]
        self.skipped[key] = f"no edge: p_yes {p:.3f} vs ask {snap.yes_ask}/{snap.no_ask}"
        return []
