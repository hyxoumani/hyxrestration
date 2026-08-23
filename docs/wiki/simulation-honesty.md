# Simulation honesty (why backtest results can be trusted, and how far)

Governing idea: a result may only be wrong in ways already priced in.
Five properties (design doc `docs/plans/hyxlab-v2/proposal.md` §0):
Provenance, Determinism, Bounded optimism, Self-verification, Durability.

## No-lookahead is structural, not conventional

Strategies see data only through `Context`/FeatureView: settlement
results hidden (a fallthrough bug once leaked them — now the
"adversarial peeker" test attempts to cheat every channel and must come
up empty), forecasts served strictly as-of the snapshot timestamp.

## Latency model (landed 2026-07-07 late)

`Simulator(latency=Δ)`: orders/cancels decided at t execute against the
FIRST subsequent snapshot of their market at ts ≥ t+Δ — the
decision-time quote is never fillable; orders whose market never prints
again are counted (`n_dropped_pending`), not filled. Δ=0 is exactly the
legacy engine. Tier-2 feed: `bookreplay.load_stream_snapshots()` replays
archived WS books into ms-fidelity snapshots (gap-honest: books unknown
inside stream_gaps until re-seeded; snapshot images emit only complete —
partial images are states that never existed). First real sweep
(2026-07-07 stream, 313k snapshots/455 markets, tight-spread probe):
1s latency ≈ +0.4¢/contract and ~1% orders unfillable; 30s ≈ +0.6¢.
Latency sensitivity is now a standard verdict dimension.

## Fill model biases (Tier 1–2, v1/v2 engine)

- Taker fills at displayed touch, capped at displayed size: OPTIMISTIC
  (assumes the quote survived you).
- Maker fills only when the touch strictly crosses the limit:
  CONSERVATIVE (real makers also fill without the touch moving).
- Candle-derived snapshots have unknown depth (∞): strategies must
  self-limit via max_qty.
- Reactive market impact is **bounded, never modeled**: size-sensitivity
  sweeps + pessimism haircuts + persistence filters; the residual is
  delegated to the tier ladder (candles → book replay → live shadow).

## Queue-position bounds (design input for Tier-2 WalkBookFill, 2026-07-07)

B7 stream data (gap-free L2 deltas + trade tape, venue ms timestamps)
supports FIFO queue tracking for a simulated maker order: queue-ahead at
entry = level total (exact); fills = decrements coinciding with trade
prints, consume from the front (exact); **cancels are anonymous in L2**
— can't tell ahead vs behind, so the fill model must run BOTH bounds
(pessimistic: all cancels behind us; optimistic: all ahead) and report
the pessimistic one. Thin books narrow the bracket via exact-size
cancel↔placement matching. Preconditions: subscribe at market birth
(hourly ticker refresh may miss up to 1h — tighten when building this)
and verify Kalshi's documented price-time priority empirically before
trusting it. **Mapping verified 2026-07-14** (`python -m
simulator.prioritycheck`): the trade→book-decrement mapping the bracket
rests on (yes-taker→no-book@1-p, no-taker→yes-book@p) holds across the
archive — 18,707 prints / 8 markets / 24h, 99.65% land an exact-size
decrement at the predicted complement level within the model's 2s
window, and the naive same-side mapping fits 0 (so it is not
coincidence); residual 0.35% are no-decrement coverage gaps, not
mapping errors; timing median 0.14ms, p95 1.4ms, tail ~5ms (the ±1ms
claim is typical, not a bound; ABSORB_WINDOW=2s is safely generous).
This verifies WHICH level a trade consumes; the front-vs-back
consumption ORDER within a level stays bracketed (pess/opt), not
assumed — that needs a live maker probe (Tier-3, capital-gated).

## Runtime accounting invariants

Checked after EVERY event, hard abort (`SimAccountingError`):
I1 cash ≡ proceeds − purchases − fees + payouts; I2 no negative
positions; I3 settlement conservation per market. An accounting bug can
never be reported as PnL. Fuzz test: 300 random snapshots × random
open/close/IOC orders must never trip them.

## Shadow ≡ replay equivalence (2026-07-12, real data)

