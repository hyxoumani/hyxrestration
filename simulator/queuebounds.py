"""FIFO queue-position bounds for a simulated maker order (Tier-2).

The stream archive (L2 deltas + trade tape, both ms-stamped) supports
exact queue accounting except for one ambiguity: cancels are anonymous
in L2 — a level decrement not explained by trade prints could have been
ahead of us or behind us. So the model runs BOTH bounds and reports the
pessimistic one (design: simulation-honesty.md "Queue-position bounds"):

- entry: queue-ahead = displayed level total (we join the back; exact).
- trade prints at our level consume from the front — both bounds move
  (the tape is authoritative; probed 2026-07-11: a print and its book
  decrement arrive within ±1ms, and taker_side maps as
  yes-taker → no-book @ 1-p, no-taker → yes-book @ p).
- anonymous cancels shrink only the optimistic bound (all-ahead-of-us);
  the pessimistic bound (all-behind-us) instead narrows via the level
  clamp: queue-ahead can never exceed the level's current total, so
  thin books converge the bracket for free.
- level increases join behind us: no effect on either bound.

Assumes Kalshi price-time priority as documented; not yet verified
empirically (design-note precondition — revisit before Tier-3 use).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

ABSORB_WINDOW = timedelta(seconds=2)  # print ↔ decrement pairing horizon


def consuming_print(side: str, price: float, taker_side: str | None, print_price: float) -> bool:
    """Does a tape print consume the (side, price) book level?

    Prints are recorded at the YES price. A yes-taker lifts resting NO
    bids at 1-p; a no-taker lifts resting YES bids at p (probed
    2026-07-11 on KXHIGHNY: 269/270 matched decrements follow this)."""
    if taker_side == "no" and side == "yes":
        return abs(print_price - price) < 1e-9
    if taker_side == "yes" and side == "no":
        return abs((1.0 - print_price) - price) < 1e-9
    return False


@dataclass
class QueueTracker:
    """Track one virtual maker order resting at (side, price).

    Feed events in recv order via on_print / on_delta; read fills off
    `filled_pess` / `filled_opt`. All quantities are contracts. The
    virtual order is NOT part of the displayed level (ledger-only)."""

    side: str
    price: float
    qty: float
    level_size: float  # displayed total at entry
    ahead_pess: float = field(init=False)
    ahead_opt: float = field(init=False)
    filled_pess: float = 0.0
    filled_opt: float = 0.0
    fill_events: list[tuple[datetime, float]] = field(default_factory=list)  # pessimistic fills

    def __post_init__(self) -> None:
        self.ahead_pess = self.level_size
        self.ahead_opt = self.level_size
        self._level = self.level_size
        self._recent_prints: list[tuple[datetime, float]] = []  # awaiting their decrement

    @property
    def done(self) -> bool:
        return self.filled_pess >= self.qty

    def on_print(self, ts: datetime, qty: float) -> None:
        """A consuming trade print at our level (caller pre-filters via
        consuming_print). Consumes queue-ahead first, then fills us."""
        self._recent_prints.append((ts, qty))
        for bound, filled_attr in (("ahead_pess", "filled_pess"), ("ahead_opt", "filled_opt")):
            ahead = getattr(self, bound)
            consumed = min(qty, ahead)
            overflow = qty - consumed
            setattr(self, bound, ahead - consumed)
            if overflow > 0:
                fill = min(overflow, self.qty - getattr(self, filled_attr))
                if fill > 0:
                    setattr(self, filled_attr, getattr(self, filled_attr) + fill)
                    if filled_attr == "filled_pess":
                        self.fill_events.append((ts, fill))
        # The venue's own book decrement for this print arrives ~same ms;
        # remember the qty so on_delta doesn't double-count it as cancel.

    def on_delta(self, ts: datetime, qty: float) -> None:
        """Signed displayed-size change at (side, price)."""
        if qty >= 0:
            self._level += qty  # joins behind us
            return
        dec = -qty
        self._level = max(0.0, self._level - dec)
        # Split the decrement into trade-explained vs anonymous cancel.
        self._recent_prints = [(t, q) for t, q in self._recent_prints if ts - t <= ABSORB_WINDOW]
        explained = 0.0
        remaining = dec
        kept: list[tuple[datetime, float]] = []
        for t, q in self._recent_prints:
            take = min(q, remaining)
            explained += take
            remaining -= take
            if q - take > 0:
                kept.append((t, q - take))
        self._recent_prints = kept
        cancel = dec - explained
        if cancel > 0:
            # Optimistic: every anonymous cancel was ahead of us.
            self.ahead_opt = max(0.0, self.ahead_opt - cancel)
        # Pessimistic narrows only via physics: queue-ahead cannot
        # exceed what is displayed.
        self.ahead_pess = min(self.ahead_pess, self._level)
        self.ahead_opt = min(self.ahead_opt, self._level)
