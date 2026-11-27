# Forecast-error probe: live NWS gridpoint highs vs observed highs

**2026-11-27. Descriptive only: no market price was read, no PnL exists,
and nothing here is a verdict.** It is prior data contact for any
WeatherNWS v2 registration and must be disclosed in one.

## What was read

- Forecasts: `nws_forecasts` from the backup slot `hyxlab.Fri.duckdb`
  (taken 11-27 03:30Z; 1,406,695 rows; no live hold). Two sources in one
  table: backfilled GFS MEX MOS (`fetched_at` < 2026-07-06 12Z, 2 runs a
  day) and the live gridpoint pull (from 2026-07-06 19:36Z, every 5 min).
- Truth: `observations` from the live archive (2,540 rows, the NWS
  climate-report daily high per station, complete through 11-25 since
  mistakes #251).
- Sample: target dates 2026-07-08..11-25, 141 days x 5 stations. The
  forecast for target day D "as of hour h" is the newest pull at or
  before `D 00:00Z + h`, no older than 1 h (ASOF join); 138-139 of 141
  days have one at every h. Error = forecast - observed, whole degrees F.

## 1. Error by lead, all stations

| as of | n | bias | sd | MAE |
|---|---|---|---|---|
| D-2 12Z | 685 | -0.19 | 2.68 | 1.98 |
| D-1 00Z | 685 | -0.11 | 2.50 | 1.88 |
| D-1 12Z | 690 | -0.09 | 2.39 | 1.80 |
| D-1 18Z | 689 | -0.10 | 2.40 | 1.80 |
| D 00Z | 690 | -0.09 | 2.27 | 1.68 |
| D 06Z | 694 | -0.13 | 2.23 | 1.66 |
| D 12Z | 695 | -0.13 | 2.02 | 1.51 |
| D 18Z | 689 | -0.15 | 2.03 | 1.53 |

## 2. By station (as of D-1 12Z / D 00Z / D 12Z)

| station | bias | sd | MAE | share within 1 F (D 00Z) | share 5 F or more off (D 00Z) |
|---|---|---|---|---|---|
| AUS | -0.28 / -0.41 / -0.24 | 2.81 / 2.73 / 2.17 | 2.10 / 1.98 / 1.70 | 50% | 9.4% |
| CHI | -0.70 / -0.76 / -0.79 | 2.31 / 2.14 / 1.83 | 1.93 / 1.78 / 1.53 | 47% | 6.5% |
| DEN | -0.17 / -0.23 / -0.37 | 2.35 / 2.17 / 1.94 | 1.83 / 1.68 / 1.49 | 50% | 4.3% |
| MIA | +0.12 / +0.21 / +0.06 | 1.78 / 1.60 / 1.61 | 1.28 / 1.14 / 1.14 | 70% | 1.4% |
| NYC | +0.57 / +0.75 / +0.68 | 2.42 / 2.27 / 2.21 | 1.84 / 1.84 / 1.72 | 49% | 4.3% |

Monthly bias at D 00Z: CHI -0.9..-1.1 in each of Aug-Nov (+0.4 in July);
NYC +0.9..+1.1 in each of Jul-Oct, then -0.3 in November; AUS swings
-1.4 (Aug) to +0.6 (Oct); DEN and MIA within +/-0.9 with no sign held.

## 3. The two sources, same season (different years)

MEX 12Z run of D-1, target dates 2025-07-08..11-25, against the
gridpoint forecast as of D-1 12Z, 2026-07-08..11-25:

| station | MEX bias / sd / MAE | gridpoint bias / sd / MAE |
|---|---|---|
| AUS | +3.16 / 2.97 / 3.61 | -0.28 / 2.81 / 2.10 |
| CHI | +1.64 / 2.46 / 2.38 | -0.70 / 2.31 / 1.93 |
| DEN | +1.62 / 3.34 / 2.76 | -0.17 / 2.35 / 1.83 |
| MIA | +0.11 / 1.72 / 1.23 | +0.12 / 1.78 / 1.28 |
| NYC | -1.04 / 2.51 / 2.05 | +0.57 / 2.42 / 1.84 |
| all | +1.10 / 3.01 / 2.41 | -0.09 / 2.39 / 1.80 |

The year differs, so this is two sources in two years, not a controlled
comparison. Over MEX's full year (2025-07..2026-07) its 12Z D-1 errors
were sd 3.2-3.7 outside MIA, with AUS biased +2.1.

## 4. Does a per-city walk-forward fit beat v1's fixed parameters?

The queue's v2 premise is "per-city bias/sigma, walk-forward". Tested on
forecast calibration alone, as of D-1 12Z: each day's gaussian
log-likelihood under a per-city (bias, sigma) fitted on strictly prior
days (ending D-2, since the observed high for D-1 is not yet published),
against v1's fixed (bias 0, sigma 2.7). Mean log-likelihood per
city-day, higher is better:

| fitting window | n | per-city bias+sigma | per-city sigma only | fixed (0, 2.7) |
|---|---|---|---|---|
| trailing 30 d | 585 | -1.431 | -1.420 | -1.392 |
| trailing 60 d | 485 | -1.422 | -1.419 | -1.404 |
| expanding | 485 | -1.408 | -1.409 | -1.404 |

The per-city fit was not better in any of the three windows pooled. By
city (30 d): better only for MIA with sigma alone (-1.173 vs -1.227);
worse for AUS, DEN, NYC, CHI. Bias correction alone moves pooled MAE
1.696 -> 1.686 at D 00Z: it helps CHI and NYC (the two with a held
sign) by 8-9% and costs AUS, DEN and MIA about as much.

The hindsight ceiling (each city's own full-sample sigma, bias 0) is
-1.35 pooled: 0.04 nats per city-day above the fixed parameters, and no
walk-forward window reached it at this sample size.

## What this does and does not say

- The live gridpoint forecast's day-ahead error is sd ~2.4 F pooled, and
  it is near-unbiased pooled; v1's sigma 2.7 is close to it. v1 was run
  on MEX, which in the same season a year earlier was sd 3.0 with a
  +1.1 F pooled bias (AUS +3.2).
- Per-city parameters fitted walk-forward on 141 days do not calibrate
  better than one fixed pair. The premise of queue item 3 has no support
  in this data yet; MIA's narrower sigma is the one per-city difference
  that held.
- It says nothing about whether Kalshi's brackets already price this
  forecast. That needs prices and is a registered backtest, not a probe.
- Three windows were tried for section 4 and all three are reported.
