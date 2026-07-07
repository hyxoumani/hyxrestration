# Simulation honesty (why backtest results can be trusted, and how far)

Governing idea: a result may only be wrong in ways already priced in.
Five properties (design doc `docs/plans/hyxlab-v2/proposal.md` §0):
Provenance, Determinism, Bounded optimism, Self-verification, Durability.

## No-lookahead is structural, not conventional

Strategies see data only through `Context`/FeatureView: settlement
results hidden (a fallthrough bug once leaked them — now the
"adversarial peeker" test attempts to cheat every channel and must come
up empty), forecasts served strictly as-of the snapshot timestamp.

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

## Runtime accounting invariants

Checked after EVERY event, hard abort (`SimAccountingError`):
I1 cash ≡ proceeds − purchases − fees + payouts; I2 no negative
positions; I3 settlement conservation per market. An accounting bug can
never be reported as PnL. Fuzz test: 300 random snapshots × random
open/close/IOC orders must never trip them.

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
