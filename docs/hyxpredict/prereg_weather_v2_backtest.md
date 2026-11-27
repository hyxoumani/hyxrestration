# Pre-registration: Tier-1 weather backtest on the live gridpoint forecast (WeatherNWS v2)

**Registered 2026-11-27, BEFORE any backtest on this sample was run and
before the runner that can run it exists** (the v1 runner cannot select
a forecast source or a target-date window). Thresholds and
configuration are fixed here first; the results section is appended
after, unmodified. Repo at registration: `734260c`.

This is a new registration, not a rescue of v1 (FAIL, 2026-07-06,
`prereg_weather_backtest.md`): v1's verdict stands and its sample
(2026-04-30..07-05) is not re-read. One thing differs from v1 -- the
forecast the model is centred on -- and no parameter does.

## Disclosure: prior data contact

- `research/weather_forecast_error_probe.md` (2026-11-27) read the
  forecasts and the observed highs for this exact window: gridpoint
  error sd 2.39 F / bias -0.09 pooled at D-1 12Z, per-station tables,
  and the finding that a walk-forward per-city (bias, sigma) did not
  calibrate better than the fixed (0, 2.7). That finding is why the
  parameters below are v1's and not per-city fits. **No market price,
  candle value, fill or PnL for this window has been read.** Candle
  ROWS were counted for coverage (below), values unread.
- v1's result is known: gross about break-even on MEX MOS, fees decided
  the sign. The expectation going in is FAIL -- the NWS point forecast
  is public and the market has had it all along. The test is run to
  close strategy-queue item 3 on evidence rather than leave it open.
- This is trial 2 of the WeatherNWS family (for DSR deflation).

## Hypothesis

A gaussian around the latest archived NWS gridpoint forecast high,
traded against Kalshi daily-high brackets before the target day begins
and only when the fee-adjusted model edge exceeds 5 cents, earns
positive settled PnL at Tier-1 fidelity over 2026-07-08..11-25.

## Fixed configuration (binding)

- Strategy: `WeatherNWS(sigma=2.7, bias=0.0, min_edge=0.05, max_qty=20,
  trade_same_day=False)` -- `strategies/weather.py` unchanged from
  `734260c`. Identical to v1.
- Markets: every `KXHIGH{NY,CHI,MIA,AUS,DEN}` market with
  `target_date` in 2026-07-08..2026-11-25 inclusive and a result of
  yes/no. Counted at registration on the 11-27 03:30Z backup slot:
  4,230 settled markets over 141 days x 5 series, 4,164 of them with
  hourly candles (period 3600 s; 159,955 rows). The 66 markets with no
  candles cannot fill and are reported as a count, not repaired.
- Fills: candle-close snapshots (`candles_as_snapshots`), taker at the
  candle's yes-ask / no-ask close, Kalshi fee model, held to
  settlement, one entry per market, simulator defaults as of `734260c`
  (including whatever crossed-candle handling the default has).
- Forecasts: ONLY rows of `nws_forecasts` with `fetched_at >=
  2026-07-06 19:36:00` (the live gridpoint pull). The backfilled MEX
  MOS rows are excluded by that bound. Served strictly as-of the
  snapshot timestamp, as the simulator already does.
- Data source: a backup slot of `hyxlab.duckdb` taken after
  2026-11-27 09:04Z (the first to hold the observed highs through
  11-25), named in the results.
- The runner change this needs (a forecast-source bound and a
  target-date window on `simulator.run_backtest`) is plumbing only. It
  ships with tests before the run, and the run's command line is
  recorded in the results.

## Validity gate (before reading PnL)

v1's Addendum-1 form, unchanged: per station, the runner's day-ahead
forecast MAE in [1.0, 5.0] F AND exact-match rate under 60%. Any
station outside -> the run is invalid and no PnL is read. (The probe
puts every station inside: MAE 1.1-2.1.)

## Endpoints

**Primary** -- total settled net PnL (payout - cost - fees) across all
five cities, and ROI = PnL / settled cost:

- **PASS**: PnL > 0 AND ROI >= +5% AND >= 200 settled fills.
- **FAIL**: ROI <= 0.
- **INCONCLUSIVE**: 0 < ROI < +5%, or < 200 settled fills.

**Robustness** (a PASS is downgraded to WEAK PASS unless all three hold):

- R1: settled PnL positive in >= 3 of 5 cities.
- R2: no single city accounts for > 80% of total positive PnL.
- R3: settled PnL positive in both halves of the window by target date
  (2026-07-08..09-16 and 2026-09-17..11-25).

## Binding interpretation clauses

1. Parameters and window are frozen. Any re-run with a different
   sigma / bias / min_edge / max_qty / window / forecast bound is
   EXPLORATORY, labelled as such, and cannot upgrade the verdict.
2. Tier-1 fills are optimistic (no displayed-size cap at candle
   granularity). A full PASS green-lights only Tier 2 (forward
   top-of-book replay), never capital. A FAIL kills the gaussian-
   around-the-public-forecast thesis on both sources, and queue item 3
   closes with it: no per-city variant is permitted afterwards, since
   the probe already showed the per-city fit does not calibrate better.
3. Capital is out of scope regardless of outcome (user-only decision).
4. One run. If the run aborts on the validity gate or a bug, the fix
   and the reason are logged in an addendum BEFORE the rerun, as in v1.

## Addendum 1 (2026-11-27, before any PnL was read)

The runner plumbing shipped the same day: `--forecast-since` and
`--target-from/--target-to` on `simulator.run_backtest`
(`tests/test_run_backtest_scope.py`, 9 tests, 8 mutants red). It adds
a scope line and a settled-PnL breakdown by half of the target window
(the R3 split, first half through 2026-09-16); strategy, simulator and
fee model are untouched.

Rehearsed once on the 11-27 03:30Z slot, chosen BECAUSE it must abort:
that slot's observed highs end 2026-07-06, so the validity gate has no
station to judge and the runner stops before the replay. It did --
`4230 markets, 4230 settled, 4164 with usable candles, 1342859 forecast
rows`, empty diagnostic, `ABORT`, 3 s wall, 2.4 s hold on the slot, no
fill and no PnL computed. The counts match the registration. This is
not the registered run and not a clause-4 abort.

The registered run, once, on the first slot taken after 11-27 09:04Z:

    .venv/bin/python -m simulator.run_backtest \
      --db /home/devs/hyx-backups/hyxlab.Sat.duckdb \
      --forecast-since 2026-07-06T19:36:00 \
      --target-from 2026-07-08 --target-to 2026-11-25

(check the slot's mtime is 2026-11-28 before running: the Sat slot is
last week's until 03:30Z.)

## Results (appended after the run -- do not edit above this line)

_Not run yet._
