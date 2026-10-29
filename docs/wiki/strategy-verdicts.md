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

- **FavLongTight v1** (2026-08-23,
  `docs/hyxpredict/prereg_favlong_tight_backtest.md`): v1's entry
  gated on a one-tick book spread — the condition v1's own post-mortem
  named as its cause of death — on band A=[0.80,0.95] and band
  B=[0.95,0.99]. **BOTH BANDS FAIL, AND THE FAVORITE-LONGSHOT FAMILY
  IS THEREFORE CLOSED** (binding: the registration forbids a third
  variant). 9,491,893 candle-snapshots / 1,618,926 markets.
  A: ROI +0.03% on $42.6K/4,762 fills, 3/5 categories positive, H1
  negative, low sub-band negative. B: ROI +0.27% on $73.5K/7,514
  fills — 5/5 categories, both halves and both sub-bands positive, and
  still four times under the +1.0% floor (DSR 0.874, n_trials 2).
  **The gate worked and there was nothing beneath it**: same band,
  same horizon, same fee model, −5.0% → +0.03%, so v1's named cause of
  death is CONFIRMED — and the recovered market prices to break-even,
  with fees taking 96.0% of band A's gross. Do not re-cite this as
  "nearly passed": the +1.0% floor exists because Tier-1 fills assume
  the ask is available.

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
   **AMENDED 2026-09-12 — that last sentence buys the wrong kind of
   independence.** Every field above counts ORDERS, and the
   `direction_*_status` verdict a re-run is read to confirm is a sign
   test whose draw is an UNDERLYING. The 08-29 and 09-12 width-24 econ
   runs were spaced 336h apart precisely to earn `new_share_vs_all =
   1.00` and did — **0 of 4,384 orders shared** — while **5 of their 7
   underlyings were the same events**, and all 4 that leaned in both
   kept their sign (KXU3-26AUG -32/-24, KXCPI-26AUG -18/-22,
   KXCPIYOY-26AUG -11/-75, KXFED-26SEP +8/+10). Order novelty 1.00,
   underlying novelty **0.29**. Spacing by `--hours` guarantees only
   that no ORDER repeats; the units roll over on the EVENTS' own clock,
   and monthly econ prints do not roll in 14 days. Weather is the
   control and is genuinely independent at both tiers, for the same
   reason it always was: `KXHIGH*` city-days expire daily, so the units
   themselves churn. Reports from 2026-09-12 carry
   `independence.units` (`new_vs_prior`, `new_vs_all`) and
   `units.repeat_sign.same_sign`: **read those, not `new_share_vs_all`,
   before counting an econ re-run as a confirming reading**, and
   schedule the next econ bracket on event rollover rather than on the
   clock.
   **First fully independent econ reading (2026-08-25):** the 19-day
   gap since 08-06 exceeded the 336h window, so this run is the first
   econ bracket at `new_share_vs_all = 1.00` — no order re-scored from
   any prior run. It is also the first with all four direction readings
   POWERED (5 underlyings, `underlying_min_sign_p` 0.03125; 8 markets,
   0.003906), and it reads a clean null: `net_disagreement` **+2** on
   1,360 orders (crossing 124, queue-pess 122, queue-opt 135), all four
   readings `not_significant`. The 08-06 reading's **-71** under-award
   lean does not survive as evidence — that run was underpowered at the
   underlying tier, and its orders are disjoint from these.
   `reports/maker_bracket/20260825T022029.json`.
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
   **Correction (2026-08-25, mistakes #33):** the third of those three
   is itself a boolean over outcomes that mean opposite things — its
   `False` covers "tested and found no direction" and "could not have
   found one". Read **`direction_verdict`** instead: its counts
   partition the four tier x bound readings over
   `significant_over` / `significant_under` / `not_significant` /
   `underpowered` / `no_direction`, and `powered` is how many readings
   could have rejected at all. Of the 7 archived reports carrying the
   sign fields, **5 are underpowered at the underlying tier**.
   Consequence for the standing data gate: pooling the leaning
   underlyings across the four certified-independent weather runs
   (07-27, 07-29, 07-30, 07-31) gives **5 over / 7 under, k=12,
   p=0.387** — no direction. The gate cannot be met by accumulating runs
   at the current width; ~78 pooled underlyings are needed for even 50%
   power against a 60/40 bias, i.e. ~26 more runs at 3 city-days each,
   or ~16 at `--markets 15` (measured: top-15 reaches 5 city-days).
   Widening top-N changes the scored population, so it starts a NEW
   comparability series rather than extending this one.
   **The widening was run (2026-08-29), and it did exactly what that
   last sentence warned about.** Two econ brackets on the same 336h
   window: `--markets 8` (2,264 orders, 8 markets / **4** underlyings,
   `new_share` 0.65) reads `powered` **2 of 4** — the underlying tier
   LOST power against 08-25's 5 underlyings, so the clean 08-25 null was
   NOT reconfirmed there; it is now no-evidence, and `net_disagreement`
   moved **+2 -> -41**. `--markets 24` (5,643 orders, 24 markets / **7**
   underlyings, `new_share` 0.60 — 2,264 of its orders ARE the 8-market
   run's) is the first bracket ever with all four readings powered AND a
   significant one: MARKET tier **`significant_under` on both bounds**
   (14 of 17 leaning markets under, `market_sign_p` 0.0064;
   `pess_but_not_crossing` 121 vs `crossing_but_not_opt` 61 — the
   crossing rule FORGOES real fills rather than inventing them), while
   the UNDERLYING tier reads **`not_significant` on both bounds** (4 of
   5 leaning underlyings under, `underlying_sign_p` 0.1875, `min` 0.03125
   — powered and negative). **Read the underlying tier.** 24 markets are
   7 events; the market-tier p treats one strike ladder as up to a dozen
   draws, which is the unit-of-independence trap this report exists to
   flag, and it is what a significant market tier over a null underlying
   tier looks like. So: no directional bias at the honest unit, and the
   only tier that shows one is the one whose independence assumption is
   known false. This starts the NEW comparability series, at width 24.
   `reports/maker_bracket/20260829T081848.json`,
   `20260829T082019.json`.
   **Read `direction_stability` (2026-08-29) before comparing any two of
   these readings.** `direction_verdict`'s counts are the same sentence
   across runs whether the bracket gained units or swapped populations —
   both 08-25 and the 08-29 width-8 run print `significant: 0`, and only
   the units say the second could not have found one at the underlying
   tier. The field carries the trajectory with `units` and per-reading
   `min_sign_p` beside every count, filters priors by composition (a
   weather reading is not an econ prior), collapses reports over the same
   orders into one reading, and leaves the 33 reports that predate
   `direction_verdict` ABSENT from the trajectory rather than plotting
   them as measured zeros.
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
   **Band curve from the same instrument (2026-08-22,
   `simulator/shadow_attribution.py`, shadow run 20260810T081931, 1,324
   settled positions — the largest settlement sample the ledger has
   ever held).** The 08-05 note above reads the probe as a single
   pooled number ("negative gross, confirms the taker FAIL"). Split by
   ENTRY PRICE it is not one number, it is monotone, and the favorite
   end of its own book has the opposite sign. Bias = entry price minus
   realized win rate, i.e. what the taker PAID over what the contract
   turned out to be worth: <5c **+0.030** (428 positions, ZERO
   winners), 5-15c +0.048, 15-25c +0.013, 25-35c −0.008, 35-45c
   **−0.119**, 45c+ **−0.275**. Sign crosses near 25-30c. Two
   consequences. (a) The pooled "probe loses" is a statement about its
   MIX (it is capped at mid < 0.5 and its mass sits under 15c), not
   about taker entry as such — and the equity decomposition says the
   same thing: of the run's −4,302.3, realized settlement PnL is
   −3,495.0 (81%) and open-book carry only −807.3, with fees at 5.6% of
   notional turning a −1,757.9 gross realized loss into −3,495.0. The
   "settle-and-slide" shape watched for six status passes was the
   cohort BOOKING that loss, not marking noise. (b) The dear end's
   negative bias was measured under a ONE-TICK SPREAD GATE (the probe's
   own entry condition), which is precisely the condition
   FavoriteLongshot v1's post-mortem blames for its death ("the spread
   decides — realized 85.2% vs 89.0¢ paid at the ask, while the
   underpricing lives at the MID"). That is a new conditioning
   variable with an a-priori mechanism, not a re-reading of the dead
   result, so it goes to a NEW registration:
   `docs/hyxpredict/prereg_favlong_tight_backtest.md` (2026-08-22,
   RUN 2026-08-23, **BOTH BANDS FAIL — FAMILY CLOSED**) — taker entry gated on a one-tick book
   spread, bands A=[0.80,0.95] (v1's, like-for-like) and B=[0.95,0.99]
   (v1's untested complement) both fixed in advance. **Family size is
   now 2 and both members are exploratory**; the registration binds
   that if both bands FAIL the fav-long family is CLOSED and no third
   variant is registered. **That condition fired**: see the Dead list.
   This queue item is CLOSED — no fav-long variant, taker or maker,
   may be registered on this evidence or a re-reading of it. Caveats carried by the measurement itself and
   repeated in the pre-reg: settled positions are 82% of positions
   (77-85% by band, mildly declining with price), the net-PnL column is
   noisy at these n (the +49.1% on 45c+/n=62 is not a result — read the
   bias column), and the mix is KXHIGH*/KXLOWT* city-temperature
   ladders, where the cheap band is where a bracket set's overround has
   to sit. This measures the weather-bracket complex, not Kalshi.
   **The weather direction gate was width-bound, not run-bound
   (2026-09-16, `reports/maker_bracket/20260916T021727.json`).** The
   07-31 power caveat concluded the gate needed "~26 more runs at 3
   city-days each, or ~16 at `--markets 15`", and every weather bracket
   since kept running at the default `--markets 8` (3-5 underlyings,
   `underlying_min_sign_p` 0.0625-0.125 -- underpowered before any data
   was read). ONE run at the width the econ series has used since 08-29
   (`--markets 24 --hours 336`) reaches **16 underlyings in a single
   reading**: `underlying_min_sign_p` **0.000122**, all four tier x bound
   readings POWERED, and 15 of the 16 underlyings new against every
   archived report (city-days expire daily, so weather units churn by
   construction -- the property that made weather the independence
   control all along). It reads a **clean null at the honest unit**: 7
   over / 6 under / 3 tied, `underlying_sign_p` 0.50; against the ceiling
   6 over / 9 under, 0.304. The market tier agrees for once
   (`market_sign_p` 0.668 floor / 0.143 ceiling,
   `direction_market_robust` FALSE on the floor), so unlike 08-29/09-12
   econ there is no significant-market-over-null-underlying split to
   adjudicate. This is the first weather reading in which a fill-model
   direction COULD have been rejected and none was. Carry the
   offsetting-nets caveat with it: `net_disagreement` +1 on
   `abs_net_by_market` **67** (crossing 588, queue-pess 587, queue-opt
   657; `crossing_but_not_pess` 155 vs `pess_but_not_crossing` 154,
   `inside_bracket` 19) is cancellation, not precision. Consequence for
   the standing gate: it was never reachable by ACCUMULATING
   default-width runs, and the 08-29 lesson still binds -- this is
   reading ONE of a new weather width-24 series, not an extension of the
   width-8 one. (The same pass's default-parameter run,
   `20260916T021652.json` -- 8 markets / 3 underlyings / `min_sign_p`
   0.125 -- is in the archive and is structurally uninformative; it is
   the `prior_report` that run 021727 compares against.)
   **Econ width-24 re-run (2026-09-16, `20260916T021816.json`) -- same
   verdict, and by the project's own test NOT a confirmation of it.** 24
   markets / 9 underlyings / 4,337 orders: market tier
   `significant_under` on both bounds, underlying tier
   `not_significant` on both (`underlying_sign_p` 0.0898, `min` 0.001953,
   powered 4 of 4) -- the 08-29 and 09-12 shape exactly, with crossing
   209 below queue-pess 309 / opt 363 (204 real fills forgone against 99
   unambiguously invented). The 09-12 amendment says do not count it:
   spaced only ~4 days into a 336h window, it carries
   `new_share_vs_all` 0.465 on ORDERS and **2 of 9 underlyings new
   against all priors**, with 5 of the 6 repeated leaners keeping their
   sign. It is the 09-12 reading re-measured. The next informative econ
   bracket is the width-24 EVENT rollover already queued (late
   September), not a clock-spaced re-run.
   **2026-10-07 width-24 pair (`20261007T081813.json` weather,
   `20261007T081859.json` econ). Weather: a second powered null.**
   1,608 orders / 24 markets / 17 underlyings. `underlying_sign_p` 0.151
   on the floor (10 over / 5 under / 2 tied) and 0.291 on the ceiling,
   `underlying_min_sign_p` 3.1e-05. All four readings `not_significant`
   and powered. Against the 09-16 weather reading it shares **0** underlyings, and 16 of 17
   are new against every prior. The one repeat is `KXCPIYOY-26SEP`, an
   econ strike the default top-24 picked up. That makes two independent
   powered nulls on weather fill-model direction. Crossing 588,
   `crossing_but_not_opt` 138, `pess_but_not_crossing` 161,
   `net_disagreement` +4 on `abs_net_by_market` 74, still cancellation.
   **Econ: the market tier's `significant_under` did not reproduce.**
   6,137 orders / 24 markets / 7 underlyings. Crossing 305 against
   queue-pess 310 / opt 376, where 09-16 had 209 vs 309. All four readings
   `not_significant` (market 0.846, underlying 0.773, min 0.0078). It is
   NOT the clean rollover that was queued: 4 of 7 underlyings are shared
   with 09-16, and all 4 kept their sign. Only 2 of 7 are new against all
   priors. So the market-tier shape seen on 08-29/09-12/09-16 failed to
   recur on mostly the same events plus fresh quotes. It is not refuted by
   an independent sample. The next econ reading that buys units is the
   Oct prints rolling, roughly 2026-10-21 or later. Numbers above use the
   comparability fix in 0491368 (mistakes #110). The archived JSONs'
   own `direction_stability`/`prior_report` were computed before the fix
   and spliced the two families.
   **2026-10-15 width-24 pair (`20261015T212655.json` econ,
   `20261015T213006.json` weather), run as transient units, and the
   weather one died first.** The unbounded `EXISTS` in
   `queuescore.select_markets` spilled past the 16 GiB cap on the
   book_events table the 10-06 poly flood grows ~14M rows/day
   (mistakes #124; bounded to venue + window, red/green tested, re-run
   clean: held 62.7s). Together the three brackets cost streamd one
   105.5s stall episode (5 failed flushes, 0 spilled). **Weather: a third
   independent floor null, and the first ceiling-only significance.**
   1,434 orders / 24 markets / 15 underlyings, **0** shared with the 10-07
   reading, 9 of 15 new against every prior. Floor (pess, the registered
   bound): 7 over / 7 under / 1 tied, `underlying_sign_p` 0.605,
   `not_significant`, powered (`min` 6.1e-05). Ceiling (opt): 3 over /
   11 under, p 0.0287, `significant_under` -- the only significant
   reading of the four, on the OPTIMISTIC bound, with `net_disagreement`
   -1 on the floor against -28 on the ceiling (crossing 581, queue-pess
   582, opt 669; `crossing_but_not_pess` 141 vs `pess_but_not_crossing`
   142 -- the floor is cancellation again). Read it as: three
   independent powered floor nulls on weather (09-16, 10-07, 10-15), and
   one ceiling reading at alpha .05 among four per report, which the next
   weather reading either repeats on fresh underlyings or does not --
   it is NOT a direction verdict. Carry the three-null tally; do not
   carry the ceiling as a lean. **Econ: 10-07's non-reproduction holds,
   still on mostly the same events.** 5,979 orders / 24 markets / 8
   underlyings; all four readings `not_significant` and powered
   (floor 4 over / 4 under, `underlying_sign_p` 0.637, `min` 0.0039;
   market tier 0.154). Crossing 249 vs queue-pess 290 / opt 345,
   `net_disagreement` -41 on `abs_net_by_underlying` 97 (KXCPI-26SEP
   -29, KXCPIYOY-26SEP -21, KXFED-26DEC -17 against KXCPI-26OCT +10,
   KXPAYROLLS-26OCT +9, KXFED-26OCT +7): the SEP events lean under and
   the OCT events lean over, which is a sign the 08-29/09-12/09-16
   `significant_under` was those September events. 5 of 8 underlyings
   are shared with 10-07 (4 kept sign, 1 flipped), 2 of 8 new against
   all priors -- not the clean rollover either; that is still the Oct
   prints rolling (~10-21 or later). A default-width 24h weather run in
   the same pass (`20261015T212439.json`, 8 markets / 4 underlyings,
   market tier `significant_under`, underlying tier `underpowered`) is
   archived and structurally uninformative, per the 07-14 note.
   **Econ width-24 re-run 2026-10-22 (`20261022T021937.json`, transient
   unit, held 101.2s; same arguments as 09-16/10-07/10-15): the fourth
   consecutive econ null, on the same events.** 5,788 orders / 24
   markets / 8 underlyings (KXCPI 2,685, KXPAYROLLS 946, KXU3 908,
   KXCPIYOY 660, KXFED 589). All four readings `not_significant` and
   powered (floor 4 over / 4 under, `underlying_sign_p` 0.637, `min`
   0.0039; market tier 12/10, p 0.416; ceiling 4/4 and 12/11). Crossing
   282 vs queue-pess 268 / opt 328; `crossing_but_not_pess` 182 vs
   `pess_but_not_crossing` 168, `inside_bracket` 4. Floor
   `net_disagreement` +14 on `abs_net_by_underlying` 110: the 10-15
   pattern again, SEP events under (KXCPIYOY-26SEP -16, KXCPI-26SEP
   -14, KXFED-26DEC -16) and OCT events over (KXCPI-26OCT +35 from +10,
   KXFED-26OCT +9, KXPAYROLLS-26OCT +9, KXCPI-26NOV +9 new), with the
   aggregate crossing -41 -> +14 only because the OCT share of the
   window grew; one sign flip (KXU3-26OCT +2 -> -2). `independence`:
   7 of 8 underlyings shared with 10-15 (6 kept sign), **0 of 8 new
   against all priors** -- the same events re-measured with another
   week of their own tape, not a replication. The clean rollover is a
   window holding no SEP event, ~336h after the last SEP settlement:
   read again ~10-29 or later. Victim side: one streamd episode, 120.2s
   span / 6 fails / `hold_lower_s` 75 / 60,029 pending / 0 spilled,
   holder named -- the per-run cost is now ~100s of stream exclusion
   (62.7s on 10-15; the window is entirely post-flood book_events).
   **Econ width-24 re-run 2026-10-29 (`20261029T082353.json`, the
   10-22 read's named rollover date; re-measured on the restructured
   reader as `20261029T083239.json`, same orders both ways; same
   arguments): the FIFTH consecutive econ null, and the SEP events are
   gone without making the units new.** 5,497 orders / 24 markets / 7
   underlyings (KXCPI 2,540, KXU3 1,087, KXFED 818, KXCPIYOY 551,
   KXPAYROLLS 501). All four readings `not_significant` and powered
   (floor 5 over / 2 under, `underlying_sign_p` 0.9375, `min` 0.0078;
   market tier 10/11/2 tied, p 0.5; ceiling identical). Crossing 200 vs
   queue-pess 206 / opt 266; `crossing_but_not_pess` 135 vs
   `pess_but_not_crossing` 141, `inside_bracket` 0. Floor
   `net_disagreement` -6 on `abs_net_by_underlying` 78: with KXCPI-26SEP
   and KXCPIYOY-26SEP out of the window the "SEP under / OCT over"
   reading of 10-15 and 10-22 does NOT survive -- the under side is now
   KXU3-26OCT -24 (was -2) and KXFED-26DEC -18, the over side KXCPI-26OCT
   +16 (from +35) and KXCPI-26NOV +14, the rest within +-3. The
   aggregate is a cancellation, not a direction. `independence`: 6 of 7
   underlyings shared with 10-22 (all 6 kept sign), 1 new
   (KXCPIYOY-26NOV), `new_share_vs_all` on orders 0.635 and on units
   0.143 -- so the rollover as DEFINED (no SEP event) arrived and the
   units still did not turn over, because the OCT events live for weeks
   and a 336h window always carries the prior read's. A clean econ
   replication needs reads spaced by the EVENT LIFETIME (a month for
   the monthly prints), not by the window; the next read that can be new
   against this one is ~11-26 or later. Victim side: the 08:17Z read
   held 404s (one episode, 28 fails, 87,947 pending, 0 spilled) -- the
   per-market-scan defect, mistakes #147, fixed the same pass; the
   restructured reader held 54.5s with the replay 3.3s of it, and the
   next weekly run reads its `cost` block.
2. **Econ prints vs ALFRED vintages** — weekly claims cadence
   accumulates sample fast. Gated on: B4 signal layer (SHIPPED 07-11; gate open).
   **Coverage probe (2026-10-14, read on the 10-13 backup; no edge was
   measured -- this is the pre-registration's INPUT, not a reading).**
   `KXJOBLESSCLAIMS` ("Weekly initial jobless claims", Economics,
   quadratic fee, weekly): 247 markets over 24 Thursday events 05-07 ..
   10-15, 10-11 `greater_or_equal` strikes per event at 5k spacing,
   close 12:25Z = 5 min before the 12:30Z print. Quotes exist for the
   14 events from 07-09 (~1,900 5-min snapshots per market, the last one
   AT close); the 9 earlier events have markets and results but no book.
   Pre-close book at T-60min (10-08 event): tails 0.98/1.00, mid-ladder
   spreads 6-7c, volumes 1-3.7k per strike. **Resolution rule verified
   against the vintage table:** for every one of the 14 events whose
   FIRST print `econ_vintages` captured (ICSA, pull started 07-12), the
   result equals `first_print >= floor_strike` on all 146 strikes, 0
   mismatches. For the 9 events before the pull existed, the table holds
   only the REVISED value, and 4 of those 9 events show exactly one
   crossed strike each -- the signature of an upward revision through a
   strike. So: the market settles on the first print, and a naive
   latest-vintage join misgrades ~4 of 9 events, which is the B4
   two-dimensional-vintage design doing its job. Revision pattern in the
   captured data, recorded as a fact and not a lead: 13 of 13 first
   prints were revised UP by 1-3k the following week (late-filed claims
   is the a-priori mechanism); it cannot touch a market that settles on
   the first print. `knowable_at` for a print is the pull's 03:59Z next
   day, ~15.5h after the market closed -- pessimistic by design, fine
   for resolution, and any feature must use the PRIOR weeks' prints.
   **Sample arithmetic at the honest unit (events, not strikes):**
   one-sided sign test at alpha 0.05 -- n=14: reject at k>=11, power 22% vs 65/35, 52% vs 75/25; n=26: reject at k>=18, power 41% vs 65/35, 82% vs 75/25; n=40: reject at k>=26, power 57% vs 65/35, 95% vs 75/25. So 14 events cannot
   power a directional reading against anything but a gross bias; ~26
   events (12 more weeks, ~2027-01-08) reach 41% power against 65/35
   and 82% against 75/25; 40 events (~2027-04-16) 57% and 95%. A calibration (atlas-style
   implied-vs-realized by strike distance) pools strikes within an event
   and must not count them as independent draws (the 08-29 lesson).
   Probe SQL: `ev = close_time::date, obs = close - 5 days; first print
   = arg_min(value, knowable_at) per obs_date; mismatch = (result='yes')
   <> (first >= floor_strike)`. Continuing claims (`KXCONTCLAIMS`, 7 weekly events
   since 08-27, 11 strikes each) is NOT yet a second pair: the archive
   holds ZERO snapshots for it (not on the collector's watchlist), `CCSA`
   is not in `alfred.SERIES`, and Kalshi's strike field changed units
   between events (08-27: 1.75-1.85, i.e. millions; from 09-03:
   1740-1840, thousands) -- a pair would need unit normalisation and a
   watchlist/pull change, both of which start a new comparability series.
   **Capture started 2026-10-15 (promoted ~03:45Z):** `KXCONTCLAIMS` is on
   the collector watchlist and `CCSA` in `alfred.SERIES`, so the 10-15
   event (11 strikes T1660..T1760, close 12:29Z, books empty at 03:27Z)
   is the first with snapshots. The first CCSA vintages landed with the
   10-15 04:40Z signals run (3,117 rows, obs 1967-01-07..2026-09-26,
   all stamped knowable 10-16 03:59Z -- the pessimistic stamp, so no
   settled event's first print counts as captured yet). Units differ:
   CCSA is persons (1,716,000 for 09-26), strikes are THOUSANDS from
   09-03 on (`T1660`) and were MILLIONS on the 08-27 event (`T1.75`).
   The offset is 12 days (every title: close 10-15 reports "week ending
   Oct 3"), one week behind ICSA's. Its sample clock starts at this
   event: ~26 quoted events is ~2027-04-09.
   The rule is a standing QA check for BOTH pairs,
   `econ market settles on the first print (KXJOBLESSCLAIMS vs ICSA)`
   and `(... KXCONTCLAIMS vs CCSA)` (`qa.qa_econ_resolution`, 2026-10-14,
   pair + unit guard 2026-10-15): events whose first print the pull
   captured within 6d of the close, mismatches FAIL and name the event,
   none captured is an UNVERIFIED watch. Each pair carries its strike
   unit (x1, x1000), and a strike whose scaled value is not within 2x of
   the print is a UNIT failure kept OUT of the settlement compare -- the
   08-27-style quoting change lands as a named failure, never as an
   always-true pass. First reading for the pair: the 10-16 10:00Z QA,
   once the 10-15 event settles (12:30Z print) and the 10-16 04:40Z
   pull captures CCSA for 10-03.
   **10-15 event settled, read 14:15Z (read-only; ALFRED keyless, no
   archive write):** 11 strikes, T1660..T1710 `yes`, T1720..T1760 `no`,
   1,177 snapshots over 11 markets 03:35Z..12:25Z (the last cycle before
   the 12:29Z close; book at 12:25Z: T1700 0.69/0.79, T1710 0.45/0.55,
   T1720 0.24/0.34 -- the market priced the print inside the bracket it
   landed in). ALFRED's 10-15 vintage, fetched after the print: CCSA
   obs 10-03 = **1,710,000**, i.e. `1,710,000 >= 1710 x 1000` is yes and
   `>= 1720 x 1000` is no -- 11 of 11 strikes agree with the first-print
   rule at the x1000 unit. PREDICTION for the 10-16 10:00Z QA: the
   CCSA line turns from UNVERIFIED to `PASS 1 event(s) / 11 strike(s),
   0 mismatches` (the 10-16 04:40Z pull stamps the 10-03 obs knowable
   10-17 03:59Z, inside the 6d window; the 10-08 event stays EXCLUDED
   even though the archive happens to hold its true first print,
   1,716,000 for 09-26 fetched before the revision, because the
   pessimistic stamp 10-16 03:59Z falls outside 10-08 + 6d -- the
   conservative side), and the ICSA line reads 15 events / 157 strikes
   (ICSA obs 10-10 first print 199,000; the 10-15 event settled 195000
   `yes`, 200000 `no`). Anything else on either line is a finding.
   **Revision direction is a per-series fact, measured 2026-10-15 from
   14 consecutive ALFRED release-day vintages (07-09..10-08, each first
   print vs the next week's vintage of the same week):** ICSA revised
   UP 13 of 14, 0 down, 1 unchanged, +1k..+3k against 5k strikes (the
   note above, extended one week); CCSA revised **DOWN 12 of 14**, 2
   up, -2k..-13k against 10k strikes (07-04 -7k, 09-05 -13k, 09-26 -2k).
   The a-priori mechanism differs: initial claims gain late filings,
   continuing claims lose them to re-employment and state corrections.
   Consequence for the pair, recorded as a fact and not a lead: a
   latest-vintage join would misgrade a CCSA event in the OPPOSITE
   direction to an ICSA one (revised value below a strike the first
   print cleared reads a settled `yes` as a mismatch), and the ratio of
   revision to strike spacing (up to 1.3x for CCSA vs 0.6x for ICSA)
   makes a crossed strike MORE likely per CCSA event; both are reasons
   the first-print check, not a latest-vintage read, is the rule for
   either pair. Neither direction can touch a market that settles on
   the first print, and a feature built on CCSA prints must use the
   vintage as of the PRIOR week, which the two-dimensional table gives
   it. **Boundary semantics verified live on both pairs (2026-10-15):**
   `greater_or_equal` is INCLUSIVE in settlement practice, which is the
   one case a `>` vs `>=` slip in the probe or the check would misgrade.
   ICSA prints land on a 5k strike about one event in five (4 of 23
   events; prints are 1k-granular) and the only CAPTURED one, 07-09
   (first print 215,000, strike 215000), settled `yes`; today's CCSA
   event settled T1710 `yes` on a first print of exactly 1,710,000. The
   three pre-pull ICSA equality rows (05-21 210k `no`, 06-04 225k
   `yes`, 06-11 230k `no`) are REVISED values sitting on a strike, i.e.
   two of the four crossed-strike events named above -- an upward
   revision onto the strike from a first print below it reads as a
   `no` at equality, which is the latest-vintage misgrade, not a
   boundary exception. The QA check's `>=` is the right comparison on
   both pairs, and the pre-reg's model must score the strike-equality
   outcome as `yes` for the ladder's `floor_strike`.
   **REGISTERED 2026-10-15 20:20Z, BEFORE the sample exists:**
   `docs/hyxpredict/prereg_econ_claims_backtest.md` (EconClaimsRW v1).
   Written now rather than "when ~26 events have quotes", because a
   registration drafted with 26 events of prices and prints in the
   archive is contaminated by whatever its author glanced at counting
   them. Frozen: a zero-parameter random walk on the prior week's FIRST
   print as-of decision time (`ctx.econ_series`, so the vintage table
   decides what was knowable), sigma from the trailing 52 weekly changes
   as known then, half-unit continuity correction toward YES (equality
   settles yes); one look per market at the first snapshot in
   [close-60min, close-50min]; taker IOC 10 contracts on the side the
   model clears the ask by >= 0.10; held to settlement; the EVENT is the
   unit. One read at 26 scorable events (sign test k=18 of n traded,
   ROI > +1.0%, both halves positive, n_traded >= 20 else UNDERPOWERED
   with one pre-declared second read at 40). A primary SURVIVE is
   exploratory (atlas contact disclosed) and authorizes only a Tier-2
   registration; a FAIL closes the random-walk claims family. Code:
   `strategies.econ_claims.EconClaimsRW` + `simulator.run_econ_claims`,
   whose count gate refuses to replay or print PnL under 26 -- it decides
   scorability from markets, snapshots and vintages alone, never from a
   price against an outcome. First gated run 20:25Z: ICSA 11 scorable
   (08-06..10-15; the 07-16/07-23/07-30 events have a 11:10-11:40Z
   focus-tape hole and the window is NOT widened to admit them), CCSA 0
   (set begins 10-22). 26th ICSA event ~2027-01-28, CCSA ~2027-04-15.
   Until then it accumulates on its own; nothing to run.
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
- **An atlas flag is a SEARCH result, at every tier including the
  strictest** (2026-09-19, mistakes #63). The quoted tier returned its
  first `confirmed` ever on 09-19 (`Economics|1h|d2`) -- one of 18
  simultaneous tests, at a nominal alpha of 0.0443, when a complete null
  expects 0.9 of them, and on the 4th look at one accumulating sample
  with the gap flat across all four. Family-wise (Holm, 0.05/18 =
  0.00278) it does not survive, and **no quoted bucket has ever confirmed
  against the search that produced it.** Read `quoted_verdict.family`,
  `quoted_alpha` and `quoted_looks` BEFORE treating any atlas bucket as a
  lead worth registering; `flagged_quoted` alone is the least informative
  of the four.

## Related
- [simulation-honesty](simulation-honesty.md) — the machinery enforcing this
- [venues](venues.md) — which strategies are possible where
