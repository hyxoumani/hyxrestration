# Strategy verdicts & queue

Every strategy is a hypothesis to falsify; verdicts come only from
pre-registered thresholds written BEFORE PnL is computed. Failed
strategies stay in the repo as records.

## Dead (do not retro-rescue)

- **L01 ag-equity sentiment×WASDE** (Phase 0, 2026-04): robustly
  falsified across FinBERT/Qwen and expanded grids. See
  `docs/phase0_postmortem.md`.
- **WeatherNWS v1** (2026-07-06, `docs/hyxpredict/prereg_weather_backtest.md`):
  gaussian around day-ahead MOS high vs Kalshi brackets. FAIL — ROI
  −3.0% on $13.2K/1,654 fills, 4/5 cities negative; gross ≈ break-even,
  **fees decide the sign**. Post crossed-candle gate: −$425 (worse).
  Smoke-test peek (+23% NYC, 10 days) was disclosed and proved to be
  noise — pre-registration did its job.

## Rejected without testing (documented reasoning)

Latency/oracle arb (infra race, fees designed against it); copy-trading
(survivorship + decay); big political/econ market forecasting (pro
counterparties); Kalshi intramarket rebalance (impossible by book
structure — see [venues](venues.md)).

## Queue (each needs its own pre-registration)

1. **Favorite-longshot bias** — most robust documented inefficiency
   (longshots overpriced, favorites underpriced; QuantPedia synthesis,
   arXiv 2602.19520). Model-free → testable on the existing 2.6M-candle
   archive. Gated on: calibration atlas (B6) for honest buckets.
2. **Econ prints vs ALFRED vintages** — weekly claims cadence
   accumulates sample fast. Gated on: B4 signal layer.
3. **WeatherNWS v2** — per-city bias/sigma, purged walk-forward (naive
   splits leak adjacent-day regimes). New registration, not a rescue.
4. **Cross-venue arb** — measurement framing; expect fee-wall null
   (~3¢/share taker-taker at mid). Gated on: hand-verified pairs +
   forward Polymarket book collection.
5. **News-lag event studies** — pattern first (B4+atlas), strategy only
   if one shows at daily horizons (GDELT honesty caps sub-daily).

## Hard rules

- Zero capital until a pre-registered PASS at Tier 2+ AND explicit user
  authorization (capital scale is user-only).
- Tier-1 PASS never green-lights capital; Tier-1 FAIL kills outright.
- Parameter changes after registration = exploratory, cannot upgrade a
  verdict. Trial counts recorded in manifests for DSR deflation.

## Related
- [simulation-honesty](simulation-honesty.md) — the machinery enforcing this
- [venues](venues.md) — which strategies are possible where
