# Pre-registration: Tier-1 favorite-longshot backtest (FavoriteLongshot v1)

**Registered 2026-07-11, BEFORE the backtest was run.** Thresholds are
fixed here first; the results section is appended after, unmodified.
Same discipline as `prereg_weather_backtest.md`.

## Disclosure: prior data contact

The calibration atlas (`simulator/atlas.py`, built and run 2026-07-11,
`reports/atlas/20260711T194527.json`) computed AGGREGATE implied-vs-
realized rates by (category, price decile, horizon) over the same
archive this backtest replays, and flagged a favorite-longshot
signature (favorite deciles 7–9 realized above implied in most
categories). This registration is therefore **motivated by in-archive
evidence** and the Tier-1 test below is partially confirmatory of data
already seen in aggregate.

Mitigations, in order of force:

1. **Tier-1 is kill-only by house rule** — a SURVIVE here green-lights
   nothing; it only justifies a *separate* Tier-2 registration on
   stream-replay data (2026-07-08 onward + accumulating), which the
   atlas never touched at fill fidelity, plus forward data the atlas
   has never seen at all.
2. **No parameter was tuned on the atlas.** The trading band, horizon,
   and sizing below are fixed from the public literature convention
   (QuantPedia synthesis; arXiv 2602.19520: the bias concentrates in
   favorites priced ~0.75–0.95 and longshots under ~0.20) and from the
   proposal's pre-atlas design notes. The atlas informed the DECISION
   to run this test now, not its parameters.
3. **Family size 1.** Exactly one variant is registered and will be
   run. Any second variant makes both exploratory (manifest
   trial-counting applies; DSR would deflate accordingly).

## Hypothesis

Buying the FAVORITE side of archived Kalshi binary markets at a taker
ask in [0.80, 0.95], one day before close, held to settlement, earns
positive settled PnL net of Kalshi fees — i.e., the documented
favorite-longshot bias survives fees and Tier-1 (optimistic-fill)
replay on this archive. (Buying the favorite side subsumes shorting
longshots: buying NO of a 0.15-yes market is buying an 0.85-favorite.)

## Fixed configuration (binding)

- Strategy: `FavoriteLongshot(band=(0.80, 0.95), qty=10,
  window_hours=(24, 12))` — one look per market: at the FIRST candle
  snapshot whose ts falls in [close−24h, close−12h], if the favorite
  side's ask (the price actually paid) is inside the band, buy 10
  contracts IOC; otherwise the market is done (no re-checking — no
  optional stopping). Positions held to settlement. Favorite side =
  YES if mid ≥ 0.5 else NO.
- Universe: all settled Kalshi binary markets (`result` in yes/no)
  with a close_time, all categories in the archive's 8-category
  allowlist; hourly candles via `candles_as_snapshots` (crossed and
  sentinel candles excluded by the standing gate).
- Fills: taker at candle ask close, Kalshi parabolic fee model
  (0.07, ceil-to-cent) — the model that killed weather v1.
- Command: `python -m simulator.run_favlong` (defaults).

## Validity gates (before reading PnL)

- G1: ≥ 300 settled fills, else INCONCLUSIVE (no PnL verdict read).
- G2: mean entry price in [0.80, 0.95] and every fill's price in-band
  (construction check; violation = implementation bug, run invalid).

## Endpoints

**Primary** — total settled net PnL (payout − cost − fees) and
ROI = PnL / settled cost:

- **FAIL (kill)**: ROI ≤ 0. The strategy joins the dead list; no
  retro-rescue.
- **SURVIVE (promotes to a Tier-2 registration; never capital)**:
  PnL > 0 AND ROI ≥ +2% AND G1 met. (+2% net-of-fees on a ~0.87
  average cost basis over day-scale holds is economically meaningful;
  set a priori — favorite-side edges are structurally small per
  contract.)
- **INCONCLUSIVE**: 0 < ROI < +2%.

**Robustness** (a SURVIVE is downgraded to WEAK SURVIVE unless all
hold):

- R1: settled PnL positive in ≥ 3 categories having ≥ 50 fills each.
- R2: no single category contributes > 80% of total positive PnL.
- R3: PnL positive in both halves of the archive window (split at the
  median close_time of filled markets).
- R4: PnL positive in both sub-bands [0.80, 0.875) and [0.875, 0.95].

**Supplementary (reported, not verdict-bearing)**: per-fill settled
return series through `simulator.iterate.deflated_sharpe`
(n_trials=1 ⇒ PSR), and the fee share of gross PnL.

## Binding interpretation clauses

1. Parameters above are frozen. Re-runs with different band/horizon/
   qty are exploratory and cannot upgrade this verdict.
2. Tier-1 SURVIVE cannot authorize capital under any wording. The only
   thing it buys is a Tier-2 registration on stream-fidelity data.
3. If the runner aborts on G2, fix the implementation and re-run; G2
   protects against bugs, not against results.
4. Category breakdowns beyond R1–R4 are descriptive only.

## Results (appended after the run, unmodified)

_pending_
