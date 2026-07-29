# Status & next steps (living page)

Updated: **2026-07-29 02:30 UTC (22ND WEATHER MAKER BRACKET — THE
SPACING GATE OPENED AND THE RUN IS THE STRONGEST READING IN THE
ARCHIVE, IN THE OPPOSITE DIRECTION TO THE LEAN THE LAST PASS GATED.
Gate check first: prior weather bracket 07-28 02:17 UTC, so at 02:15
UTC today the >~24h expiry-crossing rule was satisfied to the minute —
and the report confirms it rather than my arithmetic, `new_share: 1.0`
(249/249 new, all 26JUL28 markets vs the prior run's 26JUL27). Atlas
still data-gated until the 07-29 11:10 UTC kalshi sweep; econ needs
>=336h (next ~08-10); QA fires 07:00 UTC; divergence unchanged — shadow
run 20260722T081852 still open. THE RUN: 249 virtual orders across 8
markets (KXHIGHNY 94, KXHIGHCHI 66, KXHIGHMIA 59, KXHIGHDEN 30),
crossing **156 vs queue [165 pess, 177 opt]** — 9 orders BELOW the
pessimistic floor, with crossing_but_not_pess=21 vs
pess_but_not_crossing=30 (~0.7:1, the most under-skewed split of any
weather run). This is the first breach in the whole sequence that is
not a +/-1 knife-edge: last pass established a one-order call is ~7% of
the disagreement actually present, and 9 is outside that. It is also
`direction_market_robust: true` (4 of 6 leaning markets under, 2 over,
2 tied; abs_net_by_market 21 vs net -9, top_market_net_share 0.33).
**So it is simultaneously certified-independent AND market-robust — the
first reading in the archive that is both.** Report:
`reports/maker_bracket/20260729T021553.json`. THE FINDING, AND IT IS
THE SAME BUG CLASS FOR THE FIFTH TIME, NOW INSIDE THE TIER SHIPPED
LAST PASS: the market tier counts `markets: 8`, but those 8 are
`KXHIGHNY-26JUL28-B{77.5,79.5,81.5}` and friends — three STRIKES on one
New York temperature, on one day. Same for econ, where
`KXCPI-26JUL-T{-0.1,0.0,0.1}` is one CPI print. The honest unit is the
Kalshi EVENT, and the run's 8 markets are **4 city-days**. PROBED
BEFORE BUILDING, by rehydrating `orders_detail` from all 32 archived
reports and computing both tiers: the event tier demotes 4 runs the
market tier called direction-robust — 07-23 15:16, 07-23 21:17, 07-26
15:16, and most pointedly `20260726T211629.json`, the econ run (agg
+23) that LAST PASS CITED as proof the market tier is not vacuous. At
event level that run is 2 events over / 2 under: no majority, no
direction. Corrected here. HARDENING SHIPPED (7efd88b): `concentration`
now carries `underlyings`, `abs_net_by_underlying`,
`top_underlying_net_share`, the over/under/tied event split, and
`direction_underlying_robust`; tiers nest (underlyings <= markets <=
orders), headline and market fields untouched for cross-report
comparability per the divergence-matcher and atlas-day-tier precedent.
Three regression tests, one of which is the load-bearing subtlety: the
event key must be split from the LEFT, because strike suffixes can
themselves contain '-' (`KXCPI-26JUL-T-0.1` rsplits to
`KXCPI-26JUL-T`). Weather IDs cannot catch that — verified by
monkeypatching `event_ticker` to the plausible-wrong `rsplit` and
confirming only the econ-ID test goes red. Suite 317->320, ruff clean,
pushed. No promote — queuescore is sim-side, no timer runs it. TODAY'S
RUN SURVIVES THE NEW TIER: 4 events, 3 under (NY -8, CHI -4, DEN -3) vs
1 over (MIA +6), `direction_underlying_robust: true`. The archived
report file is deliberately NOT rewritten to carry the new block —
reports are immutable inputs to `independence_vs_prior`, same call as
the 07-28 filename decision; the next run carries it. **WHAT THIS DOES
TO THE GATED LEAN**: last pass named a data gate — accumulate
`new_share >= ~0.9` weather brackets and re-test the archive's raw
+16/-6 over-award tally at n>=8. Today is independent run **#2**
(#1 was `20260727T151833`, agg +1, market tier undirected). Two
certified-independent readings now exist and NEITHER shows over-award;
this one is robustly UNDER. That is not a finding at n=2 and the gate
stands unchanged — but the raw tally it was gating is now actively
contradicted by every reading that was allowed to count, which is the
whole reason it was gated. NEXT PASS: the 07-30 weather bracket (>24h
from 02:15 today) is independent run #3 and the first to carry the
event tier natively; two under-award events in a row at the strictest
tier would be the first real directional signal in 22 runs. Standing
conclusion unaffected: no fixed-haircut shortcut, score maker
registrations via queue-PESSIMISTIC on their own markets — if anything
today strengthens the case for the pessimistic floor being the honest
one. PRACTICAL RULE, superseding last pass's: **read
`direction_underlying_robust`, not `direction_market_robust`, before
calling any bracket over/under verdict, and read `underlyings` as the
sample size — a bracket's market count overstates it by ~2x.**
Untracked `strategies/hylshi_fade.py` re-confirmed present, still
correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-07-28 20:20 UTC (ALL STANDING REPORTS GATED — SO THE
UNIT-OF-INDEPENDENCE LENS WAS TURNED ON THE MAKER BRACKET ITSELF, AND
IT LANDS. Gate check first: atlas ran 14:20 UTC and is data-gated until
the 07-29 11:10 UTC kalshi sweep; weather bracket ran 02:17 UTC (~18h)
and per the 07-28 spacing rule needs >~24h to cross a daily expiry;
econ bracket needs >=336h (next ~08-10); QA fired 07:00 UTC; divergence
unchanged — shadow run 20260722T081852 still open. Nothing runnable, so
ladder rung 4. THE FINDING: the same bug class has now bitten three
times (atlas weekend non-readings, trailing-window bracket re-runs,
same-day Financials ladders). The maker bracket's HEADLINE is the
fourth instance and had not been checked. `crossing 147 vs [148 pess,
165 opt]` treats a run's N virtual orders as N independent draws, but
all orders in a market ride ONE book — a market whose ask happens to
walk down repeatedly contributes dozens of same-signed disagreements
off a single price path. PROBED BEFORE BUILDING, on the archived
reports: in EVERY run the per-market disagreement dwarfs the aggregate.
07-28 read `agg_net -1` against 15 of absolute per-market net; 07-27
read -1 against 15, with ONE market supplying -7 (47%). So the
aggregate is not a tight measurement, it is heavy cancellation of large
opposing per-market effects, and **a one-order breach is ~7% of the
disagreement actually present**. This mechanically explains the prior
pass's empirical finding that a 10% data increment flipped the verdict
from inside to under-by-1: the +/-1-order calls this log has been
reporting for 22 weather runs are far inside the resolution of the
measurement. HARDENING SHIPPED (1d7a2b8): every bracket report now
carries a `concentration` block — per-market cross_only/pess_only nets,
`abs_net_by_market` (the disagreement without letting markets cancel),
`top_market_net_share`, a market-level over/under/tied split, and
`direction_market_robust`, true only when the aggregate direction is
backed by a strict majority of the markets that lean either way.
Headline fields untouched for cross-report comparability, per the
divergence-matcher and atlas-day-tier precedent. Three regression tests
(30 same-signed orders in one market vs 2 opposing markets — aggregate
+28, direction correctly REFUSED; the same net spread same-sign across
4 markets — correctly kept, so the tier discriminates rather than
killing everything; a cancelling split — undirected). Suite 314->317,
ruff clean, pushed. No promote — queuescore is sim-side, no timer runs
it. VALIDATED ON REAL DATA WITHOUT MANUFACTURING A NON-READING: rather
than re-run a spacing-gated bracket to exercise the path, rehydrated
`orders_detail` from all 31 archived reports and ran the shipped
function over the full history. Result: the last three weather runs
(07-27 03:15, 07-27 15:18, 07-28 02:17) are all
`direction_market_robust: false` — the knife-edge verdicts are
correctly refused. The 07-26 econ run (agg +23, markets 5/2) IS
market-robust, confirming the tier is not vacuous. **THE LEAD, AND WHY
IT IS EXPLICITLY NOT A FINDING YET**: across the archive the aggregate
net sign runs +16/-6 weather and +6/-3 econ — a visible lean toward the
sim OVER-awarding fills, which would matter for any maker
registration. It CANNOT be tested, and saying so is the whole point of
this thread of work: those 22 weather runs share orders pairwise, so a
cross-run sign test is the identical bug one level up. Only **one** run
in the entire archive (`20260727T151833.json`) carries a certified
`new_share: 1.0` — the independence block only shipped 07-27, so the
independent sample size for this question is n=1. NAMED DATA GATE for
future passes: accumulate `new_share >= ~0.9` weather brackets (one per
>24h expiry-crossing run, so ~1/day) and re-test the over-award lean at
n>=8; do not act on the 16/6 tally before then. PRACTICAL RULE, joining
the 07-28 atlas rule (`top_day_share` > ~0.3 = a bet on one day):
**read `direction_market_robust` before calling any bracket over/under
verdict, and compare `abs_net_by_market` against `net_disagreement` —
a near-zero aggregate built from large opposing per-market nets is
cancellation, not precision.** Standing conclusion unaffected and now
better supported: no fixed-haircut shortcut, score maker registrations
via queue-PESSIMISTIC on their own markets. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-07-28 14:20 UTC (ATLAS ON THE FIRST INFORMATIVE
FINANCIALS INCREMENT SINCE THE WEEKEND — REAL DRIFT, CHASED TO A
CORRELATION BUG ONE LEVEL ABOVE THE CLUSTER TIER; SETTLEMENT-DAY TIER
SHIPPED. Atlas was the overdue standing report (prior 07-27 14:17 UTC,
24h) and its data-gate had opened — the 11:10 UTC kalshi sweep fired 3h
prior. **The `settled_by_category` hardening from the 07-27 pass paid
off on its first use**: Financials +1,030 settled markets (vs 0 on each
of the prior two runs), so this is the informative Monday-settlement
reading that pass predicted, not another weekend non-reading. Headline
87→93 flagged, 59→63 robust, and unlike the last two runs this is NOT
flat: 6 of the 8 newly flagged and 3 of the 7 newly robust buckets are
Financials, and the top-3 gap drifts on 163 common n>=100 buckets are
all Financials 24h (0.2098 d3, 0.1761 d5, 0.1141 d4) — an order of
magnitude above the 0.0069 max drift of the prior run. THE DRIFT DOES
NOT SURVIVE INSPECTION, AND THE REASON IS A NEW BUG CLASS. Financials
24h d3/d4/d5/d6 ALL flipped gap sign together (d3 -0.147→+0.063, d4
-0.056→+0.058, d5 -0.026→+0.150, d6 +0.022→+0.101) while their n
roughly doubled but their cluster counts barely moved (d5 n 114→293,
clusters 66→82). Broke each bucket down by settlement day: **07-27
alone supplies 46-61% of each bucket's n (d5: 179 of 293 = 61%) across
only ~16 "clusters"**, and that one day reads +0.262 for d5 while
07-13 reads -0.409 and 07-20 reads +0.336 — every mid-decile moving
together *within* a day, in whichever direction the index went that
day. So the 07-19 cluster tier is not conservative enough: it treats
each (series, close_time) ladder as an independent draw, but all
same-day index ladders resolve off ONE underlying path. Financials 24h
d5 was promoted to `flagged_robust` this run on evidence that is 61%
one day's move. HARDENING SHIPPED (80e7623): every bucket now reports
`days` (distinct settlement days), `top_day_share` (largest day's share
of n) and a `flagged_day_robust` tier — Wilson with n = days, the
perfect-within-DAY-correlation worst case. Tiers nest (days <= clusters
<= n), each strictly more conservative than the last;
`flagged`/`flagged_robust` untouched for cross-report comparability per
the divergence-matcher precedent. The day tier is deliberately TOO
harsh where same-day markets have unrelated underlyings (weather across
89 days collapses 16,397 markets and 3,372 genuine clusters to 89
draws) — it is a bound, not an estimate, and `top_day_share` is the
tier-neutral read. Two regression tests (250 independent series on ONE
day: cluster tier keeps the flag, day tier must swallow it,
top_day_share 1.0; same 250 spread one-per-day: day tier agrees); red
on exactly those two (KeyError 'days'), green with. Suite 312→314, ruff
clean, pushed. No promote — atlas is sim-side, no timer runs it. **THE
RESULT, AND IT STRENGTHENS THE STANDING CONCLUSION**: 93 flagged → 63
robust → **12 day-robust**, and all 12 survivors are
signature-direction, ZERO counter-signature. The motivating bucket
(Financials 24h d5, `top_day_share` 0.61 — the highest in the report)
is correctly killed. So is the run's single counter-signature robust
bucket (Financials 1h d3, newly flagged AND newly robust this run,
gap +0.0589) — the "zero counter-signature survivors" claim, which
would have broken 62/63 at the cluster tier this run, holds intact at
the strictest bound available. Every day-tier survivor carries
|gap| >= 0.08. Report: `reports/atlas/20260728T141944.json` (the
pre-fix run is `20260728T141518.json`). PRACTICAL RULE going forward,
alongside the 07-28 bracket-spacing rule: **an atlas bucket with
`top_day_share` above ~0.3 is a bet on one day, not a calibration
finding** — read `flagged_day_robust` before treating any Financials
mid-decile bucket as a strategy lead. STANDING REPORTS: weather maker
bracket (07-28 02:17 UTC) within cadence and, per the new spacing rule,
not an independent reading until it crosses a daily expiry (>~24h);
econ bracket (07-27 02:16 UTC) needs >=336h; QA (07-28 07:00 UTC);
divergence unchanged — shadow run 20260722T081852 still open. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-07-28 02:20 UTC (REPORT CLOCKS ALIGNED TO UTC — the
deferred item from the prior pass, shipped; and the first real use of
the new independence block correctly REFUSES to call this run a
reading, while narrowing yesterday's "weather is clean" claim.
THE FIX (b8f51b1): `queuescore.py` and `prioritycheck.py` stamped
`generated_at` and their report FILENAMES with a naive
`datetime.now()`, while `atlas`/`shadow`/`harness`/`divergence`/
`pair_candidates` all used `datetime.now(UTC)`. The box runs UTC-5, so
every maker-bracket filename read ~5h earlier than the instant it was
written, and every staleness figure in this log computed off a bracket
filename has been ~5h pessimistic. Both modules now use the atlas
idiom. Filename ordering stays monotone across the switch (a UTC name
is always later than the local name of the same instant), so
`independence_vs_prior`'s sorted-glob "most recent comparable prior"
lookup is unaffected; historical filenames are deliberately NOT
rewritten — they are inputs to that lookup, and rewriting them would be
a retro-rescue. The test is a tree-wide AST invariant (no
`datetime.now()` without an explicit tz, across all four packages)
rather than two call-site assertions, because atlas was already correct
and queuescore was simply missed — a per-site test would not have
caught this one and would not catch the next report module either. Red
on exactly the two files, green on the other 55, which also proves no
other module violates it. Suite 255->312 (+57 parametrized module
checks), ruff clean, pushed. No promote: both are sim-side tools, no
timer runs them. THE VERIFICATION RUN, and why it is NOT the 22nd
weather bracket: ran `queuescore --hours 24` to exercise the changed
path end-to-end. Filename `20260728T021715.json` now matches `date -u`
— fix confirmed in the real pipeline. But `independence` reports
**`new_share: 0.10`** (25 new orders of 250; prior
`20260727T151833.json`) — the prior weather run was only ~6h ago
against a 24h trailing window, so this is 90% the SAME orders
re-measured. It is logged as a non-reading and the sign sequence is
UNCHANGED at .../inside(19th)/UNDER-by-1(20th)/inside(21st). For the
record only, since a future pass will otherwise wonder: it scored
crossing 147 vs queue [148 pess, 165 opt], nominally 1 below the floor,
with crossing_but_not_pess=21 vs pess_but_not_crossing=22 (~0.95:1,
symmetric). That the SAME order set drifts from inside (146 vs 145
pess) to under-by-1 (147 vs 148) on a 10% data increment is a useful
calibration fact in its own right: the +/-1-order boundary calls this
log has been reporting are well inside the resolution of the
measurement, which retroactively supports having called both the 20th's
and this breach noise. THE REFINEMENT: yesterday's pass concluded
"weather is clean" because `KXHIGH*` markets expire daily and churn the
top-8 set incidentally. This run narrows that — weather's independence
comes from spacing that CROSSES a daily expiry boundary, not from
weather as a category. At 6h spacing both runs sit inside the same
26JUL27 expiry and share 90% of orders; the 21st run scored
`new_share: 1.0` only because it crossed 26JUL26 -> 26JUL27. Practical
rule going forward: **a weather bracket is an independent reading only
if the prior run was on a different expiry day (spacing >~24h); econ
needs >=336h.** Re-running sooner is not wrong, it is just not a
confirmation, and the report now says so itself. STANDING REPORTS:
atlas (07-27 14:17 UTC) data-gated until the 07-28 11:10 UTC kalshi
sweep, which is also the next INFORMATIVE Financials reading (Monday
settlements, per the weekend-non-reading finding); econ bracket (07-27
02:16 UTC) within cadence and needs >=336h spacing; QA (07-27 07:00
UTC, all-PASS); divergence unchanged — shadow run 20260722T081852 still
open. Untracked `strategies/hylshi_fade.py` re-confirmed present, still
correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-07-27 20:20 UTC (21ST WEATHER MAKER BRACKET — AND THE
SAME "IS THIS ACTUALLY A NEW READING?" BUG CLASS FOUND IN THE BRACKET
SEQUENCE, WHERE IT BITES THE ECON TRACK HARD. Yesterday's atlas pass
found that weekend "flat" Financials readings were the same data
re-measured, and shipped `settled_by_category` to catch it. Applied the
same question to the maker bracket and it does not survive: the window
is trailing (`max(recv_ts) - N hours`, queuescore.py:194), so any re-run
sooner than `--hours` re-scores orders the prior run already counted.
Measured over the archived reports — each **econ** 336h re-run carried
only 11–26% new orders (07-23 11%, 07-24 26%, 07-26a 13%, 07-26b 21%),
and against ALL prior runs combined just 11–14%. So the econ sign
sequence over(07-21)/under(07-23)/under-narrowing(07-25)/inside(07-26)/
inside(07-27) is NOT five independent readings — it is roughly one
reading plus four ~12% increments. **The status log's "second
consecutive inside-bounds reading confirms the under-award lean
resolved" is therefore overstated the same way the atlas confirmation
count was**: those two readings share 79% of their orders, so they were
near-guaranteed to agree. Corrected here; the standing conclusion (no
fixed-haircut shortcut, score via queue-PESSIMISTIC) is unaffected —
it never rested on the econ sequence length. **Weather is clean**, and
for a reason worth recording: it has the identical trailing window but
`KXHIGH*` markets expire daily, so the top-8 market set churns on its
own and supplies independence incidentally rather than by design.
HARDENING SHIPPED: every bracket report now carries an `independence`
block (`prior_report`, `orders_new`, `orders_shared`, `new_share`),
compared against the most recent prior report sharing a series so
weather and econ sequences are never cross-compared; null (not 100%)
when no comparable prior exists. Two regression tests (overlapping
re-run exposed as mostly-not-new; series-scoping + first-run null); red
without the fix, green with. Suite 253→255, ruff clean. THE RUN ITSELF
(21st weather bracket, prior 07-27 08:15 UTC, ~12h stale): 225 virtual
orders across 8 markets (KXHIGHMIA 106, KXHIGHCHI 61, KXHIGHNY 58),
crossing 146 vs queue [145 pess, 162 opt] — **inside** the bounds, with
`new_share: 1.0` (225/225 new, all 26JUL27 markets vs the prior run's
26JUL26) — a genuinely independent reading, the first one labelled as
such. crossing_but_not_pess=21 vs pess_but_not_crossing=20, ~1.05:1,
the most symmetric split yet. Sign sequence: .../inside(17th)/
inside(18th)/inside(19th)/UNDER-by-1(20th)/inside(21st) — the 20th's
one-order breach resolves as the noise it was called, on a fully
independent sample. Routine monitoring holds. Report:
`reports/maker_bracket/20260727T151833.json`. NOTE FOR FUTURE PASSES:
report filenames are LOCAL time (queuescore/`datetime.now()`, box is
UTC-5) while this log labels them UTC — so staleness read off filenames
has been running ~5h pessimistic. Not chased this pass; atlas already
uses UTC, so the fix is to align queuescore. Atlas (07-27 14:20 UTC,
87/59) within cadence and data-gated until the 07-28 11:10 UTC kalshi
sweep — which is also the next INFORMATIVE Financials reading (Monday
settlements); econ bracket (07-27 07:16 UTC) within cadence and now
known to need ≥336h spacing for an independent reading; QA (07-27 07:00
UTC, all-PASS); divergence unchanged — shadow run 20260722T081852 still
open. Untracked `strategies/hylshi_fade.py` re-confirmed present, still
correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-07-27 14:20 UTC (ATLAS RE-RUN — ZERO DRIFT AGAIN, AND
THE "FLAT" FAVORITE-COLLAPSE READINGS TURN OUT TO BE NON-READINGS;
FINGERPRINT HARDENED. Atlas was the most overdue standing report (prior
run 07-26 14:15 UTC, exactly 24h) and its data-gate had opened — the
06:10 CDT / 11:10 UTC kalshi sweep fired ~3h prior. The 05:00 UTC poly
sweep was still holding the archive writer lock at 9h in (its known
long-tail case), but atlas is a reader and completed fine — no waiting
on the lock. Result vs the 07-26 baseline: 87 flagged / 59 robust,
bucket set **IDENTICAL for the second consecutive run** — zero new,
zero dropped, zero robust churn; max gap drift 0.0069 on 163 common
n>=100 buckets (Climate/Weather 6h d8); zero counter-signature robust
survivors. **The real finding is why it was flat.** The Financials 1h
d9 favorite-collapse WATCH item read bit-identical to 07-26 — n 5684,
clusters 456, implied 0.9775, realized 0.9849, every digit unchanged —
which is not "stable", it is *no new data*. Broke the increment down by
category: the +2,921 settled markets landed as Commodities 1233 /
Climate-and-Weather 284 / Economics 17 on the 1h horizon and
**Financials 0**. Same for 07-26 (Financials 0). The 07-25 run, by
contrast, added 1,555 Financials 1h markets. Cause is the calendar:
Financials is KXDJI/KXINXU index ladders, which do not settle on
Sat/Sun, and the 07-26 and 07-27 sweeps both cover weekend windows. So
the status log's "sixth consecutive flat reading" and today's would-be
seventh are the SAME measurement re-run twice — the watch item has **5
informative readings, not 7**, and the daily atlas cadence is simply
uninformative for ladder-heavy categories across a weekend. The
downgrade-to-routine-monitoring call still holds (it was made on the
five genuinely independent readings), but the confirmation count was
overstated and is corrected here. Next informative Financials reading:
the 07-28 sweep, which covers Monday settlements. HARDENING SHIPPED:
`data_fingerprint` now carries `settled_by_category` alongside the
archive-wide totals, so "did this category actually gain evidence since
the last run?" is a one-line diff of two reports instead of manual
bucket arithmetic — the check that caught this was hand-rolled and
would not have been repeated. Regression test asserts the per-category
split and excludes unsettled markets; red without the fix (KeyError),
green with. Suite 252→253, ruff clean. Report:
`reports/atlas/20260727T141742.json`. Weather maker bracket (07-27
03:15 UTC), econ bracket (07-27 02:16 UTC), QA (07-27 07:00 UTC,
all-PASS) all within cadence; divergence unchanged — shadow run
20260722T081852 still open. Untracked `strategies/hylshi_fade.py`
re-confirmed present, still correctly left alone per the 07-18
provenance resolution.)**
(prior 2026-07-27 08:16 UTC (20TH WEATHER MAKER BRACKET RE-RUN —
most overdue runnable standing report (prior weather bracket 07-26
15:16 UTC, ~17h stale; econ bracket ran 07-27 02:16 UTC and is within
cadence at ~6h; atlas is data-gated — the kalshi sweep next fires
06:10 CDT / 11:10 UTC and has not fired since 07-26). QA fired 07-27
07:00 UTC: **all-PASS** (stream age 7s, 14 seq holes all gap-marked,
7.19 GB stream disk; the archive-reachable check skipped as designed
because the live writer held the lock). Archive writer lock was free
for the bracket run. Ran `python -m simulator.queuescore --hours 24`:
220 virtual orders across 8 markets (KXHIGHNY 58, KXHIGHCHI 55,
KXHIGHMIA 48, KXHIGHDEN 32, KXHIGHAUS 27). Crossing 110 vs queue
[111 pess, 120 opt]: crossing lands **1 order BELOW the pessimistic
floor** — a marginal under-award, breaking the three-run inside-bounds
streak, but by the narrowest possible margin on a mid-size sample.
crossing_but_not_pess=19 vs pess_but_not_crossing=20, ~0.95:1 — the
most symmetric disagreement split of any weather run to date (prior
runs ran 1.2–2.4:1 over-award). Sign sequence: .../OVER-OPT(15th)/
OVER-OPT(16th)/inside(17th)/inside(18th)/inside(19th)/UNDER-by-1(20th)
— a one-order breach with near-perfectly balanced two-sided
disagreement is textbook noise, not a directional flip, and it is the
opposite sign from the 15th/16th over-award pair, so it does not
revive that watch item. Routine monitoring holds. Standing conclusion
unchanged: no fixed-haircut shortcut, score maker registrations via
queue-PESSIMISTIC on their own markets. Report:
`reports/maker_bracket/20260727T031529.json`. Econ bracket (07-27
02:16 UTC, second inside-bounds), atlas (07-26 14:15 UTC, 87/59,
favorite-collapse watch flat 6th reading) both within cadence;
divergence unchanged — shadow run 20260722T081852 still open. No code
changes this pass — pure report re-run; suite unchanged at 252.
Untracked `strategies/hylshi_fade.py` re-confirmed present, still
correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-07-27 02:16 UTC (FIFTH ECON MAKER BRACKET RE-RUN —
`--series KXCPI,KXCPIYOY,KXFED --hours 336`, most overdue standing
report (prior econ run 07-26 03:15 UTC, ~23h stale; weather bracket ran
07-26 15:16 UTC and is within cadence at ~11h; atlas is data-gated —
the 07-27 06:10 UTC kalshi sweep has not fired). Archive writer lock
was free. 2,204 virtual orders across 8 markets (KXCPI 1066, KXCPIYOY
741, KXFED 397) — crossing 205 vs queue [182 pess, 217 opt]: crossing
lands **inside** the bounds for the second consecutive run.
crossing_but_not_pess=126 vs pess_but_not_crossing=103, ~1.22:1 — a
mild over-award lean, well inside normal two-sided noise and a mirror
image of the weather side's current ~1.21:1. Econ sign sequence is now
over(07-21) / under(07-23) / under-narrowing(07-25) / inside(07-26) /
inside(07-27) — the two-run under-award lean is now confirmed resolved
rather than a stable bias, matching how the weather side's over-award
streak resolved. Both category tracks are simultaneously sitting
inside-bounds with mild ~1.2:1 over-award skew — the cleanest joint
reading of the series so far. Standing conclusion holds unchanged: no
fixed-haircut shortcut, score maker registrations via queue-PESSIMISTIC
on their own markets. Report:
`reports/maker_bracket/20260726T211629.json`. Weather bracket (07-26
15:16 UTC, 19th re-run, third inside-bounds), atlas (07-26 14:15 UTC,
87/59, favorite-collapse watch flat 6th reading), QA (07-26 07:00 UTC,
all-PASS) all within normal cadence; divergence unchanged — shadow run
20260722T081852 still open. No code changes this pass — pure report
re-run; suite unchanged at 252. Untracked `strategies/hylshi_fade.py`
re-confirmed present, still correctly left alone per the 07-18
provenance resolution.)**
(prior 2026-07-26 15:16 UTC (19TH WEATHER MAKER BRACKET RE-RUN —
most overdue standing report (prior weather bracket 07-26 02:16 UTC,
~13h stale vs the usual ~6h cadence; atlas ran 09:15 UTC and is
current, econ bracket ran 03:15 UTC and is within cadence). Ran
`python -m simulator.queuescore --hours 24`: 192 virtual orders
across 8 markets (KXHIGHNY 73, KXHIGHMIA 67, KXHIGHCHI 52). Crossing
118 vs queue [114 pess, 125 opt]: crossing lands **inside** the
bounds again. crossing_but_not_pess=23 vs pess_but_not_crossing=19,
~1.21:1 — mild over-award lean, well within normal noise. Sign
sequence: .../OVER-OPT(15th)/OVER-OPT(16th)/inside(17th)/
inside(18th)/inside(19th) — third consecutive inside reading, the
17th's streak-break is now solidly confirmed as the new baseline.
Standing conclusion holds: no fixed-haircut shortcut, score maker
registrations via queue-PESSIMISTIC on their own markets. Report:
`reports/maker_bracket/20260726T151616.json`. Atlas (07-26 09:15 UTC,
87/59, favorite-collapse watch flat 6th reading), econ bracket (07-26
03:15 UTC, resolved inside-bounds), QA (07-26 07:00 UTC, all-PASS)
all within normal cadence; divergence unchanged — shadow run
20260722T081852 still open. No code changes this pass — pure report
re-run; suite unchanged at 252. Untracked `strategies/hylshi_fade.py`
re-confirmed present, still correctly left alone per the 07-18
provenance resolution.)**
(prior 2026-07-26 14:15 UTC (ATLAS RE-RUN ON FRESH 07-26 KALSHI
SWEEP — the sweep (06:10 UTC, finished 06:49 UTC) had been the
data-gate blocking this since the prior atlas run at 07-25 14:15 UTC;
ran `python -m simulator.atlas`. Result: 87 flagged / 59 robust,
bucket set IDENTICAL to the 07-25 run — zero new, zero dropped, same
counts. The Financials 1h d9 favorite-collapse WATCH item is flat
again (realized 0.9849 > implied 0.9775, still favorite-underpriced
direction, not collapsing) — sixth consecutive flat reading. Cleanest
possible stability confirmation: the favorite-longshot signature is
holding with zero drift on this increment. Report:
`reports/atlas/20260726T141531.json`. Econ maker bracket (07-26 03:15
UTC, 4th re-run, resolved to inside-bounds), weather bracket (07-26
02:16 UTC, 18th re-run, second inside-bounds), QA (07-26 07:00 UTC,
all-PASS) all within normal cadence; divergence unchanged — shadow
run 20260722T081852 still open. No code changes this pass — pure
report re-run; suite unchanged at 252. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly
left alone per the 07-18 provenance resolution.)**
(prior 2026-07-26 03:15 UTC (FOURTH ECON MAKER BRACKET RE-RUN —
`--series KXCPI,KXCPIYOY,KXFED --hours 336`, most overdue standing
report (prior run 07-25 02:16 UTC, ~25h stale vs the usual cadence;
atlas remains data-gated until the 06:10 CDT / 11:10 UTC kalshi
sweep, weather bracket last ran 6h ago and is within cadence). 1,992
virtual orders across 8 markets (KXCPI 1136, KXCPIYOY 466, KXFED
390) — crossing 172 vs queue [167 pess, 201 opt]: crossing now lands
**inside** the bounds, reversing the prior two under-award readings
(07-23 and 07-25, both landing at/below the pessimistic floor).
crossing_but_not_pess=101 vs pess_but_not_crossing=96, ~1.05:1 —
near-perfectly symmetric, the closest to balanced of any econ
reading yet. Econ sign sequence is now over(07-21) / under(07-23) /
under-narrowing(07-25) / inside(07-26) — the under-award lean fully
resolves this run rather than confirming as a stable bias. Standing
conclusion holds unchanged: no fixed-haircut shortcut, score maker
registrations via queue-PESSIMISTIC on their own markets. Report:
`reports/maker_bracket/20260726T031532.json`. Weather bracket (07-26
02:16 UTC, 18th re-run, second consecutive inside-bounds reading),
atlas (07-25 14:15 UTC, data-gated pending 11:10 UTC sweep), QA
(07-26 02:00 CDT / 07:00 UTC per timer, all-PASS) all within normal
cadence; divergence has nothing new — shadow run 20260722T081852
still open (no closed-run signal available). No code changes this
pass — pure report re-run; suite unchanged at 252. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly
left alone per the 07-18 provenance resolution.)**
(prior 2026-07-26 02:16 UTC (18th WEATHER MAKER BRACKET RE-RUN —
second consecutive inside-bounds reading, confirming the 17th's
streak-break. Prior weather bracket was 07-25 20:16 UTC (~6h stale,
within cadence). Ran `python -m simulator.queuescore --hours 24`: 241
virtual orders across 8 markets (KXHIGHNY 65, KXHIGHCHI 65, KXHIGHAUS
58, KXHIGHMIA 53) — largest market-count sample in the sequence.
Crossing 130 vs queue [120 pess, 132 opt]: crossing lands **inside**
the bounds again. crossing_but_not_pess=38 vs pess_but_not_crossing=28,
~1.36:1 — mild over-award lean but well within normal two-sided noise,
nowhere near the 15th/16th's 2:1-2.4:1 skew that breached the ceiling.
Sign sequence: .../inside/inside/inside/OVER-OPT(15th)/OVER-OPT(16th)/
inside(17th)/inside(18th) — two consecutive inside readings now confirm
the 17th was the real streak-break, not a one-off. Standing conclusion
holds: no fixed-haircut shortcut, score maker registrations via
queue-PESSIMISTIC on their own markets. Report:
`reports/maker_bracket/20260725T211647.json`. Atlas (07-25 14:15 UTC,
data-gated — next kalshi sweep not due until 11:10 UTC 07-26), econ
bracket (07-25 15:16 UTC, within cadence), QA (07-25 07:00 UTC per
timer log, all-PASS) all within normal cadence; divergence has nothing
new — shadow run 20260722T081852 still open (no closed-run signal
available). No code changes this pass — pure report re-run; suite
unchanged at 252. Untracked `strategies/hylshi_fade.py` re-confirmed
present, still correctly left alone per the 07-18 provenance
resolution.)**
(prior 2026-07-25 20:16 UTC (17th WEATHER MAKER BRACKET RE-RUN —
the confirming re-run for the active watch item (two consecutive
over-optimistic readings, 15th and 16th, both landing exactly 1 order
above the optimistic ceiling with ~2:1 skew). Prior weather bracket
was 07-25 08:15 UTC (~12h stale, inside normal cadence). Ran
`python -m simulator.queuescore --hours 24`: 145 virtual orders
across 4 markets (KXHIGHMIA 65, KXHIGHNY 54, KXHIGHCHI 25, KXHIGHAUS
1) — smaller sample than the 15th (294) or 16th (165). Crossing 89 vs
queue [84 pess, 92 opt]: crossing lands **inside** the bounds this
time, breaking the two-run over-optimistic streak. crossing_but_not_
pess=25 vs pess_but_not_crossing=20, ~1.25:1 — much closer to
symmetric than the 15th's 2.4:1 or 16th's 2:1. Sign sequence: .../
inside/inside/inside/OVER-OPT(15th)/OVER-OPT(16th)/inside(17th) — the
third reading does **not** confirm a directional bias; the back-to-
back pair was the first of its kind in the sequence but resolves as
noise rather than a regime shift. **Downgrading the active watch item
back to routine monitoring** — standing conclusion holds unchanged:
no fixed-haircut shortcut, score maker registrations via
queue-PESSIMISTIC on their own markets. Report:
`reports/maker_bracket/20260725T151622.json`. Atlas (07-25 14:15 UTC,
87 flagged/59 robust, Financials favorite-collapse watch item flat on
fifth reading), econ bracket (07-25 02:16 UTC, under-award narrowing),
QA (07-25 07:00 UTC per timer log, all timers green) all within
normal cadence; divergence has nothing new — shadow run 20260722T081852
still open (no closed-run signal available). No code changes this
pass — pure report re-run; suite unchanged at 252. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly
left alone per the 07-18 provenance resolution.)**
(prior 2026-07-25 14:15 UTC (ATLAS RE-RUN — most overdue standing
report (prior run 07-24 14:15 UTC, exactly 24h stale, at the edge of
cadence); the 06:10 UTC kalshi sweep had fired ~3h prior per the
timer log, so fresh candle data was available. Ran `python -m
simulator.atlas` against the same 07-23 14:16 UTC-derived baseline
lineage. Headline counts: 86→87 flagged (+1), 57→59 robust (+2) — 3
newly flagged (Science and Technology 1h d0, Financials 1h d6,
Commodities 6h d7), 2 cleared (Financials 1h d3, Financials 72h d9),
3 newly robust (Commodities 1h d0, Commodities 1h d6, Financials 1h
d4), 1 lost robust (Financials 1h d3) — routine two-sided churn, no
systemic shift. The Financials 1h d9 favorite-collapse WATCH item
(open since 07-18, downgraded to routine monitoring on the 07-24
14:15 UTC fourth reading) gets a fifth reading: implied 0.9775 (flat
vs 0.9776), realized 0.9849 (flat, unchanged from the prior reading),
cluster count 433→456 (still growing), still short of Wilson
clearance (`flagged_robust: false`). Five consecutive readings, no
widening trend — downgrade to routine monitoring holds. Report:
`reports/atlas/20260725T141544.json`. Weather bracket (07-25 08:15
UTC, active watch item — two consecutive over-optimistic readings,
awaiting a third confirming re-run next cycle), econ bracket (07-25
02:16 UTC), QA (07-25 07:00 UTC per timer log, all timers green) all
within normal cadence; divergence has nothing new — shadow run
20260722T081852 still open (no closed-run signal available). No code
changes this pass — pure report re-run; suite unchanged at 252.
Untracked `strategies/hylshi_fade.py` re-confirmed present, still
correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-07-25 08:15 UTC (16th WEATHER MAKER BRACKET RE-RUN —
confirming re-run of the prior over-optimistic reading, as flagged.
Prior weather bracket was 07-24 20:16 UTC (~12h stale, inside the
usual ~6-24h cadence but the flagged item to chase). Ran
`python -m simulator.queuescore --hours 24`: 165 virtual orders
across 8 markets (KXHIGHAUS 47, KXHIGHMIA 36, KXHIGHNY 35, KXHIGHDEN
25, KXHIGHCHI 22) — crossing 91 vs queue [83 pess, 90 opt]: crossing
again lands 1 order ABOVE the optimistic ceiling, same over-award
direction as the 15th run (which landed 1 above a 192 ceiling on 294
orders). crossing_but_not_pess=16 vs pess_but_not_crossing=8, a 2:1
skew toward over-awarding fills — same direction as the 15th's 2.4:1,
though this sample (165 orders) is smaller than the 15th's 294. Sign
sequence: ...over/inside/inside/inside/OVER-OPT(large,skewed,15th)/
OVER-OPT(medium,skewed,16th) — **two consecutive over-optimistic
readings now**, both skewed ~2:1, both landing exactly 1 order above
the ceiling. This is the first back-to-back same-direction pair in
the whole sign sequence (every prior "over" or "under" reading was
isolated between "inside" or opposite-sign readings) — worth
promoting from "noise, needs confirming re-run" to an active watch
item: re-run again next cycle to see if a third consecutive
over-optimistic reading appears, which would be the first real
evidence of a directional sim bias rather than two-sided noise.
Standing conclusion unchanged for now (not yet enough to call a
regime shift, still score maker registrations via queue-PESSIMISTIC
on their own markets): one more same-direction reading would change
that. Report: `reports/maker_bracket/20260725T031532.json`. Econ
bracket (07-25 02:16 UTC, under-award narrowing), atlas (07-24 14:15
UTC), QA (07-24 07:00 UTC, all-PASS) all within normal cadence;
divergence has nothing new — shadow run 20260722T081852 still open
(no closed-run signal available). No code changes this pass — pure
report re-run; suite unchanged at 252. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly
left alone per the 07-18 provenance resolution.)**
(prior 2026-07-25 02:16 UTC (THIRD ECON MAKER BRACKET RE-RUN —
`--series KXCPI,KXCPIYOY,KXFED --hours 336`, most overdue standing
report (last run 07-23 03:16 UTC, ~47h stale vs the usual cadence).
2,071 virtual orders across 8 markets (KXCPI 1189, KXCPIYOY 501,
KXFED 381) — crossing 177 vs queue [181 pess, 221 opt]: crossing
still lands AT/BELOW the pessimistic floor (under-award), same
direction as the prior run (169 vs [183, 220]), but the skew
narrowed — crossing_but_not_pess 93→100, pess_but_not_crossing
107→104, moving from ~0.87:1 toward near-symmetric ~0.96:1. Econ
sign sequence is now over(07-21) / under(07-23) / under-narrowing
(07-25) — two consecutive under-award readings, but the shrinking
gap means this isn't a clean confirmation of a stable econ-side bias
either; same standing conclusion as weather: no fixed-haircut
shortcut, score maker registrations via queue-PESSIMISTIC on their
own markets. Report: `reports/maker_bracket/20260724T211645.json`.
Weather bracket (07-24 20:16 UTC, largest-sample over-optimistic
reading, awaiting its own confirming re-run), atlas (07-24 14:15
UTC), QA (07-24 07:00 UTC, all-PASS) all still within normal cadence;
divergence has nothing new — shadow run 20260722T081852 still open
(~66h, no closed-run signal available, previously confirmed
alive/healthy). No code changes this pass — pure report re-run;
suite unchanged at 252.)**
(prior 2026-07-24 20:16 UTC (15th WEATHER MAKER BRACKET —
largest sample yet, lands AT the optimistic ceiling with an
asymmetric skew. Prior bracket was 07-23 21:17 UTC (~23h stale, at
the edge of the ~6-24h cadence); archive writer lock was free
(hyxlab-stream running normally, no sweep holding it). Ran
`python -m simulator.queuescore --hours 24`: 294 virtual orders
across 8 markets (KXHIGHNY 111, KXHIGHMIA 107, KXHIGHCHI 76) — the
biggest window yet (prior runs: 173-260). Crossing 193 vs queue
[171 pess, 192 opt] — crossing lands 1 order ABOVE the optimistic
bound, not just inside it. More notably, crossing_but_not_pess=38 vs
pess_but_not_crossing=16 — a 2.4:1 skew toward the sim over-awarding
fills, breaking the near-symmetric noise pattern of the last several
runs (12th/13th/14th were all ~1:1). The 11th run (07-21, report
`20260721T031553.json`) was the only prior crossing-above-opt event,
but that was a thin 59-order sample flagged as likely noise; this is
294 orders and still shows the same direction. One reading isn't
enough to call a regime shift — sign sequence remains under/over/
under/inside/over/inside/over/inside/under/inside/OVER-OPT(thin)/
inside/inside/inside/OVER-OPT(large,skewed) — but this is the first
large-sample over-optimistic reading and worth a confirming re-run
next cycle rather than filing as routine churn. Standing conclusion
holds regardless: no stable sign for the crossing rule's bias, score
any maker registration via queue-PESSIMISTIC on its own markets, not
a crossing proxy. Report: `reports/maker_bracket/20260724T151636.json`.
Atlas (07-24 14:15 UTC), econ bracket (07-23 03:16 UTC), QA (07-24
07:00 UTC, all-PASS) all still within normal re-run cadence;
divergence has nothing new — shadow run 20260722T081852 still open
(~60h, confirmed alive/healthy as of the prior pass). No code changes
this pass — pure report re-run; suite unchanged at 252.)**
(prior 2026-07-24 14:15 UTC (ATLAS RE-RUN — the 11:10 UTC kalshi
sweep fired (completed 12:14 UTC, sweep_log confirms), unsticking the
data-gate flagged in the 08:15 UTC cold-start check: candle max end_ts
advanced from a flat 07-23 11:00 UTC to 07-24 11:00 UTC. Ran atlas
against the 07-23 14:16 UTC baseline. Headline counts: 85→86 flagged
(+1), 59→57 robust (-2) — 2 newly flagged (Economics 72h d0, Financials
6h d9), 1 cleared (Economics 6h d8), 0 newly robust, 2 lost robust
(Commodities 1h d6, Financials 6h d4) — routine churn, no systemic
shift. The Financials 1h d9 favorite-collapse WATCH item (open since
07-18) gets a fourth reading: implied .9776 (flat), realized
.9846→.9849 (flat, +0.03pp) — gap essentially unchanged this time
(prior readings widened +0.59pp then +0.70pp; this one holds flat),
cluster count 419→433, still short of Wilson clearance
(`flagged_robust: false`). Four consecutive readings, no sustained
widening trend — fade thesis remains unsupported, watch item downgraded
to routine monitoring rather than active-widening concern. Report:
`reports/atlas/20260724T141551.json`. Weather bracket (07-24 02:17 UTC),
econ bracket (07-23 03:16 UTC), QA (07-24 07:00 UTC, all-PASS) all
still within normal re-run cadence; divergence has nothing new — shadow
run 20260722T081852 still open (~54h, no ended_at); checked the
daemon directly this pass (`systemctl --user status hyxlab-shadow`):
active/running since 07-22 08:18 UTC, RSS 501MB (under the 1G cap),
still logging fresh poll/fill counts every ~5min (fills=8344 as of
08:47 UTC) — confirmed alive and healthy, not stuck; the long open
duration is just the shadow_runs table having no closed-run signal
available, as previously noted. No code
changes this pass — pure report re-run; suite unchanged at 252.)**
(prior 2026-07-24 08:15 UTC (COLD-START CHECK — everything
current or gated, no report re-run warranted. QA fired again at 07-24
07:00 UTC (02:00 CDT) — all-PASS, second clean run since the
tradepass fix, nothing new to root-cause. Weather maker bracket
(02:17 UTC, ~6h old) and econ maker bracket (03:16 UTC, ~5h old) are
both well inside the recent ~6-24h re-run cadence, not stale enough
to re-run yet. Atlas remains data-gated: next kalshi sweep fires
11:10 UTC 07-24, hasn't happened (`collector.sweep --doctor` still
shows Climate/Weather candle counts flat vs the 07-23 sweep).
Divergence: `shadow_runs` in `data/hyxshadow.duckdb` shows no run_id
newer than `20260722T081852` (still the latest started_at, no closed-
run signal available from that table — confirms the standing "still
open" read, nothing to reconcile). No code changes this pass; suite
unchanged at 252. Untracked `strategies/hylshi_fade.py` re-confirmed
present and still correctly left alone per the 07-18 provenance
resolution — sibling project's artifact, user-gated, not touched.)**
(prior 2026-07-24 02:17 UTC (14th WEATHER MAKER BRACKET —
no drift, crossing still inside bounds. Prior weather bracket was
07-23 20:16 UTC (~11h stale, in line with the recent ~6-24h cadence);
archive writer lock (stream side) was held by the live stream daemon
but queuescore's reader retried and completed fine, consistent with
the standing "readers don't take writer.lock" note. Re-ran the default
top-print-count window (`python -m simulator.queuescore --hours 24`):
260 virtual orders across 8 markets (KXHIGHNY 99, KXHIGHMIA 86,
KXHIGHCHI 75) — crossing 142 vs queue [140 pess, 163 opt], landing
INSIDE the bracket again, crossing_but_not_pess=32 / pess_but_not_crossing=30
— near-symmetric two-sided noise, no sign flip. Consistent with the
standing conclusion: no stable sign for the crossing rule's bias, score
any maker registration via queue-PESS on its own markets, not a
borrowed calibration. Report: `reports/maker_bracket/20260723T211702.json`.
Atlas (07-23 14:16 UTC) is data-gated until the next kalshi sweep
(11:10 UTC 07-24, hasn't fired yet — candles max end_ts still 07-23
11:00 UTC); econ maker bracket (07-23 03:16 UTC) and QA (07-23 07:00
UTC, all-PASS) both current; divergence has nothing new — shadow run
20260722T081852 still open (~42h, no ended_at recorded, matches the
standing "still open, healthy" read). No code changes this pass — pure
report re-run; suite unchanged at 252.)** (prior 2026-07-23 20:16 UTC (13th WEATHER MAKER BRACKET —
no drift, crossing still inside bounds. Prior weather bracket was
07-22 21:17 UTC (~23h stale); archive writer lock was free, so
re-ran the default top-print-count window (`python -m
simulator.queuescore --hours 24`): 173 virtual orders across 8
markets (KXHIGHNY 93, KXHIGHMIA 80) — crossing 96 vs queue [95 pess,
112 opt], landing INSIDE the bracket again, crossing_but_not_pess=22
/ pess_but_not_crossing=21 — near-symmetric two-sided noise, no sign
flip. Consistent with the standing conclusion: no stable sign for the
crossing rule's bias, score any maker registration via queue-PESS on
its own markets, not a borrowed calibration. Report:
`reports/maker_bracket/20260723T151636.json`. Atlas (07-23 14:16 UTC),
econ maker bracket (07-23 03:16 UTC), QA (07-23 07:00 UTC, all-PASS)
all current; divergence has nothing new — shadow run 20260722T081852
still open (~36h). No code changes this pass — pure report re-run;
suite unchanged at 252.)** (prior 2026-07-23 14:16 UTC (ATLAS RE-RUN — fresh data from the
06:10 UTC kalshi sweep, which finished 07:15:53 UTC today (5,395
markets, 56,560 candles, 38 errors); archive writer lock was free.
Ran a full-archive atlas against the 07-22 20:16 UTC baseline. Headline
counts: 85 flagged (unchanged), 56→59 robust (+3) — 2 newly flagged
(Economics 6h d8, Financials 24h d8), 2 cleared (Commodities 24h d2,
Financials 1h d8), 5 newly robust, 2 lost robust — routine churn, no
systemic shift. Signature check: 38/59 robust buckets in the classic
favorite-longshot direction, zero counter-signature robust survivors —
still intact. The Financials 1h d9 favorite-collapse WATCH item (open
since 07-18, first data point 07-22) gets a third reading: implied
.9775→.9776 (flat), realized .9834→.9846 — gap widened again, +0.59pp
→ +0.70pp, still favorite-underpriced, still not `flagged_robust`
(cluster count 395→419, still short of Wilson clearance at that gap).
Three consecutive readings now point away from "collapsing"; the fade
thesis remains unsupported. Report: `reports/atlas/20260723T141618.json`.
Maker bracket (econ, 08:16 UTC) and weather bracket (02:17 UTC) both
current; divergence has nothing new — shadow run 20260722T081852 is
still open (~30h, 4,428 fills, healthy) with no closed run since the
last reconciliation; QA all-PASS at 07:00 UTC. No code changes this
pass — pure report re-run; suite unchanged at 252.)** (prior
2026-07-23 08:16 UTC (SECOND NON-WEATHER (ECON) MAKER
BRACKET RE-RUN, rolling 14-day window — under-award confirmed again,
signature unchanged from the single 07-21 data point. QA at 07:00 UTC
07-23 (02:00 CDT) ran all-PASS — the first run since the tradepass fix
(a75916e) shipped, and the "trade tape covers retention window" check
that failed 07-22 passed cleanly, closing that loop. Archive writer
lock was free but atlas is data-gated (next kalshi sweep 11:10 UTC,
hasn't fired yet) and the live shadow run (20260722T081852, 23h in,
3,560+ fills, RSS 418MB — healthy) is still open so divergence has
nothing new to reconcile; ran the econ-series maker bracket instead
since it has only 3 prior data points vs weather's 12+. `python -m
simulator.queuescore --series KXCPI,KXCPIYOY,KXFED,KXU3,
KXJOBLESSCLAIMS,KXPAYROLLS --hours 336` (same series/window shape as
07-21, window rolled forward ~2 days): 1,867 virtual orders across 8
markets (KXCPI 969 / KXCPIYOY 532 / KXFED 366 — KXU3/KXJOBLESSCLAIMS/
KXPAYROLLS still too thin) — crossing 169 vs queue [183 pess, 220 opt]:
crossing sits BELOW the pessimistic floor, UNDER-awarding (14 real pess
fills the sim forgoes net; crossing_but_not_pess=93 /
pess_but_not_crossing=107, two-sided noise as usual). Order volume
dropped sharply vs 07-21's 6,363 (same series/cap) — expected, not
alarming: the window rolled past the 26JUN CPI print's peak trading
burst, leaving mostly quieter 26JUL forward-contract activity plus the
now-frozen 26JUN post-settlement tail. Econ bracket sign sequence is
now under(07-14)/over(07-15a)/over(07-15b)/over(07-21)/UNDER — still no
stable sign, reinforcing the standing weather conclusion: score any
maker registration via queue-PESS on its own markets, category and
window don't matter. Report:
`reports/maker_bracket/20260723T031612.json`. No code changes this
pass — pure report re-run; suite unchanged at 252.)** (prior
2026-07-23 02:17 UTC (12th WEATHER MAKER BRACKET — no
drift, crossing rule still lands inside queue bounds. Prior weather
bracket was 07-21 03:15 UTC (~46h stale); archive writer lock was
free, so re-ran the default top-print-count window (`python -m
simulator.queuescore --hours 24`): 242 virtual orders across 8 markets
(KXHIGHNY 101, KXHIGHCHI 58, KXHIGHMIA 57, KXHIGHAUS 26) — crossing
150 vs queue [151 pess, 160 opt], landing INSIDE the bracket again
(same qualitative shape as most weather runs), crossing_but_not_pess=24
/ pess_but_not_crossing=25 — near-symmetric two-sided noise, no sign
flip. Consistent with the standing conclusion: no stable sign for the
crossing rule's bias, score any maker registration via queue-PESS on
its own markets, not a borrowed calibration. Report:
`reports/maker_bracket/20260722T211719.json`. Atlas (07-22 20:16 UTC),
divergence (07-22 14:16 UTC, fourth reconciliation) both current; QA
next fires 07:00 UTC 07-23 — first run since the tradepass fix
(a75916e) shipped, watch for PASS. No code changes this pass — pure
report re-run; suite unchanged at 252.)** (prior
2026-07-22 20:16 UTC (ATLAS RE-RUN — Financials
favorite-collapse WATCH item gets its first real data point, and it
points the opposite way. Kalshi sweep finished this morning (07:21 UTC,
5,887 markets) and the archive writer lock was free, so ran a fresh
full-archive atlas (`reports/atlas/20260722T201636.json`) against the
07-19 20:18 UTC baseline. Headline counts: 85 flagged (was 79, +6) but
56 robust (was 58, -2) — churn in both directions, nothing systemic
(9 newly flagged incl. some mid-decile buckets like Financials 1h d3/d9
and Commodities 6h d4/d6, 3 cleared; robust gained Commodities 6h d4 +
Financials 24h d1/d7, lost Commodities 1h d4/d6/24h d2 + Financials
1h d8/6h d3). Signature count 36/56 robust buckets in the classic
favorite-longshot direction, zero counter-signature robust survivors —
still intact. The specific WATCH item (07-18: "if Financials favorite
gaps keep collapsing across the next 2-3 sweeps, that arm is genuinely
fading") now has its first follow-up cluster-robust reading: Financials
1h d9 (n=4,145→4,926, clusters 350→395) implied .979→.9775, realized
.9805→.9834 — gap widened from +0.15pp to +0.59pp in the
favorite-underpriced direction (now `flagged=True`, still not
`flagged_robust` — cluster count too small to clear Wilson at that
gap). That's the opposite of collapsing; one more sweep still needed
before calling it (only two robust-tagged readings exist so far, this
being the second), but the fade thesis has no supporting data yet.
Maker bracket still fresh at 07-21 15:21 UTC (~29h), divergence current
as of the last entry, QA next runs 07-23 02:00 UTC. No code changes
this pass — pure report re-run; suite unchanged at 252.)** (prior
2026-07-22 14:16 UTC (DIVERGENCE — fourth run reconciled,
near-perfect again. Found shadow run `20260721T032349` had closed
(superseded by two restarts at 08:18 UTC today, 4,997 fills, 03:24
07-21 → 08:18 07-22) and never been divergence-checked; archive writer
lock was free so ran it. Result: 99.98% match vs shadow / 99.84% vs
replay (4,996/4,997 shadow, 4,996/5,004 replay), price_delta mean/
median/abs_mean all 0.0 across every matched fill, fees 205.67 (shadow)
vs 206.19 (replay) and gross cash 3650.18 vs 3660.84 — near-identical,
not the bit-identical 100% of the last two closed runs, but the tiny
gap resolves entirely via existing classifiers: 1 shadow leftover is
`reseed_twin`, replay's 8 leftovers are 6 `gap` + 2 `reseed_twin`, ZERO
`unexplained` on either side. The taker-haircut-≈0 finding now holds on
a fourth independent closed run with full cause-accounting, no residual
mystery. Report: `reports/shadow_divergence/20260721T032349.json`. No
code changes this pass — pure report re-run; suite unchanged at 252.)**
(prior 2026-07-22 08:20 UTC (QA FAILURE FIXED — trade-tape
retention gap closed at the root, not just the symptom. The 07-22
02:00 UTC `hyxlab-qa` run FAILED for the first time in a while:
"trade tape covers retention window — 1 traded markets unswept"
(`KXHIGHTLV-26JUL18-B102.5`). Root cause: `collector.trades_backfill`
— the retro-pass designed to catch `collector.sweep`'s per-market
trade-fetch `HTTPError`s — was never wired to a systemd timer; it only
ran once historically for the initial 60-day retention catch-up.
Meanwhile `sweep.py`'s inline trade fetch swallowed `HTTPError`
silently (`except requests.HTTPError: pass`, no log line), and once a
series' watermark advances past a market, the daily sweep can never
retry it — only the (unscheduled) retro-pass could. Found 154 pending
markets total (not just the 1 QA's 55-day/volume>0 filter flagged); ran
`trades_backfill` manually to clear the backlog — 85,038 trades
inserted, 0 errors, QA now all-PASS. Shipped so it self-heals going
forward (a75916e): `hyxlab-tradepass.timer` (daily 06:35 UTC, between
sweep 06:10 and QA 07:00), enabled + armed; `sweep.py`'s HTTPError
branch now logs the ticker + status code instead of swallowing it.
Suite still 252 (existing glob-based `test_systemd_units.py` invariants
cover the new unit files with no new tests needed); promoted + pushed.)**
(prior 2026-07-22 02:17 UTC (DIVERGENCE CHECK CLEARED — the
memory-gated pending item on shadow run 20260719T082112 is resolved.
Poly sweep's writer lock had released (last run finished before 21:15
UTC 07-21 collect; `flock -n` on `data/writer.lock` confirmed free), so
ran the closed-run divergence check: PERFECT convergence again, 4,573/
4,573 fills matched both directions, zero unmatched in either stream,
price_delta mean/median/abs_mean all 0.0, fees 174.28 and gross cash
3044.18 identical to the cent shadow vs replay. Third full closed run
now reconciled bit-for-bit (after 20260713T064302's 11,943 fills and
20260716T130721's 11,521) — taker haircut ≈ 0 keeps holding, no drift
to chase. Report: `reports/shadow_divergence/20260719T082112.json`.
Current shadow run 20260721T032349 is still open (started post the
07-21 03:23 UTC restart, 4,052 fills and counting) — next divergence
check waits for it to close. Maker bracket and atlas are current as of
the prior entry; suite 252 green, no code changes this pass.)** (prior
2026-07-21 20:25 UTC (FIRST NON-WEATHER MAKER BRACKET —
coverage caveat closed for Economics. All 11 prior brackets were 100%
`KXHIGH*` weather (top-print-count default); ran
`simulator.queuescore --series KXCPI,KXCPIYOY,KXFED,KXU3,
KXJOBLESSCLAIMS,KXPAYROLLS --hours 336` (full 14-day stream history for
those series) to test whether the crossing-rule bracket generalizes
outside weather. Result: 6,363 virtual orders across KXCPI/KXCPIYOY/
KXFED/KXU3 (KXJOBLESSCLAIMS/KXPAYROLLS too thin to seat an order) —
crossing 404 vs queue [368 pess, 436 opt]: crossing lands INSIDE the
bracket, same qualitative shape as most weather runs, with
crossing_but_not_pess=250 / pess_but_not_crossing=214 showing the same
two-sided noise. This is real evidence, not an assumption: the
"no stable sign, score via queue-PESS on your own markets" conclusion
now has a non-weather data point behind it, not just weather brackets
extrapolated across categories. Report:
`reports/maker_bracket/20260721T152147.json`. Meanwhile the poly sweep
(05:00 UTC start, PID 33990) ran unusually long today — past its usual
~2-7h into a 15h+ tail, ~600 markets short of the full 16,237-market
universe as of this check, actively writing (DB mtime keeps advancing)
so not hung, just slow (possibly heavier 429 backoff) — divergence
check on shadow run 20260719T082112 stays lock-gated one more window;
don't wait on it in a loop.)** (prior
2026-07-21 08:20 UTC (11th MAKER BRACKET — first
crossing-ABOVE-optimistic run. Box is calm post-storm (17Gi free, no
hytest processes), but the daily poly sweep (started 05:00 UTC, PID
33990) holds the archive writer lock until ~12:00 UTC, so the
memory-gated divergence check on shadow run 20260719T082112 is now
merely lock-gated — same "don't wait on the lock in a loop" rule,
deferred to next window. Ran the maker bracket instead (stream-db
only, no archive dependency): crossing 40 vs queue [31 pess, 35 opt]
— crossing sits ABOVE the optimistic ceiling, not just above pess.
Every prior "over" run (7th, 8th run-9-adjacent) landed within/near
the opt bound; this is the first time crossing exceeds BOTH bounds
outright. Likely small-sample: only 59 orders this window (vs 179–285
on recent runs) after a quiet stretch, so treat as noisy until the
next run confirms — but flagging since a fixed-haircut model would
have been wrong in the over-award direction here too. Sign sequence
now: under/over/under/inside/over/inside/over/inside/under/inside/
OVER-OPT. Still no stable sign — regime-dependent crossing-rule bias
conclusion holds; any maker registration must score fills via
queue-PESSIMISTIC bounds on its own markets, not a crossing proxy.
Report: `reports/maker_bracket/20260721T031553.json`.)** (prior
2026-07-20 08:40 UTC (BOX-WIDE OOM STORM — external cause,
capture survived, batch units hardened. A sibling-workspace job
(`/home/devs/workspace/hytest`, `impl.m1.gate`, 16 shards × ~4.9G ≈
58G of the box's 60G, no swap) saturated the machine from ~16:00 UTC
07-19; the kernel OOM killer shot hyxlab-stream TWICE (16:53, 17:05 —
auto-restarted, reconnects gap-marked, stats flushing normally since),
the 07-19 20:15 autoloop's background divergence replay (app.slice,
21:17), then today's poly sweep (06:10 UTC, 70 min in, 3.1G peak;
Gamma keyset walk was also degraded — INCOMPLETE at 11,400 markets on
persistent 500s) and QA (07:25 UTC, 1.6G peak, after logging stream
age 378s — pressure-induced — and PASSing domains + book-seq with 27
gap rows). NOT our leak: shadow's own RSS is the counter-evidence
(below). Both missed jobs self-heal on tomorrow's timers (poly
day-buckets mature over ~2 days; QA 07:00) — deliberately NOT retried
today, the box still shows only ~1.9G available and a retry risks
collateral kernel kills. hytest is the user's job; left untouched.
HARDENING SHIPPED: all 7 timer-driven oneshot units now carry
`OOMScoreAdjust=500` (unprivileged units can't LOWER the daemons'
score, but raising the batch units' makes the kernel sacrifice
restartable work before live capture); 2 unit-invariant regression
tests, suite 250→252; promoted. SHADOW RSS WATCH ITEM CLOSED: run
20260719T082112 at the full-day mark — VmRSS 270MB (305@6h → 285@12h
→ 270@24h, vs the killed run's ~500MB-and-climbing at 10h), and it
rode out the entire OOM storm untouched: the equity-curve trim is a
confirmed plateau, the mid-run OOM class is dead. Promote restarted
stream+shadow (routine; new shadow run begins post-promote). STILL
PENDING, memory-gated: first divergence check on run 20260719T082112
(the replay was OOM-killed 21:17 07-19; re-run when hytest releases
the box). Atlas data-gated until the 11:10 kalshi sweep as usual.)**
(prior 2026-07-19 20:20 UTC (ATLAS CLUSTER-ROBUST TIER SHIPPED —
the 07-18 ladder-correlation caveat is now quantitative machinery, not
a prose warning (b554473). Sibling strikes of one (series, close_time)
ladder settle on ONE outcome (avg cluster size: Commodities 36.3,
Financials 15.5, Economics 12.3, Weather 7.4), so every atlas bucket
now reports `clusters` and a `flagged_robust` tier — the Wilson
interval recomputed with n = clusters, the perfect-within-cluster-
correlation worst case; true confidence lies between the tiers. The
original `flagged` field is untouched for cross-report comparability
(the divergence-matcher precedent). Full-archive run
`reports/atlas/20260719T201823.json`: **79 flagged → 58 robust**. All
21 demotions are small-|gap| (~0.01–0.06) extreme-decile buckets —
Commodities/Economics d0/d9 at every horizon, Econ 7d d0, Sci-Tech 1h
d9 / 6h d0 — exactly where ladder-inflated n was manufacturing
significance; the favorite-longshot signature SURVIVES the robust tier
(39/58 signature-direction, incl. the big Financials/Commodities 1h–6h
favorite and longshot deciles at 100–300 clusters each). The lone
counter-signature robust survivor is Financials 1h d8 (realized .787
vs implied .849, 161 clusters) — this FEEDS the open WATCH item on
collapsing Financials favorite gaps: the fade is now cluster-robust,
not just cohort noise, though still one bucket; next weekday sweeps
still decide. Pre-reg implication recorded: any favorite-longshot
registration should size its evidence on `clusters`, not n. Suite
248→250 (single-ladder-collapses-flag + independent-markets-keep-flag
regressions). Also: shadow RSS watch item at ~12h into run
20260719T082112 — VmRSS 285MB, DOWN from 305MB at 6h (killed run was
~500MB and climbing at 10h): the equity-curve trim is plateauing as
predicted; final full-day confirmation ~08:21 07-20.) (prior
2026-07-19 14:20 UTC (BOTH LOCK-GATED ITEMS CLEARED — the
poly sweep's writer lock released; ran the FINAL closed-window
divergence check on shadow run 20260716T130721 AND the 11:10-sweep
atlas re-run in one pass. (1) DIVERGENCE, full closed run 13:07 07-16
→ 22:03 07-18 (2.37 days, 11,521 fills — +293 past the 07-18 check,
including the OOM-kill run-end boundary): PERFECT convergence again on
the complete window — 11,521/11,521 matched both directions, ZERO
unmatched in either stream (all four causes 0, so the abrupt OOM end
produced no boundary leftovers), every match exact-tier, price_delta
mean = median = abs_mean = 0.0, fees 472.75 and gross 8365.47
identical to the cent on both sides. The run is now closed AND fully
reconciled end-to-end; taker haircut ≈ 0 stands on two complete
multi-day runs. Report: `reports/shadow_divergence/20260716T130721.json`
(regenerated over the 07-18 partial check). (2) ATLAS on +1,458
settled markets (77,210→78,668; a small weekend increment): flagged
80→79, zero added, one dropped — Financials 7d d0, an extreme-longshot
whose tiny gap (−0.028→−0.011) crossed inside Wilson; max gap drift on
111 common meaningful-n buckets +0.016 (that same bucket). Signature
HOLDS, no drift to chase. WATCH item on collapsing Financials favorite
gaps stays OPEN but uninformative this window: the increment barely
touched Financials 1h buckets (n +0–2 per decile; 1h d6 gap unchanged
at +0.007), so it neither confirms nor clears the fade — needs the
next weekday sweeps. Report: `reports/atlas/20260719T141830.json`.
(3) Shadow OOM-fix RSS verify on live run 20260719T082112: VmRSS
305MB at ~6h in (HWM 835MB from the DuckDB seed) vs the killed run's
~500MB@10h climb — the equity-curve trim is holding; keep the watch
item until a full day confirms the plateau.)** (prior
2026-07-19 08:50 UTC (SHADOW MID-RUN OOM CLASS FOUND AND
FIXED — hyxlab-shadow was kernel-OOM-killed AGAIN 2026-07-18 22:03 UTC,
but mid-run at the 1G cgroup cap after 2.3 days, NOT the seed-time
DuckDB class fixed 07-12. Root cause: `Simulator.step()` appends one
`(datetime, equity)` tuple to `result.equity_curve` per snapshot —
the killed run stepped 5.25M snapshots, ~800MB of curve at ~150B/entry,
a ~340MB/day leak by design (fine for bounded backtests, unbounded for
the forever-running daemon; the replacement run was already at ~500MB
after 10h). Fix (5d7e4aa, promoted): max_drawdown is now a running
stat updated at append time in the sim, and shadow trims the in-memory
curve to the latest point after each poll's ledger persist — the full
per-poll curve already lives in `shadow_equity`, and backtest-path
behavior is unchanged (simui chunked≡one-shot equivalence untouched).
Both regression tests fail without the fix; suite 246→248. Fallout:
shadow run 20260716T130721 is now CLOSED at 11,521 fills (13:07 07-16 →
22:03 07-18), the OOM-interrupted 20260718T220427 closed at ~1,823
fills/10.3h, and run 20260719T082112 is live WITH the fix. NEXT: final
closed-window divergence check on 20260716T130721 (+293 fills past the
perfect 07-18 check, incl. the run-end boundary) — attempted this
session but the archive is writer-locked by the ~7h poly sweep (started
05:00 UTC); lock-gated until ~12:00 UTC, same gate as the 11:10-sweep
atlas re-run. QA 07-19 07:00 UTC verified all-PASS.) (prior
2026-07-19 02:16 UTC (MAKER BRACKET re-run — 9th weather
bracket, exactly 24h since the 8th (07-18 02:16 → 07-19 02:16, a
genuinely fresh window of continuously-accumulating stream trade
data). Default weather-dominated run, 240 join-the-touch virtual
orders across 8 KXHIGH markets (NY 102 / MIA 80 / CHI 58): crossing
rule fills 147 vs queue [150 pess, 160 opt] — crossing sits BELOW the
pessimistic floor, UNDER-awarding this window (26 real pess fills the
sim forgoes vs 23 crossing-not-pess). The weather sign sequence is now
under/over/under/inside/over/inside/over/inside/UNDER across nine
runs — still no stable sign, so the regime-dependent crossing-rule
bias conclusion firmly HOLDS and there is still no fixed-haircut
shortcut: any maker registration must score fills via
queue-PESSIMISTIC bounds on its own markets. Report:
`reports/maker_bracket/20260718T211601.json`.) (prior 2026-07-18 20:20 UTC (DIVERGENCE first check on the NEW
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

- **External box-wide memory pressure (2026-07-19→)**: sibling
  workspace `hytest` (`impl.m1.gate`, 16 shards ≈ 58G of 60G, no swap)
  starves everything; kernel OOM killed stream ×2, one autoloop
  replay, the 07-20 poly sweep and QA. Hardened 07-20: oneshot units
  carry `OOMScoreAdjust=500` so batch work dies before capture
  daemons. The user's job — do not kill it. While it runs: skip
  memory-heavy ad-hoc work (divergence replays, atlas over full
  archive is ~fine but monitor), expect degraded WS connects, and
  verify the next poly sweep (05:00) and QA (07:00) complete. If a
  matured poly day-bucket lands short, the ~2-day maturation backfill
  covers a single missed sweep.

- **DuckDB vs cgroup memory caps**: hyxlab-shadow was kernel-OOM-killed
  at boot twice (2026-07-11, 2026-07-12 — systemd auto-restart
  recovered both) because DuckDB's default memory_limit scales with
  SYSTEM RAM, far above the unit's MemoryMax=1G; the seed-time ORDER BY
  blew the cap. FIXED for shadow 2026-07-12 (`stream_conn`: 512MiB
  engine cap, 2 threads, spill to `data/duckspill-shadow`, and it now
  uses the mandated `connect_retry`). **A SECOND, distinct OOM class
  hit 2026-07-18 22:03 UTC**: mid-run kill after 2.3 days from the
  per-snapshot in-memory equity curve (~340MB/day) — FIXED 2026-07-19
  (5d7e4aa: running max_drawdown + shadow trims the curve per poll).
  VERIFIED CLOSED 2026-07-20: run 20260719T082112 plateaued —
  VmRSS 305MB@6h → 285MB@12h → 270MB@24h (HWM 835MB, the DuckDB seed
  spike) — and survived the 07-19/07-20 box-wide OOM storm untouched;
  no third accumulator. `hyxlab-simui` shares the 1G cap and replays big
  archive windows — same exposure (it holds full curves by design for
  bounded windows); apply bounds if it ever OOMs.

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
