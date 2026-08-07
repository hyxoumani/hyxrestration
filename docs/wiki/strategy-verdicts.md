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
   **Reading-independence caveat (2026-07-27):** the bracket window is
   trailing (`max(recv_ts) - N hours`), so a re-run sooner than `--hours`
   re-scores orders the prior run already counted. Measured across the
   archived reports: each **econ** 336h re-run carried only 11–26% new
   orders (11–14% against all prior runs combined), so its five-reading
   sign sequence is roughly one reading plus four ~12% increments —
   consecutive econ readings agree largely because they are the same
   measurement, and must NOT be counted as independent confirmations.
   **Weather** brackets are genuinely independent despite the same
   trailing window, incidentally: `KXHIGH*` markets expire daily, so the
   top-N market set churns (the 07-27 21st run scored 100% new orders).
   Every report now carries an `independence` block (`new_share`); check
   it before treating a re-run as a confirming reading. To get an
   independent econ reading, space re-runs by at least `--hours` or
   shorten the window.
   **Power caveat (2026-07-31) — this one bounds every directional claim
   the bracket has ever made.** `direction_market_robust` /
   `direction_underlying_robust` are strict-MAJORITY tests over the
   leaning units. A majority is not a measurement: with an odd number of
   leaning units a strict majority always exists, so at the default
   `--markets 8` (which reaches only ~3 city-days) the tier can only fail
   when the aggregate sign contradicts the unit majority. Measured with
   the shipped function over all 34 archived runs: `robust` fires on
   **24**, ten of them at a one-sided sign-test p of **exactly 0.50**,
   and **not one run of the 34 reaches p <= 0.05**. Worse, **31 of the 34
   were underpowered by construction** — their `min_sign_p` (the p they
   would have produced had every underlying agreed) already exceeded 0.05
   before any data was read. Every report from 2026-07-31 carries
   `underlying_sign_p`, `underlying_min_sign_p` and
   `direction_underlying_significant`; **read those, not `robust`.**
   Consequence for the standing data gate: pooling the leaning
   underlyings across the four certified-independent weather runs
   (07-27, 07-29, 07-30, 07-31) gives **5 over / 7 under, k=12,
   p=0.387** — no direction. The gate cannot be met by accumulating runs
   at the current width; ~78 pooled underlyings are needed for even 50%
   power against a 60/40 bias, i.e. ~26 more runs at 3 city-days each,
   or ~16 at `--markets 15` (measured: top-15 reaches 5 city-days).
   Widening top-N changes the scored population, so it starts a NEW
   comparability series rather than extending this one.
   **Live-paper fee evidence (2026-08-05, closed 2026-08-07 at wave
   4):** the diagnostic probe's shadow ledger (run 20260803T142853,
   499 daily-cohort markets settled over four waves) prices the taker
   leg from live paper: fees ran **5.5% of spend** — stable across
   all four waves — while gross went **+4.4% → −10.3% → −21.9% →
   −9.7%** by wave (wave 4: 141 markets, 08-05 cohort, 18/25 series
   negative, worst in weather highs/lows). The probe buys the
   sub-0.50 side of tight books — the longshot side — so its negative
   gross is an independent live confirmation of the fav-long taker
   FAIL, not new information about the maker variant; what the design
   should take is the fee magnitude (5.5%, the stable number) and the
   wave-to-wave gross variance (26pp spread on ~$1–5k/day), which
   bounds how many settlement waves any live verdict needs.
   Separately, the first macro tranche (KXPAYROLLS/KXU3 jobs-report
   brackets, 10 markets, $2,561 spend) settled 08-07 at **$0.00
   payout — a total loss**: a longshot bracket portfolio on a single
   monthly print loses whole, an extreme illustration of the same
   direction. The ledger closed at the 08-07 19:31Z host reboot
   (restart = fresh state by design): run aggregate 509 settled,
   −26.2% gross / −31.7% net (−$5,381.58 paper), 333 open markets
   ($8.1k spend, incl. remaining macro) stranded unscored. The
   fee/variance numbers above are the durable Tier-2 inputs; the
   probe ledger is CLOSED — do not extend it, the successor run
   20260807T193303 starts a new comparability series.
   **First independent econ bracket reading (2026-08-06,
   `reports/maker_bracket/20260806T021621.json`):** the 336h econ
   re-run (KXCPI/KXCPIYOY/KXFED/KXU3, 2,627 orders, 8 markets, 4
   underlyings, all 26JUL events) scored **new_share_vs_all 0.80** —
   the first econ reading that is not mostly a re-measurement (all
   five prior re-runs carried 11–26% new; calendar spacing to the
   07-26 prior was only ~223h, but print churn in the late-JUL books
   made it independent by the report's own binding metric). Two
   findings: (a) **no direction**, underlyings split 2 over / 2 under
   (sign_p 0.6875), and min_sign_p 0.0625 means 4 underlyings could
   not have shown one anyway — consistent with the 07-31 power caveat,
   no drift. (b) **The crossing rule's regime-dependent bias is now
   confirmed in econ, not just weather**: crossing filled 189 vs
   queue-pess 260 / opt 299 — crossing sits BELOW the floor
   (net −71; strict tier −74), forgoing 162 real fills against 88
   unambiguously invented. The 07-21 econ run had crossing INSIDE the
   bracket [368, 404, 436]; same books, later life-cycle, opposite
   lean. Endpoints must use queue-PESS fills — now evidenced in both
   categories.
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