The shadow-vs-replay divergence report on the first fully post-fix
window (run 20260712T004818: 15.3h live, 2,300 fills) shows EXACT
convergence: 2,300/2,300 fills matched, all price deltas 0, gross
cash and fees identical to the cent. The 69%/93% divergence measured
on the first report (run 20260709T234859) is fully attributed to
since-fixed infrastructure: flush-failure data loss (mistakes #12),
venue-unfiltered gap blanking (review H2), and the unrecorded trading
anchor. Replicated 2026-07-12 on a second
independent window (run 20260712T161018: 7.2h, 1,130/1,130 fills, all
deltas 0). Consequence: Tier-3 shadow and Tier-2 replay are ONE
semantics on identical data — the calibration question is now solely
about what the archive misses vs the venue (latency tail, fill-model
vs reality), not about internal consistency.

The divergence report classifies its handful of unmatched fills by
cause: `boundary` (within 60s of a window edge), `gap` (inside a
coverage break), `reseed_twin` (2026-07-17 refinement — an exact
(market, side, qty, price) counterpart exists in the OPPOSITE stream,
just time-shifted past the 2s match window: the start-of-run
seed-settling signature, where both streams produce the identical fill
at offset moments because their seeded books have not yet converged),
else `unexplained` — the only place a hidden fill-model discrepancy
could hide. On the closed 3.3-day run 20260713T064302 (11,943 fills,
99.92%/99.82% match, all price deltas 0), the twin refinement
reclassifies all 10 shadow leftovers and 15 of 16 replay leftovers
from `unexplained` to `reseed_twin`; genuine `unexplained` drops to
**1** (a single KXCPIYOY-26JUN fill at 06:47 UTC, still ~5.5 min into
seed-settling). So even the residual sub-0.2% is demonstrably
timing-shifted seed convergence, not fill-model divergence — the
taker-side haircut ≈ 0 conclusion has no unexplained residual left to
hide behind. The twin test is existence-only (asserts an identical
fill exists opposite, does not net counts).

**2026-07-18 — first PERFECT convergence, on a second independent
multi-day run.** Run 20260716T130721 (post-restart successor to the
07-13 run; 2.3 days, 11,228 fills, first divergence check): 100.00%
match both directions, ZERO unmatched fills in either stream (all
four causes 0), every match exact-tier, all price deltas 0, fees and
gross cash identical to the cent. This run's book seed settled
cleanly inside a gap-free stream window, so there were no
seed-boundary leftovers to classify — confirming that the 07-13
run's sub-0.2% residual was start-of-run noise, and that taker
haircut ≈ 0 is a property of the machinery, not one lucky window.
Report: `reports/shadow_divergence/20260716T130721.json`.

## The settlement leg (2026-08-02)

Until 5f05302 the shadow daemon had **no settlement path at all**.
`_settle` is reached only from `finalize()`, which sits after the
`while` loop in `main()`, and the unit runs with no `--duration` — so
the loop is `while True` and finalize never executes. However long a
run lived, it never credited a payout, never retired a settled
contract and produced no settlement record. This is a PRIOR cause to
the 100%-unobserved outcome coverage recorded on 08-01: coverage says
the data never exercised the path, this says nothing called it. See
mistakes #17. `_mark` is unaffected — it runs from `_equity` on every
snapshot, so the d07d8e8 carry fix was genuinely live.

The daemon now settles every poll (idempotent via the `qty > 0` guard)
and writes `shadow_settlements` (run_id, strategy, venue, market_id,
side, qty, result, payout, ts). The record exists because the fill
ledger holds opens and closes only: a consumer reconstructing a book
by summing signed fill qty resurrects every already-settled position —
the 7a89892 double count one level out, in the ledger instead of in
`_equity`. This is the named prerequisite for position continuity
across restart, which is not yet built.

**First realized settlement PnL, and it is a PROBE cost, not a
strategy result.** Replaying the shipped `_settle` over all 39
archived shadow runs against real `markets.result` settles 1,585
positions across 30 runs (winners and losers both retired; positions
in unresolved markets correctly left open). Pooled on MATCHED scope —
cost and fees restricted to the settled markets — payout 44,386.97
against cost 51,599.89 and fees 2,843.55: **realized -10,056.47, or
-19.5% of cost**, negative in 27 of 30 runs.

Read that number with its scope. Every shadow run to date is
`TightSpreadProbe`, a taker probe that crosses the spread to measure
fill realism; it is not a strategy under test and there is no
pre-registration, so this is **not a verdict** and nothing here kills
or clears anything. What it does measure is the round-trip cost of
crossing plus adverse selection, end to end through settlement, for
the first time — fees alone are 5.5% of cost. The settled subset is
also not a random sample of the book: it is the markets that have
resolved, which skews short-horizon (weather).

**A counting trap this measurement walked into first.** Summing
`payout` over the settled subset against `cost` over the WHOLE book
reads -19,644 and is an artifact — it is negative by construction
whenever any position is left open, because the unsettled positions
contribute cost with no possible payout. A payout summed over the
settled subset is only comparable to a cost summed over the same
subset. Same class as `flagged_day_weighted` / `new_share_vs_all` /
`tier_stability`.

**A second counting trap, same family (2026-08-23).** Comparing
equity across hours by each hour's MINIMUM measures volatility, not
level. The shadow daemon persists ~177 equity points an hour, so
`min(equity)` over an hour is an extreme of that hour's mark noise —
and the deeper an hour's noise, the lower its minimum, regardless of
where it opened or closed. Six status passes narrated an afternoon
"trough" in run `20260821T015256` from readings like "a NEW RUN LOW
−301.3 (17Z)". `simulator/shadow_diurnal.py` prices the artifact:
mean intra-hour range is 29–64 overnight but 253.2 at 17Z and **301.8
at 20Z** — 20Z is the loudest hour of the day, reading −153.1 at its
minimum and **+70.0 at its close**. The `min_gap` column (close minus
low) is 225.5 at 17Z and 223.2 at 20Z against 12–27 overnight, so
roughly 225 points of the "trough" was the sampling rule. Read at the
close the curve is one clean daily oscillation: +72 (03Z) → −247
(16Z) → +150 (22Z).

**Rule: pick a sampling convention before comparing a curve across
buckets, and publish the dispersion beside the level.** Hour-end is
the level series; the range belongs in the same row so the confound
cannot be read without seeing the noise that produced it. Same class
as `flagged_day_weighted` and the matched-scope trap above — the
statistic was fine, the comparison across unequal denominators was
not.

Corollary the same report establishes: the daily shape is a MARKING
story, not a transaction-cost one. Splitting each hour as `d_equity =
−entry_drag + reval` (a taker pays the ask and is marked at the mid,
so new fills book `(ask − mid) + fee` on entry) gives drag of 9–21/hr
against reval swings of ±195 — and the two big reval hours carry ZERO
settlements, so they are not `settle-and-slide`. The drag figure is a
MODEL: it assumes `mid == ask − half a tick`, true by construction
only under a one-tick spread gate, so a fill from an ungated strategy
nulls `reval` rather than reporting a number built on an unmeasured
spread.

## Replay-equivalence guarantee (2026-07-08)

Feeding the sim incrementally (simui's `ReplaySession.advance` in
arbitrary time chunks, with pending-gap bookkeeping) is proven to
produce **bit-identical fills and equity** to the canonical one-shot
`replay_snapshots → Simulator.run` path — permanent test on a seeded
synthetic stream (images/deltas/gaps/latency), plus a real-data check
on the 587k-event KXHIGHCHI-26JUL07 window (35/35 fills, 56,454 equity
points identical). Consequence: what a human trades in simui is exactly
what a backtest would score; there is one replay semantics, not two.

## Correctness gates (each caught a real defect)

- **Forecast MAE gate**: day-ahead MOS vs climate report must be
  1–5°F with exact-match <60%, else no PnL is computed. Fired on Miami
  (MAE 1.28 — legitimately easy station); leak ruled out via 25%
  exact-match before any PnL was seen.
- **Crossed-candle gate**: Kalshi candle bid/ask closes can be crossed
  or sentinel (34,055 rows = 1.3%); excluded at replay. Weather-v1
  re-run through clean data: FAIL confirmed slightly worse (−$425).
- **Mirror invariant** (landed 2026-07-07): Kalshi no_ask ≡ 1−yes_bid;
  violation = corrupt pipeline, never opportunity.
  `Store.mirror_violations()` runs in `sweep --doctor` (0 on the live
  archive) + synthetic corruption tests.
- **Capability guard** (landed 2026-07-07): strategies declare
  book-structure needs (`Strategy.requires`); callers declare feed
  capabilities (`hyxlab/capabilities.py` helpers); `Simulator.__init__`
  raises `VacuousBacktestError` on mismatch — undeclared counts as
  absent. Motivated by a vacuous PoC — rebalance arb on Kalshi candles
  can NEVER fire (complement books), and the sim returned a polite zero
  instead of an error. The guard also flushed out a vacuous determinism
  self-test (rebalance over complement quotes = zero fills) and removed
  the same dead rebalance run from `run_backtest.py`.

## Pinning & reproducibility

Golden synthetic episode (PnL exact to the cent, $6.52), determinism
probe (same inputs ⇒ identical metrics), run manifests in `data/runs/`
(git rev, params, data fingerprint, trial counts for DSR deflation).

## Gotchas

- Test helpers that derive NO quotes as YES-complements make two-sided
  discounts impossible by construction — twice caused vacuous tests.
- A backtest on candle closes is hourly-decision fidelity; strategies
  living inside the gaps (latency, MM) cannot be evaluated there.

## Related
- [strategy-verdicts](strategy-verdicts.md) — what these rules killed
- [data-pipeline](data-pipeline.md) — provenance columns feeding this
