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

- **FavoriteLongshot v1** (2026-07-11,
  `docs/hyxpredict/prereg_favlong_backtest.md`): buy the favorite side
  at taker ask in [0.80, 0.95], 24h before close, hold to settlement.
  FAIL — ROI −5.0% on $74.4K/8,363 fills, negative in 4/5 categories,
  both halves, both sub-bands; gross already negative, **the spread
  decides**: realized 85.2% vs 89.0¢ paid at the ask, while the
  atlas's favorite-underpricing lives at the MID. Successor idea
  (maker-side entry, scoreable via queue-position bounds) requires a
  NEW registration.

## Rejected without testing (documented reasoning)

Latency/oracle arb (infra race, fees designed against it); copy-trading
(survivorship + decay); big political/econ market forecasting (pro
counterparties); Kalshi intramarket rebalance (impossible by book
structure — see [venues](venues.md)).

## Queue (each needs its own pre-registration)

1. ~~Favorite-longshot bias (taker)~~ — TESTED AND KILLED 2026-07-11
   (see Dead list). **Maker variant quantified 2026-07-12** (24h
   horizon, favorite-side bands, n=1.7k–12.9k per band): won−ask
   NEGATIVE in every band (taker dead everywhere); won−mid +0.5¢ to
   +4.4¢ (real, peaks at bands 0.75–0.85); won−bid +1.8¢ to +11.3¢
   GROSS — before adverse selection, which is the whole question.
   Horizon curve (2026-07-12, bands 0.75–0.90): edge@mid is U-shaped —
   +9.9¢ @1h, +8.4¢ @6h, +3.7¢ @24h, +6.5¢ @72h — but near-close bids
   sit 10¢+ under mid (thin end-of-life books), so the 1h/6h gaps are
   the least capturable and most adversely selected. 24h is the
   conservative design point. Registration gated on: enough Tier-2
   stream data to score maker fills via queue-position bounds
   (accumulating now; bracket day 2 shows the crossing rule's bias
   flips sign by regime, so endpoints must use queue-PESS fills), then
   a NEW pre-reg; any horizon sweep counts into the DSR family.
   **Coverage caveat (2026-07-14):** all six maker brackets to date are
   100% `KXHIGH*` weather high-temp (queuescore picks top-print series,
   which are uniformly weather). If this fav-long maker candidate lands
   in Financials/Commodities/Climate bands (where the atlas gap lives),
   the weather-only bracket gives it NO queue-bounds validation — it
   must run its own bracket on its own markets before registration.
   **Partially closed 2026-07-21:** `--series` support (already in
   queuescore) run against Economics (KXCPI/KXCPIYOY/KXFED/KXU3, n=6,363,
   full 14-day history) — crossing lands INSIDE queue bounds [368 pess,
   404 crossing, 436 opt], same qualitative shape as weather runs, so
   the "no stable sign, score via queue-PESS" conclusion is not a
   weather artifact. Financials/Commodities specifically still need
   their own bracket once stream coverage there is dense enough to seat
   orders (`reports/maker_bracket/20260721T152147.json`).
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
