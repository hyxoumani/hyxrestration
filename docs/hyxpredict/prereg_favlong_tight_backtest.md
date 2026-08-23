# Pre-registration: spread-conditioned favorite entry (FavLongTight v1)

**Registered 2026-08-22, BEFORE the backtest was run.** Thresholds are
fixed here first; the results section is appended after, unmodified.
Same discipline as `prereg_favlong_backtest.md`, whose FAIL this
registration is a direct successor to — and, read carelessly, a
retro-rescue of. The next two sections exist to make that judgeable.

## Why this is not a retro-rescue of FavoriteLongshot v1

FavoriteLongshot v1 (2026-07-11) bought the favorite side at the taker
ask in [0.80, 0.95], 24h before close, and FAILED: ROI −5.0% on
$74.4K/8,363 fills, negative in 4/5 categories and both halves. Its
recorded cause of death is specific and is the whole basis for this
registration:

> gross already negative, **the spread decides**: realized 85.2% vs
> 89.0¢ paid at the ask, while the atlas's favorite-underpricing lives
> at the MID.

That is a statement about a CONDITION (how far the ask sat above the
mid), not about the band. This registration fixes that condition rather
than moving the band to where the loss is smaller: entries are taken
**only when the book's spread is one tick**, so the ask is within half a
tick of the mid and the 3.8¢ ask-vs-mid gap that killed v1 cannot open.

The house rule against retro-rescues is about re-reading a dead result
until it looks alive. The discriminator applied here: v1's own
post-mortem named the variable, the variable is measurable independently
of outcome, and this test is run on entries v1 mostly never took (the
tight-spread subset is 24% of the 80-95¢ candidate rows). If it FAILS,
the fav-long family is closed for good and no third variant is
registered — that commitment is part of this registration.

## Disclosure: prior data contact

Motivated by `simulator.shadow_attribution` run 2026-08-22 over shadow
run `20260810T081931`. `strategies.probe.TightSpreadProbe` — which is
explicitly "NOT a money thesis", buys YES at the touch whenever the
spread is one tick and the mid is under 0.5, and takes no view — settled
1,324 positions and priced out monotone in entry price:

    band      n   avg_entry  win_rate   bias = entry − win_rate
    <5c     428      0.030     0.000        +0.030
    5-15c   379      0.090     0.042        +0.048
    15-25c  188      0.194     0.181        +0.013
    25-35c  140      0.306     0.314        −0.008
    35-45c  127      0.401     0.520        −0.119
    45c+     62      0.483     0.758        −0.275

The favorite end (negative bias = the taker UNDERpaid) is the signal
this test buys, and the measurement was taken under a one-tick spread
gate — i.e. exactly the condition registered below. This is therefore
**motivated by in-archive evidence and is partially confirmatory**:

1. **Overlapping data.** The probe's fills come from the stream archive
   over 2026-08-10→08-20, on the same venue the Tier-1 replay covers.
   The replay's universe is far larger, but the overlap is real and is
   not excluded — excluding it would be a parameter choice made after
   seeing the data.
2. **Family size 2, and it deflates both members.** This is the second
   variant in the favorite-longshot family. Per the standing rule on
   `prereg_favlong_backtest.md` ("any second variant makes both
   exploratory"), a SURVIVE here is exploratory, counts 2 trials into
   the DSR family, and green-lights nothing on its own.
3. **Power check performed, outcomes untouched.** Candidate entry rows
   were COUNTED before registration to confirm the test can resolve:
   80-95¢ tight 170,720 rows / 10,960 markets; 95-99¢ tight 218,088 /
   12,867. No result, payout, or PnL column was read.
4. **No parameter tuned on the probe.** The spread gate is one tick
   because that is v1's named cause of death and the probe's own gate,
   not because a sweep preferred it. Bands are v1's band plus its
   untested complement (below). Horizon is copied from v1 unchanged.

## Hypothesis

Buying the favorite side of archived Kalshi binary markets at a taker
ask, **conditional on a one-tick book spread**, one day before close and
held to settlement, earns positive settled PnL net of Kalshi fees.

Secondary, registered here so it cannot be chosen after the fact: the
same test on [0.95, 0.99], the band v1 never traded. This band is where
the probe's measurement is strongest (428 sub-5¢ positions, ZERO
winners) and where Kalshi's `0.07·p·(1−p)` fee is smallest as a share of
stake — at a 97¢ favorite the fee is ~0.2% of the amount risked, against
~5.6% of notional on the probe's cheap book. It is also where the 1¢
tick floor bites hardest, which is the reason to doubt it.

