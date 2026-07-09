"""Strategy↔data capability contract (correctness gate, 2026-07-07).

A backtest that cannot possibly produce a fill is not a null result — it
is a vacuous test, and the simulator must refuse to run it. Motivating
failure (mistakes log #3): intramarket rebalance replayed on Kalshi
candle-snapshots, where NO quotes are synthesized as the YES complement
(no_ask = 1 − yes_bid), so YES+NO asks can never sum below $1; the sim
returned a polite zero twice before anyone noticed.

The contract has two halves: strategies declare the book structure their
trigger needs (`Strategy.requires`), and the run wiring declares what
each venue's snapshot feed actually provides (via the helpers below).
Undeclared capabilities count as absent — declaring the feed is part of
wiring up a run, not an optional nicety.
"""

from __future__ import annotations

from collections.abc import Iterable

from hyxlab.models import Snapshot

# YES and NO quotes come from separate order books, so a two-sided
# discount (YES ask + NO ask < $1) is possible. True for Polymarket token
# pairs; structurally false for Kalshi (one mirrored book, no_ask ≡
# 1 − yes_bid) and for ANY candle-derived snapshot, where the NO side is
# synthesized as the complement regardless of the venue's live structure.
INDEPENDENT_NO_BOOK = "independent_no_book"

LIVE_VENUE_CAPS: dict[str, frozenset[str]] = {
    "kalshi": frozenset(),
    "polymarket": frozenset({INDEPENDENT_NO_BOOK}),
}


class VacuousBacktestError(RuntimeError):
    """The data can never trigger the strategy — a test that cannot fail
    must be an error, not a zero."""


def live_feed_caps(snapshots: Iterable[Snapshot]) -> dict[str, frozenset[str]]:
    """Capabilities of a live/polled snapshot feed, per venue present."""
    return {v: LIVE_VENUE_CAPS.get(v, frozenset()) for v in {s.venue for s in snapshots}}


def candle_feed_caps(snapshots: Iterable[Snapshot]) -> dict[str, frozenset[str]]:
    """Candle-derived snapshots synthesize NO as the YES complement
    (store.candles_as_snapshots), so no venue in such a feed provides an
    independent NO book."""
    return {v: frozenset() for v in {s.venue for s in snapshots}}


def _unsatisfied(strat, caps: dict) -> frozenset[str]:
    req = frozenset(getattr(strat, "requires", ()) or ())
    if not req or any(req <= venue_caps for venue_caps in caps.values()):
        return frozenset()
    return req


def check_capabilities(strategies: Iterable, data_capabilities: dict | None) -> None:
    """Raise VacuousBacktestError for any strategy whose requirements no
    venue in the declared feed can satisfy."""
    caps = data_capabilities or {}
    for strat in strategies:
        req = _unsatisfied(strat, caps)
        if req:
            declared = {v: sorted(c) for v, c in caps.items()} or "nothing"
            raise VacuousBacktestError(
                f"strategy '{getattr(strat, 'name', strat)}' requires {sorted(req)} "
                f"but the feed declares {declared}; this backtest could never "
                "produce a fill — refusing to run a test that cannot fail"
            )


def partition_runnable(strategies: Iterable, data_capabilities: dict | None) -> tuple[list, list]:
    """(runnable, refused) split for multi-strategy runners: they should
    skip impossible strategies LOUDLY and still run the rest, rather than
    die on the first vacuous one. Direct Simulator construction stays a
    hard error."""
    caps = data_capabilities or {}
    runnable, refused = [], []
    for strat in strategies:
        (refused if _unsatisfied(strat, caps) else runnable).append(strat)
    return runnable, refused
