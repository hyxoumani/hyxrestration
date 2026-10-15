# Pre-registration: weekly claims prints vs the Kalshi strike ladder (EconClaimsRW v1)

**Registered 2026-10-15 20:20Z, BEFORE the sample exists.** The primary
pair has 11 scorable events in the archive (the scorer's own count, read
after the text below was written and corrected only here) and the
registration scores at 26, so the model, the decision time, the fill rule
and the thresholds below are frozen roughly three months before any PnL
can be computed.
The results section is appended after, unmodified. Same discipline as
`prereg_favlong_tight_backtest.md`.

Strategy-verdicts queue item 2 ("Econ prints vs ALFRED vintages") said
"write the pre-reg when ~26 events have quotes". This writes it NOW
instead: a registration written after 26 events of prices and prints are
sitting in the archive is contaminated by whatever its author glanced at
while counting them. Nothing about the sample size changes; only the
order of writing and reading does.

## Disclosure: prior data contact

Everything read before this registration is in
`docs/wiki/strategy-verdicts.md` item 2 and is restated here so the
contamination is judgeable:

1. **Resolution rule, read against outcomes.** For every ICSA event
   whose first print the pull captured, `result == (first_print >=
   floor_strike)` on all 146 strikes; the CCSA 10-15 event agreed on
   11/11 at the x1000 unit; `greater_or_equal` is inclusive. This read
   OUTCOMES against PRINTS, never against PRICES, and it is what fixes
   the settlement function the model has to predict.
2. **One book, one event.** The 10-08 ICSA ladder at T-60min: tails
   0.98/1.00, mid-ladder spreads 6-7c, volumes 1-3.7k per strike. One
   event's prices, no outcome joined. It is the only price read, and it
   sets nothing below except the friction estimate behind the edge
   floor.
3. **Revision direction.** ICSA first prints revise UP 13/14 by 1-3k;
   CCSA revise DOWN 12/14 by 2-13k. Prints only. The model predicts a
   FIRST print from a FIRST print, so revision direction does not enter
   it; it is why the anchor must be the first-print vintage.
4. **The calibration atlas** (`reports/atlas/`, re-run through 10-13)
   flags Economics buckets by horizon and implied decile; these markets
   are in its Economics category. That is implied-vs-realized evidence
   ACROSS the category at the 1h/6h/24h/72h horizons, and this test's
   T-60min sits between two of them. The atlas did not choose any
   parameter below (no decile band, no horizon from it), but a SURVIVE
   here is partially confirmatory of it and is labelled exploratory on
   that ground.
5. **Sample arithmetic** (sign test power at n = 14 / 26 / 40) was
   computed from the event COUNT, not from any outcome.

No implied probability has been compared with any outcome for these
series, and no PnL of any kind has been computed. The scorer below
refuses to compute one before the registered count is reached.

## Hypothesis

At one hour before the Thursday 12:25Z close, the `KXJOBLESSCLAIMS`
strike ladder misprices the probability that the 12:30Z initial-claims
first print clears a strike, relative to a no-parameter random walk on
the prior week's first print, by enough that taking the mispriced side
at the taker ask and holding to settlement earns positive settled PnL
net of Kalshi fees. Secondary, same hypothesis on `KXCONTCLAIMS` vs
the CCSA first print.

The null is that the ladder already prices the print at least as well
as the random walk after friction. A random walk is the weakest model
that is vintage-honest with zero fitted parameters; it is chosen so
that a SURVIVE cannot be a fitting artefact and a FAIL closes the idea
that the ladder is naive about the print.

## Fixed configuration (binding)

- **Pairs.** Primary: `KXJOBLESSCLAIMS` vs ALFRED `ICSA`, strike unit
  x1, observation offset 5 days (the Thursday close reports the week
  ending the previous Saturday). Secondary: `KXCONTCLAIMS` vs `CCSA`,
  strike unit x1000 (strikes are thousands from the 09-03 event on; a
  strike whose scaled value is not within 2x of the anchor is a UNIT
  failure and the market is excluded, never traded), offset 12 days.
- **Model.** For an event closing on date `D`, the anchor is the
  series' most recent observation knowable at decision time via
  `ctx.econ_series`, i.e. the two-dimensional vintage table as of that
  instant. The event is SCORABLE only if the anchor's `obs_date` is
  exactly `D - offset - 7d` (the prior week's print, present and not
  stale) and the trailing 53 observations as-of decision time are
  consecutive weekly dates. Predicted first print = anchor value
  (random walk, zero drift). `sigma` = sample standard deviation
  (ddof 1) of the 52 week-on-week changes over those 53 observations,
  as known at decision time. `P(yes | strike K) = 1 - Phi((K - 500 -
  anchor) / sigma)`: prints are 1k-granular and `>=` is inclusive, so
  the half-unit continuity correction is toward YES. No parameter is
  fitted; nothing is tuned.