## Fixed configuration (binding)

- Entry: at the FIRST hourly candle whose `end_ts` falls in
  [close−24h, close−12h] — one look per market, no re-checking, no
  optional stopping. Take the entry iff BOTH hold at that candle:
  - `round((yes_ask_close − yes_bid_close) · 100) <= 1` (the gate), and
  - the favorite side's ask is inside the band under test.
  Favorite side = YES if mid ≥ 0.5 else NO; the NO ask is `1 − yes_bid_close`
  and its spread is the same one tick by construction.
- Bands (both registered, both reported, neither is the headline):
  **A = [0.80, 0.95]** (v1's band, the like-for-like comparison) and
  **B = [0.95, 0.99]** (v1's untested complement).
- Size: 10 contracts IOC. Held to settlement. No exits.
- Universe: all settled Kalshi binary markets (`result` in yes/no) with
  a `close_time`, the archive's 8-category allowlist, hourly candles via
  `candles_as_snapshots`, crossed/sentinel candles excluded by the
  standing gate — identical to v1 so the comparison is like-for-like.
- Fills: taker at the candle ask close; Kalshi parabolic fee model
  (0.07, ceil-to-cent) — the model that killed weather v1 and fav-long v1.

## Thresholds (binding, fixed before the run)

Judged per band, independently:

- **SURVIVE** requires ALL of: net ROI > +1.0% after fees; n ≥ 2,000
  fills; positive in ≥ 4 of 5 categories with ≥ 100 fills; positive in
  BOTH time halves of the archive; and net ROI > 0 in each of the band's
  two sub-halves (A: 0.80–0.875 / 0.875–0.95; B: 0.95–0.97 / 0.97–0.99).
- **FAIL** otherwise. A band with n < 2,000 fills reads UNDERPOWERED,
  not FAIL, and neither survives nor kills — it is reported as unresolved
  and no further variant is registered on its evidence.
- The +1.0% floor is above zero deliberately: Tier-1 fills are
  optimistic (the ask is assumed available), so a result inside noise of
  break-even is not evidence of an edge that would survive Tier-2.

If both bands FAIL, **the favorite-longshot family is closed** and this
document records that. If a band SURVIVEs it authorizes exactly one
thing: a NEW Tier-2 registration scored on stream-replay data with
queue-position bounds. It authorizes no capital, per the standing rule
(zero capital without a pre-reg PASS and explicit user authorization).

## Results

### Executed 2026-08-23 17:02:15 UTC — run `20260823T170215_4c2fb295`

Transient unit `favlong-tight-replay`, git rev `8496f7d`, 2h45m wall /
7.9 GB peak over **9,491,893 candle-snapshots / 1,618,926 markets**
(archive span 2024-04-12 13:00 → 2026-08-23 14:00). Both bands scored
in one pass. Output below is the run's stdout, appended unmodified.

```
-- band A = [0.8, 0.95] --
{
 "band": [
  0.8,
  0.95
 ],
 "settled_fills": 4762,
 "mean_entry_price": 0.8939,
 "cost": 42566.1,
 "fees": 329.96,
 "payout": 42910.0,
 "pnl": 13.94,
 "roi": 0.0003,
 "fee_share_of_gross": 0.9595,
 "by_category": {
  "Climate and Weather": {
   "n": 2521,
   "pnl": 253.24
  },
  "Commodities": {
   "n": 1178,
   "pnl": -129.79
  },
  "Crypto": {
   "n": 94,
   "pnl": -126.65
  },
  "Economics": {
   "n": 216,
   "pnl": 24.48
  },
  "Financials": {
   "n": 366,
   "pnl": -95.42
  },
  "Mentions": {
   "n": 348,
   "pnl": 59.1
  },
  "Science and Technology": {
   "n": 39,
   "pnl": 28.98
  }
 },
 "halves_pnl": {
  "H1": -14.74,
  "H2": 28.68
 },
 "sub_bands": {
  "low": {
   "pnl": -49.34,
   "roi": -0.0037
  },
  "high": {
   "pnl": 63.28,
   "roi": 0.0022
  }
 },
 "thresholds": {
  "roi_gt_1pct": false,
  "n_ge_2000": true,
  "cats_positive_ge_4": false,
  "n_cats_ge_100_fills": 5,
  "positive_cats": [
   "Climate and Weather",
   "Economics",
   "Mentions"
  ],
  "both_halves_positive": false,
  "both_sub_bands_positive": false
 },
 "psr_supplementary": {
  "sr": 0.0004,
  "sr0": 0.0075,
  "dsr": 0.3115,
  "n_returns": 4762,
  "n_trials": 2,
  "skew": -2.5646,
  "kurt": 7.8513
 },
 "verdict": "FAIL (kill)"
}
-- band B = [0.95, 0.99] --
{
 "band": [
  0.95,
  0.99
 ],
 "settled_fills": 7514,
 "mean_entry_price": 0.9777,
 "cost": 73467.7,
 "fees": 152.51,
 "payout": 73820.0,
 "pnl": 199.79,
 "roi": 0.0027,
 "fee_share_of_gross": 0.4329,
 "by_category": {
  "Climate and Weather": {
   "n": 4250,
   "pnl": 140.92
  },
  "Commodities": {
   "n": 2001,
   "pnl": 0.27
  },
  "Crypto": {
   "n": 93,
   "pnl": -70.34
  },
  "Economics": {
   "n": 374,
   "pnl": 55.02
  },
  "Financials": {
   "n": 584,
   "pnl": 46.98
  },
  "Mentions": {
   "n": 81,
   "pnl": 23.58
  },
  "Science and Technology": {
   "n": 131,
   "pnl": 3.36
  }
 },
 "halves_pnl": {
  "H1": 177.89,
  "H2": 21.9
 },
 "sub_bands": {
  "low": {
   "pnl": 34.31,
   "roi": 0.0024
  },
  "high": {
   "pnl": 165.48,
   "roi": 0.0028
  }
 },
 "thresholds": {
  "roi_gt_1pct": false,
  "n_ge_2000": true,
  "cats_positive_ge_4": true,
  "n_cats_ge_100_fills": 5,
  "positive_cats": [
   "Climate and Weather",
   "Commodities",
   "Economics",
   "Financials",
   "Science and Technology"
  ],
  "both_halves_positive": true,
  "both_sub_bands_positive": true
 },
 "psr_supplementary": {
  "sr": 0.0202,
  "sr0": 0.006,
  "dsr": 0.874,
  "n_returns": 7514,
  "n_trials": 2,
  "skew": -7.2431,
  "kurt": 53.9769
 },
 "verdict": "FAIL (kill)"
}
BOTH BANDS FAIL -> the favorite-longshot family is CLOSED (binding).
```

**Verdict: BOTH BANDS FAIL. The favorite-longshot family is CLOSED.**
Per the commitment fixed above ("If it FAILS, the fav-long family is
closed for good and no third variant is registered"), this is binding:
no third fav-long variant may be registered, on this evidence or any
re-reading of it.

What the test actually established, stated so it is not mis-cited
later: **the spread gate did exactly what v1's post-mortem said it
would, and it was not enough.** v1 bought [0.80, 0.95] ungated and
returned **−5.0% ROI**; band A is the same band, same horizon, same
fee model, gated to a one-tick spread, and returns **+0.03%**. The
~5-point recovery is the ask-vs-mid gap being closed, so the named
cause of death is CONFIRMED rather than explained away. What the
recovery reveals is that there was nothing underneath it: net of the
Kalshi parabolic fee the favorite side prices to break-even, and fees
eat **96.0% of band A's gross** (+$344 gross → +$13.94 net on
$42.6K). Band B, where the fee is smallest as a share of stake, is
the better of the two on every axis — ROI +0.27%, 5/5 scored
categories positive, both halves positive, both sub-bands positive —
and still misses the +1.0% floor by a factor of four, with DSR 0.874
under n_trials 2. The floor was set above zero precisely because
Tier-1 fills are optimistic; a +0.27% Tier-1 result is not an edge
that survives queue position.

