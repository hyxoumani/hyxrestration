# Status & next steps (living page)

Updated: **2026-07-18 20:20 UTC (DIVERGENCE first check on the NEW
shadow run 20260716T130721 — the run that took over after the 07-16
13:07 service restart, now 2.3 days / 11,228 fills, never previously
divergence-checked. Result is the lab's first PERFECT convergence:
match rate 100.00% both directions (11,228/11,228), zero unmatched
fills in EITHER stream (all four causes 0 — no boundary, no gap, no
reseed_twin, no unexplained), every match at the exact tier (0 split,
0 nearest-relaxed), price_delta mean = median = abs_mean = 0.0, and
fees and gross cash identical to the cent (fees 460.47 / gross
8147.46 on both sides). This beats the closed 07-13 run's best
(99.92%/99.82% with a sub-0.2% timing-shifted residual) and shows the
residual there really was seed-boundary noise from that run's messier
start: this run's post-restart book seed settled cleanly inside a
gap-free stream window, leaving nothing to classify. Exact
convergence of the taker-side fill model is now confirmed on a
SECOND independent multi-day run — different days, different anchor,
different seed — taker haircut ≈ 0 is a property of the machinery,
not of one lucky window. Report:
`reports/shadow_divergence/20260716T130721.json`.) (prior 2026-07-18 14:15 UTC (ATLAS re-run on +3,959 fresh settled
markets — the 07-18 11:10 UTC kalshi sweep fired (settled
73,251→77,210, candles 3.161M→3.244M). Flagged 81→80. Headline verdict
unchanged — the favorite-longshot signature HOLDS — but this is the
first window with a genuinely counter-signature CLUSTER, and the
chase-drift analysis attributes it to a correlated settlement cohort,
not regime change. What moved: the Financials FAVORITE arm weakened
sharply — 1h d6/d7/d9 all dropped by converging to implied (d6 gap
+0.101→+0.007, the max drift 0.094 on 110 common meaningful-n
buckets), and newly-flagged 1h d8 / 6h d4 / 72h d9 flipped to
NEGATIVE gaps (favorites overpriced, counter-signature). Marginal
(new-fill-only) realized rates expose the cause: EVERY Financials
favorite decile realized far below implied on the increment (1h d6
marginal .175 vs implied .649; d8 .417 vs .849; d9 .674 vs .979; 6h d5
.203 vs .549) while Economics/Commodities favorites stayed at/above
implied — and the Financials LONGSHOT deciles simultaneously realized
~0 (d1 marginal .066 vs implied .147). Both arms moving the same
direction at once is the index-ladder-correlation signature: the fresh
Financials cohort is dominated by KXDJI/KXINXU ladders, and 07-17 was
a big-move day (KXDJI 348 no / 82 yes; KXINXU 311 no / 109 yes, vs a
balanced 07-16) — one day's index level settles hundreds of sibling
strikes together, so effective n on the increment is closer to ~2
day-outcomes than to 3,959 markets. This is the documented atlas
correlation caveat made concrete: bucket Wilson intervals OVERSTATE
confidence for ladder-heavy categories, and any favorite-longshot
pre-reg must not treat Financials-1h favorite buckets as independent
evidence. Non-Financials signature intact (Commodities 72h d2 newly
flagged signature-direction; Econ/Commodities favorite marginals
normal). WATCH: if Financials favorite gaps keep collapsing across the
NEXT 2–3 sweeps (different day-outcomes), that arm is genuinely
fading — cumulative-gap convergence over multiple independent days
would be real drift, not cohort noise. Report:
`reports/atlas/20260718T141523.json`.) (prior 2026-07-18 08:20 UTC (hylshi_fade.py provenance RESOLVED —
the 07-13 "unexplained untracked file" mystery is closed. "hylshi" is a
real sibling project on this box (`/home/devs/workspace/hylshi`): an
active LIVE-trading Kalshi weather-fade stack with its own git repo,
venv, experiment ledger (EXP-nnn), and systemd units
(`hylshi-watchdog.service`, `hylshi-trade-quality-review.timer` —
installed 07-13, NOT ours, they run from the hylshi workspace).
`strategies/hylshi_fade.py` is the leftover artifact of hylshi's
**EXP-423** ("Replay the live weather-fade playbook through
hyxrestration's order-lifecycle simulator as an independent
execution-level validation") — the USER STOPPED that dispatched agent
mid-run; hylshi's ledger marks it DEFERRED, "not re-dispatching
without an explicit user request." File created 07-13 13:58 UTC,
exactly the flagged window. Disposition unchanged and now grounded:
left untracked/untouched — the user actively stopped the run, and in
this lab the file is still the retro-rescue pattern (live rules
presented as a candidate without pre-registration); it enters
`strategies/` only via normal pre-reg if the user asks. Also learned:
hylshi reads OUR archives directly (its EXP-016/EXP-423 notes name
hyxlab.duckdb and even our .venv python) — a second cross-project
READER on the single-writer DuckDB, same class as our own lock-aware
readers; no action needed (readers don't take writer.lock) but it
explains any occasional read contention. Same session: 07-18 07:00 UTC
QA all-PASS verified; atlas data-gated until the 11:10 UTC kalshi
sweep; maker bracket fresh at 02:16 UTC; divergence current on the
closed run.) (prior 2026-07-18 02:16 UTC (MAKER BRACKET re-run — 8th weather
bracket, first since 07-16 15:16 (~35h; the report draws on
continuously-accumulating stream trade data, so this is a genuinely
fresh 24h window 07-17 02:16→07-18 02:16, not a re-score). Default
weather-dominated run, 223 join-the-touch virtual orders across 8
KXHIGH markets (NY 122 / MIA 51 / CHI 50): crossing rule fills 127 vs
queue [118 pess, 131 opt] — crossing lands INSIDE the bracket this time
(29 crossing-not-pess vs 20 pess-not-crossing, near symmetric). The
weather sign sequence is now under/over/under/inside/over/inside/over/
INSIDE across eight runs — still no stable sign, so the
regime-dependent crossing-rule bias conclusion firmly HOLDS and there
is still no fixed-haircut shortcut: any maker registration must score
fills via queue-PESSIMISTIC bounds on its own markets. Report:
`reports/maker_bracket/20260717T211609.json`.) (prior
2026-07-17 20:16 UTC (ATLAS re-run on +5,320 fresh settled
markets — the 07-17 06:10 kalshi sweep fired (settled 67,931→73,251,
candles 3.115M→3.161M). Flagged 79→81, and the favorite-longshot
signature HOLDS with no drift to chase: of 4 newly-flagged buckets 3
are signature-direction (Economics 72h d0 extreme-longshot overpriced
implied .029 vs realized .018 n=947; Financials 1h d6 favorite
underpriced .649→.750 n=204; Financials 6h d3 longshot overpriced .342
vs .297 n=529) and 1 is mid-decile (Climate 24h d5 .544→.584); of 2
dropped, neither is a directional reversal — Financials 1h d8 (a
signature favorite bucket) gained +41 fills and its realized converged
.917→.852 toward implied .847 (gap +0.071→+0.006, mean-reverting, now
inside Wilson), and Financials 6h d4 is a mid-decile that likewise
converged .504→.466. Max gap drift on the 109 meaningful-n (≥200 both
runs) common buckets 0.084 (Financials 1h d5, a MID-favorite decile,
gap +0.245→+0.161 as it gained +45 fills — converging toward implied,
non-directional, not a favorite-longshot bucket). Five atlas re-runs
now across +20.5k settled markets (07-13→07-17) all confirm the
signature is persistent, not a one-window artifact; favorite-longshot
PRE-REG remains the test. Report:
`reports/atlas/20260717T201600.json`.) (prior 2026-07-17 14:20 UTC (DIVERGENCE by-cause classifier
REFINED + shipped — the `reseed_twin` cause. The 07-17 02:19 re-run
left 10 shadow + 16 replay leftovers tagged `unexplained`, with a note
that they were start-of-run seed-boundary fills the classifier lumped
in wrongly. This session finishes that follow-up: `_cause` now checks,
before falling through to `unexplained`, whether an exact (market,
side, qty, price) counterpart exists in the OPPOSITE stream (just
time-shifted past the 2s match window) — the seed-settling signature
where both streams produce the identical fill at offset moments while
their seeded books converge. Re-run on the closed run 20260713T064302
(11,943 fills, unchanged 99.92%/99.82% match, price delta 0):
shadow leftovers 10/10 → `reseed_twin`; replay leftovers 6 gap / 15
`reseed_twin` / **1** `unexplained` (down from 16). The single genuine
`unexplained` is a KXCPIYOY-26JUN fill at 06:47 UTC — still ~5.5 min
into seed-settling. Net: the residual sub-0.2% is demonstrably
timing-shifted seed convergence, NOT fill-model divergence; the
taker-side haircut ≈ 0 conclusion now has essentially no unexplained
residual to hide behind. Existence-only twin test (asserts an
identical opposite fill exists, does not net counts); 2 new unit tests
(twin→reseed_twin, different-price→stays unexplained). Suite 244→246.
Report regenerated: `reports/shadow_divergence/20260713T064302.json`.)
(prior 2026-07-17 02:19 UTC (DIVERGENCE re-run on the main shadow
run 20260713T064302, now CLOSED at 11,943 fills — the largest window yet
(+30% over the 07-15 check's 9,222; a new shadow run 20260716T130721 took
over at 13:07 UTC after a service restart, freezing this one into a
fully-closed 3.3-day window 06:41 07-13 → 13:06 07-16). The
exact-convergence simulation-honesty finding holds and TIGHTENS again:
match rate 99.92% vs shadow / 99.82% vs replay (up from 99.89%/99.76%),
price_delta mean = median = abs_mean = 0.0 across all 11,933 matched
fills — every matched fill is price-identical shadow↔offline-replay.
Fees near-identical (shadow 458.56 vs replay 459.22, +0.14%); gross cash
8086.13 vs 8098.67. 10 unmatched shadow (all `unexplained`) / 22 unmatched
replay (6 gap, 16 `unexplained`), 0.08–0.18%. Note on the new
by-cause classifier (af01c63): the 10 shadow-unmatched all fall in the
first ~55 min after anchor (06:44→07:35 UTC) — start-of-run seed-boundary
fills the classifier tags `unexplained` rather than `boundary` (they
predate the replayer's book seed settling), NOT price disagreement; a
follow-up could widen the `boundary` window to the first minute of a run
to reclassify them. Fourth divergence re-run across the 07-13 run's life
(2,185 → 9,222 → 11,943 fills) all confirm the taker-side fill-model
haircut ≈ 0 on a fully-closed 3.3-day live-shadow window, not just the
original convergence slice. Report:
`reports/shadow_divergence/20260713T064302.json`.) (prior
2026-07-16 20:16 UTC (MAKER BRACKET re-run — 7th weather
bracket, first since 07-15 03:20 (~41h; the report draws on
continuously-accumulating stream trade data, so this is a genuinely
fresh window, not a re-score). Default weather-dominated run, 190
join-the-touch virtual orders across 8 KXHIGH markets (NY 91 / CHI 53 /
MIA 46), 24h window 07-15 20:16→07-16 20:16: crossing rule fills 128 vs
queue [110 pess, 127 opt] — crossing sits 1 ABOVE the optimistic
ceiling, OVER-awarding (31 crossing-not-pess vs 13 pess-not-crossing;
net +18 fills the sim may be inventing over the pessimistic floor). The
weather sign sequence is now under/over/under/inside/over/inside/OVER
across seven runs — still no stable sign, so the regime-dependent
crossing-rule bias conclusion firmly HOLDS and there is still no
fixed-haircut shortcut: any maker registration must score fills via
queue-PESSIMISTIC bounds on its own markets. Report:
`reports/maker_bracket/20260716T151619.json`.) (prior 2026-07-16 14:15 UTC: ATLAS re-run on +5,323 fresh settled
markets — the 07-16 kalshi sweep fired (settled 62,608→67,931, candles
3.056M→3.115M). Flagged 78→79. The favorite-longshot signature HOLDS,
no drift to chase: of 3 newly-flagged buckets, 2 are signature-direction
(Economics 7d d0 extreme-longshot overpriced implied .030 vs realized
.018 n=728; Financials 1h d7 favorite underpriced .752→.868 n=242) and
1 is mid-decile (Financials 6h d4 .455→.504); of 2 dropped, both are
mid/weakened non-signature (Commodities 1h d5 .544→.609 mid; Financials
6h d3 longshot .339→.278 edge weakened below significance). Max gap
drift on the 76 common buckets 0.112 (Financials 1h d4, a MID decile,
gap −0.212→−0.100: the anomaly is CONVERGING toward implied as it gained
+142 fresh fills — mean-reverting, non-directional, and not a
favorite-longshot bucket). Four atlas re-runs now across +15.2k settled
markets (07-13→07-16) all confirm the signature is persistent, not a
one-window artifact; favorite-longshot PRE-REG remains the test. Report:
`reports/atlas/20260716T141520.json`.) (prior 2026-07-15 20:15 UTC:
DIVERGENCE re-run on the main shadow
run 20260713T064302, now 9,222 fills spanning 2.5 days — 4× the prior
07-13 check (2,185 fills, one day). The exact-convergence
simulation-honesty finding holds, and TIGHTENS on the larger sample:
match rate 99.89% vs shadow / 99.76% vs replay (up from 99.5%/99.0%),
and price_delta mean = median = abs_mean = 0.0 across all 9,212 matched
fills — every matched fill is price-identical shadow↔offline-replay.
Fees near-identical (shadow 353.85 vs replay 354.51, +0.19%); gross cash
6265.18 vs 6277.72. 10 unmatched shadow / 22 unmatched replay fills
(0.1–0.2%) are boundary/coverage, not price disagreement. The taker-side
fill-model haircut ≈ 0 conclusion is now confirmed on 2.5 days of live
shadow, not just the original convergence window. Note: streamd is a
continuous stream writer so the live hyxstream.duckdb is lock-contended;
connect_retry rode through it (the report opens read-only between the
daemon's ~5-min flush bursts). Report:
`reports/divergence/20260715T201543_run20260713T064302.json`.) (prior
2026-07-15 14:16 UTC: ATLAS re-run on +5,659 fresh settled
markets — first real increment since 07-14. The 07-15 kalshi sweep
fired 12:15 UTC (settled 56,949→62,608, candles 2.91M→3.06M), so atlas
is no longer data-gated. Flagged 74→78. The favorite-longshot signature
STRENGTHENS again: all 10 newly-flagged buckets are signature-direction
(longshot deciles overpriced — Economics 1h d2 .243→.141, Financials 7d
d0 .031→0, Commodities 6h d3 .345→.259; favorite deciles underpriced —
Economics 1h d6 .642→.804, Financials 1h d8 .849→.895, Commodities 1h d6
.646→.741). 6 dropped, none against the thesis: 3 extreme-favorite at
24h/72h (Economics 24h d9, 6h d8, 72h d9 — realized nudging toward 1.0,
crossing inside Wilson) and 3 Commodities-72h longshot buckets (d0/d1/d2)
whose overpricing edge weakened below significance at the longest
horizon — an honest note that the longshot edge is thinnest at 72h,
strongest intraday. Max gap drift on the 68 common buckets 0.053
(Financials 1h d4), modest and non-directional. Three atlas re-runs now
across +7.3k settled markets (07-13→07-15) all confirm: persistent, not
a one-window artifact; favorite-longshot PRE-REG remains the test, no
drift to chase. Report: `reports/atlas/20260715T141537.json`.)** (prior
2026-07-15 08:22 UTC: SECOND non-weather maker bracket — the
econ crossing-rule bias FLIPS SIGN, just like weather. Run on the
book-covered econ series KXFED,KXCPI,KXCPIYOY,KXJOBLESSCLAIMS with
`--markets 24` (needed to surface the live 26JUL forward contracts; the
26JUN CPI contracts that dominated run 1 have gone post-event, books
frozen, zero join-the-touch opportunities): 273 orders / 100%
non-weather — crossing 20 vs queue [14 pess, 16 opt], crossing sits
ABOVE the optimistic ceiling (OVER-awarding; 11 crossing-not-pess vs 5
pess-not-crossing). Run 1 (07-14 21:18) UNDER-awarded below the pess
floor (26 vs [32,34]); run 2 OVER-awards above the opt ceiling. Two
non-weather brackets, opposite signs, both meaningful n (184 / 273):
the crossing-rule bias flips sign day-to-day WITHIN the econ-print
category exactly as within weather — regime-dependence is NOT
weather-specific, and the no-fixed-haircut conclusion holds across
categories. Caveats: (a) the two windows overlap ~18h so the flip is
driven by the ~6h fresh tail + composition shift (June→July contracts);
(b) run 2 widened `--markets` to 24 vs run 1's default 8, so not
parameter-identical. STRUCTURAL COVERAGE LIMIT documented: queue bounds
need live L2 book depth, and watchlist book subscriptions cover only
weather (5 KXHIGH cities) + econ-prints (KXFED/KXCPI/KXCPIYOY/KXU/
KXPAYROLLS/KXJOBLESSCLAIMS). Crypto (KXBTC 2.5M tape prints/24h),
sports, esports have the exchange-wide TRADE tape but NO streamed book —
so the maker bracket cannot validate the crossing rule outside
weather+econ without adding those series to the book watchlist (a
collector change, capital-neutral, USER-gateable if a non-econ maker
lead ever needs it). Atlas ladder item re-run same session: byte-
identical fingerprint (56,949 settled / 2.91M candles) — the 07-15
kalshi sweep fires 11:10 UTC (06:10 CDT, box is UTC-5) and hasn't run
yet, so no new settled markets since 07-14; atlas data-gated, no drift.
Reports: `reports/maker_bracket/20260715T032022.json` (273-order
broader run, the headline); `20260715T031804.json` (thin CPI-only,
51 orders); atlas `reports/atlas/20260715T081535.json`.) (prior 2026-07-15 02:18 UTC:
coverage gap CLOSED — first non-weather
maker bracket: queuescore now takes `--series`; run on Financials/
Economics KXCPI+KXCPIYOY+KXFED, 184 orders / 100% non-weather — crossing
26 vs queue [32 pess, 34 opt], crossing UNDER-awards below the
pessimistic floor, 18 real pess fills forgone vs 12 crossing-not-pess.
Confirms the crossing-rule bias is regime-dependent ACROSS categories,
not just within weather; a Financials maker registration cannot borrow
the weather bracket's calibration and must score via queue-pess bounds
on its own markets. Report: `reports/maker_bracket/20260714T211807.json`.
Suite 240→242.) (prior 2026-07-14: maker-bracket category hypothesis
killed: all six brackets were 100% KXHIGH weather high-temp — coverage
gap flagged, bias flips within a single category; `market_composition`
now in the report) (physical package split shipped 07-09:
`collector/` / `simulator/` / `strategies/` / `hyxlab` kernel, systemd
units vendored in `scripts/systemd/`, promote.sh installs them. QA
negative-levels root cause found and fixed 07-11: flush() dropped its
batch when a reader held the file lock — 18 silent 15s archive holes
Jul 9–11, now retro-gap-marked; QA reconstruction was also unsound
(max(seq) vs subscription-scoped seq) — rewritten time-ordered. All QA
green. **Divergence report v1 SHIPPED same day** (`python -m
simulator.divergence`): run 20260709T234859 (42h, 3,065 shadow fills)
vs offline replay — matched fills price-identical (mean Δ 5e-6),
match rate 69%/93% pre-fix — RESOLVED 2026-07-12: the first fully post-fix window converges EXACTLY (2,300/2,300 fills, all deltas 0; see simulation-honesty). Historic asymmetry was coverage
honesty (62 gaps in window; 57% of shadow fills sit in a gap's 65-min
re-seed shadow, incl. the 12 retro flush-failure windows replay blanks
but live shadow traded through). Taker-side fill-model haircut ≈ 0.
**Maker queue-position bounds SHIPPED 07-11 late**
(`simulator/queuebounds.py` + `python -m simulator.queuescore`;
trade↔delta mapping probed: no-taker hits yes@p, yes-taker hits
no@1-p, ±1ms alignment). First 24h bracket, 143 join-the-touch
virtual orders across 8 markets: crossing rule filled 75 vs queue
bracket [78 pess, 86 opt]. Second bracket 2026-07-12 (218 orders):
crossing 98 vs [88, 93] — the rule flipped to OVER-awarding (28% of
its fills lack queue evidence, vs 12% day one). Third bracket
2026-07-13 03:17 UTC (194 orders, 8 markets): crossing 97 vs
[98 pess, 107 opt] — flipped back to UNDER-awarding (crossing now
sits below the pessimistic floor itself, 22 real pess fills the
crossing rule forgoes). Three brackets, three different signs
(under/over/under) confirms this is not a one-day fluke: the
crossing-rule bias is genuinely regime-dependent and flips sign day to
day; any
maker registration must score fills via queue-PESSIMISTIC bounds
directly, never a fixed haircut on the crossing rule. **Fourth bracket
2026-07-13 14:17 UTC** (234 orders, 8 markets): crossing 101 vs
[98 pess, 103 opt] — for the first time crossing lands INSIDE the
queue bracket (25 crossing-not-pess vs 22 pess-not-crossing, near
symmetric). Four runs, no stable sign — under/over/under/inside — the
regime-dependent-bias conclusion holds; still no fixed-haircut
shortcut. **Fifth bracket 2026-07-13 20:18 UTC** (230 orders, 8
markets — all `KXHIGH*` weather high-temp this window): crossing 151 vs
[135 pess, 150 opt] — crossing sits just ABOVE the optimistic ceiling,
OVER-awarding again (37 crossing-not-pess vs 21 pess-not-crossing).
Five runs, sign sequence under/over/under/inside/over — still no stable
sign; regime-dependent-bias conclusion firmly holds, no fixed-haircut
shortcut. Same session: divergence report re-run on live shadow run
20260713T064302 (06:41–20:16 UTC full day, 2,185 fills — ~2× the 14:16
check) — 99.5%/99.0% match, price delta 0 across mean/median/abs —
confirms the post-fix exact-convergence finding still holds on fresh
data. Atlas re-run 2026-07-13 20:16 UTC: byte-identical to the 14:17
run (same data fingerprint 52,734 settled markets / 2.83M candles,
same 68 flags, max gap drift 0.0) — the settled set only advances on
the daily sweeps (05:00/06:10 UTC), so atlas re-runs are data-gated
until the next sweep; no intraday re-run value. **Queue-bounds mapping
VERIFIED 2026-07-14 02:20 UTC** (ladder item 2 — `python -m
simulator.prioritycheck`, new): the trade→book-decrement mapping the
maker bracket rests on (probed on one market, 269/270) now holds across
the archive — 18,707 prints / 8 markets / 24h, 99.65% land an exact-size
decrement at the predicted complement level within the model's 2s
window; the naive same-side mapping fits 0 (not coincidence); residual
0.35% are no-decrement coverage gaps; timing median 0.14ms, p95 1.4ms.
Removes the "not yet verified empirically" caveat from queuebounds; the
front-vs-back consumption ORDER within a level stays bracketed
(pess/opt), needing a live maker probe (Tier-3, capital-gated). Suite
234→240. **Sixth bracket 2026-07-14 08:15 UTC** (143 orders, 8
markets — again all `KXHIGH*` weather high-temp this window): crossing
85 vs [82 pess, 90 opt] — crossing lands INSIDE the queue bracket for
the second time (20 crossing-not-pess vs 17 pess-not-crossing, near
symmetric). Six runs, sign sequence under/over/under/inside/over/inside
— still no stable sign; regime-dependent-bias conclusion firmly holds,
no fixed-haircut shortcut. **Category hypothesis KILLED 2026-07-14
(ladder item 3, `series_composition` in queuescore + all six shipped
brackets audited):** the earlier hint that the sign might track market
category was based on a false premise — ALL six brackets are 100%
`KXHIGH*` weather high-temp markets (the earlier note that four windows
"spanned mixed categories" was wrong). queuescore selects the top-N
Kalshi series by stream trade-print count, and those are uniformly
weather high-temp. So the sign flips (under/over/under/inside/over/
inside) all occur WITHIN a single category, which STRENGTHENS the
regime-dependent, no-fixed-haircut conclusion (the bias flips day to
day even holding category fixed) and removes any category-shortcut. It
also exposes a COVERAGE GAP: this bracket validates the crossing rule
ONLY for weather high-temp; a maker registration in any other category
(e.g. a Financials fav-long maker) has zero queue-bounds validation and
must run its own bracket on its own markets first. Runs now emit a
`market_composition` field so the mix is visible per report.
B4 signal layer, B5 core, and B6 atlas ALL shipped same evening (see queue). **FavoriteLongshot v1 pre-registered and KILLED same night** (ROI −5.0% on 8,363 fills; the spread decides — atlas gap lives at mid, taker pays the ask; see strategy-verdicts.md). Pair candidates report DONE same night (100 leads; Fed-funds bounds pair on both venues awaits USER resolution-rule verification). **Queue drained of unblocked agent work** — remaining items are user-gated (pair verification, backup destination, FRED key, NTP, key rotation, simui-service call) or data-gated (event study, Tier-2 maker fav-long registration: both need weeks of accumulation).)
Cold-start order: this page → [hyxlab-architecture](hyxlab-architecture.md)
→ `docs/sessions/2026-07-08-05.md` (session handoff, gitignored).

## Where the project is

**The data-collection layer is structurally complete** (user direction
2026-07-07: all data first, then the simulation platform). Everything
either venue still serves is captured or capturing on timers, and rot
trips alarms:

- **Kalshi**: 2.6M hourly candles (60d capture, 35.7k markets, 8-category
  allowlist — sports/entertainment/politics exclusion USER-CONFIRMED
  2026-07-08); trade tape 5.6M+ prints (retro-pass finishing overnight;
  forward capture rides the daily sweep); live WS books (watchlist
  series) + exchange-wide trade firehose.
- **Polymarket**: metadata + volume/liquidity series + ~60d hourly price
  history for all markets ≥$10k volume (universe now ~4,600 and growing)
  + trade tails (API caps at last 3,000/market — forward tape is our
  WS); live books for top-50 volume markets' tokens. **2026-07-08:
  Gamma capped /markets offset at 2000 hours after that day's sweep —
  enumeration moved to /markets/keyset same day (fix promoted to
  stable before the next 05:00 run; see [venues](venues.md)).
- **Ground truth**: 33k MOS forecasts, climate observations.
- **Timers**: collect 5min; poly sweep 05:00; kalshi sweep 06:10;
  QA 07:00 UTC (both archives; tape-coverage + freshness alarms).
- **Deployment**: daemons run from the `stable` worktree;
  `scripts/promote.sh` is the only shipping path. Import boundary
  (collection ↛ sim) test-enforced.

**Sim machinery already standing**: sim v2 (order lifecycle, accounting
invariants), four correctness gates, capability guard, latency model
(`Simulator(latency=Δ)`), BookReplayer (stream → ms snapshots; first
Tier-2 sweep: 1s latency ≈ +0.4¢/contract), simui replay terminal with
a proven chunked≡one-shot replay equivalence (see
[simulation-honesty](simulation-honesty.md)). Suite green (count moves; test-gate enforces).

**Falsification record**: weather v1 pre-reg FAIL (−$425, fees decide).

## Execution queue (sim platform, user-approved)

1. ~~Cross-venue pair candidates report~~ DONE 2026-07-11 late
   (`python -m simulator.pair_candidates` → `reports/pairs/`): 100
   ranked leads; the top class is Fed funds upper-bound markets listed
   on BOTH venues (score 0.54, same close). USER gate: verify
   resolution rules coincide before any pair enters watchlist.json.
2. ~~Shadow harness (Tier-3) v1~~ LIVE 2026-07-08: `hyxlab-shadow.service`
   — persistent Simulator tailing the stream archive (books seeded from
   history, trading strictly from the stream head), same latency model
   as backtests, fills/equity per run_id in `data/hyxshadow.duckdb`.
   Probe strategy running. **Next iteration**: maker queue-position-bound
   scoring + shadow-vs-replay divergence report (the calibration
   haircut).
3. ~~B4 FeatureView + signal feeds~~ **SHIPPED 2026-07-11 late**:
   `econ_vintages`/`news_items` tables; ALFRED keyless vintage pull
   (7 series incl. DFEDTARU/L; value-diffed daily so the restamped
   knowable_at can't forge vintages; historical vintages need a
   FRED_API_KEY — user item); GDELT bulk 15-min GKG filter-and-discard
   (templates in `collector/queries/gdelt.json`, format probed live);
   `simulator/features.py` FeatureView — bisect as-of, two-dimensional
   vintage semantics, news prefix-sum windows, P1 property-tested;
   Context delegates (`ctx.econ_latest/econ_series/news_window`).
   `hyxlab-signals.timer` daily 04:40 UTC + QA freshness checks.
   Release-datetime refinement (08:30 ET prints via FRED calendar)
   deferred until a FRED key exists; knowable_at stays pessimistic.
4. **B5 iteration machinery** — CORE SHIPPED 2026-07-11 late
   (`simulator/iterate.py`): Deflated Sharpe (Bailey–López de Prado;
   inv-normal vs table values, PSR special case hand-checked),
   E[max SR] deflation benchmark, purged walk-forward folds with
   close-date embargo (belt in neither train nor test), family_report
   (a sweep's best variant is only quotable deflated). Remaining for
   full B5: grid runner over episodes + `fit(train_view)` calibration
   protocol + size_sensitivity/persistence_filter post-processors —
   these land with the first calibrated strategy that needs them.
5. **B6 calibration atlas SHIPPED 2026-07-11 late** (`python -m
   simulator.atlas`): 68 flagged buckets (n≥200, implied outside
   Wilson 95%), a consistent favorite-longshot signature across
   categories — longshot deciles 1–2 overpriced (Commodities 1h d1:
   implied .146 vs realized .015; Financials 1h d1: .147 vs .044),
   favorite deciles 7–9 underpriced (Financials 6h d7: .755 vs .970
   n=500; d8: .844 vs .970 n=762). Caveats: buckets are correlated
   (same market at multiple horizons, sibling strikes), and fees +
   spread eat part of the gap — the favorite-longshot PRE-REG BACKTEST
   is the test, per hard rules. Report: `reports/atlas/*.json`.
   Event study v1 remains open for full B6. **Stability re-run
   2026-07-13 02:16 UTC** (+1,444 settled markets since 07-11): the
   same 68 buckets flag — zero dropped, zero new, max realized−implied
   gap drift 0.004 (Economics 1h d7, +0.101→+0.097). The
   favorite-longshot signature is persistent, not a one-window
   artifact; no drift to chase. **Stability re-run 2026-07-14 14:15
   UTC** — largest increment yet (+4,215 settled markets since 07-13,
   52,734→56,949, +8% of corpus, two fresh sweeps): flagged 68→74. The
   signature STRENGTHENS rather than drifts — all 8 newly-flagged
   buckets sit in the favorite-longshot direction (longshot deciles
   1–3 overpriced: Commodities 1h d2/d3, Financials 24h d2, …;
   favorite deciles 6–8 underpriced: Financials 6h d6 +0.214,
   Commodities 1h d7 +0.172, Climate 6h d8 +0.069). Only 2 dropped and
   neither is signature-relevant: Climate 24h d5 (a mid-decile, .544
   implied) and Sci/Tech 24h d0 (extreme longshot, implied .012 →
   realized .002, negligible magnitude crossing inside Wilson). Max gap
   drift on common buckets 0.036 (Financials 6h d5, +0.249→+0.213),
   modest and non-directional. Report:
   `reports/atlas/20260714T141530.json`.
6. ~~Debug frontend~~ **simui SHIPPED 2026-07-08** (v1 + Kalshi-style
   restyle + resilience): interactive market-replay terminal
   (`python -m hyxlab.simui`, localhost:8877) — archived events replay
   like a live Kalshi event page; user buy/sell + attached strategies
   fill through the real Simulator; per-account profile. Chunked
   session replay proven bit-identical to the one-shot backtest path
   (synthetic test + real 587k-event window). Client auto-reconnects;
   server clock errors log + pause instead of dying silently.
   Stream-tier Kalshi only. Later: decision-replay overlay, doctor
   view, candle-tier + Polymarket replay.
7. **Strategies** (only after 2–6): favorite-longshot pre-reg first;
   weather v2 and econ-print candidates behind it.

## Autonomous loop (2026-07-12)

`hyxlab-autoloop.timer` runs one bounded headless Claude Code
iteration every 6h (02/08/14/20:15 UTC) against the investigation
ladder — the never-stop directive as infrastructure, surviving
interactive sessions. flock-guarded; hooks + hard rules bind headless
runs identically. Knobs: cadence (timer), turn cap (autoloop.sh),
permission mode.

## Standing user items (non-blocking)

**`strategies/hylshi_fade.py` — provenance RESOLVED 2026-07-18,
disposition still user-gated**: the file is the partial artifact of
sibling project hylshi's EXP-423 (replay its live weather-fade
playbook through our simulator), whose dispatched agent the USER
STOPPED mid-run on 07-13; hylshi's ledger defers it pending an
explicit user request. Left untracked/untouched here: it is still the
retro-rescue pattern (live rules presented as a pre-validated
candidate), and weather has a killed precedent (WeatherNWS v1). If
the user wants the EXP-423 sim validation, it runs as a normal
pre-registered backtest in this lab — or delete the stray file; both
are user calls. See the 2026-07-18 08:20 status entry for full
evidence.

**Off-box backup destination** (local tier SHIPPED 2026-07-12:
`hyxlab-backup.timer` daily 03:30 UTC, 7-slot rotation in
`data/backups` via consistent read-locked copies — guards corruption/
deletion, not disk loss; point `HYXLAB_BACKUP_DIR` at any off-box
mount to finish the job);
`sudo timedatectl set-ntp true` (box ~20s fast; daemon logs the step);
rotate Kalshi API key;
Phase 0 write-up (pending prose artifact); micro-probe budget decision
(parked until explicitly authorized). ~~simui as a systemd unit~~
SHIPPED 2026-07-12 (`hyxlab-simui.service`, localhost:8877, paper
state only — disable anytime with `systemctl --user disable --now`).

## Small follow-ups (agent-actionable)

- ~~Sweep-shrink tripwire~~ DONE 2026-07-11 (QA: last completed day vs
  prior-week peak, 0.5 threshold; reachability check is now lock-aware
  so the multi-hour poly sweep no longer false-alarms).
- Cross-venue pair candidates report (queue item 1) is mostly
  mechanical and can ride along with other work.

## Deep review 2026-07-11 — triage record

`docs/reviews/2026-07-11-deep-review.md` (4 High / 8 Medium / 7
hygiene). **Implemented 2026-07-11**: H1 (writer-lock: open_retry,
guarded poly flushes, flock sweep lock, nonzero aborts), H2 (gap rows
filtered to kalshi-books coverage — plus the trades-channel case the
review missed), H3+M6+M2 (truncation signals; get_trades returns a
truncated flag, trades_swept records 'truncated'), H4 (size-0 quote
fills nothing), M1, M3 (pending-size log), M4 (per-source isolation),
M5 (hourly metadata refresh), M8 (matching_note caveat), Order field
validation, QA per-market snapshot baseline, CLAUDE.md/wiki drift,
stray root doc moved.

**Pushbacks (not applicable as filed)**:
- M7 (mark-at-zero for unquoted positions): marking DOWN is the
  conservative direction this lab wants — a flattering mark is the
  failure mode, a pessimistic max_drawdown is survivable. Documented
  bias, not a bug; revisit only when drawdown gates a pre-reg verdict.
- Hygiene 3's `## Metric` TBD in CLAUDE.md: bootstrap placeholder by
  design until the lab has a single optimizable metric.
- H1's framing "flock honored only by poly_sweep/trades_backfill":
  collect and kalshi-sweep DO hold writer.lock — via their systemd
  units' flock wrappers. The real exposure was readers (QA, doctor,
  backtest, simui), which never flock; fixed via open_retry.

**Backlogged (valid, not urgent)**:
- ~~`hyx/` legacy package quarantine~~ DONE (ef70546: moved under
  `phase0/hyx`).
- ~~StreamStore spill-to-sidecar cap for multi-hour reader wedges~~
  DONE 2026-07-12 (SPILL_CAP=400k; failed flush spills oldest rows to
  `<db>.spill.jsonl`, drained sidecar-first in one transaction on the
  next good flush, survives restart; 5 recovery-claim tests per
  mistakes #12).
- ~~requirements.txt ↔ requirements-stable.txt version-skew check~~
  DONE 2026-07-12 (`tests/test_requirements_sync.py`: stable must be
  exact pins; shared pins must satisfy dev specifiers; every stable
  pin must be installed in the suite's venv at exactly that version —
  pin bumps are deliberately a two-step edit+pip-install).
- ~~`streamd.open_tickers` shorter retry when the initial set is
  empty~~ DONE (ladder shipped in ef70546; regression test added
  2026-07-12). Residual dead-air class ALSO CLOSED 2026-07-12:
  `_fetch_until_nonempty` (last ladder rung repeats forever) means
  neither book task ever idles permanently or subscribes with an
  empty set; flusher() logs spilled-sidecar rows during a wedge.
- ~~Divergence matcher: nearest-in-window + split-aware matching
  (v2)~~ DONE 2026-07-12: tiered exact→split→nearest (2s window,
  `--nearest-window`); pre-existing report fields stay exact-tier-only
  so shipped reports remain comparable; convergence window re-run
  bit-identical (2,300/2,300 exact, 0 relaxed).

## Watch items (not yet alarming)

- **DuckDB vs cgroup memory caps**: hyxlab-shadow was kernel-OOM-killed
  at boot twice (2026-07-11, 2026-07-12 — systemd auto-restart
  recovered both) because DuckDB's default memory_limit scales with
  SYSTEM RAM, far above the unit's MemoryMax=1G; the seed-time ORDER BY
  blew the cap. FIXED for shadow 2026-07-12 (`stream_conn`: 512MiB
  engine cap, 2 threads, spill to `data/duckspill-shadow`, and it now
  uses the mandated `connect_retry`). `hyxlab-simui` shares the 1G cap
  and replays big archive windows — same exposure, unobserved so far;
  apply the same bound if it ever OOMs.

- **Poly swept universe decline is partly a measurement artifact**
  (found 2026-07-12): day-buckets MATURE for ~2 days as later sweeps
  backfill price history into past days (Jul 10 read 5,692 on Jul 11
  but 6,672 on Jul 12). The tripwire compares a fresh (immature)
  yesterday against matured peaks — biased toward false alarms; the
  0.5 threshold absorbs the ~15–20% maturation effect. True trend is
  a mild decline (7.2–7.4k steady-state), not the ~5%/day slide
  first estimated. Watch only if matured days trend below ~5k or the
  sweep runtime keeps growing.

## Hard rules in force

Zero capital without pre-registered Tier-2+ PASS **and** explicit user
authorization. No retro-rescues of failed strategies. Probe before
build. Every new store writer ships with a stored-timestamp assertion
(mistakes #10). Vacuous backtests must refuse to run.
