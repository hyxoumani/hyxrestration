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
