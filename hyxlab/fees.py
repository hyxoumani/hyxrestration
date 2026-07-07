"""Venue fee models, verified against published schedules July 2026.

Both venues use parabolic fees: fee = factor × count × P × (1−P), peaking
at P=50¢ and →0 at the extremes.

- Kalshi (kalshi.com/fee-schedule, June 2026 PDF + /series metadata,
  verified 2026-07-06): taker factor 0.07, rounded UP to the next cent on
  the total. Maker fees depend on the series' `fee_type`: `quadratic`
  (11,040 of 11,170 series) = makers pay ZERO; `quadratic_with_maker_fees`
  (130 series) = makers pay ¼ of taker (0.0175). `fee_multiplier` scales
  the whole schedule (13 series are fee-free at 0). Resolve per series via
  `kalshi_model()`.
- Polymarket US (effective 2026-04-03, docs.polymarket.com/trading/fees):
  taker factor 0.05 (max $1.25/100 @ 50¢), maker REBATE factor 0.0125
  (modeled as a negative fee).
- Polymarket intl crypto: taker factor 0.07 (peak ~$1.75/100 @ 50¢),
  makers pay zero (rebate program distributes taker fees separately and
  is not modeled here).

Decision-relevant consequence (see docs/hyxpredict/research/
deepdive_reanalysis.md §2): taker-taker cross-venue arb near 50¢ pays
~3¢/share in combined fees — roughly the entire advertised spread.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def parabolic_fee(price: float, count: float, factor: float) -> float:
    return factor * count * price * (1.0 - price)


def _ceil_cent(x: float) -> float:
    # 1e-9 guards float noise so exact cents don't round up an extra cent.
    return math.ceil(x * 100.0 - 1e-9) / 100.0


@dataclass(frozen=True)
class FeeModel:
    name: str
    taker_factor: float
    maker_factor: float  # negative = rebate paid to the maker
    round_up_cent: bool = False

    def fee(self, price: float, count: float, *, taker: bool) -> float:
        """Fee in dollars for `count` contracts at `price`. Negative = rebate."""
        factor = self.taker_factor if taker else self.maker_factor
        fee = parabolic_fee(price, count, factor)
        if self.round_up_cent and fee > 0:
            fee = _ceil_cent(fee)
        return fee

    def taker_frac(self, price: float) -> float:
        """Taker fee per contract at `price` — for edge thresholds."""
        return self.fee(price, 1.0, taker=True)


KALSHI = FeeModel("kalshi", taker_factor=0.07, maker_factor=0.0, round_up_cent=True)
KALSHI_MAKER_FEES = FeeModel(
    "kalshi_maker_fees", taker_factor=0.07, maker_factor=0.0175, round_up_cent=True
)


def kalshi_model(fee_type: str | None, fee_multiplier: float | None = 1.0) -> FeeModel:
    """Per-series Kalshi fee model from /series metadata."""
    mult = 1.0 if fee_multiplier is None else float(fee_multiplier)
    maker = 0.0175 if fee_type == "quadratic_with_maker_fees" else 0.0
    return FeeModel(
        f"kalshi[{fee_type or 'quadratic'}x{mult:g}]",
        taker_factor=0.07 * mult,
        maker_factor=maker * mult,
        round_up_cent=True,
    )


POLYMARKET_US = FeeModel("polymarket_us", taker_factor=0.05, maker_factor=-0.0125)
POLYMARKET_INTL_CRYPTO = FeeModel("polymarket_intl_crypto", taker_factor=0.07, maker_factor=0.0)

# Default lookup by venue string used in Snapshot/MarketInfo.
FEE_MODELS: dict[str, FeeModel] = {
    "kalshi": KALSHI,
    "polymarket": POLYMARKET_US,
}