- **Decision time.** The FIRST snapshot whose `ts` falls in
  `[close - 60min, close - 50min]` -- one look per market, no
  re-checking, no optional stopping. Two collector cadences, so one
  missed 5-minute cycle does not drop the market. A market with no
  snapshot in the window is not traded and not counted.
- **Entry.** At that snapshot, for each strike independently: buy YES
  10 contracts IOC at the taker touch iff `p_yes - yes_ask >= 0.10`;
  else buy NO 10 contracts IOC iff `(1 - p_yes) - no_ask >= 0.10`. A
  `None` or 1.00 ask never trades. Held to settlement, no exits.
  The 0.10 floor is friction plus margin: the taker fee at the ask is
  at most 1.75c and the one book read showed ~3.5c of half-spread, so
  ~5c is paid to be wrong-side of the touch and the model must clear
  it by at least as much again.
- **Fills.** Tier-1: immediate at the decision-time quote (latency 0),
  Kalshi parabolic taker fee (0.07, ceil-to-cent; both series are
  `quadratic`). Optimistic, as every Tier-1 registration in this repo
  is, and the +1.0% floor below exists for that reason.
- **Unit of analysis.** The EVENT (one close date), never the strike:
  strikes within an event settle on one number. Event PnL is the sum
  over its fills; an event is TRADED if it has at least one fill; a
  traded event WINS if its net PnL is strictly positive (ties lose).
- **Code.** `strategies.econ_claims.EconClaimsRW` and
  `simulator.run_econ_claims`, both committed with this document. The
  scorer counts scorable events from the markets, snapshots and
  vintages alone -- no outcome and no price enters the count -- and
  refuses to replay or print any PnL while the count is under the
  registered number.

## Scoring moments (binding)

- **First and only planned read: 26 scorable events on the pair.** For
  ICSA the scorable set begins at the 08-06 event and holds 11 through
  10-15, so the 26th lands ~2027-01-28. The 07-09 event's anchor
  pre-dates the pull; the 07-16, 07-23 and 07-30 events have quotes but
  NO snapshot inside the decision window -- the focus tape has a DAILY
  ~11:10-11:45Z hole over 07-09..08-02 (every day, not just Thursdays;
  cause outside journal retention), closed from 08-03 on.
  The window is NOT widened to reach them: a window chosen after seeing
  which events it admits is a parameter chosen on the data, and the
  registered one (two cadences) tolerates one missed cycle, not six.
  For CCSA the set begins at the 10-22 event (the 10-15 anchor was
  stamped knowable 10-16) and the 26th lands ~2027-04-15. The scorer's
  gate is the count, not a date; its first run, 2026-10-15 20:25Z,
  printed `GATED: 11 scorable events < 26` for ICSA and `0 < 26` for
  CCSA with no replay and no PnL.
- **One pre-declared second read, taken only if the first is
  UNDERPOWERED** (fewer than 20 traded events among the 26): at 40
  scorable events, with the same thresholds. If still UNDERPOWERED the
  verdict is FAIL (the rule does not engage with the market often
  enough to be a strategy). There is no third read.
- Power at the registered read, from the wiki's arithmetic on the sign
  test (one-sided, alpha 0.05): n=26 rejects at 18 wins and has 41%
  power against 65/35 and 82% against 75/25. The read is deliberately
  low-powered against a small bias: a strategy that needs 40+ events to
  show at all is not one this repo would size on.

## Thresholds (binding, fixed before the run)

Judged per pair, independently; the primary is the headline.

- **SURVIVE** requires ALL of: traded events `n_t >= 20`; event wins
  `>= k(n_t)`, the smallest `k` with `P(Bin(n_t, 1/2) >= k) <= 0.05`
  (n=20: 15, n=26: 18, n=30: 20, n=40: 26); net ROI `> +1.0%` on cost
  after fees; net PnL `> 0` in BOTH time halves of the traded events
  split at the low-median close date.
- **UNDERPOWERED** if `n_t < 20`: neither survives nor kills; triggers
  the single pre-declared second read.
- **FAIL** otherwise.

A primary SURVIVE is **exploratory** (atlas contact, disclosure 4) and
authorizes exactly one thing: a Tier-2 registration scored on
stream-replay data with queue-position bounds. It authorizes no
capital, per the standing rule. A secondary SURVIVE on its own is
exploratory and family-deflated (n_trials 2) and authorizes nothing.

If the primary FAILs, **the random-walk claims family is closed**: no
re-read with a different edge floor, decision time, window or sigma
estimator. A model that uses information the random walk does not
(seasonal structure, consensus surveys, state-level filings) would be a
NEW registration and is a retro-rescue if its motivation is this
result's PnL rather than evidence independent of it.

## Results

_(appended at the registered read; nothing here yet)_
