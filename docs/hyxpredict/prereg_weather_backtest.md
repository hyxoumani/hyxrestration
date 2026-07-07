# Pre-registration: Tier-1 weather backtest (WeatherNWS v1)

**Registered 2026-07-06, BEFORE the full-sample backtest was run.**
Same discipline as Phase 0 (`docs/phase0_testing.md`): thresholds are fixed
here first; the results section is appended after, unmodified.

## Disclosure: prior data contact

A 10-day, NYC-only smoke test was run on 2026-07-06 to validate the
pipeline (rate-limit handling + MOS-parsing MAE diagnostic). It produced
43 fills, settled net PnL +$84.36 on $366.60 cost, and forecast MAE 2.1°F.
This is ~2–3% of the full sample and one of five cities. It weakly
contaminates the full-sample test (a catastrophically broken strategy
would already have been visible). It did NOT inform any parameter choice —
all parameters below are the v1 defaults set before any backtest ran.

## Hypothesis

A gaussian model around the archived day-ahead NWS/MOS forecast high,
traded against Kalshi daily-high brackets only when the fee-adjusted model
edge exceeds 5¢, earns positive settled PnL — i.e., the "retail anchoring"
weather edge described in public sources still existed over the past year
at Tier-1 fidelity.

## Fixed configuration (binding)

- Strategy: `WeatherNWS(sigma=2.7, bias=0.0, min_edge=0.05, max_qty=20,
  trade_same_day=False)` — repo defaults as of this registration.
- Data: all settled `KXHIGH{NY,CHI,MIA,AUS,DEN}` markets with close time
  in the 365 days ending 2026-07-06; hourly candles; fills at candle
  yes-ask/no-ask close (taker), Kalshi fee model (0.07 parabolic,
  ceil-to-cent); positions held to settlement; one entry per market.
- Forecasts: archived GFS-MOS extended (MEX) 00Z/12Z runs from IEM,
  served strictly as-of candle timestamps.
- Command: `python -m hyxlab.run_backtest` (defaults).

## Validity gate (before reading PnL)

Day-ahead forecast MAE per station must fall in [2.0, 5.0]°F. Any station
outside → the run is invalid (parsing/convention bug), no PnL is read.
(The runner enforces this mechanically.)

## Endpoints

**Primary** — total settled net PnL (payout − cost − fees) across all five
cities, and ROI = PnL / settled cost:

- **PASS**: PnL > 0 AND ROI ≥ +5% AND ≥ 200 settled fills.
- **FAIL**: ROI ≤ 0.
- **INCONCLUSIVE**: 0 < ROI < +5%, or < 200 settled fills.

**Robustness** (a PASS is downgraded to WEAK PASS unless all three hold):

- R1: settled PnL positive in ≥ 3 of 5 cities.
- R2: no single city accounts for > 80% of total positive PnL.
- R3: settled PnL positive in both halves of the 365-day window.

## Binding interpretation clauses

1. Parameters above are frozen. Any re-run with different sigma/bias/
   min_edge/max_qty is EXPLORATORY, must be labeled as such, and cannot
   upgrade the verdict.
2. Tier-1 fills are optimistic (no displayed-size cap at candle
   granularity; max_qty=20 is the only size discipline). Therefore even a
   full PASS green-lights only **Tier 2** (forward top-of-book replay) —
   never capital. A FAIL at Tier 1 kills the thesis outright.
3. Capital deployment is out of scope regardless of outcome (user-only
   decision, separate authorization).

## Addendum 1 (2026-07-06, before any PnL was read)

Two protocol events during execution, logged before results:

1. **Sample is smaller than registered.** Kalshi's public settled-market
   history for the KXHIGH* series begins 2026-04-30 (verified: queries
   with `max_close_ts` before that date return empty). The registered
   window ("365 days ending 2026-07-06") therefore resolves to
   **2026-04-30 → 2026-07-05, 2,070 settled markets, summer-only**. All
   thresholds unchanged; external-validity caveat: no cold-season regimes
   in sample.
2. **Validity gate misfired on MIA** (day-ahead MAE 1.28°F, below the
   registered 2.0 floor). The floor existed to catch parsing bugs/leaks.
   Leak ruled out before reading PnL: exact-match rate 25.0% (a
   forecast←observation leak would be ≈100%; 25% is what MAE 1.28 with
   integer °F predicts), error std 1.73 with ±6°F misses, and the
   identical code path yields 2.2–3.0 MAE elsewhere — a parsing bug would
   not be station-selective. Miami summer highs are climatologically
   easy. **Revised gate**: MAE ∈ [1.0, 5.0] AND exact-match rate < 60%
   per station. No other criteria touched.

## Results (appended after the run — do not edit above this line)

**Run 2026-07-06, `python -m hyxlab.run_backtest` (registered defaults).**

Validity gate (Addendum-1 form): PASS — all five stations MAE ∈ [1.28, 2.98],
exact-match ∈ [11%, 25%].

| Endpoint | Registered threshold | Observed | Verdict |
|---|---|---|---|
| Settled net PnL | > 0 for PASS | **−$395.55** | — |
| ROI (PnL / settled cost) | ≥ +5% PASS / ≤ 0 FAIL | **−3.0%** ($13,195.80 cost) | **FAIL** |
| Settled fills | ≥ 200 | 1,654 | adequate sample |

Per city: AUS −9.8%, CHI −7.5%, DEN −2.6%, MIA −0.5%, **NYC +8.1%** —
4 of 5 cities negative (R1 would also have failed).

## Verdict: FAIL — thesis killed at Tier 1

Per binding clause 2, WeatherNWS v1 (gaussian around the archived day-ahead
MOS high, fixed sigma 2.7, no bias correction, 5¢ min edge) is dead. Notably:

- Gross PnL before fees was ≈ break-even (−$75.80 on $13.2K traded): the
  naive model has roughly zero edge against these markets, and fees
  (−$319.75) decide the sign. The market prices these brackets at least as
  well as raw MOS.
- Tier-1 fills are optimistic (no depth cap), so live results would be
  **worse** than this.
- The 10-day NYC smoke peek (+23% ROI) was disclosed in this registration;
  the full sample shows NYC is the *only* positive city. The peek was
  unrepresentative noise — pre-registration did its job.
- AUS has the largest forecast warm-bias (+2.1°F) and the worst PnL —
  consistent with the loss being partly self-inflicted calibration error.

## Sanctioned follow-ups (new registrations required; cannot rescue v1)

1. **WeatherNWS v2**: per-city bias/sigma fit on the first half of the
   window, evaluated out-of-sample on the second half, registered before
   evaluation. The AUS bias signal and NYC result justify the experiment;
   they do not predict its outcome.
2. **Econ-print thesis** (CPI/claims vs public nowcasts) — next family in
   the memo's queue, same pipeline.

Caveats on external validity either way: 67-day summer-only sample
(Kalshi's public settled history for KXHIGH* starts 2026-04-30), MEX is a
model product and not identical to the NWS point forecast retail sees.
