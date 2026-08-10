# Status & next steps (living page)

Updated: **2026-08-10 08:30 UTC (RUNG-1 PASS FINDS AND FIXES A DAEMON
DEATH: RUN 20260808T063109 DIED AT 02:16Z ON AN UNHANDLED LEDGER LOCK
CONFLICT — PERSIST IS NOW HELD-FOR-RETRY, PROMOTED 2a69145.**
**(1) Shadow crash root-caused**: at 02:16:14Z `ledger.persist` raised
`duckdb.IOException` — hyxshadow.duckdb writer-locked by an ad-hoc
python PID 1476939 (a default read-write connect, almost certainly the
prior status pass's cohort query). No handler on the persist path →
exit 1 → systemd restart opened run 20260810T021644, ending the
cohort-accumulating run 20260808T063109 at 1d20h, one day before its
second cohort read. The 08-09 weather-block settlements will land in
the ledger under whichever run holds the open positions — the ~11:30Z
read still happens but the 0808 run's open positions died with it;
treat the second-cohort read as PARTIAL. **(2) Fix shipped**: persist
declines now log + hold rows for retry (counters advance only on
success, exactly-once landing regression-tested — same shape as
streamd's flush declines); ops.md rule: ad-hoc queries on ANY live DB
connect read-only; mistakes #20. Promoted 2a69145; shadow restarted
08:19:31Z on fixed code as run 20260810T081931 (the 6h run 021644 was
the cheapest-possible restart cost — no settlements before ~16h).
**(3) Reload line green**: 95,572 at 07:18Z, below the ~101k baseline
even mid-poly-sweep. **(4) Bounded-burst day 7 clean**:
`collect_skips.jsonl` still ends 08-07 07:44Z — zero collector skips;
collector 08:15Z cycle exit 0, errors 0. **(5) Poly sweep on pace**:
started 05:00Z, 4,600/16,391 at 08:14Z, ~488 min left → ETA ~16:25Z,
inside the measured band. **(6) One streamd kalshi-trades reconnect
(07:42Z keepalive timeout, 1 gap, clean resubscribe)** — known benign
class. Host stability + NTP remain USER-GATED. NEXT PASS: (1) 10:00Z
QA — expect the single batch-budget FAIL until 08-14; (2) ~11:30Z
08-09 weather-block settlements — verify which run_id they credit to
and record the partial-cohort caveat; (3) poly completion ~16:25Z,
then doctor; (4) watch for the first '[shadow] ledger persist
declined' journal line — the fix's first live decline should flush
next poll with zero loss.**
(prior **2026-08-10 02:25 UTC (OVERNIGHT RUNG-1 PASS — RELOAD LINE
COMPLETES ITS POST-SWEEP DRAIN TO BASELINE; EVERYTHING ELSE GREEN AND
TIME-GATED.** **(1) Reload line drained exactly as predicted**: hourly
prints 117,042 (19:39Z) → 113,165 (21:39Z) → 106,395 (00:39Z) →
102,092 (01:40Z) — a monotone post-sweep decline back to the ~101k
baseline, far under the 150k tripwire; the b962b5c filtered-load shape
is now confirmed across a full sweep cycle (climb during backfill,
drain after). **(2) Shadow run 20260808T063109 healthy**: 14,264
fills / 7,726 polls at 02:12Z (~340 fills/hr overnight), RSS 332MB /
cgroup 536MB — flat. Settlement cohort unchanged at 112 (last lands
08-09 15:38Z); equity marked −676 at 02:15Z vs −426 at 20:16Z — open-
position mark drift + fees on ~2k new fills under the documented
pessimistic marking, not a settlement event; the cohort read stands
at −$196. **(3) Bounded-burst day 6 clean**: `collect_skips.jsonl`
still ends 08-07 07:44Z — zero collector skips. **(4) Doctor 02:20Z
clean**: 0 kalshi mirror violations; sweep_log 48h = 6,835 ok / 18
truncated / 3 error (errors 8→3 as the poly-tail window ages out);
stream archive 406.6M book events, 1,176 gaps, 10.9GB. **(5) Two
streamd flush declines** (23:38Z, 00:36Z, both vs the shadow reader
PID 788) held-for-retry and flushed next round — zero loss, the known
benign single-writer class. **(6) No reboot** — uptime 1d19h44 off
the 06:30Z 08-08 boot; NTP still inactive. Host stability + NTP
remain USER-GATED. NEXT PASS: (1) 05:00Z poly sweep — first
sweep-hour reload print, peak >150k is actionable; (2) 10:00Z QA —
expect the same single batch-budget FAIL until the 08-04 overrun ages
out 08-14; (3) 08-09 weather-block settlements ~11:30Z — second
cohort read of the wave series.**
(prior **2026-08-09 20:30 UTC (RUNG-1 PASS — ALL THREE TIME-GATED
ITEMS LANDED GREEN: FIRST SETTLEMENT COHORT IN (LONGSHOT SIDE LOSES
AGAIN), POLY FINISHED IN-BAND, RELOAD LINE DRAINING POST-SWEEP).**
**(1) First settlement cohort of run 20260808T063109 is in**: 112
settlements by 20:16Z (first trickle 08-08 11:32Z, main 08-08 weather
block 08-09 11:37Z) — 15 yes / 97 no, $1,177.06 payouts vs $1,373.51
notional across the 2,439 fills on those markets → cohort P&L ≈
−$196 (−14%), equity −$426 at 20:16Z. Same shape as the closed wave-3
probe finding (the longshot side loses decisively); this run exists to
accumulate cohorts, no action — next read when the 08-09 weather block
settles ~tomorrow 11:30Z. **(2) Poly sweep finished 19:33Z**: 869.2min
(14h29m) wall, inside the measured 13h41m–17h11m band; 16,214 markets,
1,065,871 prices / 2,014,657 trades, 8 errors (crypto 429 tails + one
ReadTimeout, all logged truncation-aware). **(3) Doctor post-poly
20:17Z clean**: 0 kalshi mirror violations; sweep_log 48h = 7,545 ok /
20 truncated / 3 error; stream archive 404.5M book events, 1,175 gaps,
10.8GB. **(4) Reload line draining as predicted**: 131,573 (13:38Z
sweep-hour peak) → 119,893 (18:38Z) → 117,042 (19:39Z) — declining
once the sweep wound down, under the 150k tripwire; the b962b5c
filtered-load shape holds. **(5) Bounded-burst day 5 clean**:
`collect_skips.jsonl` still ends 08-07 07:44Z — zero collector skips;
two mid-flush lock declines in streamd (18:21Z, 19:53Z vs the poly
writer) both held-for-retry and flushed next round, zero loss.
**(6) Shadow daemon flat**: RSS 332MB / cgroup 559MB at 20:15Z; 12,229
fills / 6,676 polls (~260 fills/hr weekend pace). Host stability + NTP
remain USER-GATED. NEXT PASS: (1) 08-09 weather-block settlements
~11:30Z tomorrow — second cohort read; (2) 10:00Z QA — expect the
same single batch-budget FAIL until the 08-04 sweep overrun ages out
08-14; (3) reload line first sweep-hour print tomorrow — peak >150k
is actionable.**
(prior **2026-08-09 14:30 UTC (RUNG-1 PASS — 10:00Z QA LANDED AS
THE PREDICTED SINGLE TRUTHFUL FAIL; EVERYTHING ELSE GREEN; REMAINING
ITEMS TIME-GATED (~16:45Z FIRST SETTLEMENTS, ~19:56Z POLY FINISH).**
**(1) QA 10:00Z**: exactly one FAIL — batch-budget (sweep 11.49h/10.5h
in-window until 08-14; tradepass overruns age out 08-10/08-11), as
predicted; zero new skip/lock findings (the lock-skip check now reads
SKIP/UNVERIFIED — no cycle has needed to wait out the lock since the
fix, the good problem); tape-coverage WATCH is a draining tail (5
unswept, 0.0h waited); fade-window PASS 0 lost cycles over 7 windows.
**(2) Shadow run 20260808T063109 healthy**: 9,505 fills / 5,611 polls
at 14:13Z (~250 fills/hr weekend pace, down from ~312 overnight —
volume, not health), RSS 357MB / cgroup 563MB flat; first settlements
~16:45Z today open the first cohort read. **(3) Reload line**: 93,743
(05:36Z) → 131,573 (13:38Z), a monotone climb spanning the poly sweep
(05:00Z start) — sweep-hour backfill churn under the 150k tripwire;
per the shape-vs-level rule the read that matters is the first
non-sweep-hour print after ~20:00Z. **(4) Bounded-burst day 4 clean**:
`collect_skips.jsonl` still ends 08-07 07:44Z — zero collector skips.
**(5) Poly sweep ETA slipped ~17:00Z → ~19:56Z** (10,000/16,214 at
14:14Z): the ~15h wall is inside the measured 13h41m–17h11m band and
the unit is DELIBERATELY budget-exempt (Polymarket quota, wall-clock
lever — `collector/qa.py` constants block); nothing actionable.
**(6) Doctor at 14:16Z**: declined the lock (poly writer active) —
expected single-writer contention; QA already read 0 kalshi mirror
violations at 10:00Z. Host stability + NTP remain USER-GATED. NEXT
PASS: (1) shadow first settlements ~16:45Z — first cohort read of the
new wave series; (2) poly completion ~19:56Z, then doctor; (3) first
non-sweep-hour reload print after ~20:00Z — >150k is actionable.**
(prior **2026-08-09 08:15 UTC (RUNG-1 PASS — ALL CHECKABLE ITEMS
GREEN; REMAINING NEXT-PASS ITEMS ARE TIME-GATED (10:00Z QA, ~16:45Z
FIRST SETTLEMENTS).** **(1) Reload line at/below baseline**: hourly
prints 93,743–100,574 over the last six hours (02:36–07:37Z) —
under the ~101k baseline, far under the 150k tripwire; nothing
actionable. **(2) Shadow run 20260808T063109 healthy**: 8,022 fills
/ 4,546 polls at 08:13Z (~312 fills/hr sustained since the 06:31Z
08-08 start), RSS 305MB / cgroup 523MB — flat; first settlements
still ~16:45Z today, opening the first cohort read of the new wave
series. **(3) Bounded-burst day 3 clean**: `collect_skips.jsonl`
still ends 08-07 07:44Z — zero collector skips through a third day;
the >300s-lock-hold class stays closed. **(4) Doctor**: 0 kalshi
mirror violations, sweep_log 48h {ok 7749, error 5, truncated 22} —
errors flat vs 20:20Z, +2 truncated is normal crypto-tail churn.
**(5) Poly sweep in progress**: started 05:00Z, 4,200/16,214 markets
at 08:10Z, ETA ~17:00Z — normal multi-hour profile, no errors beyond
one 408 trades-tail early-stop. **(6) No reboot** — uptime 1d1h44m
off the 06:30Z 08-08 boot; NTP still inactive. Host stability + NTP
remain USER-GATED. NEXT PASS: (1) 10:00Z QA — expect the truthful
batch-budget FAIL (until 08-14) and zero new skip/lock findings;
(2) shadow first settlements ~16:45Z — first cohort read; (3) reload
line — only a non-sweep-hour print >150k is actionable; (4) poly
sweep completion ~17:00Z.**
(prior **2026-08-09 02:25 UTC (OVERNIGHT RUNG-1 PASS — EVERYTHING
CHECKABLE IS GREEN; THE RELOAD-LINE QUESTION IS ANSWERED AT
BASELINE.** **(1) Reload line, non-sweep-hour reads**: 101,466 at
00:35Z → 99,680 at 01:35Z — back at (below) the ~101k baseline,
far under the 150k tripwire. The 119,315 sweep-hour peak was
backfill churn exactly as modeled; the shape-vs-level rule holds
and nothing is actionable. **(2) Shadow run 20260808T063109
healthy**: 6,265 fills / 3,481 polls at 02:15Z (~314 fills/hr
sustained since 06:31Z start), RSS 354MB / cgroup 562MB — flat;
first settlements still expected ~16:45Z 08-09 to open the first
cohort read. **(3) Bounded-burst day 2 clean**:
`data/collect_skips.jsonl` still ends 08-07 07:44Z — zero collector
skips through a second full sweep day; the >300s-lock-hold class
stays closed. **(4) Doctor**: 0 kalshi mirror violations, sweep_log
48h {ok 6831, error 5, truncated 20} — unchanged from the 20:20Z
reading. **(5) No reboot overnight** — uptime 19h44 off the 06:30Z
boot; NTP still "synchronized: no / NTP service: inactive". Host
stability + NTP remain USER-GATED. NEXT PASS: (1) 10:00Z QA —
expect the truthful batch-budget FAIL (until 08-14) and zero new
skip/lock findings; (2) shadow first settlements ~16:45Z — first
cohort read of the new wave series; (3) reload line — only a
non-sweep-hour print >150k is actionable.**
(prior **2026-08-08 20:20 UTC (RUNG-1 VERIFICATION PASS — ALL FOUR
CHECKABLE NEXT-PASS ITEMS ARE GREEN; NOTHING NEW BROKE.** **(1) The
re-fired 06:10Z sweep finished clean at 16:22Z**: 3,169/3,169 series,
52,529 markets / 273,747 candles, 2 errors, 10 truncated, `aborted:
False`, 484.0 min — 8.1h wall, inside the 10.5h budget, so it adds
NOTHING to the QA batch-budget window (the 08-07 recovery overrun
remains the only member, aging out 08-14). Doctor post-sweep: 0
kalshi mirror violations, sweep_log 48h {ok 6831, error 5, truncated
20}. Service-level memory peak 7.6G (batch unit, no cap, OOMScore-
Adjust=500 — observed, not alarming). **(2) Bounded-burst day-1
stays clean end-to-end**: `data/collect_skips.jsonl` still ends at
08-07 07:44Z (old code) — a full sweep with mid-series flushes
produced ZERO collector skips start to finish. The >300s-lock-hold
class stays closed. **(3) Shadow reload line behaved exactly as
modeled**: peaked 119,315 at 16:34Z while the sweep was stamping
results (backfill churn), back to 110,725 by 19:34Z — comfortably
under the 150k tripwire; treat sweep-hour spikes as expected shape,
flag only a level shift. RSS actually FELL 488MB (14:35Z) → 292MB
(20:15Z), cgroup current 486MB — the morning reading was transient
churn, not growth; no window tightening warranted. **(4) New shadow
run 20260808T063109 healthy**: 4,454 fills / 2,431 polls at 20:15Z
(~325 fills/hr), accumulating toward first settlements ~08-09
16:45Z. Nothing else checkable until then — QA fires 10:00Z, poly
sweep 05:00Z. NEXT PASS: (1) 10:00Z QA — expect batch-budget FAIL
(truthful, until 08-14) and zero new skip/lock findings; (2) shadow
first settlements ~16:45Z 08-09 open the new wave series — first
cohort read; (3) reload line — only a non-sweep-hour print >150k is
actionable; (4) host stability + NTP remain user-gated (two hard
resets in 11h; clock undisciplined).**
(prior **2026-08-08 14:35 UTC (THE HOST HARD-RESET AGAIN AT 06:30Z —
SECOND IN 11 HOURS; SHADOW RUN 20260807T193303 DIED AT 3,518 FILLS
WITH ZERO SETTLED COHORTS, AND THE BOUNDED-BURST SWEEP CODE PASSED
ITS FIRST PRODUCTION RUN: ZERO COLLECTOR SKIPS.** **(1) REBOOT #2,
06:30Z (01:30 CDT)**: journal cut mid-line at 01:30:06 CDT, no
shutdown record in wtmp (`last -x` shows both recent boots with no
preceding shutdown), pstore empty, no MCE in the prior boot — a hard
power/hardware reset, cause not recoverable from software. Pattern:
19:31Z 08-07 and 06:30Z 08-08. HOST STABILITY IS NOW USER-GATED
(hardware/power inspection; also NTP still dead post-reboot —
"synchronized: no, NTP service: inactive" — so every reset re-skews
an undisciplined clock). Consequence: shadow run 20260807T193303
closed by design at 3,518 fills / 11h — it died ~10h BEFORE its
first settlements (~16:45Z), so the run banked zero wave data; no
retro-rescue. New run 20260808T063109 (started 06:31Z) restarts the
cohort clock — first settlements now expected ~2026-08-09 afternoon.
The 06:10Z sweep was killed ~20 min in; the timer re-fired at 08:17Z
and the run is healthy (1900/3169 at 14:15Z, 1 error, 4 truncated,
ETA ~17:30Z, within its own 10.5h budget). **(2) BOUNDED-BURST
(134228a) PRODUCTION PASS**: ~6h of sweep so far with continuous
mid-series flushes (KXBTC15M/KXBTCD/KXETH15M at ~250k-trade bursts,
largest 4,643 candles + 246k trades) and `data/collect_skips.jsonl`
has ZERO entries today — the last skips are 08-07 07:34–07:44Z under
old code. Collector fresh throughout (QA age 304s, 0 exit-75). The
>300s-lock-hold class is verified gone in production, not just in
tests. **(3) QA 10:00Z**: exactly one FAIL, batch-budget — and the
"~1 more day" prediction was WRONG: the 08-07 recovery sweep (11.49h
vs 10.5h budget) sits in the 7-day window until 08-14; tradepass
overruns age out 08-10/08-11. The FAIL is truthful (the recovery run
was a deliberate overrun) — expect clear 08-14 absent new overruns.
Yesterday's skip-FAIL aged out as predicted; tape-coverage WATCH is
a draining tail (3 unswept, 0.0h waited). **(4) SHADOW MEMORY AFTER
REBOOT #2**: the boot seed scan (3.66M archived events, 670k top
states) drove the cgroup to the 1G cap — memory.events max=344,
oom_kill=0 — page-cache reclaim absorbed it and the service is
healthy; no code change (the spike is DuckDB read cache, kernel-
reclaimable). Tripwire: if a future boot shows oom_kill>0, bound the
seed scan then. RSS 488MB at 7h (vs 305MB yesterday — bigger reload
dict + fill accumulation); hourly reload line prints 103–109k vs
~101k yesterday, still under the 150k tripwire — growth is settle
churn plus the recovery backfill stamping results; the 1,892
forever-riders from yesterday's note are draining into the settled
clause as expected. NEXT PASS: (1) sweep completion ~17:30Z — confirm
clean finish + doctor; (2) new shadow run's first settlements
~08-09 16:45Z start the wave series; (3) reload-line trend — if the
churn keeps it >150k, tighten 3d→1d at the next NATURAL restart
(reboots are providing them, sadly); (4) QA batch-budget FAIL
expected to persist until 08-14 — flag only if a NEW unit overruns.**
(prior **2026-08-08 02:30 UTC (METADATA-RELOAD SPOT-CHECK CLOSED:
THE HOURLY LINE PRINTS ~101k, NOT THE PREDICTED ~57k — THE FILTER IS
CORRECT, THE ESTIMATE WAS WRONG.** The b962b5c filter works exactly
as written; the miss was in the size model. Measured against the
archive: 13,481 unsettled kalshi markets + 91,082 settled-but-closed-
within-3-days = ~104.5k eligible (log printed 101,722 at 19:34 CDT,
100,980 at 20:34). The settled-recency term DOMINATES because kalshi
now settles 25–55k markets/day (hourly crypto brackets: KXBTCD/
KXETHD/KXSOLD lead the churn) — the dict is bounded by settle-rate ×
window (~90MB at 3 days), not archive age, so it does NOT grow
unboundedly; the ~65k/~57k estimate predated the churn measurement.
Memory verdict: shadow RSS 305MB at 7h uptime (266MB at boot —
consistent with allocator high-water after one reload double-hold,
re-check for plateau next pass), cgroup current 376MB, peak 690MB
(the documented ~685MB boot seed scan, not the reload). 3x headroom
under the 1G cap. Correctness does not depend on the window (held
markets are pinned via `include=`), so no code change: tripwire
encoded in the MARKETS_ALIVE_DAYS comment — if the reload line grows
past ~150k, tighten 3d→1d at the next NATURAL shadow restart only
(a mid-run restart closes the live probe ledger; never do it for a
memory tune). Also noted: 1,892 unsettled kalshi markets closed >3d
ago ride the unsettled clause forever until results land — dominated
by crypto dailies whose results the recovery sweep is still
backfilling; expected to drain, worth an eye next pass. Timers/
daemons all nominal at 02:15Z. NEXT PASS unchanged from 21:10:
(1) 06:10Z sweep — first bounded-burst (134228a) run, verify no
>300s lock hold; (2) QA — 4 skips age out, batch-budget FAIL ~1 more
day, possible truthful extra skip from yesterday's old-code sweep;
(3) shadow run 20260807T193303 first settlements ~16:45Z start a new
wave series; (4) RSS plateau + reload-line check (~101k expected).**
(prior **2026-08-07 21:10 UTC (ALL THREE SWEEP-GATED CHECKS
LANDED, AND THE HOST REBOOTED AT 19:31Z — SHADOW'S PROBE LEDGER IS
CLOSED AT 509 SETTLED / −31.7% NET, THE b962b5c FILTERED-METADATA
FIX IS NOW LIVE (266MB RSS vs 800MB+), AND NTP IS CONFIRMED DEAD
POST-REBOOT.** **(1) Recovery sweep completed 17:39Z** (pre-reboot):
3,169/3,169 series, 75,474 markets / 277,478 candles in 688.9 min, 3
errors, 10 truncated, `aborted: False` — the breaker did NOT trip on
a healthy-but-429-heavy venue; recovery count confirms the outage
backlog cleared (vs 56,941 in the last normal run). Doctor clean
post-reboot (0 mirror violations). Note: this run still executed
pre-134228a code (one late KXMVE* flush could truthfully bump
tomorrow's QA skip count; the mid-series-flush fix takes effect from
tomorrow's 06:10Z firing). **(2) Atlas persistence check
(`reports/atlas/20260807T201613.json`)**: Financials 24h deciles 3–6
ALL persist at the day-robust tier (tiers 110/79/29/10, quoted still
0); caveat — the recovery sweep added only ~2–4 settlements and 2
days per decile, so this is the prior corpus plus two days, not yet
an independent reading; realized-over-implied 0.64–0.84 vs 0.35–0.65
implied, top_day_share 0.32–0.43. Keep it a lead, not a candidate;
re-check after genuinely new settlement mass. **(3) Wave 4 settled
12:00–16:02Z (pre-reboot, ledger intact)**: daily 08-05 cohort 141
mkts, payout $3,506.25 / spend $3,883.85 / fees $215.97 = **−9.7%
gross / −15.3% net** (18/25 series negative, worst KXHIGHAUS −$173);
gross by wave +4.4 → −10.3 → −21.9 → −9.7, fees pinned at ~5.5%.
Separately the first macro tranche settled same window: KXPAYROLLS
(6) + KXU3 (4) jobs-report brackets paid **$0.00 on $2,561 spend** —
a longshot bracket book on one monthly print losing whole. **(4) THE
19:31Z REBOOT** (cause unknown, uptime 45min at check): all timers
and daemons came back (collect 0-error at 27.8s, stream active,
doctor clean); shadow restarted as run 20260807T193303 with the
b962b5c filtered-metadata code — 266MB RSS under 1G, the 2G runtime
bridge no longer needed. Old run 20260803T142853 CLOSED by design:
509 settled, −26.2% gross / −31.7% net (−$5,381.58 paper), 333 open
markets ($8.1k, incl. KXFED/KXCPI/remaining KXPAYROLLS macro)
stranded unscored — no retro-rescue, the fee (5.5%) and variance
(26pp) numbers are banked in strategy-verdicts queue #1 and the
probe ledger is closed. **(5) NTP**: `timedatectl` post-reboot reads
"synchronized: no, NTP service: inactive" — USER-GATED (needs root:
`timedatectl set-ntp true`); flagged since the backlog review, now
demonstrably live after a reboot. Archive timestamps currently ride
an undisciplined clock. NEXT PASS: (1) tomorrow 06:10Z sweep — first
run with the 134228a bounded-burst code, verify no >300s lock hold;
(2) 10:00Z QA — today's 4 skips age out, batch-budget FAIL ~1 more
day, possible truthful extra-skip from today's old-code sweep; (3)
new shadow run 20260807T193303 accumulates its own daily cohorts
(~25 fills/hr observed) — first settlements ~16:45Z tomorrow start a
NEW wave series; (4) hourly metadata reload line should print ~57k
(filtered) — spot-check on next pass.**
(prior **2026-08-07 14:55 UTC (THE 10:00Z QA'S NEW FAIL — 4 COLLECT
CYCLES SKIPPED FOR THE LOCK — WAS THE RECOVERY SWEEP HOLDING THE
WRITER LOCK FOR ~21 MINUTES ON ONE FLUSH: KXBTC'S POST-OUTAGE BACKLOG
BUFFERED ~3.85M TRADE ROWS AND THE SINGLE PER-SERIES BURST WROTE THEM
ALL UNDER ONE HOLD; FIXED 134228a (MID-SERIES FLUSH EVERY 250K ROWS),
PROMOTED, PUSHED.** QA read: the known aged batch-budget FAIL plus
`collector cycles are not skipped for the lock — 4 skipped in 24h
(max 3)`. Chased to ground with the lock's holder note: all four
skips (07:29–07:44Z) name hyxlab-sweep pid 3360306 holding since
07:23:56Z — a single `writer_burst` right after KXBTC truncated at
4,216 markets; collect's DB counters bracket the flush at +3.85M
trade rows (158.89M→162.75M), ~21 min at the measured ~3k rows/s.
The docstring's "the DB is touched once per series for ~ms" is
falsified at recovery scale. **Fix**: `sweep_series` flushes candles/
trades/swept-marks mid-series every `FLUSH_ROWS=250k` buffered rows —
each burst stays under collect's 300s open budget; watermark still
advances ONLY in the final burst and every intermediate write is
idempotent, so crash semantics are unchanged (2 regression tests,
fail-without-fix verified, suite 609). Promoted: today's RUNNING
sweep keeps old code (if another giant series flushes late today,
tomorrow's QA may truthfully count more skips — the fix takes effect
from tomorrow's 06:10Z firing); promote restarted hyxlab-stream per
its own rule (gap row, benign), shadow untouched per the 02:55Z
deferral. The QA budget (3/24h) was NOT loosened — the check was
right. Gotcha encoded in data-pipeline.md. **Sweep at 14:04Z**:
2100/3169 series, 50,759 markets (recovery signal confirmed), 2
errors, 8 truncated, no breaker trip — ETA ~18:00Z. NEXT PASS: (1)
sweep completion ~18:00Z — recovery-count + breaker verification;
(2) Financials 24h deciles 3–6 atlas persistence check after it; (3)
wave 4 settlements ~16:45Z; (4) tomorrow 10:00Z QA — today's 4 skips
age out of the 26h window; batch-budget FAIL ~1 more day.**
(prior **2026-08-07 08:20 UTC (RUNG-1 PASS — RECOVERY SWEEP STILL
RUNNING AT 2h05m, WORKING NOT HUNG; ALL OTHER SYSTEMS NOMINAL,
REMAINING ITEMS GATED ON ITS COMPLETION.** The 06:10Z sweep is in its
predicted larger-than-usual recovery shape: heavy Kalshi 429
rate-limiting through the crypto giants (KXBTC truncated at 4,216
markets on per-series budget — resumes next run, by design), process
at 24% CPU with live sockets, no breaker trip so far — the 0caa3a6
outage-vs-work-list fix is holding. Doctor clean (0 mirror
violations; markets 494,524), collect cycles 0-error at ~37s, shadow
816MB/1.0G peak under the 2G runtime bridge (restart still deferred
past the macro block per 02:55Z policy). QA fires 10:00Z. GATED until
sweep completes: (1) recovery-count + breaker verification, (2)
Financials 24h deciles 3–6 atlas persistence check; (3) wave 4
~16:45Z. Next wakeup armed for the sweep-completion check.**
(prior **2026-08-07 02:55 UTC (THE SHADOW DAEMON WAS ~35MB FROM AN
OOM KILL WITH WAVE 4+ EXPOSURE ON BOARD — THE HOURLY METADATA RELOAD
MATERIALIZES THE FULL 486K-ROW MARKETS TABLE (~430MB, +13K ROWS/DAY
SINCE THE 08-02 BREADTH WIDENING) AND DOUBLE-HOLDS IT ON SWAP; FIXED
b962b5c, RUNNING PROCESS BRIDGED TO 2G, RESTART DEFERRED.** Rung-1
pass at 02:15Z: doctor clean, timers on cadence, but shadow read
VmRSS 807MB / HWM 966MB against MemoryMax=1G after 3.3 days — where
the 07-20 verification plateaued at 270MB. Chased to ground: NOT a
third accumulator. `Store.markets()` is unfiltered; the archive grew
473k→486k in ~30h post-widening, so the metadata dict alone is ~430MB
resident (measured), and the hourly refresh transiently holds old+new
(~865MB measured) — every reload rolled dice with ~35MB headroom, on
a process whose open cohorts die with it. **Fix (b962b5c, promoted)**:
markets() gains venue/alive_days/include filters; shadow loads kalshi
unsettled-or-closed-within-3d (~57k rows, ~60MB — 4x RSS cut, reload
transient 365→60MB) and PINS held markets so a result landing after
the recency window (the weeks-out macro block) still credits its
payout — the existing settlement tests exercise the pin path, two new
regression tests cover filter + reload/pin (suite 607). **Bridge**:
the RUNNING daemon keeps old code + its open positions; MemoryMax
raised to 2G via set-property --runtime (evaporates on restart, by
which time the new code makes 1G comfortable). promote.sh ran with
--defer=hyxlab-shadow.service; hyxlab-stream restarted (store.py
moved) at 02:22 CDT, gap row per design. **RESTART POLICY**: next
shadow restart (promote or crash) picks up the fix automatically; no
urgency now the cliff is gone — prefer restarting AFTER the macro
block settles so the current run's ledger stays whole. NEXT PASS:
unchanged from 20:30Z — (1) 06:10Z sweep recovery (larger-than-usual
count expected; breaker must NOT trip on a healthy venue); (2) 07:00Z
QA (batch-budget FAIL ages out ~1 more day; stream restart gap is
benign); (3) Financials 24h deciles 3–6 atlas persistence check after
the recovery sweep; (4) wave 4 ~16:45Z.**
(prior **2026-08-06 20:30 UTC (WAVE 3 IS THE WORST READING YET —
143 MARKETS AT −21.9% GROSS / −27.5% NET — AND IT SETTLES THE
QUESTION THE FIRST TWO WAVES LEFT OPEN: THE PROBE'S LONGSHOT SIDE
LOSES GROSS, DECISIVELY, NOT MARGINALLY.** Rung-1 pass at 20:15Z on
the one live item (wave 3 settlements, landed 11:00–16:00Z).
**(1) WAVE 3 (143 markets, mostly 08-04 fills): payout $3,865.63 on
$4,946.74 spend + $277.22 fees = −$1,358.33 net.** Gross by wave now
reads +4.4% → −10.3% → −21.9% — a 26pp spread, far beyond the ±7pp
two-wave estimate — while the fee ratio is rock-stable (5.5% → 5.6%
of spend). 20/26 series negative; worst: KXHIGHNY −$219,
KXJOBLESSCLAIMS −$189 (3 mkts), KXHIGHAUS −$167. **Three-wave
aggregate: 358 settled markets, −14.3% gross / −19.9% net, −$2,099.78
paper.** The durable Tier-2 inputs are unchanged in kind, sharpened
in number: fees 5.6% of spend (the stable datum), gross variance 26pp
(the volatile one) — both updated in strategy-verdicts queue #1.
**(2) Open exposure**: 08-05 cohort 132 mkts/$3.7k settles ~16:45Z
tomorrow (wave 4), 08-06 cohort 154 mkts/$1.3k, plus the 32-market
$3.4k macro block from 08-03 (weeks out). **(3) Shadow healthy**:
fills persisted through 20:16Z, equity −$3,123 (open cohorts marked
pessimistically per the documented bias). NEXT PASS: (1) tomorrow
06:10Z sweep recovery — expect a larger-than-usual market count as
the outage backlog clears, and watch that the breaker does NOT trip
on a healthy venue; (2) 07:00Z QA — batch-budget FAIL ages out ~1
more day; (3) Financials 24h deciles 3–6 atlas persistence check
unlocks after the recovery sweep; (4) wave 4 ~16:45Z — with a 26pp
gross spread, keep reading waves for the fee/variance ledger, but no
"stability" claim is pending anymore: the gross question is closed.**
(prior **2026-08-06 14:35 UTC (THE 10:00Z QA READ EXACTLY AS
PREDICTED — THE SINGLE KNOWN BATCH-BUDGET FAIL AND NOTHING ELSE: THE
NEAR-EMPTY SWEEP TRIPPED NO COVERAGE OR SHRINK FLAG, AND THE ONE ODD
DATUM IN THE OUTPUT (`econ vintages fresh — age -0.7d`) IS PESSIMISM
BY CONSTRUCTION, NOT A TIMESTAMP BUG.** Rung-1 pass at 14:15Z. **(1)
QA verdict**: single FAIL on batch-run-budget from the aged
08-03/08-04 tradepass rows (15.10h/8.83h vs 4h — historical, no
action), all 16 other checks PASS/SKIP/WATCH as documented. The
predicted "possible sweep-shrink/coverage flags from the near-empty
sweep" did NOT fire — `sweep ran in last 36h` counts ok entries
across the window (yesterday's 542-min run covers it) and tape
coverage is tradepass's domain, untouched by the outage. **(2)
Collector clean through the outage aftermath**: 14:15Z cycle 0
errors, fetch 23.9s (the measured floor), 661 kalshi snaps; breadth
timer firing on cadence. Breaker commit 0caa3a6 confirmed promoted
(stable worktree AND origin at 0caa3a6) — tomorrow's 06:10Z recovery
run executes the breaker code. **(3) The negative vintage age,
chased to ground**: `alfred.pessimistic_knowable_at` stamps 23:59
US/Eastern on the FETCH date; the signals timer fires 04:40Z —
already the next ET date — so the stamp lands ~04:00Z the FOLLOWING
day, 18h after a 10:00Z QA run. −0.7d is the steady state, pessimism
only delays knowability (no lookahead possible), and the signature +
the real alarm threshold (more negative than ~−1.0d) are encoded in
data-pipeline.md Gotchas. NEXT PASS: (1) wave 3 settlements ~16:45Z
— third probe gross reading (08-05 cohort, 140 mkts/$4.6k); (2)
tomorrow 06:10Z sweep recovery — expect larger-than-usual market
count as the backlog clears, and watch that the breaker does NOT
trip on a healthy venue; (3) Financials 24h deciles 3–6 atlas
persistence check remains data-gated on that recovery sweep; (4)
07:00Z QA tomorrow — batch-budget FAIL ages out when the 08-03/08-04
rows leave the 7d window.**
(prior **2026-08-06 08:45 UTC (THE 06:10Z KALSHI SWEEP FAILED
VENUE-SIDE — 2,569 OF 3,120 SERIES ERRORED IN AN HOUR-LONG /markets
DEGRADATION (503s + A 429 STORM THAT OUTLASTED THE 4-TRY BACKOFF) —
AND THE RUN STILL REPORTED SUCCESS TO SYSTEMD; A CONSECUTIVE-FAILURE
CIRCUIT BREAKER IS NOW SHIPPED.** Rung-1 gate check found today's
sweep done in 64.3 min (vs 542.5 yesterday) with 3,523 markets / 5,726
candles (vs 56,941 / 186,106): every series from ~550 onward errored,
counters frozen, errors incrementing 1:1 with series. **Root cause is
the venue, not our load**: 429s began 06:19Z (before tradepass at
06:35Z, which ran 7.5 min mid-window and SUCCEEDED on
`/markets/trades` with 2 backoffs), and breadth crashed on a straight
**503** from `/markets` in the same window — endpoint-scoped
degradation. Breadth at 08:17Z is clean (1.7s fetch), the API has
recovered. **No data lost**: a failed series never advances its
watermark (`run_sweep` logs `error`, `set_watermark` unreached), so
tomorrow's 06:10Z run resumes from today's floors — expect a
larger-than-usual market count as the recovery signal, and trade-tape
holes are tradepass's job by design. **The defect worth fixing**: the
sweep fail-fasted through 2,569 consecutive failures — ~10k useless
requests against a venue refusing service, on the rate budget capture
daemons share — then printed "Finished". SHIPPED: `ABORT_CONSEC_ERRORS
= 25` breaker in `run_sweep` (one success resets; alternating
error/success proven not to trip) + exit 75 on abort so systemd
records the failure and the next firing resumes from watermarks
(`tests/test_hyxlab_sweep_breaker.py`, suite 606; gotcha encoded in
data-pipeline.md). NEXT PASS: (1) 10:00Z QA — expect the known aged
batch-budget FAIL, PLUS possible sweep-shrink/coverage flags from
today's near-empty sweep: truthful, no action, clears after
tomorrow's recovery run; (2) wave 3 settlements ~16:45Z — third probe
gross reading; (3) Financials 24h deciles 3–6 atlas persistence check
is NOW DATA-GATED on tomorrow's recovery sweep (today added ~0 new
settlements — an atlas re-run would re-measure the 08-05 corpus); (4)
verify tomorrow's 06:10Z sweep recovers and clears the backlog.**
(prior **2026-08-06 02:30 UTC (THE FIRST INDEPENDENT ECON BRACKET
READING LANDED — 80% NEW ORDERS WHERE ALL FIVE PRIOR ECON RE-RUNS
CARRIED 11–26% — AND IT CONFIRMS THE CROSSING RULE'S REGIME FLIP IN
ECON: CROSSING 189 FILLS SITS BELOW THE QUEUE-PESS FLOOR OF 260.**
Rung-1 pass on the one live item from the 20:45Z list: econ maker
bracket re-run (`reports/maker_bracket/20260806T021621.json`, 336h,
KXCPI/KXCPIYOY/KXFED/KXU3, 2,627 orders / 8 markets / 4 underlyings,
all 26JUL events). **(1) Independence**: new_share_vs_all **0.7994**
— calendar spacing to the 07-26 prior econ run was only ~223h (the
20:45Z "legal after 08-04" line counted from 07-21, the wrong prior),
but late-JUL print churn made the reading independent by the metric
the wiki says is binding; this is the first econ reading that is not
mostly the prior one re-measured. **(2) No direction**: underlyings
2 over / 2 under, sign_p 0.6875, min_sign_p 0.0625 — underpowered by
construction per the 07-31 caveat, no drift, no claim. **(3) The
durable datum**: crossing 189 vs pess 260 / opt 299, net −71 (strict
−74) — crossing FORGOES 162 real fills vs 88 invented in these
late-life econ books, while the 07-21 econ run had crossing INSIDE
[368, 404, 436]. The bias flip by regime, previously a weather-only
observation, is now shown in econ; "score endpoints via queue-PESS"
is evidenced in both categories (encoded in strategy-verdicts queue
#1). NEXT PASS: (1) wave 3 ~16:45Z 08-06 — third probe gross reading
(±7pp/day variance, several more waves before stability claims); (2)
07:00Z QA — batch-budget FAIL on aged 08-03/08-04 rows expected ~1
more day; (3) Financials 24h deciles 3–6 persistence check on next
atlas; (4) next INDEPENDENT econ bracket: check new_share_vs_all, not
the calendar — the trailing window plus churn decides, not spacing
arithmetic.**
(prior **2026-08-05 20:45 UTC (SETTLEMENT WAVE 2 REFUTED THE WAVE-1
READING — THE PROBE'S GROSS EDGE WAS NOT REAL: 137 MARKETS AT −10.3%
GROSS BEFORE FEES, TWO-WAVE AGGREGATE −7.7% GROSS / −13.2% NET — AND
ALL THREE STANDING REPORTS RAN ON THE REOPENED GATE, ALL HEALTHY.**
Gate check (`date -u` 20:15): sweep finished 15:13Z (542.5 min, 0
errors, 8 truncated), tradepass timer confirmed done from journal
(9,969 markets, 192.8 min), shadow healthy (815MB, 19.2k fills).
**(1) WAVE 2 (17:52Z, 137 markets): payout $4,137.81 on $4,613.39
spend + $254.49 fees = −$730.07 net.** Wave 1's "+4.4% gross, fees
flip the sign" did not survive: 17/24 series negative, and the
aggregate over 215 settled markets is payout $5,186.25 / spend
$5,617.41 / fees $310.29 (5.5% of spend) = **−$741.45 net**. Read
against `strategies/probe.py`: the probe buys the sub-0.50 side of
tight books — the LONGSHOT side — so negative gross is an independent
live-paper confirmation of the fav-long taker FAIL, not a surprise;
the durable outputs are the fee magnitude (5.5% of spend) and the
wave-to-wave gross variance (±7pp/day), both fed into
strategy-verdicts queue #1 for the Tier-2 maker design. Open
exposure: 08-05 cohort 140 mkts/$4.6k (settles ~16:45Z tomorrow —
wave 3 is the next stability reading), 08-06 122 mkts/$1.3k, plus 42
monthly macro (KXPAYROLLS/KXU3/KXCPI/KXFED, $2.8k, settle weeks out —
so shadow is NEVER flat; "cohort break" can only ever mean the daily
break). **(2) STANDING REPORTS**: atlas (20260805T201720, settled
414k vs 271k on 08-03): flagged 108→110, robust 69→77, day-robust
22→26 — gains cluster in Financials 24h deciles 3–6; strictest
day-weighted tier stable at 9; quoted tier still 0/data-gated. Maker
bracket (20260805T201757): 310 orders, independence 1.0 (all-new,
weather churn as documented), crossing 168 / pess 149 / opt 170,
invented-lower-bound 39, forgone 28; min_sign_p 0.0625 — cannot show
direction at default width, per the 07-31 power caveat, no drift.
Divergence vs the CURRENT run (the no-arg default picked the old
07-22 run — most-fills default, worth knowing): 99.5% matched,
residual mostly reseed_twin, 31 unexplained (~0.1%) ALL clustered in
the run's first 4.5h (start boundary), cash within 0.2%. **(3) TWO
WATCH ITEMS CLOSED**: (a) KXSOLD/KXSOLE truncation — identical counts
2 days running looked like the rot signature, but sweep_log shows
watermarks advancing 18–25h/run with a stable ~2-day lag: the count
is the series' deterministic daily production, not a cursor;
signature encoded in data-pipeline.md (judge stall by watermark,
never by count). (b) Shadow restart — CLOSED AS UNNECESSARY: restart
= fresh state (shadow.py:22), would strand $5.9k open cohort, and no
code shadow executes has changed since its 08-03 start; restart only
when sim-side code it runs actually moves. NEXT PASS: (1) wave 3
~16:45Z 08-06 — third gross reading; with ±7pp/day variance the
probe ledger needs several more waves before any stability claim; (2)
07:00Z QA — batch-budget FAIL on aged 08-03/08-04 rows expected ~1
more day, then clean; (3) Financials 24h deciles 3–6 day-robust
cluster is the freshest atlas lead — check persistence on the next
atlas before treating it as a candidate; (4) econ maker bracket
re-run legal after 08-04 (336h spacing).**
(prior **2026-08-05 14:20 UTC (THE 16,868-MARKET TRADEPASS BACKLOG
IS DEAD — THE TIMER RUN SWEPT ALL 9,969 REMAINING MARKETS IN 192.8
MIN, 17 MIN INSIDE ITS 210-MIN DEADLINE, AND THE 10:00Z QA CONFIRMED
THE TAIL AT 6 FRESH MARKETS: DRAINING, NOT ROT.** Gate check (`date
-u` 14:15), all from persisted state per mistakes #19: **(1)
TRADEPASS BACKLOG CLEARED** — journal shows `done: {markets: 9969,
trades: 6194476, empty: 1742, errors: 35, elapsed_min: 192.8}` at
09:47:49Z (14.5h CPU / 3.2h wall, 5G peak; errors were 429 backoffs,
none fatal). Combined with the 02:17Z drain's 6,907, the whole
16,868 backlog cleared in one day vs the ~2d ETA. Deadline code now
2-for-2 live (drain stopped AT deadline, timer finished INSIDE it).
**(2) 10:00Z QA verdict exactly as predicted**: single FAIL on
batch-run-budget from the aged 08-03/08-04 tradepass rows (15.10h and
8.83h vs 4h — historical, age out ~2d, truthful, no action); tape
coverage WATCH at **6 unswept, oldest 0.0h** — the independent
`remaining≈0` confirmation. **(3) Sweep gate SLIPPED ~1.2h**: at
14:15Z it was 2500/3110 series (~118 min left, 8 truncated incl.
KXSOLD/KXSOLE at per-series budget) → ends ~16:15Z, not ~15:00Z;
standing reports stay lock-gated until then, and wave-2 settlements
land ~16:45Z right behind it — so rungs (2) and (3) likely merge into
one post-16:15Z pass. NEXT PASS: (1) after ~16:15Z: atlas +
maker-bracket + divergence standing reports on two days of new
settlements; (2) settlement wave 2 ~16:45Z → is -1.1% net stable?
feed fee-sign into Tier-2 maker fav-long design; (3) shadow restart
at the natural cohort break AFTER wave 2 settles the open 08-04
positions; (4) watch whether KXSOLD/KXSOLE per-series truncation
persists across runs (resumes are logged non-ok by design — rot only
if the same series truncates without progress).**
(prior **2026-08-05 08:45 UTC (THE DEADLINE CODE'S FIRST LIVE RUN
PASSED TO THE MINUTE, THE BACKLOG DIES TODAY, AND THE 18-HOUR
timings= TAPE SETTLED THE FETCH QUESTION: A 29s PAGINATION FLOOR PLUS
A MILD CONTENTION TAX — THE REAL TAIL IS THE FLOCK, WHERE THE SWEEP'S
DAILY ~6-MIN HOLD AT ~07:30Z COSTS EXACTLY ONE COLLECT CYCLE, BILLED
AND INSIDE BUDGET.** Gate check (`date -u` 08:15): **(1) DRAIN
VERIFIED FROM THE JOURNAL** (mistakes #19 honored — persisted state,
not "was started"): `hyxlab-tradepass-drain` ran 02:17–04:17Z exactly as
scheduled, stopped by its own deadline at **120.2 min** with 6,907/
16,868 markets (4.99M trades, 7 errors), 9,961 left pending — first
live validation of EXP-964, PASS. **(2) The 06:35Z timer run is live
and on pace**: 5,000/9,969 at 08:12Z, ~1.6h left — finishes the WHOLE
backlog inside its 210-min deadline (~09:50Z), a day ahead of the ~2d
ETA. **(3) RUNG-2 VERIFICATION SHIPPED (journald-only; archive
lock-gated by the sweep until ~15:00Z)**: yesterday's "fetch is
watchlist pagination, not contention" claim, asserted from ~4 cycles,
now measured over 213 cycles cut by concurrency window: fetch median
**29.2s with zero concurrent consumers** (three independent windows
agree: 24–30s), 30–39s with one, **44.2s median / ~65s p90 with
sweep+tradepass both running** — floor confirmed, tax real but
bounded. The sharper finding: **flock wait dominates the tail** — 9/20
two-writer cycles waited >1s (max 188s; worst cycle total 209.8s of
the 300s period), and both 08-04 and 08-05 the collector SKIPPED its
07:34Z cycle after ~210s waiting on a single ~6-min sweep hold
(holder-attributed in collect_skips.jsonl). Deterministic signature:
ONE sweep-window skip/day is normal, more is drift; already inside
qa_collect_skips' 3/24h budget, so no new check — encoded in
data-pipeline.md instead. Also closed: 08-04 afternoon `errors: 1`
per cycle was NWS MIA gridpoint 500s (external, self-cleared 0 by
morning). NEXT PASS: (1) tradepass timer wall + `remaining: 0`
confirmation after ~09:50Z; 10:00Z QA verdict (batch-budget FAIL on
aged 08-03/08-04 journal rows still expected, truthful); (2) sweep
ends ~15:00Z → atlas + maker-bracket + divergence standing reports on
two days of new settlements; (3) settlement wave 2 ~16:45Z → is -1.1%
net stable? feed fee-sign into Tier-2 maker fav-long design; (4)
shadow restart at the natural cohort break AFTER wave 2 settles the
open 08-04 positions.**
(prior **2026-08-05 02:35 UTC (THE 20:42Z MANUAL DRAIN NEVER RAN — IT
DIED WITH THE SESSION THAT LAUNCHED IT, BEFORE SWEEPING ONE MARKET —
AND THE RELAUNCH IS NOW A TRANSIENT SYSTEMD UNIT THAT CANNOT DIE THAT
WAY.** Gate check (`date -u` 02:15): `trades_swept` last row 15:24Z
(the 08-04 timer run's crash), pending unchanged at **16,868** — the
"launched 20:42Z" drain left zero rows and zero output. Root cause: it
was a harness background task, killed silently when the session ended;
no journald trace because it was never a unit. Logged as **mistakes
#19** (ops-blindness, #5's family, new mode) and escalated to an
ops.md rule: jobs meant to outlive the turn go through `systemd-run
--user`; verify liveness from persisted state, never from "was
started". **RELAUNCHED 02:17Z** as transient unit
`hyxlab-tradepass-drain` (stable worktree, `--deadline-min 120`, ends
~04:17Z — clear of the 05:00Z poly sweep and the 06:35Z tradepass
timer): journald-captured, session-independent, first line confirmed
live (`16868 settled markets pending`). This is the deadline code's
first real live validation. EXPECT: drain ends ~04:17Z having cut
roughly half the backlog; the 06:35Z timer run (210-min default) takes
the rest or most of it; 07:00Z QA still FAILS batch-run-budget on the
08-03/08-04 journal rows until they age out (~2d) — truthful, no
action. NEXT PASS: (1) read `hyxlab-tradepass-drain` journal + 06:35Z
timer wall (<4h expected, deadline-bounded by construction); (2)
second settlement wave ~16:45Z — is -1.1% net stable? feed fee-sign
into Tier-2 maker fav-long design; (3) atlas + divergence standing
reports; (4) shadow restart still deferred to a natural cohort break.**
(prior **2026-08-04 20:55 UTC (THE FIRST SETTLEMENTS LANDED AND THE
LEDGER'S FIRST VERDICT IS: THE PROBE'S GROSS EDGE IS REAL AND TAKER
FEES EAT ALL OF IT — AND THE SAME GATE-CHECK FOUND THE TRADEPASS DEAD
AT 60%, KILLED BY THE EXACT UNGUARDED OPEN open_retry's DOCSTRING
WARNS ABOUT.** Gate check first (`date -u` 20:15): the 06:10Z sweep
finished 15:06Z at **8.93h** (0 errors, 8 truncated series) — under
the 10.5h budget, so 08-03's 10.11h reads as partly one-time backlog;
every gated rung reopened. **(1) SETTLEMENTS: 78 rows at 16:45Z**
(run 20260803T142853, all 08-03 weather cohort): cost $1,004.01 ->
payout $1,048.44 (**+4.4% gross**, 12/78 markets won) minus **$55.80
fees = -$11.37 net**, 0 maker fills of 1,423 — the probe's edge is
real and taker fees flip its sign, which is the maker-bracket thesis
showing up in live paper. The settlement machinery is verified
end-to-end; the ~30h run paid. **(2) BATCH BUDGET VERDICT**: sweep
PASS; **tradepass FAIL, and worse than the budget question** — 08-03
ran 15.10h vs 4.0h (3.69h INSIDE the 23:00Z fade window, first crypto
tape backlog ~43k markets), and 08-04 **crashed at 26,000/42,978**:
`_flush` opened `Store(db)` bare under the flock, and DuckDB refuses a
RW open while any read-only holder (QA/doctor/simui — none flock) is
attached. Full-pass pace said ~14.5h; the worklist is unbounded by
construction. **LANDED (EXP-964, f63a4e6)**: wall-clock deadline
(default 210min; 0 disables) — the pass was already per-market
resumable via trades_swept and oldest-close-first, so stopping costs
calendar days, not data, and QA's 4.0h constant becomes true by
construction — plus `_flush` and the schema-DDL open now go through
`open_retry`. Suite 597->603 (both load-bearing tests
mutation-verified post-commit, mistakes #18 honored), ruff clean,
promoted (stream restarted; timers pick up code at next firing),
pushed. **Manual deadline-boxed drain launched 20:42Z from stable
(--deadline-min 120, ends ~22:42Z, clear of the fade window)** — cuts
the ~17k backlog and live-validates the deadline today. **EXPECT
tomorrow's 07:00Z QA to FAIL batch-run-budget on the 08-03/08-04
journal rows until they age out (~2d journald)** — truthful, no
action; new runs should read <4h. **(5) SHADOW RESTART DELIBERATELY
DEFERRED even though the settlement gate opened**: shadow only READS
through the kernel, gains nothing from the upsert or tradepass
changes, and a restart mid-cohort risks stranding the open 08-04
positions (11,069 fills, 386 markets, equity -$377 unsettled);
restart at a natural break only. (4) fetch_s post-sweep: steady
20-31s vs 35.5s under sweep contention — mild, most of fetch is the
watchlist's own pagination, not contention. NEXT PASS: (1) drain
result + tomorrow's 06:35Z tradepass wall (<4h expected); (2) second
settlement wave ~16:45Z tomorrow -> is -1.1% net stable? feed the
fee-sign finding into the Tier-2 maker fav-long design; (3) atlas +
divergence standing reports on the reopened gate; (4) backlog fully
drained ETA ~2 days at 3.5h/day. Still open: shadow continuity
workaround; atlas quoted tier data-gated; econ needs >=336h (~08-10).**
(prior **2026-08-04 14:40 UTC (THE HYPOTHESIS THIS PAGE CARRIED FOR
THREE ENTRIES WAS REFUTED BY MEASUREMENT, AND THE MEASUREMENT PAID
TWICE: BATCHING THE 31 PER-SERIES `upsert_markets` CALLS INTO ONE DID
NOT CAUSE THE EXP-957 WALL-CLOCK RISE — ONE executemany CALL COSTS
11.0s AND 31 CHUNKED CALLS COST 12.4s ON THE SAME PRODUCTION-SCALE
TABLE, BECAUSE THE PER-STATEMENT PK MAINTENANCE IS THE COST EITHER WAY
— AND CHASING IT FOUND THE 10x FIX: THE SAME 5,913 ROWS AS ONE
SET-BASED STAGED OR-REPLACE COST 1.0s.** Gate check first, hard
(`date -u` 14:15): the 06:10Z sweep is STILL RUNNING (~8h in, ~114 min
left at 14:02 -> completes ~16:00Z), so shadow-settlements /
`qa_batch_run_budget` / atlas all stay gated; ladder fell to rung 2
(verify an unverified design-note assumption). **LANDED (EXP-963,
42fdb53 + 57be0cf + 7a27b9d)**: (1) `upsert_markets` rewritten
set-based — staging table + one OR REPLACE, with a `seq` column
preserving executemany's last-wins duplicate-key semantics (DuckDB's
OR REPLACE over a SELECT keeps an ARBITRARY source row — probed, not
assumed) and an empty-batch guard closing a latent crash (executemany
raises on an empty parameter list, so a cycle whose every fetch failed
rolled back its whole write); dedupe verified by mutation. (2) The
collect cycle now PRINTS its decomposition every 5 minutes
(`timings={fetch,wait,open,write,close,total}` -> journald), because
the EXP-957 scratch figures stopped reconciling with production within
a day. **FIRST LIVE CYCLE (14:30:39Z, from stable, sweep + backfill
both running): fetch 35.5s, wait 0.0, open 0.0, write 3.6s, close 0.0,
total 39.2s.** Read it against the 08-03 pair: lock hold 20.7s ->
**3.6s (-83%)**, cycle total 59.9s -> **39.2s** — the entire EXP-957
wall-clock rise is gone, and the decomposition locates what remains
exactly where suspected: the FETCH half (35.5s of 39.2s), which varies
with 429 contention from the concurrent sweep/backfill. Watch fetch_s
drop after the sweep ends ~16:00Z — that is the natural experiment.
**THE PROMOTE ENCODED ITS OWN EXCEPTION (7a27b9d)**: hyxlab/ moved, so
the EXP-961 guard correctly fired the shadow restart rule — but shadow
only READS through the kernel (`Store(read_only=True)`, never
`upsert_markets`), and its ~24h run awaiting the first settlement is
worth more than new code it does not call. Third hand-decomposition
avoided: `promote.sh --defer=hyxlab-shadow.service` promotes
everything, skips the named restart loudly, and shadow runs the OLD
kernel until its next natural break (restart it only AFTER the first
settlement lands). Stream restarted clean 14:29:45Z. Mistakes **#18**
(git checkout as mutation-undo reverted the uncommitted fix; commit
before mutation-testing). Suite **595 -> 597**, ruff clean, promoted,
pushed. NEXT PASS, in order: (1) sweep completes ~16:00Z ->
`shadow_settlements` first row is the whole point of the run; (2)
`qa_batch_run_budget` verdict on 10.11h (one-time backlog vs steady
state); (3) atlas gate reopens; (4) read the timings= series across
the sweep boundary for the fetch-contention signature; (5) shadow
restart only after (1) observes a settlement. Still open: shadow
position continuity across restart is a workaround, not a fix; atlas
quoted tier data-gated; econ needs >=336h (~08-10).**
(prior **2026-08-04 08:45 UTC (THE FALSE-ALARM CLASS THE LAST ENTRY
NAMED WAS CLOSED HOURS BEFORE ITS FIRST SCHEDULED FIRING: THE
TAPE-COVERAGE CHECK WOULD HAVE FAILED TODAY'S 10:00Z QA ON A BACKFILL
THAT WAS HEALTHY AND DRAINING, BECAUSE THE 06:10Z SWEEP IS STILL MID-RUN
AT QA TIME EVERY DAY THE SWEEP RUNS LONG.** Gate check first, hard
(`date -u` 08:15, `systemctl list-timers`): the 06:10Z kalshi sweep
fired on schedule and is **still running** (2h in — this is the
discriminator run for the 10.5h budget question; `qa_batch_run_budget`
will read it once it completes, ~16:20Z if 10h is the new steady state);
QA next 10:00Z; **atlas gate stays CLOSED until the sweep completes**,
not merely until it starts — its outputs are what atlas reads. **THE TOP
RUNG SURVIVED BUT HAS NOT YET PAID**: `hyxlab-shadow` is alive since
08-03 14:28:53Z (~18h, past the ~06:30Z target, 6553 fills, memory
731M/1G), and `shadow_settlements` is **still 0 rows** — the sweep that
resolves its holdings is mid-run, so the first-settlement rung stays
data-gated until it lands. Nothing may restart shadow meanwhile; the
promote guard enforced that unprompted (see below). **LANDED (06c2c30,
EXP-962)**: the trade-tape check is backfill-aware. The 08-03 pass
watched it report a live, draining `trades_backfill` (count fell 3 -> 2
during the read) identically to rot. The discriminator is per-market
PERSISTENCE judged from the archive — what LANDED, never what is
presumed running, the same doctrine as EXP-961: tradepass is daily, so a
genuinely queued market clears within one cycle. Three renderings, kept
distinct: nothing unswept -> PASS; unswept with sweeps landing and all
inside a 30h first-OBSERVED grace -> **WATCH (draining tail, not rot)**,
non-failing; no sweep landed for 26h (sweeper dead), or a market past
its grace despite sweeps landing (stuck, not draining) -> **FAIL, and it
keeps failing** — both are repairable, unlike a capture hole, so the
EXP-960 decay-to-WATCH shape is deliberately NOT used. First-seen ages
run from QA's own observation (close_time would start the clock while a
market legitimately queues behind older work) and are pruned on
coverage so a re-appearance gets a fresh clock. **VALIDATED LIVE BEFORE
COMMIT**: today's mid-sweep archive reads `WATCH — 7 unswept but sweeps
landed 0.0h ago` where the old check read FAIL — i.e. the fix's first
save is the very QA run two hours from now. Suite **591 -> 595**, ruff
clean, promoted (guard restarted `hyxlab-stream` only — collector/
moved; shadow untouched), pushed. NEXT PASS, in order: (1) after the
sweep completes (~16-17Z), check `shadow_settlements` — the first
settlement is the whole point of the 18h run; (2) read
`qa_batch_run_budget`'s verdict on whether 10.11h was the one-time
crypto backlog or the steady state; (3) the atlas gate reopens then too.
Still open: EXP-957 wall-clock decomposition (batched `upsert_markets`
vs 31 per-series calls, unverified); shadow position continuity across
restart is a workaround, not a fix; atlas quoted tier data-gated; econ
needs >=336h (~08-10).**
(prior 2026-08-04 02:45 UTC (THE FADE-WINDOW ASSERTION WAS AN
INVARIANT OVER TWO CONSTANTS, SO IT WAS GREEN THROUGH A 2.1h BREACH OF
ITSELF — AND THE LAST ENTRY'S OWN MARGIN ARITHMETIC WAS OFF BY 5h
BECAUSE IT JUDGED A COMPLETED RUN BY A SCHEDULE THAT RUN NEVER RAN
UNDER. TWENTY-SIXTH INSTANCE OF THE CLASS, ONE LEVEL UP AGAIN: A
SCHEDULE DESCRIBES FUTURE RUNS; ONLY THE JOURNAL DESCRIBES THE ONES
THAT HAPPENED.** Gate check first, hard rather than estimated (`date -u`
02:15, `systemctl list-timers`): kalshi sweep last fired 08-03, next
08-04 06:10Z, so **the atlas gate is CLOSED**; QA next 08-04 10:00Z;
weather bracket #8 ~02:15; econ needs >=336h. Tree **clean** and the new
Stop hook quiet — the first pass in three to start on the ladder rather
than on someone else's uncommitted work. **THE RUNG TAKEN WAS THE ONE
THE LAST ENTRY DEFERRED**, and it was bigger than the errand it looked
like. The deferred task was "give `hyxlab-sweep`'s budget constant the
completed number". The number arrived — **11:10:00Z -> 21:16:38Z,
10h06m38s** — and it is **2.1h ABOVE the 8.0h that was written down**,
which means `test_kalshi_batch_units_finish_before_the_live_fade_window`
had been passing all day on a budget reality had already broken. **THE
DEFECT IS STRUCTURAL, NOT ARITHMETIC**: that test compares
`OnCalendar + BATCH_RUN_BUDGET_H` against 23:00Z — two CONSTANTS — so it
is green for exactly as long as they agree with each other, whatever the
units are really doing. No check in the repo could see this; only the
journal can. **AND THE SAME BLIND SPOT PRODUCED A WRONG NUMBER IN THIS
LOG**: the last entry scored that run "7.6h clear of 23:00Z" by adding
its age to the timer's CURRENT 06:10Z spec. The run started **11:10Z**,
under the pre-EXP-950 local-time schedule it was actually launched from,
and finished with **1h43m** of margin — a near miss reported as a
comfortable one. **LANDED (e39c0f2, EXP-961)**: budget corrected 8.0 ->
10.5 and MOVED into `collector.qa`, so the test and the new check cannot
hold divergent copies; `qa_batch_run_budget` measures COMPLETED runs
from the journal, and the two failure modes are deliberately shaped
differently — a **budget breach FAILs and keeps failing** (repairable:
re-measure, or make the unit faster), a **fade-window overlap FAILs on a
new date then decays to WATCH** (a past overlap cannot be un-spent, and
a permanent FAIL is the noise that trains an operator to stop reading
QA — the EXP-960 shape). Overlap is computed from each run's own
measured interval, never from spec + budget, which is the whole lesson
encoded. **VALIDATED LIVE FROM STABLE**: `PASS batch units within
measured run budget — 3 completed run(s) over 7d; worst
hyxlab-sweep.timer 10.11h/10.5h, hyxlab-tradepass.timer 0.09h/4h`.
Mutation-checked: parsing the CPU half of `Consumed ... over ...`
(1h12m, which clears even the OLD budget) and same-day-only overlap
arithmetic each redden a test. **THE SECOND COMMIT IS THE PROMOTE CLASS
ESCALATING OUT OF PROSE** (5e3b133): the last entry's rule — read what
changed before running a script that restarts daemons — had to be
applied BY HAND for a second consecutive pass, so per the mistakes-log
doctrine it is now in the script. `promote.sh` takes the diff BEFORE the
fast-forward (while stable still points at the deployed commit) and
restarts a daemon only when the packages its own ExecStart runs actually
moved; `--restart-all` forces the old behaviour. **IT PAID OFF ON ITS
FIRST RUN**: this promote restarted `hyxlab-stream` (collector/ moved)
and left `hyxlab-shadow` alone — **still up since 14:28:53Z**, 12.3h
into the ~16h it needs. Under the old script that run would have died
with **~4h to go**. The guard is package-granular and therefore
conservative in the cheap direction: stream took a restart it did not
strictly need (a WS reconnect and a gap row, self-healing), which is the
right asymmetry against a settlement run that cannot be re-acquired.
Suite **572 -> 591**, ruff clean, pushed. **ONE LIVE QA FAIL, PROBED
BEFORE REPORTING, AND IT IS NOT ROT**: `trade tape covers retention
window — 3 traded markets unswept`. `collector.trades_backfill` (pid
1950816) is **live and draining** — 1.4k-9.3k markets/hour every hour
for the last 6h, and the count fell **3 -> 2 while this pass watched**
(the two left are KXSOLD-26AUG0122-T72.9999 and
KXFOXNEWSMENTION-26AUG01-CUBA, both closed 08-02). The check has no
notion of "a backfill is currently running", so it reports a draining
tail exactly as it would report rot — a false-alarm CLASS, not a false
alarm about the data, and the next hardening candidate. NEXT PASS:
**the first settlement is still the top rung** — run `20260803T142853`
is alive and must reach **08-04 ~06:30 UTC**, and `shadow_settlements`
is still **0 rows** archive-wide; the 06:10Z sweep is what resolves its
holdings, so nothing may restart shadow before then. Then: the **08-04
sweep is the discriminator** on whether 10.11h was the one-time crypto
backlog or the new steady state — `qa_batch_run_budget` will say so out
loud either way; QA 10:00Z is the first run carrying it; the atlas gate
reopens after 06:10Z. Still open: the EXP-957 wall-clock rise wants its
decomposition (batched `upsert_markets` vs 31 per-series calls, still
unverified); the trade-tape check wants backfill-awareness; shadow
position continuity across restart is still a workaround, not a fix; the
atlas quoted tier is data-gated.**
(prior 2026-08-03 20:30 UTC (THE LADDER WAS PREEMPTED BY AN
UNCOMMITTED TREE FOR THE SECOND PASS RUNNING, SO THE CLASS WAS ESCALATED
OUT OF PROSE AND INTO A STOP HOOK — AND THE PROMOTE THAT SHIPPED IT WAS
DECOMPOSED RATHER THAN RUN, BECAUSE `promote.sh` RESTARTS
`hyxlab-shadow` UNCONDITIONALLY AND NOTHING UNDER `simulator/` HAD
CHANGED. TWENTY-FIFTH INSTANCE OF THE CLASS, ONE LEVEL UP AGAIN: A
PROMOTE IS NOT ONE INDIVISIBLE ACT.** Gate check first, hard rather than
estimated (`date -u` 20:15, `systemctl list-timers` — journald prints
CDT): the kalshi sweep fired 06:10 UTC today and atlas already ran
14:30, so **the atlas gate is CLOSED** until the 08-04 06:10 sweep; QA
next 08-04 10:00; weather bracket #8 is 08-04 ~02:15; econ needs >=336h
(next ~08-10). Every standing report gated — **and `git status`
preempted the ladder anyway**: four modified files plus an untracked
test, none of which this log has ever mentioned. **DATED THROUGH THE
TRAP THE LAST ENTRY NAMED, NOT AROUND IT**: `ls --time-style=+...Z`
appends a literal Z without converting, so it was re-read with `TZ=UTC
--time-style=full-iso` — the work is **14:52–15:19 UTC**, i.e. AFTER the
last entry's 14:40 "tree clean, pushed", so a pass wrote 626 lines and
ended without committing them, ~5h ago. Provenance established rather
than assumed: the two other live `claude` processes have cwd `gpud` and
`hylshi`, so no concurrent agent on this repo. **THE 08-03 DEFECT DID
NOT RECUR IN ITS WORST FORM, AND THAT WAS CHECKED THE RIGHT WAY** —
installed units diffed against the repo (not the two worktrees against
each other): `hyxlab-collect.service`, `hyxlab-collect.timer` and
`hyxlab-breadth.timer` all **IDENTICAL**, so nothing had shipped ahead
of git this time. **LANDED, after review rather than on trust**: four
commits — 030af02 **EXP-957**, the load-bearing one (the writer lock is
needed for the WRITE, never for the FETCH: `collect` held
`data/writer.lock` across every HTTP call, and the cycle is restructured
to FETCH -> acquire -> WRITE -> release, buffered through a `Cycle`
dataclass and written in ONE transaction so a crash cannot leave a cycle
half in the archive — a partial cycle reads like data, and losing one is
tolerable where corrupting one is not); 6f92ed7 **EXP-959** (assert the
Kalshi batch units clear the 23:00-04:00Z fade window, an invariant that
was previously an unasserted side effect of EXP-950's timezone pin, and
which neither the pin test nor the dependency-order test covers);
b86cf70 **EXP-960** (a day-wide skip budget of 3/24h is blind to the
hours that carry the P&L — a night losing three consecutive fade-window
cycles passed cleanly, so capture holes are now counted per-night from
the journal, with FAIL on a NEW holed night and non-failing WATCH on one
already reported, because an unrecoverable hole that FAILs forever is
noise and noise trains an operator to stop reading QA). Suite **548 ->
572**, ruff clean. **ONE DEFECT FOUND BY REVIEWING THE INHERITED WORK,
AND IT IS THE FOURTH COMMIT** (2585633): moving the fetch ahead of the
acquire left the skip timer running from the top of the CYCLE, so
`waited_s` billed the lock for ~29s of HTTP it never held. No consumer
breaks — `qa_collect_skips` counts rows and never reads the magnitude —
so it is a reporting defect, not a logic one, and it is still worth
fixing because the field's only reader is the operator diagnosing lock
contention, on the very instrument EXP-944 added to stop that question
being answered by inference. Verified by mutation: reverting to `t0`
reddens the new test and nothing else. **THE PROMOTE WAS DECOMPOSED,
AND THIS IS THE PASS'S OPERATIONAL FINDING.** `promote.sh:33` restarts
`hyxlab-stream` and `hyxlab-shadow` unconditionally, and shadow run
`20260803T142853` was 5.8h into the **~15.7h** it must survive to
produce the first realized settlement in this archive's history.
Measured rather than assumed: `git diff --name-only 5e06eb1..HEAD`
touches only `collector/`, `tests/` and `docs/` — **nothing under
`simulator/` at all** — and both changed modules
(`collector.collect`, `collector.qa`) are timer-driven `ExecStart`s from
the stable worktree, which the script's own line-32 comment says "pick
up new code on next run". So the restart would have destroyed the
archive's first settlement shot for **exactly zero** benefit. Every
promote step was run except that one line; stable is at **2585633**,
imports smoke-tested in the stable venv, units reinstalled, and shadow
pid 2254781 verified still alive at its original 14:28:52 start.
**VALIDATED IN THE REAL PIPELINE, AND IT IS DECISIVE TO THE TENTH OF A
SECOND.** The `lockid` sidecar records the acquire instant, so the claim
is measured, not inferred: cycle 20:20:00Z acquired the lock at
**20:20:39.09Z**, cycle 20:25:00Z at **20:25:39.36Z** — **39.1s and
39.4s of HTTP with the lock FREE**, where the old code held it from
second zero. **Lock hold fell 36.6s -> 20.7s (-43%)**, reproducible
across both cycles, against the two immediately-prior old-code cycles
(20:10 37.8s, 20:15 35.4s) under the same contention (`collector.sweep
--days 2` at 9h12m and `trades_backfill` at 8h47m were both live
throughout). **THE COST IS REAL, REPRODUCIBLE, AND REPORTED RATHER THAN
BURIED**: total wall clock rose **36.6s -> 59.9s (+64%)**, from 12% to
20% of the 300s timer period — inside budget, but not free. **AND THE
DESIGN NOTE'S OWN ARITHMETIC DOES NOT RECONCILE WITH PRODUCTION, WHICH
IS THE NEXT LEAD**: EXP-957's docstrings claim fetch 28.9s + write 15.8s
of a "~52s hold", yet the measured OLD cycle was **36.6s end to end** —
less than the note's two halves summed — and the new fetch alone is
39.2s. The direction and the mechanism are confirmed exactly as claimed;
the absolute figures were taken under conditions that no longer
reproduce, so they must not be quoted as current. **THE NEW QA CHECK RAN
LIVE FROM STABLE AND PASSED**: `0 lost cycle(s) over 2 measured
window(s) of 7 (budget 1/window); 5 window(s) UNMEASURED`, plus a
correct non-failing `WATCH` that `hyxlab-poly-sweep` was still running
inside the 08-01 and 08-02 windows **but cost no cycles** — the leading
indicator firing without a false alarm, which is the whole design.
**AN HONEST LIMITATION, MEASURED NOT GUESSED**: journald holds only
**16M, reaching back to 08-02 01:50Z (~2 days)**, so a 7-night lookback
can never be fully populated and **the 07-29 breach night the check was
built from is already unmeasurable**. The instrument does the right
thing — it reports UNMEASURED rather than clean, because None is not
zero — but its effective reach is ~2 nights, and that is a property of
retention, not of the code. **ONE WATCH ITEM AT ITS THRESHOLD, AND THE
RULE THAT GOVERNS IT WAS FOLLOWED RATHER THAN OVERRIDDEN**:
`hyxlab-sweep` has been running **9h12m** and is still going, exceeding
the **8.0h** over-allowance documented in the constant EXP-959 just
shipped. It does NOT breach the assertion (06:10 + 9.2h = 15.4h UTC,
still 7.6h clear of 23:00Z), and the constant's own comment requires
worst **COMPLETED** wall clock, never a duration inferred from a running
process's age — so it stays until the run finishes. Update it next pass
with the real number. **HARDENED, BECAUSE THE CLASS RECURRED** (6ac1775):
the uncommitted-tree failure is now a **Stop hook**, per the mistakes-log
doctrine that anything recurring jumps straight to rule/test/hook. It
**escalates on CHANGE** — fingerprints `git status --porcelain` and
blocks once per distinct dirty state — so a deliberate leftover cannot
loop the agent without bound, the same FAIL-then-WATCH shape EXP-960
adopted for capture holes. Eight scenarios against real throwaway repos,
including the discrimination control (committing clears it) that fails
both an always-allow and an always-block implementation. **A STANDING
LINE IN THIS LOG WAS STALE AND IS RETIRED HERE**: `strategies/
hylshi_fade.py` has been described as "untracked, correctly left alone"
for many consecutive entries. It is **TRACKED**, committed in 62ad5b4.
`git ls-files` says so; the line was being copied forward, not checked.
PRACTICAL RULE, joining a-deployed-change-is-not-a-committed-one /
a-closed-market-is-not-a-settled-one / an-ambiguous-in-bracket-fill-is-
not-an-invented-one / a-wide-book-is-not-a-price / a-skipped-check-is-
not-a-passed-one / an-untriggered-path-is-not-an-unreached-one /
unobserved-is-not-unobservable: **a promote is not one indivisible act.
Read what actually changed before running a script that restarts
daemons — `promote.sh` restarts `hyxlab-shadow` unconditionally, and
when nothing under `simulator/` moved, that restart is pure cost paid
against the archive's scarcest asset, an unbroken run. And a fix that
moves work out of a lock moves the CLOCK too: check every duration the
old order was measuring, because `waited_s` kept timing from the top of
the cycle and silently started billing the lock for the fetch.** NEXT
PASS: **the first settlement is still the top rung** — run
`20260803T142853` is alive and must reach **08-04 ~06:30 UTC** (~10h
out at time of writing), and `shadow_settlements` is still **0 rows**
archive-wide; weather bracket #8 is 08-04 ~02:15 and is the first to
report `concentration_strict` natively; **QA 08-04 10:00 UTC** is the
first run carrying the fade-window check; the atlas gate reopens after
the 08-04 06:10 sweep. Still open: the EXP-957 wall-clock rise wants its
decomposition chased (batched `upsert_markets` vs 31 per-series calls is
the leading hypothesis, unverified); the `hyxlab-sweep` budget constant
wants the completed number; shadow position continuity across restart —
which this pass's decomposed promote is a workaround for, not a fix;
the atlas quoted tier is data-gated.**
(prior 2026-08-03 14:40 UTC (A PASS OF WORK SAT UNCOMMITTED AND HAD
ALREADY SHIPPED ITSELF TO PRODUCTION — THE TIMER FIX INSIDE IT WAS LIVE
IN SYSTEMD AND ABSENT FROM GIT, AND IT SILENTLY MOVED THE DAILY RESULT
WRITE FIVE HOURS EARLIER, RETIRING THE PROMOTE RULE THE LAST ENTRY
ENDED ON. TWENTY-FOURTH INSTANCE OF THE CLASS, ONE LEVEL UP AGAIN: A
DEPLOYED CHANGE IS NOT A COMMITTED ONE.** Gate check first, hard rather
than estimated (`date -u` 14:15, `systemctl list-timers` — journald
prints CDT): the kalshi sweep FIRED 11:10 UTC today, so **the atlas gate
is OPEN** and atlas is runnable; QA fired 07:00; weather bracket #8 is
08-04 ~02:15; econ needs >=336h (next ~08-10). **BUT THE LADDER WAS
PREEMPTED, AND CORRECTLY**: `git status` showed a large uncommitted tree
this log has never mentioned — `collector/lockid.py` untracked plus six
modified collectors, `qa.py`, and two timer units. **AND IT WAS BLOCKING
THE LOOP THAT PRODUCES THESE PASSES**: `autoloop.sh` journalled `error:
cannot pull with rebase: You have unstaged changes` at 14:15, so the
autoloop had stopped syncing with origin entirely. Provenance established
rather than assumed — `ps` shows the running claude is THIS pass (pid
2178057), so no concurrent agent; file mtimes 13:31–13:55 UTC (`ls
--time-style=+...Z` appends a literal Z and does **not** convert, a trap
worth naming) date the work to the previous iteration, which ended
without committing. **THE PART THAT MAKES IT MORE THAN HOUSEKEEPING**:
`promote.sh:29` copies `scripts/systemd/hyxlab-*` from **`$DEV`**, the
dev working tree — not from stable. So the previous pass's UNCOMMITTED
timer edit was **already installed in production**: installed units read
`OnCalendar=*-*-* 06:10:00 UTC` while both git worktrees read the
suffix-less line. Deployed state ahead of committed state, with git
showing nothing wrong. **LANDED, after review rather than on trust**:
three logical commits (4e2023c EXP-944 `lockid` — every writer names
itself at acquire time, before `open_retry` can spin 300s holding the
flock, with `alive` re-derived from /proc and matched on cmdline so a
recycled pid cannot name an innocent; 0d37940 EXP-943 — an absent skip
journal is decided against systemd's independent count of exit-75
cycles, since `collect.main()` exits 75 on exactly the path that calls
`record_skip()`, so the two disagree only when the producer is inert;
8c1beb5 — a timer whose Description claims UTC must pin UTC). Suite
**526 -> 548**, one real ruff error fixed (SIM115 in the new test; a
context manager subsumed its `try/finally`), promoted to `8c1beb5`,
pushed, tree clean and the autoloop's rebase unblocked. **VALIDATED IN
THE REAL PIPELINE, BOTH HALVES.** The sidecar appeared on the next
collect cycle and reads `hyxlab-breadth.service` pid 2225555 — **the
exact process `lockid.py`'s own docstring could only INFER was behind
the 12:47–13:06 UTC outage**, now a record instead of an inference. QA
run live from stable reads journal **3** exit-75 cycles against **3**
sidecar rows: producer **proven alive**, a real PASS. **THE HEADLINE
PREDICTION OF THE LAST ENTRY FAILED, AND THE CAUSE IS MEASURED NOT
GUESSED.** That entry promised "the first realized settlement in this
archive's history ~2.7h out" once the 11:10 sweep landed. The sweep
landed 3h ago and **`shadow_settlements` is STILL 0 rows.** Probed
before reporting: run `20260803T080023` held **140 markets and ZERO of
them close before now** — earliest close among its holdings is **08-04
04:59 UTC**, tomorrow. The "40 of its markets closed while it was alive"
belonged to the PREVIOUS run `20260802T204103`; the 08:00 run inherited
the prediction but not the holdings, and **was never a settlement
candidate at all.** A hypothesis was killed on the way: `_settle` reads
`markets` from `data/hyxlab.duckdb` (`shadow.py:189`), the same DB the
sweep writes `result` to, so the wrong-database theory is FALSE and
checking beat asserting. **THE LOAD-BEARING CONSEQUENCE, WHICH NOTHING
HAD STATED: THE TIMER FIX CHANGES THE SETTLEMENT ECONOMICS.** Moving the
sweep 11:10Z -> 06:10Z moves the once-daily `markets.result` batch write
five hours earlier, so a market closing 04:59Z now waits **1.2h** for its
result instead of **6.2h** — the required unbroken run lifetime to
observe ANY settlement drops by ~5h. **THIS RETIRES THE PROMOTE RULE THE
LAST ENTRY ENDED ON**: "restart just after the sweep completes (~11:30
UTC)" is now simply the wrong clock, and the corrected form is not a
clock at all. **CORRECTED RULE: a run's restart deadline is set by the
SWEEP THAT RESOLVES ITS HOLDINGS, not by the hour.** Applied here: the
promote restarted shadow as `20260803T142109` at 14:21Z; its earliest
holding closes 08-04 04:59Z, resolved by the 08-04 06:10Z sweep, so it
needs **~16.2h unbroken life** — the SHORTEST required lifetime in this
archive's history (every prior run needed 22.5h+). Restarting cost
nothing measurable: the killed run and a fresh one had to survive to the
**same instant**, and 14.5h remained to re-acquire the same ladder.
**ONE WATCH ITEM, REAL AND AT ITS THRESHOLD**: the collect-skip check
passes at **3 skips against `COLLECT_SKIP_MAX_24H` = 3** — one more and
it FAILS. Cause is structural, not noise: `hyxlab-breadth` is `*:2/5`
and `hyxlab-collect` is `*:0/5`, **two minutes apart**, while collect
waits only 240s; breadth held the writer lock ~19 min on 08-03 and
starved three cycles. **THEN THE PREEMPTED LADDER RUNG WAS RUN AFTER ALL, AND THE 08-02
PREDICTION CAME TRUE VERBATIM.** Atlas fired once the archive freed:
`reports/atlas/20260803T143000.json`, the FIFTH day-weighted reading and
the first carrying `flagged_quoted` natively. Tiers 101->108 flagged,
66->69 robust, 19->22 day-robust — and **the strictest Wilson tier broke
its streak, 6 -> 9**, ending four consecutive readings of identical
membership. **PROBED BEFORE REPORTING, AND THE THREE ENTRANTS ARE THE
ARTIFACT ITSELF**: all three are **Crypto d4** (1h, 6h, 24h) with
`median_spread` **0.99**, `wide_share` **0.98**, and `quoted_n` of
**7/7/12** against the MIN_N 200 bar — carrying the three largest gaps in
the whole report (+0.3649, +0.4019, +0.4123). These are the BNB ladders
the 08-02 pass diagnosed at `Crypto|24h|d4`, which then sat in the
FLAGGED tier. **THAT PASS PREDICTED THIS IN WORDS AND THE DATA HAS NOW
CONFIRMED IT**: "an empty book is stably empty, so more days of it
TIGHTEN the Wilson interval and make the bucket MORE robust." One
reading later the artifact has climbed from flagged to the strictest
Wilson tier on nothing but accumulated days of empty book. **This is the
sharpest vindication `flagged_quoted` could get** — it is the only tier
that holds them out, and it does so at quoted_n 7-12. **THE SIX
ORIGINALS ALL HELD (KEPT 6, LOST 0)**, so the zero-oscillation
observation survives for the original membership and the growth is
entirely contamination. **`flagged_quoted` reads ZERO for a SECOND
consecutive reading**: still not one bucket in the archive whose
longshot-fade flag survives on two-sided books, and even the six
originals are wide-contaminated (`wide_share` 0.48–0.80). Still
data-gated, still not tuned. **AND QA RAN GREEN LIVE**: moving the timer
fired a `Persistent=true` catch-up at 14:28Z which reached **17/17
all-pass** in 39.6s, including the new `producer proven alive against 3
journalled exit-75 cycle(s)`. Two watch items closed in passing — the
07:00Z run's `trade tape covers retention window` FAIL **cleared** (1
unswept -> 0, the 11:10Z sweep landed it, so it was correctly not chased
as a finding), and the poly universe ratio ticked **0.572 -> 0.582**
(5,555 against a 9,551 peak), still above the 0.5 threshold and still
below the 0.66 benign floor. **ONE CLAIM OF MY OWN CORRECTED WITHIN THE
PASS, BECAUSE THE EVIDENCE ARRIVED AFTER THE COMMIT MESSAGE**: 5e06eb1
says the tradepass "holds a read-write DuckDB connection for its whole
run". **That is too strong.** Both QA and atlas connected read-only at
~15:05Z and ~15:10Z while pid 1950816 was *still alive* at 3h+, so it
holds the exclusive connection in **BURSTS**, not continuously. The
timer fix stands and the reasoning is unchanged in direction — QA's
budget is 60s against a job whose bursts span 5m–3h of wall clock — but
the collision would have been **INTERMITTENT, not daily**, which is the
worse failure and the exact shape the 08-02 pass named: QA's green days
were green by RACE LUCK. The fix removes the race rather than re-tuning
a budget against it. PRACTICAL RULE, joining a-closed-market-is-not-a-
settled-one / an-ambiguous-in-bracket-fill-is-not-an-invented-one /
a-wide-book-is-not-a-price / a-skipped-check-is-not-a-passed-one /
an-untriggered-path-is-not-an-unreached-one / unobserved-is-not-
unobservable: **a deployed change is not a committed one. `promote.sh`
installs systemd units from the DEV WORKING TREE, so an uncommitted edit
ships to production while `git log` shows nothing — check the INSTALLED
artefact against the repo, not the two repos against each other, because
both worktrees agreeing is exactly what a suffix-less timer looked like.
And an autoloop that cannot rebase has silently stopped syncing: read
its journal, not just its output. And a systemd timer moved with
`Persistent=true` fires a CATCH-UP immediately if the new hour has
already passed today — QA ran the moment it was promoted.** NEXT PASS:
**the first settlement is the top rung** — the live run is
`20260803T142853` (the second promote restarted shadow again; the
14:21 run lasted 7 min), and it must survive to **08-04 ~06:30 UTC**.
At **~15.7h** this is the first time the required lifetime is inside
the range a 12-day-uptime box has actually delivered, and
`shadow_settlements` is still **0 rows** archive-wide. Weather bracket
#8 is 08-04 ~02:15 and is still the first to report
`concentration_strict` natively; **QA next 08-04 10:00 UTC**, now
downstream of BOTH the sweep and the tradepass for the first time.
Still open: shadow position continuity across restart — and note the
two promotes this pass cost two runs, which is the continuity argument
making itself again; the atlas quoted tier is data-gated;
breadth/collect lock contention is the sharpest new lead. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-08-03 08:30 UTC (THE ARCHIVE OBSERVED ITS FIRST OUTCOME
IN 39 RUNS AND STILL SETTLED NOTHING — BECAUSE `_settle` GATES ON
`markets.result`, WHICH IS A ONCE-DAILY BATCH WRITE AT 11:10 UTC, NOT ON
THE CLOCK. TWENTY-THIRD INSTANCE OF THE CLASS, ONE LEVEL UP AGAIN: A
CLOSED MARKET IS NOT A SETTLED ONE.** Gate check first, hard rather than
estimated (`systemctl list-timers`, `date -u` 08:15 — journald prints
CDT): the kalshi sweep is next 08-03 11:10 UTC so **atlas stays gated**;
weather bracket #8 is 08-04 ~02:15; econ needs >=336h (next ~08-10). QA
fired 07:00 UTC and the **05:00–07:00 promote window this log staked out
had already been executed at 08:00** — stable reads `30f33c3`, so the
capture-hole fix (6dcdcb7) and the collection hardening are LIVE.
**HOUSEKEEPING**: two commits sat unpushed (fef7861, 30f33c3); suite
green at 519, pushed. **THE HEADLINE EVENT DID HAPPEN**: `shadow_coverage`
reads run `20260802T204103` at **`coverage_fills` 0.1098 — 343 observed
fills, the first non-zero reading in 39 runs.** 40 of its markets closed
while it was alive. **AND `shadow_settlements` IS EMPTY.** Zero rows, on
a table created live on 08-02. **PROBED BEFORE REPORTING, AND THE GATE IS
THE WRONG ONE.** `_settle` does not test the clock; it tests
`markets.result in ('yes','no')`. `result` is not written when a market
closes: the collector's 5-minute `upsert_markets` only carries markets
that are still LIVE, so a settled result reaches the archive **solely
through the daily kalshi sweep at 11:10 UTC**. Measured on the live
archive rather than inferred: **every kalshi market that closed on 08-03
is unresolved — 124 markets across 03:00–08:00 UTC — while everything
through 08-02 21:00 is resolved.** All 40 of the run's closed markets
read `result = ''`, and still did 3h after close. **SO THE SHORTFALL THIS
LOG HAS BEEN TRACKING IS THE WRONG NUMBER**: `hours_to_first_outcome`
measures time-to-close, but settlement needs time-to-close PLUS the batch
lag — **6.2h for a market closing 04:59 UTC, 23.7h for one closing 11:30
UTC**. Run `20260802T204103` needed to live to **11:10 UTC (14.5h)**; its
`h_to_1st` of 2.62h said it had cleared the bar 5.4h earlier. It had
cleared the CLOSE bar. **THIS ALSO RETIRES THE PROMOTE PLAN THREE
ENTRIES OF THIS LOG WERE BUILT ON**: the 05:00–07:00 window was chosen to
land after the 04:59 outcome, and **even executed perfectly it would not
have produced a settlement**, because the run still had to survive to
11:10. The window was optimised against the wrong gate. INSTRUMENT
SHIPPED (5fdac67, 9218756): `settle_coverage_*` partitions the same fills
over the identical observed/pending/missed split against the predicate
`_settle` actually uses, plus `hours_to_first_settleable` and
`unresolved_fills`. `coverage_*` keeps its close-time meaning
**unchanged** so archived reports stay comparable, per the
`concentration`/`unobserved_*` precedent — the two are a bracket on WHAT
WAS OBSERVED, and a run can pass one and fail the other. **The
resolution instant is not recorded anywhere, so it is BRACKETED rather
than guessed**: the floor requires `updated_at <= run_end` (conservative
— a row re-touched later reads unsettled), the ceiling only that the
market closed in-life and has a result now. A live run's unresolved fill
reads **pending, never 0.0** — the 08-01 censoring lesson carried onto
the new partition. Seven regression tests; the load-bearing one runs an
**identical ledger and identical lifetime** through both gates and
asserts `coverage_fills` 1.0 against both settle bounds 0.0, so a
close-blind implementation fails on the contrast rather than on a
missing key, with a discrimination control asserting a genuinely
settleable fill still reads 1.0 under both bounds. Verified by mutation,
seven: result-blind settlement, empty-string-counts-as-resolved, floor
collapsed onto ceiling, pending folded into missed, per-run capping,
mean-of-per-run-ratios, and 0.0-instead-of-None. **ONE MUTATION SURVIVED
AND THE TEST WAS WRONG, NOT THE CODE — THE SAME CLASS AS 08-02 AND 08-03,
IN MY OWN TEST AGAIN**: per-run capping passed because the pooled fixture
gave the settled run exactly ONE fill, so `sum` and `any` are numerically
identical on it. Three settled fills reddens it, and still reddens the
ratio-averaging mutation the test was written for. Suite 519->526, ruff
clean, pushed. **NO PROMOTE, verified rather than assumed**: `grep` over
`scripts/systemd/` shows no unit references `shadow_coverage`, and stable
is otherwise current at `30f33c3`. **VALIDATED IN THE REAL PIPELINE AND
THE POOLED READ IS THE VERDICT**: the shipped module reproduces the
ad-hoc probe exactly — `20260802T204103` reads `cov_fills` 0.1098 with
**settle floor AND ceiling both 0.0**, and recent-5 pooled reads settle
**0.0 / 0.0 with 4,753 of 4,753 fills in markets the archive has no
result for.** The bracket is degenerate here, which is the cleanest
possible answer: no bound can settle a contract with no recorded result.
**STATED AT THE STRENGTH THE DATA SUPPORTS**: this does not show
`_settle` is broken — the 08-02 replay settled 1,585 archived positions
correctly. It shows the live daemon has **never once reached** it, and
that the coverage instrument built to detect exactly that answered a
different question for two days. **THE OPERATIONAL ITEM, AND IT IS DATED
FROM THE RIGHT GATE THIS TIME: DO NOT RESTART `hyxlab-shadow` BEFORE
~11:30 UTC TODAY.** Run `20260803T080023` is live from 08:00; the 11:10
sweep will write results for everything that closed 08-03 up to that
point, and if the run holds any of those it produces **the first realized
settlement in this archive's history** ~2.7h out. **CORRECTED PROMOTE
RULE, replacing the 05:00–07:00 window**: the cheapest restart moment is
**just after the daily sweep completes (~11:30 UTC)**, because that is
when a fresh run has maximum runway to the NEXT result write — settling
anything needs ~24h of unbroken process life spanning one 11:10 sweep,
and a 12-day-uptime box has already shown that cannot be promised.
**ONE QA FAILURE, REAL AND NOT CHASED**: the 07:00 run reached all 16
checks (the 1105e8e fix confirmed live) and reads **FAIL `trade tape
covers retention window — 1 traded markets unswept`**; same check that
fired 07-22. One market, and the 11:10 sweep may clear it — watch it next
pass, it is not a finding yet. PRACTICAL RULE, joining
an-ambiguous-in-bracket-fill-is-not-an-invented-one /
a-wide-book-is-not-a-price / a-skipped-check-is-not-a-passed-one /
an-untriggered-path-is-not-an-unreached-one /
unobserved-is-not-unobservable / shadow-coverage-before-shadow-equity:
**a closed market is not a settled one. Read the field the CODE gates on,
not the one that sounds equivalent — `_settle` tests `markets.result`,
and a result is a once-daily batch write, not an event at close. A
coverage instrument must partition on the downstream code's actual
predicate or it certifies the wrong thing; ours read 0.1098 for a run
that settled nothing. And when a deadline is derived from the wrong
gate, executing it perfectly still fails.** NEXT PASS: the **11:10 sweep
then the ~11:30 restart window** is the operational ladder, and the first
settlement is the thing to check; the same sweep re-opens the atlas gate
for the FIFTH reading, now carrying `flagged_quoted`; the QA tape-
coverage FAIL wants a second reading; weather bracket #8 is 08-04 ~02:15
and is the first to report `concentration_strict` natively. Still open:
shadow position continuity across restart — now with a sharper case, since
the required unbroken lifetime is ~24h and not ~6h; the atlas quoted tier
is data-gated. Untracked `strategies/hylshi_fade.py` re-confirmed present,
still correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-08-03 02:30 UTC (WEATHER BRACKET #7 RAN AND READ AS THE
SECOND UNANIMOUS RUN — THEN ASKING WHY CROSSING BEAT THE OPTIMISTIC
CEILING FOUND THAT THE DIRECTION TEST CHARGES THE SIM FOR AMBIGUITY,
AND THE POOLED WEATHER SIGNIFICANCE DOES NOT SURVIVE THE FIX.
TWENTY-SECOND INSTANCE OF THE CLASS, ONE LEVEL UP AGAIN: AN AMBIGUOUS
IN-BRACKET FILL IS NOT AN INVENTED ONE.** Gate check first, hard rather
than estimated (`systemctl list-timers`, `date -u` 02:15 — journald
prints CDT): the kalshi sweep is next 08-03 11:10 UTC so **atlas stays
gated**; QA next 07:00; econ needs >=336h (next ~08-10). The weather
bracket WAS due (prior 08-02 02:16:27), so ladder rung 1. **THE RUN**:
`reports/maker_bracket/20260803T021550.json`, 292 virtual orders across
8 markets, `new_share_vs_all: 1.0` (292/292 against 21 priors), so it
certifies independent at the strictest tier. It reads 4 underlyings
**all net over**, `underlying_sign_p` 0.0625 — the second unanimous
weather run, and the market tier reads **p=0.003906, SIGNIFICANT**.
**BUT THE HEADLINE NUMBER IS OUT OF ITS OWN BRACKET**: crossing 177
against queue [154 pess, **166 opt**] — **11 above the optimistic
ceiling**, the largest positive excursion in the 37-run archive (prior
max +5, and most readings are NEGATIVE). **PROBED BEFORE REPORTING, and
the count is not the set**: 40 orders cross-but-not-opt and 29
opt-but-not-cross, so the sets differ by **69 orders while the totals
differ by 11** — the headline understates the disagreement 3.6x through
cancellation. **THEN THE LOAD-BEARING HALF, FOUND BY ASKING WHAT
`crossing_but_not_pess` ACTUALLY COUNTS.** The direction test scored an
over-award whenever the **PESSIMISTIC FLOOR** missed a fill the crossing
rule awarded. But the queue evidence is a **BRACKET**, and an order the
floor misses while the **ceiling fills it** lies INSIDE that bracket —
that is the bracket saying *unknown*, which is the whole reason it is a
bracket, not the sim inventing a fill. The report's own note said "fills
the sim **may be** inventing" and every tier then dropped the "may be".
**THE BIAS IS STRUCTURAL, NOT NOISY**: `filled_pess <= filled_opt`
always (**verified: 0 violations in 21,168 archived orders**), so the
loose over-award set is a **superset** of the strict one for every
order. The difference is therefore ONE-SIDED — a floor-only direction
test can only ever read MORE over, never less. Over dominating the
archive is partly the test's construction, not the fill models.
**REPLAYED ACROSS ALL 37 ARCHIVED REPORTS AND IT MOVES REAL VERDICTS**:
**22 of 37 runs change**, **seven flip sign** (07-14 +3->-1, 07-23
+1->-6 and +2->-8, 07-26 +5->-2, 07-27 +1->-1, 07-30 +6->-1) — and
**both runs this log has celebrated as unanimous lose it**: 08-01's
"first unanimous run" 4/0 p=0.0625 -> 3/0 p=0.125, and today's #7 4/0
p=0.0625 -> **3/1 p=0.3125**. **THE POOLED READ IS THE VERDICT AND IT
DOES NOT SURVIVE.** Deduped by `order_key` across every weather report
(4,323 distinct orders, 56 underlyings): loose **41 over / 15 under,
p=0.000343, SIGNIFICANT**; strict **30 over / 23 under, p=0.205, NOT
significant**, aggregate +107 -> +30. On all categories pooled (12,832
orders) the direction survives but weakens by three orders of magnitude
(p=1.1e-05 -> 0.038, agg +175 -> +64). **STATED AT THE STRENGTH THE
DATA SUPPORTS**: this does NOT show the crossing rule is unbiased — the
strict reading still leans over on both pools. It does mean the
**significance of the weather over-award was carried by ambiguous
in-bracket fills**, and no maker registration should have rested on it.
INSTRUMENT SHIPPED (93be12a, 9dd64b8): reports carry the three-way split
`crossing_but_not_opt` (no queue model fills it — unambiguously
invented) / `inside_bracket` (ambiguous) / `pess_but_not_crossing`
(unambiguously forgone), plus `concentration_strict`, which re-runs
every tier against the ceiling. `concentration` keeps the floor reading
**unchanged** so archived reports stay comparable, per the
divergence-matcher / atlas-day-tier precedent — the two are a bracket on
the DIRECTION exactly as pess/opt are a bracket on the fill count. **The
UNDER side deliberately does not split**: an order the sim declines
while even the floor fills it is forgone under either bound, so only the
over side was ever loose. Seven regression tests; the load-bearing one
runs **identical orders, identical outcomes, identical day balance**
through both bounds and asserts the verdict **REVERSES** — significant
OVER at the floor (p=0.03125), significant UNDER at the ceiling — so a
bound-blind implementation fails on the contrast rather than on a
missing key, with a discrimination control asserting a genuinely
invented fill does NOT disappear when the bound tightens. Verified by
mutation, five: bound-blind `over_award`, strict-marks-everything,
tightening the forgone side too, defaulting to the ceiling, and
counting `inside_bracket` over all orders. **ONE MUTATION SURVIVED AND
THE TEST WAS WRONG, NOT THE CODE — AND IT IS THE SAME CLASS AS 08-02,
IN MY OWN TEST AGAIN**: `inside_bracket` over all orders passed because
the test **re-derived the partition at the call site** instead of
calling the shipped path, so `main()`'s assembly was never exercised.
Extracted to `over_award_split()` and the test now calls it; it reddens
exactly that mutation, plus a second test asserting a forgone fill is
never counted as inside the bracket (it has `filled_opt > 0` too, so the
naive read double-counts the forgone side onto BOTH sides). Suite
436->443, ruff clean, pushed. **NO PROMOTE, verified rather than
assumed**: `grep` over `scripts/systemd/` shows no unit references
queuescore. Validated in the real pipeline — the shipped module
re-run reproduces the ad-hoc probe exactly (40 / 7 / 24) and the market
tier flips **p=0.003906 significant -> p=0.109 not significant** on run
#7 alone; the re-run correctly self-certifies as `new_share_vs_all: 0.0`,
i.e. not new evidence. **THE OPERATIONAL ITEM IS UNCHANGED AND STILL THE
HIGHEST-VALUE EVENT ON THE BOARD**: `shadow_coverage` reads the live run
`20260802T204103` at **5.68h with `h_to_1st` 2.62h** — first outcome
**~04:59 UTC**, 1,631 fills correctly `pending`. In 38 archived runs
this archive has **never once observed an outcome end to end**. Promote
in the **08-03 05:00–07:00 UTC** window: after the first outcome
observation, before QA at 07:00, and ahead of the 05:00 poly sweep's
contention window where the un-promoted capture-hole fix (6dcdcb7) pays
most. PRACTICAL RULE, joining a-wide-book-is-not-a-price /
a-skipped-check-is-not-a-passed-one /
an-untriggered-path-is-not-an-unreached-one /
unobserved-is-not-unobservable / read-`tier_stability`-before-any-atlas-
count / shadow-coverage-before-shadow-equity / `underlying_sign_p` /
`new_share_vs_all` / connection-scoped-`seq`: **an ambiguous in-bracket
fill is not an invented one. When the evidence is a BRACKET, a
direction test must name which END it charges — and since the floor's
disagreement set contains the ceiling's for every order, charging the
floor leans one way BY CONSTRUCTION. Read `concentration_strict` beside
`concentration`; a direction significant in one and not the other is a
property of the bound, not of the models. And a count above a ceiling is
not a set above it — 11 net hid 69 disagreeing orders.** NEXT PASS: the
**04:59 outcome then the 05:00–07:00 promote window** is the operational
ladder; the 11:10 sweep re-opens the atlas gate for the FIFTH reading,
now carrying `flagged_quoted`; QA 07:00; weather bracket #8 is 08-04
~02:15, and it is the first that will report `concentration_strict`
natively. Still open: shadow position continuity across restart
(prerequisite `shadow_settlements` SHIPPED); the atlas quoted tier is
data-gated on quoted-observation accumulation. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-08-02 23:00 UTC (THE ATLAS GATE OPENED AND THE STRICTEST
TIER HELD FOR A FOURTH READING — THEN THE NEW CATEGORY THAT ARRIVED
TODAY FLAGGED AN IMPOSSIBLE GAP, AND CHASING IT FOUND THAT EVERY TIER IN
THE REPORT BOUNDS CORRELATION AND NONE OF THEM BOUNDS WHETHER `implied`
WAS A PRICE. TWENTY-FIRST INSTANCE OF THE CLASS, ONE LEVEL UP AGAIN: A
WIDE BOOK IS NOT A PRICE — AND THE CONTAMINATION RISES WITH STRICTNESS,
SO THE TIER THIS LOG TRUSTS MOST IS THE DIRTIEST ONE.** Gate check
first, hard rather than estimated (`systemctl list-timers`, `date -u`
22:47 — journald prints CDT): the kalshi sweep fired 08-02 11:10 UTC and
the last atlas ran 08-01 14:22, so **the atlas gate had OPENED** and
atlas was the one runnable standing report — ladder rung 1. Weather
bracket #7 is 08-03 ~02:15; econ needs >=336h (next ~08-10); QA next
08-03 07:00. **HOUSEKEEPING FIRST, AND IT WAS REAL**: six commits sat
unpushed (28db2af..de3bc83) from passes that shipped code without
updating this log — suite green at 431, ruff clean, pushed. **THE ATLAS
RUN**: `reports/atlas/20260802T225939.json`. Tiers 98->101 flagged,
64->66 robust, 19->19 day-robust, and the strictest tier **6 -> 6
day-weighted with IDENTICAL MEMBERSHIP, churn 0, zero oscillators, for a
FOURTH consecutive reading** (`tier_stability` reads `readings: 3` prior
distinct data states, self excluded). **READ THE UNIVERSE BEFORE THE
TIER COUNT, THOUGH**: today's category widening (d6cc21d) added **two
categories that existed in no prior reading** — Crypto (25,903 settled)
and Mentions (549) — and settled markets jumped 137,922 -> 165,814. Tier
counts across that boundary are not like-for-like. Deciles are absolute
price bins (`floor(mid*10)`), NOT population quantiles, so the new
categories cannot have moved any existing bucket's boundary; that part
is clean and was checked in the SQL rather than assumed. **THE FINDING,
AND IT STARTED AS A LEAD I DID NOT BELIEVE**: `Crypto|24h|d4` entered the
flagged AND cluster-robust tiers at implied **0.4812 against realized
0.0446**. PROBED BEFORE REPORTING, and it is an artifact: **all 202 of
its observations have spread > 0.5, median 0.95.** They are six BNB
ladders of ~35 strikes with exactly one YES each, so realized 0.045 is
the ladder base rate and implied 0.48 is the midpoint of an EMPTY BOOK —
bid ~0.01, ask ~0.96. **THEN THE LOAD-BEARING HALF, FOUND BY ASKING
WHETHER THIS WAS A NEW-CATEGORY PROBLEM. IT IS NOT, AND IT RUNS THE
WRONG WAY.** The share of observations with spread > 0.5 measured across
the whole archive: **4.6% flagged -> 5.3% cluster-robust -> 18.1%
day-robust -> 34.2% day-weighted** (at spread > 0.2: 11.3% -> 13.0% ->
58.6% -> **71.9%**). **The strictest tier is the most contaminated tier
in the report.** The mechanism is not coincidence: `mid` is (bid+ask)/2,
so an empty book manufactures an implied near 0.5 against whatever the
ladder's true base rate is — which IS a large implied-minus-realized gap
— and every tier selects on gap size. The crossed-candle gate and the
0.995/0.005 sentinel were both built to exclude empty books and each
tests a **CORNER, not a WIDTH**, so bid 0.05 / ask 0.95 sails through
both. And statistical strictness cannot reach it, because the artifact
is **systematic, not noisy**: an empty book is stably empty, so more days
of it TIGHTEN the Wilson interval and make the bucket MORE robust.
**WHICH REFRAMES THIS LOG'S OWN ZERO-OSCILLATION HEADLINE**: identical
day-weighted membership across four readings is exactly what an artifact
looks like. Stability was being read as evidence for the signature; it
is equally evidence for an empty book. INSTRUMENT SHIPPED (6f26210):
`flagged_quoted` re-runs the strictest test on the subsample whose books
were two-sided (`spread <= 0.20`, chosen because a wider book puts more
ambiguity on `implied` than the **0.08–0.19** of every gap this report
has ever flagged — a bound wider than the effect is not a bound),
requiring the same MIN_N and the **same SIGN**; buckets keep their
original decile, since the question is whether THIS bucket's flag is
carried by quoted books, not what a re-binned population would say.
Every bucket now reports `median_spread`/`mean_spread`/`wide_share`
unconditionally, so contamination is readable where no tier selected it,
and the printed summary breaks wide-share out **per tier** — the rise
with strictness is invisible from any single tier's rows. Six regression
tests; the load-bearing one runs identical outcomes, day balance and
implied through wide and tight books and asserts the wide one clears
every Wilson tier while FAILING the quoted tier, so a width-blind
implementation fails on the contrast rather than on a missing key.
Verified by mutation, five: dropping the sign agreement, dropping the
MIN_N bar, a width-blind filter, taking the spread from a different
candle than the mid, and 0.0-instead-of-None. **ONE MUTATION SURVIVED
AND THE TEST WAS WRONG, NOT THE CODE**: 0.0-instead-of-None passed
because the assertion sat on a fixture that HAS quoted books, so
`quoted_n == 0` was never reached — the untriggered-path lesson from
08-02 02:50, in my own test. Moved onto the all-wide fixture, where the
path is guaranteed; it now reddens exactly that test (59e5e32). Suite
431->436, ruff clean, pushed. **NO PROMOTE, verified rather than
assumed**: `grep` over `scripts/systemd/` shows no unit references atlas.
**VALIDATED IN THE REAL PIPELINE, AND THE RESULT IS THE HEADLINE:
`flagged_quoted` reads ZERO. Not one of the six day-weighted survivors
survives on quoted books** — and the three ways of failing are different
and must not be pooled: **REFUTED (1)** — `Financials|6h|d2`, 373 quoted
observations, gap **flips sign** −0.1275 -> **+0.0179**; **NOT
SIGNIFICANT (1)** — `Financials|1h|d2`, 458 quoted observations, ample
evidence, gap collapses −0.1508 -> −0.0446 and stops excluding;
**SILENT (4)** — Climate 1h d1, Economics 1h d2/d3/d4, all with
quoted_n 83–146 against the 200 bar, which is silence, not a rejection.
**THE PATTERN ACROSS ALL SIX IS THE ONE THE ARTIFACT PREDICTS AND IT IS
NOT ITSELF A TEST**: every single gap shrinks toward zero once empty
books are removed (−0.0786->−0.0424, −0.1271->−0.0838,
−0.1434->−0.0153, −0.1900->−0.0776, −0.1508->−0.0446,
−0.1275->+0.0179), some by 10x. **STATED AT THE STRENGTH THE DATA
SUPPORTS**: this does NOT prove the longshot-fade signature is an
artifact — four of six buckets have too few quoted observations to
speak. It does mean the signature is **not currently supported by any
bucket with quoted evidence**, and the two that had enough evidence both
declined to confirm it. **AN HONEST LIMITATION, NAMED RATHER THAN
TUNED**: MIN_N=200 on the quoted subsample leaves most buckets silent,
and lowering it to reach a verdict would be fitting the threshold to the
answer. This is now **data-gated**: it needs quoted observations to
accumulate. Some buckets already have power (`Financials|6h|d6` 932
quoted of 1,378; `Financials|6h|d1` 920 of 1,948; `Commodities|6h|d1`
510 of 645), so the instrument is not toothless — it is the
day-weighted six specifically that are thin. PRACTICAL RULE, joining
a-skipped-check-is-not-a-passed-one /
an-untriggered-path-is-not-an-unreached-one /
unobserved-is-not-unobservable /
read-`tier_stability`-before-any-atlas-count /
shadow-coverage-before-shadow-equity / NULL-guard-is-not-a-range-
predicate / paying-is-not-retiring / `underlying_sign_p` /
`flagged_day_weighted` / `new_share_vs_all` / `top_day_share` /
connection-scoped-`seq`: **a wide book is not a price — read
`median_spread` and `wide_share` before reading any implied-minus-
realized gap, because (bid+ask)/2 on an empty book manufactures exactly
the gap the tiers select on. A gate that tests a CORNER (crossed,
0.995/0.005) does not test a WIDTH. And statistical strictness cannot
launder a systematic artifact: an empty book is stably empty, so more
evidence makes it look MORE robust and zero tier oscillation is what an
artifact looks like, not what a finding looks like.** **THE OPERATIONAL
ITEM, AND IT IS THE HIGHEST-VALUE EVENT ON THE BOARD — DO NOT PROMOTE
BEFORE 2026-08-03 05:00 UTC.** `shadow_coverage` reads the live run
`20260802T204103` at 2.11h with `h_to_1st` **6.18h**: its first market
close is **08-03 04:59:00 UTC**, and 642 fills sit correctly `pending`,
not 0.0. **In 38 archived runs this archive has never once observed an
outcome end to end** (recent-5 pooled coverage 0.0 over 3,015 genuinely
missed fills), and at 6.18h this is by a wide margin the shortest
shortfall ever recorded — every prior run needed 8–27h. The last
promote (20:41 UTC, stable at 3bba15b) killed the previous holder of
that title. **THE SEQUENCING**: promote in the **08-03 05:00–07:00 UTC**
window — after the first outcome observation, before QA at 07:00 — which
also lands ahead of the 05:00 poly sweep's 13.7–15.8h contention window,
where the un-promoted capture-hole fix (6dcdcb7) pays the most. That fix
plus de3bc83/16921c3 are pushed but NOT promoted; the QA fix (1105e8e)
already is. Cost of waiting is ~6h of a bounded, measured collector-drop
defect (69 dropped cycles in the last 24h, unchanged rate) against the
first end-to-end observation in the archive's history. NEXT PASS: the
**08-03 04:59 outcome then the 05:00–07:00 promote window** is the
operational ladder; weather bracket #7 is 08-03 ~02:15 (pooled 11 over /
10 under, k=21, p=0.5000); the 08-03 11:10 sweep re-opens the atlas gate
for the FIFTH reading, now with `flagged_quoted` carried and one prior
reading to compare against. Still open: shadow position continuity
across restart (prerequisite `shadow_settlements` SHIPPED); the quoted
tier is data-gated on quoted-observation accumulation. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-08-02 08:35 UTC (QA WAS THE ONE RUNNABLE STANDING REPORT
AND ITS "ALL CHECKS PASS" COVERED HALF THE CHECKS — THE ARCHIVE HALF HAS
BEEN SKIPPED ON 10 OF THE LAST 14 RUNS, AND THE CAUSE IS A RACE MY OWN
TIMERS LOSE BY ~1 SECOND, EVERY DAY, BY CONSTRUCTION. TWENTIETH INSTANCE
OF THE CLASS, ONE LEVEL UP AGAIN: A SKIPPED CHECK IS NOT A PASSED ONE.**
Gate check first, hard rather than estimated (`systemctl list-timers`,
`date -u` 08:15 — journald prints CDT): the kalshi sweep is next 08-02
11:10 so **atlas stays gated**; weather bracket #7 is 08-03 ~02:15; econ
needs >=336h (next ~08-10). **QA fired 07:00 UTC and was the one runnable
standing report**, so ladder rung 1. It printed `[qa] all checks pass` —
and it ran **8 checks, not 16**. The 8th line reads `PASS  main archive
reachable — skipped: live writer holds lock`, and `qa_archive` then
RETURNS. **THE CAUSE, MEASURED NOT GUESSED, AND IT IS A ONE-SECOND
RACE**: the lock holder was pid 318680 against QA's own pid 318679 —
**consecutive**, so it started in the same instant, not hours earlier as
the module's poly-sweep rationale assumes. `hyxlab-collect` is
`OnCalendar=*:0/5` and `hyxlab-qa` is `07:00:00 UTC`, a 5-minute
boundary, so the two fire in the SAME SECOND every day. The collector
holds the archive write lock **02:00:00 -> 02:00:11 (~11.1s)** while QA's
budget was 5 attempts x 2s **~= 10s** — it gave up at 02:00:10, **one
second before the release.** Over Jul 20 – Aug 02 the archive half was
skipped on **10 of 14 runs**, and **6 of those printed "all checks pass"
and exited 0**. The green days are not luck in the lock: they are the
days the STREAM half ran slow enough to push the archive connect past the
release (08-01's stream half finished 02:00:12, and that run read 16/16 —
which is why this log's 08-01 "QA is fully green for the first time"
headline was TRUE, and true by accident). **WHAT WENT UNWATCHED IS
EXACTLY THE SILENT-ROT HALF**: collector freshness, sweep-ran-in-36h, the
kalshi mirror invariant, poly freshness, the poly universe-shrink
tripwire built specifically to catch the 07-08 Gamma cap that halved the
swept universe, econ/gdelt feed cadence, and tape coverage — the failure
class the module's own docstring names as the main operational threat.
They are not decorative: the 07-22 run caught `trade tape covers
retention window — 1 traded markets unswept` on a day it did reach them.
FIX SHIPPED (1105e8e), three coupled parts, and the retry is the LEAST
load-bearing: (1) a lock-held section reports **SKIP**, never PASS, and
`main()` will not print "all checks pass" when any section was skipped;
(2) the skip is **BOUNDED** by a per-section completion record — past 36h
without the section actually completing, that is a FAIL, because the
watch is then genuinely off. The first SKIP starts the clock, so a
section locked from the very first run can still go stale **without
false-alarming a fresh deployment** — recording nothing leaves it green
forever, failing immediately alarms every new box, and the mutation that
drops `first_seen` proves the difference; (3) the lock-wait budget goes
to **60s, ~5x the collector cycle**. It does NOT cover the poly sweep,
measured at **13.7–15.8h wall clock over 8 runs** (the docs' "~7h" is
stale by half), and no budget could — which is precisely why the bound in
(2) carries the weight and the retry alone would have been a fix that
looks complete and closes only the easy case. Six regression tests; the
load-bearing one asserts a locked section emits SKIP **and that none of
the archive's own checks ran**, so a PASS-shaped skip fails on the
printed contract rather than on a missing key. Verified by mutation,
five: reverting the skip to PASS, dropping the staleness escalation,
never recording a completion, not starting the clock on first skip, and
reverting the retry budget each redden their own tests with no
collateral. Suite 398->404, ruff clean, pushed. **VALIDATED IN THE REAL
PIPELINE, AND THE ANSWER TO "WHAT WAS ROTTING" IS: NOTHING.** The shipped
module against the live archives reaches the archive half and reads
**16/16 all-pass**. That is the honest result — the defect was in the
watching, not in the data, and it is reported as such rather than dressed
up as a catch. **ONE DRIFT WORTH NAMING, NOT ACTING ON**: `poly swept
universe not shrinking` reads yesterday **5,428 distinct markets against
a prior-week peak of 9,494** — ratio **0.572**, above the 0.5 threshold
so it passes, but below the **0.66** this check's own comment records as
the benign floor observed 07-11. Watch it; it is not a finding yet.
**NO PROMOTE, and this is a real cost accepted rather than an
absence**: unlike atlas / queuescore / shadow_coverage, `hyxlab-qa.service`
DOES run `collector.qa` from the stable worktree, so this fix needs a
promote to take effect — but `promote.sh` restarts `hyxlab-shadow`.
Checked hard per the standing rule: `shadow_coverage` reads run
`20260802T022756` live at 5.88h with **`h_to_1st` 20.64h** (first outcome
08-03 ~05:00 UTC) and its 1,058 fills correctly `None`, not 0.0 — the
ff30414 partition working live. Promoting now destroys the first run in
this archive's history that could be observed end to end, 20.6h before
its payoff, to save one day of a bounded reporting defect. **THE
SEQUENCING, which is the operational output**: promote in the **08-03
05:00–07:00 UTC** window — after the first outcome observation, before
the next QA at 07:00 — so the run is preserved AND the fix is live before
QA next runs. The cost of waiting is exactly one more silently-skipped
archive half, and it is the cheap side of the trade. PRACTICAL RULE,
joining an-untriggered-path-is-not-an-unreached-one /
unobserved-is-not-unobservable / read-`tier_stability`-before-any-atlas-
count / shadow-coverage-before-shadow-equity / NULL-guard-is-not-a-range-
predicate / paying-is-not-retiring / `underlying_sign_p` /
`flagged_day_weighted` / `new_share_vs_all` / `top_day_share` /
connection-scoped-`seq`: **a skipped check is not a passed check, and a
green summary line is not a green run — read how many checks RAN before
reading whether they passed. A skip that is not bounded by a completion
record is indistinguishable from a pass forever. And when two schedules
can collide, assume they will: `*:0/5` and any :00 boundary are the same
instant, so a retry budget tuned to the writer's hold time is a race, not
a margin.** NEXT PASS: the **08-03 05:00–07:00 promote window** above is
the operational item; the 08-02 11:10 sweep re-opens the atlas gate for
the FOURTH day-weighted reading, which starts to power the
zero-oscillation claim; weather bracket #7 is 08-03 ~02:15; the current
run's first outcome at 08-03 ~05:00 is still the first observable end to
end and `hours_to_first_outcome` binds before any restart. Still open:
shadow position continuity across restart, prerequisite (`shadow_settlements`)
SHIPPED. Untracked `strategies/hylshi_fade.py` re-confirmed present, still
correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-08-02 02:50 UTC (WEATHER BRACKET #6 RUN AND THE POOLED
SERIES IS NOW A DEAD COIN FLIP — THEN THE HARD DATED CONSTRAINT THIS LOG
LAID LAST PASS WAS BROKEN BY A HOST REBOOT, AND CHASING WHY THE
SETTLEMENT FIX STILL HAD NOTHING TO ACT ON FOUND THE PATH WAS NEVER
WIRED. NINETEENTH INSTANCE OF THE CLASS, ONE LEVEL UP AGAIN: AN
UNTRIGGERED PATH IS NOT AN UNREACHED ONE.** Gate check first, hard rather
than estimated (`systemctl list-timers`, `date -u` 02:15 — journald prints
CDT): QA next 08-02 07:00; the kalshi sweep next 08-02 11:10 so **atlas
stays gated**; econ needs >=336h (next ~08-10). The weather bracket WAS
due (prior 08-01 02:16:33), so ladder rung 1. **THE RUN**: 270 virtual
orders across 8 markets, crossing **130 vs queue [126 pess, 144 opt]** —
inside the bracket on both sides, unlike run #5's 3-above-ceiling.
Report: `reports/maker_bracket/20260802T021627.json`, `new_share_vs_all:
1.0` (270/270 against 20 priors), so it certifies independent at the
strictest tier. It is also the first weather run in the archive to reach
**5 underlyings**, giving `underlying_min_sign_p` **0.03125** — the first
single weather run whose alpha=0.05 ceiling was reachable at all. It read
2 over / 3 under, p=0.8125: not robust, not significant. **POOLED**:
rehydrating `orders_detail` across all six certified-independent runs
reproduces the prior 9 over / 7 under (k=16, p=0.402) exactly and now
reads **11 over / 10 under, k=21, p=0.5000.** A dead coin flip — the
series has been able to say something since k=16 and says nothing, more
cleanly than before. **A COUNTING TRAP IN MY OWN PROBE, CAUGHT BY THE
DISAGREEMENT**: the first rehydration read k=19 against the log's k=16,
because I keyed the underlying with a hand-rolled `-B[\d.]+$` strip.
Kalshi weather markets carry BOTH `-B<x>` and `-T<x>` strikes on one
city-day, so `KXHIGHCHI-26JUL27-T90` split off from
`KXHIGHCHI-26JUL27` and five city-days double-counted as ten
underlyings — inflating the sample size and the apparent power. The
SHIPPED `event_ticker` (queuescore.py:152) is correct, takes the first
two dash-segments, and documents this exact hazard. Re-pooled through it,
the log's k=16 reproduces to the unit. No code change: the defect was in
the ad-hoc probe, and the lesson is to call the shipped key. **THEN THE
CONSTRAINT, AND IT WAS BROKEN BY SOMETHING NO POLICY OF MINE
CONTROLS.** The last pass ended with "DO NOT PROMOTE BEFORE 2026-08-02
04:59 UTC" to protect run `20260801T022320`, 8.6h from the first outcome
observation in the archive since 07-31. I honored it. **The run is dead
anyway** — `shadow_coverage` reads it at 23.42h life, killed **3.18
hours short** of its first outcome. Cause, measured not guessed: a HOST
REBOOT at 08-02 01:49 UTC (`journalctl --list-boots` shows a boot-ID
change; PID 3021854 -> 786; prior uptime 12 days). **THIS FALSIFIES THE
REMEDY, NOT THE DIAGNOSIS**: abstaining from promoting bought 23.4h
against the ~6h promote-cadence runs, so the 08-01 08:30 diagnosis was
right — and it was still not enough, because run lifetime has an
exogenous component. Observing a weather ladder needs an unbroken ~26.7h
process life, and a 12-day-uptime box just showed it cannot be promised.
So position continuity across restart is not an optimization; it is the
only mechanism that makes outcome observation robust at all. **THE
FINDING, FOUND BY ASKING WHY THE SETTLEMENT FIX STILL HAD NOTHING TO ACT
ON, AND IT IS THE LOAD-BEARING HALF.** `_settle` is called ONLY from
`finalize()`, which sits after the `while` loop in `shadow.py:main` — and
the unit runs with no `--duration`, so that loop is `while True` and
**finalize is unreachable in the daemon.** Settlement never ran in
production at all: no payout ever credited, no contract ever retired, at
any coverage. The 08:30 pass explained the three consecutive "no live
position to act on" readings with 100%-unobserved coverage; that is TRUE
and SUFFICIENT, which is exactly why it stopped the enquiry — but it is
not prior. Even at full coverage the path could not have fired. The
contrast that makes it precise: `_mark` (d07d8e8) runs from `_equity`
every snapshot and WAS genuinely live, so the same pass's conditional
negative was right for the mark fix and wrong for the settlement fix,
and nothing separated them until the call sites were read. FIX SHIPPED
(5f05302): settle every poll — idempotent via the existing `qty > 0`
guard, so per-poll needs no bookkeeping — and give settlement its own
`shadow_settlements` record. The record is the 20:25 pass's named
prerequisite for continuity: the fill ledger holds opens and closes
only, so summing signed fill qty resurrects every already-settled
position, the 7a89892 double count one level out. A settlement has no
price, fee or counterparty, so it is NOT a synthetic fill. Nine
regression tests; the load-bearing one asserts cash and the retired book
after a `poll_once` with `finalize()` NEVER called, so a
settle-at-shutdown daemon fails on arithmetic rather than on a missing
row. Verified by mutation, six: dropping the per-poll call, recording
nothing, winners-only, dropping the `qty > 0` idempotence guard, wall
clock instead of sim clock, and dropping the persist high-water mark
each redden exactly their own tests with no collateral — and the
winners-only mutation is the one 7a89892 originally SURVIVED. Suite
389->398, ruff clean, pushed. **A PROCESS FAILURE OF MY OWN, RECORDED
BECAUSE IT ALMOST COST THE WORK**: the mutation harness reverted with
`git checkout -- simulator/sim.py simulator/shadow.py`, which wiped the
uncommitted implementation along with the mutation. The tests survived
(different files) and the code was re-applied from context, but the rule
is now: **commit before mutating, then revert against the commit.**
**PROMOTED, and the constraint is restated rather than waived**: its
subject is dead, and the incoming run `20260802T014915` was 1.2h old
with 26.7h to its first outcome — the cheapest restart moment available.
More decisively, WITHOUT this fix that run could not have realized a
settlement even had it survived; preserving it unfixed would have
preserved a run structurally unable to produce the observation the
constraint existed to protect. Daemon up 02:27:56 as run
`20260802T022756`, `shadow_settlements` created on the live DB.
**CORRECTED RULE, replacing the dated one: promote EARLY in a run's
life, never late — the cheapest moment to restart is just after a
restart. A deadline is the wrong shape, because the deadline is not the
only thing that kills a run.** **VALIDATED IN THE REAL PIPELINE, AND IT
PRODUCES THE FIRST REALIZED SETTLEMENT PnL**: replaying the shipped
`_settle` over all 39 archived runs against real `markets.result`
settles **1,585 positions across 30 runs**, winners and losers both
retired, positions in unresolved markets correctly left open. Pooled on
MATCHED scope, payout 44,386.97 against cost 51,599.89 and fees
2,843.55: **realized -10,056.47, -19.5% of cost**, negative in 27 of 30.
**READ THE SCOPE BEFORE THE NUMBER, TWICE OVER.** (1) Every shadow run
in the archive is `TightSpreadProbe`, a taker probe that crosses the
spread to measure fill realism — NOT a strategy under test, and no
pre-registration exists, so this is **not a verdict** and kills nothing.
It is the round-trip cost of crossing plus adverse selection, end to end
through settlement, measured for the first time; fees alone are 5.5% of
cost. (2) The naive version of this number is an ARTIFACT and was caught
before it was reported: summing payout over the settled subset against
cost over the WHOLE book reads -19,644 and is negative by construction
whenever any position is left open. Matched scope or nothing. PRACTICAL
RULE, joining unobserved-is-not-unobservable / read-`tier_stability`-
before-any-atlas-count / shadow-coverage-before-shadow-equity /
NULL-guard-is-not-a-range-predicate / paying-is-not-retiring /
`underlying_sign_p` / `flagged_day_weighted` / `new_share_vs_all` /
`top_day_share` / connection-scoped-`seq`: **an untriggered code path is
not an unreached one. Before attributing a null result to the data, find
the caller — a coverage instrument measures whether the data could have
exercised a path and says nothing about whether anything calls it. And a
sufficient explanation is the most dangerous kind, because it ends the
search.** NEXT PASS: the current run's first outcome is **08-03 ~04:31
UTC** and it is the first that could ever be OBSERVED end to end, so
`hours_to_first_outcome` before any restart still binds; QA 08-02 07:00;
the 08-02 11:10 sweep re-opens the atlas gate for the FOURTH
day-weighted reading, which starts to power the zero-oscillation claim;
weather bracket #7 is 08-03 ~02:15. Still open and now with its
prerequisite SHIPPED rather than only named: shadow position continuity
across restart — `shadow_settlements` gives a reconstruction the
retirement record it was missing, and the reboot makes the case for
building it. Untracked `strategies/hylshi_fade.py` re-confirmed present,
still correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-08-01 20:25 UTC (EVERY STANDING REPORT GATED, SO THE
DEFERRED SHADOW-CONTINUITY ITEM GOT PROBED — AND THE ASSUMPTION UNDER IT
HOLDS WHILE THE MEASUREMENT BESIDE IT DOES NOT: THE "100% UNOBSERVED"
HEADLINE COUNTED A RUN THAT WAS STILL RUNNING. EIGHTEENTH INSTANCE OF THE
UNIT-OF-COUNTING CLASS, ONE LEVEL UP AGAIN: AN UNOBSERVED FILL IS NOT AN
UNOBSERVABLE ONE. THE PASS ENDS WITH A HARD DATED CONSTRAINT ON MY OWN
PROMOTE CADENCE.** Gate check first, hard rather than estimated
(`systemctl list-timers`, `date -u` 20:16 — journald prints CDT): the
kalshi sweep fired 11:10 UTC and atlas already ran 14:15 AND 14:22 after
it, so **atlas is gated until 08-02 11:10**; weather bracket ran 08-01
02:16 so #6 is 08-02 ~02:15; econ needs >=336h (next ~08-10); QA fired
08-01 07:00 (next 08-02). Nothing standing was runnable, so ladder rung 2:
verify an unverified design-note assumption. **THE ASSUMPTION**: the
08:30 pass deferred position continuity across restart on the strength of
"the ledger persists every fill, so net position and cost basis are
exactly reconstructible". **It is TRUE as written — and insufficient, for
a reason the ledger cannot show.** `shadow_fills` carries signed qty (+
open / - close), price, fee per (strategy, venue, market, side), and on
the live ledger 0 of 91,639 fills are closes, so position is monotone and
trivially reconstructible. But `_settle` (sim.py:354) zeroes
`ctx._positions` and credits cash while writing **nothing** to the
ledger — settlement is not a fill. So summing `shadow_fills` RESURRECTS
every already-settled position, and the prior run already banked its
payout: the exact double-count class 7a89892 fixed one level up. **AND
THE DATA CANNOT CATCH IT**: at 100% unobserved there are zero settled
positions in the shadow archive, so the naive fill-sum reconstruction
validates perfectly today and breaks the moment continuity starts
working. The fix's own success is what would make it wrong. Recorded, not
built — continuity still needs a settlement record in the ledger first.
**THEN THE MEASUREMENT, AND IT CORRECTS THIS LOG'S OWN HEADLINE.**
Probing the live ledger found the current run `20260801T022320` alive
since 02:23 and **still filling at 17.9h** — 3x the ~6h cadence the last
pass diagnosed — because the last two passes correctly did NOT promote.
Yet `shadow_coverage` read it at **exactly 0.0, 3,428/3,428 unobserved**.
That looked like an instrument defect and was chased as one; it is not.
The earliest market close in the entire run is **08-02 04:59**, so the
run needs a **26.6h** lifetime to observe its FIRST outcome and is at
17.9h. Nothing was missed — nothing is due yet. **THE 08:20 HEADLINE IS
THEREFORE WRONG**: its "4,802 of 4,802 pooled over the last five runs,
100.0%" included this same run at life 5.95h with **1,059 fills (22% of
the pool)**, still running, whose 0.0 was structurally guaranteed. A live
run's zero is CENSORING, not failure. INSTRUMENT SHIPPED (ff30414): a
dated fill partitions into observed / **pending** (run still live, market
not closed yet) / **missed** (run dead, permanently unobservable);
coverage is observed/(observed+missed) with pending excluded from BOTH
sides, and a run with only pending fills reads **None, not 0.0**.
Liveness keys on the last equity tick within `LIVE_GRACE_S`=300s, chosen
against the measured tick cadence (max gap ~37s, p99 ~35s across every
recent run — ~8x headroom). `unobserved_*` KEEPS its pre-partition
meaning so archived reports stay comparable, but `coverage_*` is
REPLACED rather than kept: per the QA-seq-headline precedent, for a live
run the old value was an artifact, not a coarser valid bound. Also adds
`hours_to_first_outcome`, the shortfall a run was killed by. Five
regression tests; the load-bearing one runs an IDENTICAL ledger past the
liveness boundary and asserts **1.0 live against 0.25 dead**, so counting
censored fills as failures fails on arithmetic rather than a missing key.
Verified by mutation, four: reverting the partition, always-live,
latest-instead-of-earliest close, and pending back in the denominator
each redden their own tests. One PRE-EXISTING test broke and that is
diagnostic rather than collateral — `test_recent_window_isolates_the_
current_regime` used fixture dates in the FUTURE, so its runs read live;
its subject is the window, not liveness, so `now` was pinned explicitly
instead of the assertion being relaxed. Suite 384->389, ruff clean,
pushed. **NO PROMOTE, and this time for TWO independent reasons**: `grep`
over `scripts/systemd/` shows no unit references `shadow_coverage` (same
call as atlas and queuescore) — and promoting would restart
`hyxlab-shadow` and kill the live run 8.6h before its first observation.
**WHAT THE CORRECTED READING SAYS, AND THE ORIGINAL FINDING'S DIRECTION
SURVIVES**: recent-5 pooled coverage is still **0.0**, now computed over
**3,743 genuinely missed fills** rather than a pool diluted with censored
ones. The sharpest new number is that `20260731T203829` was killed
**2.6 hours short** of its first outcome. **HARD DATED CONSTRAINT, and it
is the operational output of this pass: DO NOT PROMOTE BEFORE 2026-08-02
04:59 UTC.** Run `20260801T022320` is 8.6h from the first outcome
observation in the archive since 07-31 08:20, and it would be the first
live exercise of BOTH the `_mark` carry fix (d07d8e8) and the settlement
retirement (7a89892) — the two hardenings this log has recorded three
times as having no live position to act on. A promote is the one act that
destroys it. PRACTICAL RULE, joining read-`tier_stability`-before-any-
atlas-count / shadow-coverage-before-shadow-equity / NULL-guard-is-not-a-
range-predicate / paying-is-not-retiring / `underlying_sign_p` /
`flagged_day_weighted` / `new_share_vs_all` / `top_day_share` /
connection-scoped-`seq`: **an unobserved fill is not an unobservable one.
Read `missed` and `pending` separately before reading any coverage
number, and never pool a LIVE run's zero — it is censoring, and no data
could have avoided it. Check `hours_to_first_outcome` before restarting
the shadow daemon.** NEXT PASS: **08-02 04:59 UTC is the constraint above
and the highest-value event on the board**; 08-02 ~02:15 is weather
bracket #6 (pooled 9 over / 7 under, k=16, p=0.402); the 08-02 11:10
sweep re-opens the atlas gate for the FOURTH day-weighted reading, which
starts to power the zero-oscillation claim; QA 08-02 07:00. Still open
and deliberately not rushed: shadow position continuity, now with a named
prerequisite (a settlement record in the ledger) rather than only a
design objection. Untracked `strategies/hylshi_fade.py` re-confirmed
present, still correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-08-01 14:35 UTC (THE ATLAS GATE OPENED AND THE STRICTEST
TIER HELD IDENTICAL FOR A THIRD READING — THEN THE COUNTER-SIGNATURE THAT
APPEARED ONE TIER DOWN TURNED OUT TO BE MEMBERSHIP CHURN, AND THIS LOG
HAS BEEN READING IT AS NARRATIVE. SEVENTEENTH INSTANCE OF THE
UNIT-OF-COUNTING CLASS, ONE LEVEL UP AGAIN: A TIER'S SURVIVOR COUNT IS
NOT ITS MEMBERSHIP. Gate check first, hard rather than estimated
(`systemctl list-timers`, `date -u` 14:15 — journald prints CDT, so the
06:10 CDT `hyxlab-sweep` is 11:10 UTC): the kalshi sweep fired 3h ago and
the last atlas ran 07-31 14:15, so **the atlas gate the last three passes
named had OPENED** and atlas was the one runnable standing report.
Weather bracket ran 08-01 02:16 so #6 is 08-02 ~02:15; econ needs >=336h
(next ~08-10); QA fired 08-01 02:00 (next 08-02). **THE ATLAS RUN**:
`reports/atlas/20260801T141522.json`, a large increment (219,426 ->
227,961 bucket observations). Headline flagged 94->98 (18->19 groups),
robust 64->64, day-robust **14->19**, and the strictest tier
**day-weighted 6->6 with IDENTICAL MEMBERSHIP** — all six deciles <=4
with a positive implied-minus-realized day-weighted gap (Climate 1h d1
+0.0781, Economics 1h d2/d3/d4 +0.1253/+0.1389/+0.1811, Financials 1h d2
+0.1508, Financials 6h d2 +0.1275), zero high-decile survivors. **The
longshot-fade narrowing REPLICATES for a third consecutive reading.**
Still not a verdict: no pre-registration exists. **THE DRIFT IS ONE TIER
DOWN AND IT LOOKS LIKE A COUNTER-SIGNATURE**: all five day-robust gains
are Financials at deciles 5/6/7 with the OPPOSITE sign (realized >
implied — favorites underpriced), the exact counter-signature the last
three passes recorded as absent. **PROBED BEFORE BUILDING, and it is not
one.** Every one of the five crossed on **+1 day** of evidence, and
`Financials|24h|d7` flipped tier on **+4 markets** (day_lo 0.7498 ->
0.7545 against implied 0.7500). All five die at the day-weighted tier,
and the day-weighting collapses them — `6h|d6` -0.1672 -> -0.0236 (7x),
`1h|d6` -0.1464 -> -0.0268 (5.5x). That is the 07-30 day-weighted tier
(8a6ac3c) discriminating on live drift for the first time rather than in
the abstract. **THEN THE LOAD-BEARING HALF, FOUND BY ASKING WHETHER THE
TIER COUNTS THIS LOG REPORTS ARE STABLE AT ALL**: replaying tier
membership over all 27 archived reports, **19 buckets have ever been
day-robust and FIVE have left the tier and returned — every one of them
a Financials mid/high decile** (`1h|d5`, `24h|d7`, `6h|d5`, `6h|d6`,
`6h|d7`). **THIS CORRECTS THIS LOG'S OWN NARRATIVE**: the 07-31 pass
wrote that "both day-robust demotions are again HIGH deciles
(Financials|24h|d7, Financials|6h|d7)" and read it as the signature
narrowing to fading longshots. Both are back today. The demotion was a
one-reading dropout, not a narrowing — the narrowing claim survives only
at the day-weighted tier, which has **zero** re-entries and identical
membership across all three of its readings. INSTRUMENT SHIPPED
(ad3f935): reports carry `tier_stability` — per tier the churn against
the last distinct data state plus `gained`/`lost`/`oscillators`, per
surviving bucket `persistence` and `reentered`. **THREE UNITS OF
COUNTING DECIDE WHETHER THE NUMBER MEANS ANYTHING, and each is a
separate test**: (1) a reading is a distinct DATA state, not a report
file — the archive holds three duplicate-`data_fingerprint` pairs
(07-28, 07-29, 07-30), each a re-run minutes after shipping a tier, and
counting files gives each a guaranteed-zero churn step that biases every
stability estimate toward stable (27 files -> 21 readings; day-robust
mean churn 2.43 by file against 2.60 by reading); dedup keeps the LAST
report per state, because that re-run is exactly how a new tier first
appears and keep-first silently discards the only reading carrying it;
(2) a tier's denominator counts only readings whose report CARRIES the
tier, or `flagged_day_weighted` reads 2/21 for a tier that has never lost
a member; (3) a bucket's denominator counts only readings where it was
ELIGIBLE (present in `buckets`), since a bucket below n>=200 is absent
from the DATA, not from the tier — otherwise every genuinely new survivor
reads as churn. Eight regression tests. **VALIDATING IN THE REAL PIPELINE
FOUND A DEFECT THE FIXTURES DID NOT**: on a re-run the already-written
report shares the current run's fingerprint, so the run compares against
ITSELF — churn reads 0 and every survivor gains a free reading. Excluded
and tested. Verified by mutation, eight: file-counting, keep-first dedup,
tier-blind denominator, eligibility-blind denominator,
persistence-as-latest-reading, reentry-without-trailing-gap,
1.0-on-no-priors and self-comparison each redden exactly their own test
with no collateral. Suite 376->384, ruff clean, pushed. **NO PROMOTE, and
verified rather than assumed**: `grep` over `scripts/systemd/` shows no
unit references atlas — same call as queuescore and shadow_coverage.
Validated live by re-running the shipped report
(`20260801T142204.json`, identical fingerprint to the 14:15 run), which
reproduces the ad-hoc probe exactly — 21/13/6/2 readings, churn 6/4/5/0,
oscillators 15/2/5/0 — and correctly excludes itself. **AN HONEST
LIMITATION**: `flagged_day_weighted` has only **2** prior readings, so
its zero-oscillation record is the weakest-powered claim on the page —
it is the tier with the least opportunity to churn, not yet the tier
proven not to. Three more readings before that reads as evidence.
PRACTICAL RULE, joining shadow-coverage-before-shadow-equity /
NULL-guard-is-not-a-range-predicate / paying-is-not-retiring /
`underlying_sign_p` / `flagged_day_weighted` / `new_share_vs_all` /
`cross_bucket_overlap.groups` / `top_day_share` / connection-scoped-`seq`:
**read `tier_stability` before reading any atlas tier COUNT as drift. A
promotion or demotion at the day-robust tier is routinely one added day
of evidence crossing a Wilson endpoint, five buckets have already
oscillated in and out, and every narrative this log has built on a
day-robust count change is churn until `persistence` says otherwise. And
two atlas reports sharing a `data_fingerprint` are one reading.** NEXT
PASS: 08-02 ~02:15 is weather bracket #6 (pooled 9 over / 7 under, k=16,
p=0.402); the 08-02 11:10 sweep re-opens the atlas gate for the FOURTH
day-weighted reading, which is the one that starts to power the
zero-oscillation claim; QA 08-02 07:00. Still open and deliberately not
rushed: shadow position continuity across restart (the coverage
instrument from the 08:30 pass measures the truncation at 100% and does
not fix it — that is a design call, not an end-of-pass hardening).
Untracked `strategies/hylshi_fade.py` re-confirmed present, still
correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-08-01 08:30 UTC (QA IS FULLY GREEN FOR THE FIRST TIME IN
THIS LOG AND THE 26JUL31 CLEAR LANDED EXACTLY AS PREDICTED — THEN ASKING
WHY THE MARK FIX *AGAIN* TOUCHED NOTHING FOUND THAT THE SHADOW TRACK HAS
STOPPED OBSERVING OUTCOMES ENTIRELY, AND THAT MY OWN PROMOTE CADENCE IS
THE CAUSE. SIXTEENTH INSTANCE OF THE CLASS, ONE LEVEL UP AGAIN: THE
OBSERVATION DESTROYS THE THING OBSERVED. Gate check first, hard rather
than estimated (`systemctl list-timers`, `date -u` 08:15 — note journald
prints CDT, which cost a wrong "no entries" read before it was caught):
atlas gated until the 08-01 11:10 kalshi sweep (~2h54m out); weather
bracket ran 02:16 so #6 is 08-02; econ needs >=336h (next ~08-10). Two
things WERE runnable. **QA, fired 07:00 UTC, and it is ALL-PASS** —
16/16 including `book seq contiguous or gap-marked — 0 missing seq in 0
hole events, 11 own-channel gap rows, 0 unexcused` and `void frames are
known types — 54 void frames; all known`. The pre-restart residue that
reddened the seq check for three days has rolled out of the 26h window,
so the f91517b hole-scoping fix and the 22c9556 type-recording fix are
both confirmed clean on a full window with zero legacy dilution.
**THE 26JUL31 CLEAR REPRODUCED TO THE SECOND**: 30 void rows, all
`orderbook_snapshot`, at 04:59:18 / 05:59:18 / 06:59:18, KXHIGHNY+MIA
then CHI+AUS then DEN — the class-B pattern now holds on a second
independent day. Shadow rode it with no restart, no OOM, peak 507MB
against the 1G cap (steady 419MB), so the seed fix holds under a second
live boot. **BUT THE POSITION AUDIT CAME BACK EMPTY A THIRD TIME**: the
live run holds ZERO 26JUL31 positions — it holds 26AUG01 ladders. That
is now three consecutive passes where the `_mark` carry fix (d07d8e8)
and the settlement retirement (7a89992) had no live position to act on,
and the last two passes each recorded that as a clean conditional
negative. **IT IS NOT A COINCIDENCE, AND CHASING IT IS THE FINDING.**
Run lifetime has collapsed monotonically — **126h, 42h, 24h, 11.9h,
12.0h, 6.0h, 6.2h, 5.7h, 5.9h** — tracking the ~6h autonomous promote
cadence exactly, because `promote.sh` restarts `hyxlab-shadow` and
`shadow.py:22` documents "Restart = fresh sim state (positions reset)".
The weather ladders that supply most fills expire ~24h after opening.
**PROBED BEFORE BUILDING, against the live ledger joined to
`markets.close_time`**, and the number is unambiguous: the share of
fills whose market closes AFTER the run that opened them had already
ended reads **32.8% -> 46.9% -> 71.3% -> 99.8% -> 96.9% and then 100.0%
for every run since 07-31 08:20 — 4,802 of 4,802 pooled over the last
five runs.** Re-measured with run-end taken from the last EQUITY tick
rather than the last fill (the conservative choice, since a run keeps
marking after it stops trading) it is unchanged. **TWO VALIDITY BOUNDS
FOLLOW, and they bind on readings taken elsewhere in this log**: (1)
`shadow_equity` is NOT a strategy's equity curve — it is a ~6h fragment
that opens positions and is killed before any resolve, so it measures
enter-and-hold-for-6h, and pooling equity or drawdown across runs does
not recover the missing settlement leg, which for a weather ladder is
where the PnL is; (2) the mark and settlement paths hardened on 07-31
are UNREACHABLE in production at this cadence — at 100% unobserved,
"no live position was touched" is arithmetic, not luck. INSTRUMENT
SHIPPED (fa7efe5): `simulator/shadow_coverage.py` reports per-run and
pooled coverage by fill COUNT and by NOTIONAL — both units, per the
standing unit-of-counting lesson, because a count can read reassuring
while every large position sits unobserved. Undated markets are counted
and reported but never folded into either side of the ratio, and a run
with no dated fills reads `None` rather than 0.0 or 1.0, since both
defaults would print as findings the data does not support. Five
regression tests; the load-bearing one asserts the NUMBERS on a fixture
where the two units disagree (three tiny observed fills and one large
unobserved one read **0.75 by count and 0.0625 by notional**), so a
one-unit implementation fails on arithmetic rather than a missing key,
plus the run-end discrimination control, the undated control, the
None-not-zero control, and the recent-window test (a long historical
run must not dilute the current regime: pooled 0.5, recent 0.0).
Verified by mutation, four: last-fill-as-run-end, folding undated into
observed, computing the notional ratio from counts, and defaulting empty
coverage to 0.0 each redden exactly their own test. The first mutation
attempt was DISCARDED as dishonest — it reddened all five, but via a
fixture schema error rather than the semantics, and was re-run as
min-instead-of-max on the equity tick. Suite 370->376 (5 mine plus one
auto-parametrized boundary check the new module picks up), ruff clean,
pushed. **NO PROMOTE, and verified rather than assumed**: `grep` over
`scripts/systemd/` shows no timer runs any sim-side report — same call
as atlas and queuescore. Validated in the real pipeline: the shipped
module run against the live ledger reproduces the ad-hoc probe exactly
(`reports/shadow_coverage/20260801T082037.json`). **AN HONEST
LIMITATION**: on this archive `coverage_notional` tracks `coverage_fills`
closely (0.42 vs 0.379, 0.675 vs 0.672), so the second unit did NOT
change any reading here — it is measured because it CAN diverge and the
test proves it is measured, not because it has yet mattered. **WHAT IS
NOT DONE, AND IS DELIBERATELY NOT RUSHED**: this measures the truncation,
it does not fix it. The remedy is position continuity across restart —
the ledger persists every fill, so net position and cost basis are
exactly reconstructible — but restart-resets-state is a DELIBERATE
design call, and adopting a prior run's book would mix fill semantics
across code versions and risks double-counting equity against the runs
already recorded. That is a design decision to take deliberately, not a
hardening to ship at the end of a pass. PRACTICAL RULE, joining
NULL-guard-is-not-a-range-predicate / paying-is-not-retiring /
`underlying_sign_p` / `flagged_day_weighted` / `new_share_vs_all` /
`cross_bucket_overlap.groups` / `top_day_share` / connection-scoped-
`seq`: **read `shadow_coverage` before reading any shadow equity,
drawdown or PnL number. A shadow run only observes a position's outcome
if it outlives the market's close, and since 07-31 08:20 not one fill in
the ledger has met that bar — the promote that ships a fix is the same
act that guarantees the fix has nothing to act on.** NEXT PASS: the
08-01 11:10 sweep re-opens the atlas gate for the second reading of the
replicated longshot-fade narrowing; 08-02 ~02:15 is weather bracket #6
(pooled 9 over / 7 under, k=16, p=0.402); the 26AUG01 clear at 08-02
~04:59 is the first that a run could survive IF continuity lands.
Untracked `strategies/hylshi_fade.py` re-confirmed present, still
correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-08-01 02:40 UTC (25TH WEATHER MAKER BRACKET, INDEPENDENT
RUN #5 AND THE FIRST UNANIMOUS ONE — THEN THE NAMED "NEXT HARDENING
CANDIDATE" TURNED OUT TO BE A PLAN DEFECT, NOT A VOLUME ONE: THE SEED'S
NULL-GUARD DEFEATED THE SCAN'S RANGE PUSHDOWN. FIFTEENTH INSTANCE OF THE
CLASS, ONE LEVEL UP AGAIN: BOUNDING THE RESULT SET IS NOT BOUNDING THE
SCAN. Gate check first, hard rather than estimated (`systemctl
list-timers`, `date -u` 02:16): atlas gated until the 08-01 11:10 kalshi
sweep; econ needs >=336h (next ~08-10); QA next 08-01 07:00; the 26JUL31
clear is 08-01 ~04:59, still ~2.7h out. The weather bracket WAS due — the
prior ran 07-31 02:16:26 — so ladder rung 1. **THE RUN**: 277 virtual
orders across 8 markets, crossing **170 vs queue [157 pess, 167 opt]** —
3 orders ABOVE the optimistic ceiling. Report:
`reports/maker_bracket/20260801T021633.json`, `new_share_vs_all: 1.0`
(277/277 against 19 priors), so it certifies independent at the strictest
tier. It is also the first weather run in the archive to ATTAIN its power
ceiling: 4 underlyings, **all 4 net over**, so `underlying_sign_p` ==
`underlying_min_sign_p` == **0.0625** — unanimity, and still not
significant at alpha 0.05, which is the `min_sign_p` rule earning its keep
on live data rather than in the abstract. **POOLED, which is the unit the
07-31 pass established**: rehydrating `orders_detail` across all five
certified-independent runs reproduces the prior 5 over / 7 under (k=12)
exactly and now reads **9 over / 7 under, k=16, p=0.402**. k=16 is also
the smallest pooled k at which alpha=0.05 is reachable at all, so the
series has only just become able to say anything — and it says no
direction. **THEN THE DEFERRED ITEM, THE ~9%-HEADROOM BOOT PEAK THE LAST
PASS NAMED, AND THE DIAGNOSIS IN THE LOG WAS WRONG**: it was attributed to
"the engine-side scan peak", i.e. to seed VOLUME. PROBED BEFORE BUILDING,
against the live 170M-row stream archive, and the measurement falsifies
that immediately — replaying the seed path at **1,033,470 rows peaks at
699MB** and at **8,331 rows peaks at 684MB**. The cost is INVARIANT to the
window, so no seed-window narrowing could ever have touched it. Isolated
it further: opening the DB and running the same filtered ORDER BY with
`.fetchall()` costs 104MB and 0.0s. The whole difference is the predicate.
`recv_ts >= coalesce(?, recv_ts)` reads the COLUMN on its right side, so it
is not a range predicate — DuckDB cannot push it into the scan as a
min/max filter and evaluates it row-by-row over all 170M rows. Measured
head-to-head on an identical 9,387-row result: **685MB / 0.8s with the
coalesce against 157MB / 0.16s with a plain `>=`** — 4.4x the memory and
5x the time for the same rows. HARDENING SHIPPED (66f2ef5): the NULL case
moves out of SQL into Python — with a floor, a plain `recv_ts >= ?`; with
none, no predicate at all. `recv_ts` is NOT NULL (schema, and 0 null rows
on the live archive), so the two branches are EXACTLY equivalent to the
coalesce, which is why this is a plan fix and not a semantics change. Two
regression tests. The memory effect needs 170M rows and cannot be
reproduced at fixture scale, so the load-bearing one asserts the MECHANISM
(the seed SQL carries `recv_ts >= ?` and no `coalesce`) alongside the
semantics (with a book gap 9 events in, M1 — whose snapshots are all
pre-floor — must be UNSEEDED, so the floor is shown to be doing real
work); the second is the discrimination control for the obvious WRONG fix,
binding a NULL floor into a plain `>=`, which returns zero rows and leaves
every book unseeded. Verified by mutation, two: reverting to the coalesce
reddens exactly the two new tests, and the naive always-bind fix reddens
the control plus three EXISTING seed tests. Suite 368->370, ruff clean,
pushed and **PROMOTED** (same call as 494a2ac/d07d8e8/d4d1fac:
`simulator.shadow` runs in the live `hyxlab-shadow` daemon). **THE
PROMOTE IS THE LIVE TEST AND IT IS DECISIVE**: the outgoing pre-fix
process logged `915M memory peak` over its 5h44m life — exactly the figure
the last pass named — while the incoming one seeded from **1,038,257
archived events**, the largest seed this log has recorded and **28x** the
36,664 of the previous boot, and peaked at **416MB against the 1G cap**
(steady state 196MB). A 28x larger seed at 45% of the memory: the cost was
the predicate, not the window. Headroom goes from ~9% to ~61% and the
OOM-at-boot class is closed rather than reduced. **STILL OPEN AND NOT
CLAIMED AS DONE**: the confirmatory re-run of the real fav-long backtest
under the settlement fix (7a89992) is running again (4,004,743
candle-snapshots over 177,995 markets) and had not finished at the time of
writing; note it reads a LARGER data window than the pre-reg run and is
therefore a code validation only, NOT a re-registration and NOT a verdict
— the FAIL stands on `settled_net_pnl` -3,718.34 either way. PRACTICAL
RULE, joining `underlying_sign_p` / `flagged_day_weighted` /
`new_share_vs_all` / `cross_bucket_overlap.groups` / `top_day_share` /
connection-scoped-`seq` / own-(category,horizon) / paying-is-not-retiring:
**a NULL-guard written into SQL is not free. `col >= coalesce(?, col)`
silently converts a pushed-down range scan into a full row-by-row filter,
and the cost is constant in the result size — so it hides from every
volume-based diagnosis. Branch on NULL in Python instead.** NEXT PASS:
08-01 ~04:59 UTC is the 26JUL31 clear, the first the `_mark` fix sees live
and now the first under both a streamed and a range-scanned seed; the
08-01 11:10 sweep re-opens the atlas gate for a second reading of the
replicated longshot-fade narrowing; QA 07:00. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-07-31 20:55 UTC (EVERY STANDING REPORT GATED, SO THE
DEFERRED SHADOW-LEDGER QUESTION GOT ANSWERED — AND IT IS A CLEAN
NEGATIVE THAT THEN LED TO A METRIC WHICH DOUBLE-COUNTS EVERY SETTLED
WINNER AND FLIPS THE SIGN OF THE ARCHIVE'S ONLY PRE-REG RUN.
FOURTEENTH INSTANCE OF THE CLASS, ONE LEVEL UP AGAIN: PAYING A
POSITION OUT IS NOT RETIRING IT. Gate check first, hard rather than
estimated (`systemctl list-timers`, `date -u` 20:16): the kalshi sweep
last fired 07-31 11:10 UTC and atlas already ran 14:15 AFTER it, so
atlas is gated until the 08-01 11:10 sweep; weather bracket next 08-01
~02:15 (independent run #5, first carrying `underlying_sign_p`
natively); econ needs >=336h (next ~08-10); QA next 08-01 02:00.
The live item the last pass named — the 26JUL31 clear — is 08-01
~04:59, so it too is gated: the archive holds **42** void rows and the
newest is 07:45:07, nothing since. Nothing standing was runnable, so
ladder rung 4. **THE DEFERRED ITEM FIRST, AND IT IS A CLEAN NEGATIVE,
MEASURED NOT ARGUED**: the last pass shipped `_mark` forward-only and
asserted that any shadow run between 494a2ac (08:20) and d07d8e8
(14:23) marks expired positions at zero — but never checked whether the
PERSISTED ledger carries the damage. Run `20260731T082004` is exactly
that window (started 08:20:04, died 14:22:52). It has **zero 26JUL30
fills** — every clear in the archive (04:59, 05:59, 06:59, 07:45)
PREDATES the window, and the run that did hold the 10 expired positions
(`20260730T202207`) ended 08:19:57, entirely under the pre-494a2ac
code. So the 0.0-mark bug was live for 6h03m and **never touched a
position**; no persisted equity series is contaminated. The standing
claim is true as written but conditional, and the archive is clean.
**THE FINDING, FOUND BY ASKING WHAT ELSE READS A POSITION FOR VALUE**:
`_settle` credits a winner's payout to cash but leaves the position
standing in `ctx._positions`, and `_compute_metrics` then calls
`_equity()`, whose `_mark` returns 1.0 for a position on the winning
side of a settled market. **Every settled winner is counted twice.**
PROBED BEFORE BUILDING, against the archive: the only manifest carrying
the field is the fav-long pre-reg run
(`data/runs/20260711T230707_e7ba056d`), and it reads `final_equity`
**+67,561.66** against a true post-settlement equity of **-3,718.34** —
overstated by exactly the 71,280.00 of `settled_payout`, which
**flips the sign of the run's result**. **WHAT IT DOES NOT TOUCH, AND
THIS IS THE LOAD-BEARING HALF OF THE HONESTY**: the pre-registration's
primary endpoint is settled net PnL (payout - cost - fees), computed
from the ledger, which reads -3,718.34 correctly. **The FAIL (kill)
verdict STANDS and no verdict is overturned** — the broken metric
merely sat beside the deciding one. Losers mark 0.0 either way, so the
overstatement is always exactly `settled_payout`; and `max_drawdown` /
`shadow_equity` accumulate inside `step()`, where cash carries no
payout yet, so both are unaffected and the persisted shadow ledger is
clean. HARDENING SHIPPED (7a89992): settlement retires the contract,
both sides regardless of sign — a settled loser marks 0.0 so the
arithmetic cannot catch a winners-only retirement, which is why the
loser test asserts the book state directly. Five regression tests
asserting the NUMBERS (5.83 not 15.83; the open-position control reads
1.38 so a fix that merely stopped counting positions fails; the mixed
run reads 7.21 not 17.21). Verified by mutation, three: reverting the
retirement reddens exactly the two number tests; blanket retirement
reddens exactly the unsettled control; and winners-only initially
**SURVIVED** — the loser test had no book assertion — which is why that
assertion was added, and it now reddens exactly that test. **THEN THE
PROMOTE ITSELF PRODUCED A PRODUCTION INCIDENT, AND CHASING IT FOUND THE
SAME CLASS IN THE MEMORY GUARD**: `hyxlab-shadow` was kernel-OOM-killed
2s into boot at 20:34:03. `DUCK_MEM` caps DuckDB at 512MiB under the
1G cgroup, but the seed then called `.fetchall()`, building an
unbounded PYTHON list beside the bounded engine. The seed window is
"since the last book gap" and its size is decided by a **RACE**:
promote.sh restarts stream and shadow together, so if shadow reads the
floor before stream writes its `daemon_start` row it seeds from the
PREVIOUS break. MEASURED on the live archive: **2,084,503 rows (~417MB
of tuples) against the 21,419 the winning ordering gives — a 97x swing
on a race.** It recovered only on the systemd restart 30s later, once
stream's gap row existed. Same shape as the 07-11/07-12 boot OOMs the
`DUCK_MEM` note records, which is why capping the ENGINE never closed
it. HARDENING SHIPPED (d4d1fac): stream via `fetchmany(SEED_BATCH)`;
the ORDER BY still buffers inside the engine where it is bounded and
spills to `DUCK_TMP`. Two regression tests — the seed path must never
call `fetchall()` (a connection proxy makes it raise), and a batch size
that does not divide the row count must seed the same book as one that
swallows it whole. Verified by mutation: reverting to `fetchall()`
reddens exactly the first; dropping the last row of each batch
initially **SURVIVED**, because repeated snapshots of ONE market make
every row but the last irrelevant — the fixture now spreads events
across four markets and it reddens exactly the boundary control. Suite
361->366->368, ruff clean, both pushed and **PROMOTED** (same call as
494a2ac/d07d8e8: `simulator.sim` and `simulator.shadow` run in the live
`hyxlab-shadow` daemon). The second promote is itself the live test —
same simultaneous restart, and shadow came up `active` with **zero
OOMs**, seeding from 36,664 events. **AN HONEST LIMITATION**: peak
memory on that boot was still **915MB against the 1G cap** (steady
state 348MB), so the Python-side list is gone but the engine-side scan
peak leaves only ~9% headroom. The OOM risk is REDUCED, NOT
ELIMINATED, and that is the next hardening candidate. Also open and
NOT claimed as done: the confirmatory re-run of the real fav-long
backtest under the fix timed out at 900s mid-replay (4M
candle-snapshots over 178k markets) and is re-running; the fix's
validation currently rests on the fixture tests plus the manifest
arithmetic (`open_cost` is 0.0, so true equity == cash == -3,718.34,
and `final_equity - cash` == `settled_payout` exactly). PRACTICAL RULE,
joining `underlying_sign_p` / `flagged_day_weighted` /
`new_share_vs_all` / `cross_bucket_overlap.groups` / `top_day_share` /
connection-scoped-`seq` / own-(category,horizon): **paying a position
out is not retiring it — read `settled_net_pnl`, never `final_equity`,
for any run containing settled markets, because `final_equity`
overstates by exactly `settled_payout` and can flip a loss into a
profit. And bounding an engine's memory is not bounding the result set
it hands to Python.** NEXT PASS: 08-01 ~02:15 UTC weather bracket
independent run #5 (pool leaning UNDERLYINGS, 5 over / 7 under, k=12,
p=0.387 so far — not runs); 08-01 ~04:59 UTC is the 26JUL31 clear, the
first the mark fix sees live and now also the first under a streamed
seed; the 08-01 11:10 sweep re-opens the atlas gate for a second
reading of the replicated longshot-fade narrowing. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-07-31 14:35 UTC (ATLAS RE-OPENED ON A LARGE INCREMENT AND
THE LONGSHOT-FADE NARROWING REPLICATED — THEN THE DEFERRED PHANTOM-LADDER
AUDIT CLEARED THE MAKER BRACKET AND FOUND THAT YESTERDAY'S FIX HAD
SILENTLY REPURPOSED THE PHANTOM AS A 100%-LOSS MARK. THIRTEENTH INSTANCE
OF THE CLASS, ONE LEVEL UP: RECORDING A FRAME IS NOT HANDLING IT, AND
HANDLING IT FOR FILLS IS NOT HANDLING IT FOR VALUE. Gate check first,
hard rather than estimated (`systemctl list-timers`): the kalshi sweep
fired 07-31 11:10 UTC and the last atlas ran 07-30 14:20, so the atlas
gate the last three passes named had OPENED and atlas was the one
runnable standing report; weather bracket next 08-01 ~02:15 (independent
run #5, the first carrying `underlying_sign_p` natively); econ needs
>=336h (next ~08-10); QA fired 07-31 07:00 UTC; divergence unchanged.
**THE ATLAS RUN, AND IT IS A GENUINELY BROAD INCREMENT**: Commodities|1h
+3,295, Financials|1h +1,542, Financials|6h +1,522, every active
(category, horizon) gaining. Report:
`reports/atlas/20260731T141517.json`. Tiers 91->94 flagged (28->27
groups), 63->64 robust (25->26), day-robust 13->14 (8->10 groups), and
the strictest tier **5 -> 6 day-weighted (4 -> 5 groups)**. The gained
day-weighted survivor is `Climate and Weather|1h|d1` (n=278, 76 days,
day-weighted gap **-0.0775**), and both day-robust demotions are again
HIGH deciles (`Financials|24h|d7`, `Financials|6h|d7`). So the 07-30
narrowing REPLICATES on a large independent increment rather than
decaying: all **6** day-weighted survivors are deciles <=4 with a
NEGATIVE gap (Climate 1h d1, Economics 1h d2/d3/d4, Financials 1h d2,
Financials 6h d2), zero high-decile survivors, zero counter-signature.
The tradeable half of the favorite-longshot signature in this archive
remains FADING LONGSHOTS, now on 5 distinct groups. Still not a verdict:
no pre-registration exists. **THEN THE DEFERRED ITEM — "whether any
ARCHIVED report or shadow run was computed against a phantom ladder" —
AND THE FIRST HALF IS A CLEAN NEGATIVE, MEASURED NOT ARGUED**: replayed
`score_market` over all 30 cleared markets under the shipped code and
under a monkeypatched pre-fix `apply` where a void row is a no-op. **178
orders vs 178, 0 of 30 markets differing on any field.** The reason is
structural and worth recording: the clear is TERMINAL, so a market emits
no further events, `replay_snapshots` emits no further snapshots, and no
order can be armed or crossed against the phantom. Every archived
`maker_bracket` report is therefore uncontaminated, and so is the
divergence track for the same reason. **THE FINDING IS IN THE OTHER
CONSUMER, AND IT IS THE LOAD-BEARING HALF**: `hyxlab-shadow` HOLDS
POSITIONS. Run 20260730T202207 carried 10 open long-yes positions in
expired 26JUL30 ladders across the clears (~330 contracts). Marking runs
through `Simulator._mark`, which checks `info.result` first and otherwise
falls back to the last snapshot's `mid()` — and 494a2ac made that
snapshot a two-sided-None top, so `mid()` is None and the fallback
returned **0.0, for BOTH sides**. That is a total loss on a live
position, and it breaks the yes/no complementarity every other branch in
the function keeps: a long-yes + long-no pair worth exactly 1.0 under any
outcome marks at 0.0. **PROBED BEFORE BUILDING, against the live
archive**, and the window is not hypothetical: `markets.close_time` for
these ladders is **04:59 — exactly the clear instant**, corroborating
that the clear IS the expiry, while `updated_at` on the settlement result
is **11:34** from the daily sweep. So the settlement branch does not
rescue the mark for **6h36m**, and for a market clearing just after the
11:10 sweep the wait is ~24h. Measured over the 10 real positions:
settlement truth **+45.00** (two ladders resolved yes), carrying the last
observed mid reads **+51.44** (14% high), and the shipped fallback reads
**+0.00** — off by the entire position, marking 45 contracts of WINNERS
at zero. HARDENING SHIPPED (d07d8e8): `_mark` now carries the last
two-sided mid per (venue, market); 0.0 stays the fallback for a market
never seen two-sided (unchanged behaviour, not silently widened), and the
settlement branch still wins over the carry. The design call is that the
clear is real for FILLS — no counterparty rests on an empty ladder, which
is exactly what 494a2ac fixed and is NOT being walked back — but an empty
book carries no information about VALUE. Five regression tests; the
load-bearing one asserts the NUMBER (25 YES last seen at mid 0.96 must
mark 24.0 across the clear and equity must not move), plus the
complementarity invariant (a risk-free yes/no pair marks 10.0, not 0.0),
the settlement-overrides-the-carry control, the never-two-sided case, and
the production-consequence case (`max_drawdown` is a verdict metric
accumulated per snapshot, so a 0.0 mark prints a ~24.0 drawdown that
never happened). Verified by mutation, three separate ones: reverting to
the 0.0 fallback reddens exactly three and leaves both controls green;
returning the carried mid without the side complement reddens exactly the
complementarity test; and short-circuiting the carry ABOVE the settlement
branch initially **SURVIVED** — the control set `result` from the start,
so no mid was ever carried and there was nothing to shadow, making the
assertion vacuous. Rewritten to follow the real production ordering
(book trades -> book clears -> sweep writes `result` hours later) it now
reddens exactly that test. Suite 356->361, ruff clean, pushed, and
**PROMOTED** — same call as 494a2ac and for the same reason:
`hyxlab-shadow` is a live daemon running `simulator.shadow`, which drives
`Simulator`, so this code is in production. Daemon back up 14:23:37 UTC
on d07d8e8, seeded from 792,517 archived events; stable worktree verified
at that commit. Validated in the real pipeline by replaying the SHIPPED
`_mark` over the 10 real positions, reproducing the probe exactly
(+51.44 vs +45.00 truth vs +0.00 shipped). **A NEW DATUM ON THE CLEAR
ITSELF, which the last pass's "top of the hour" reading does not cover**:
the archive now holds **36** clearing void rows, not 30, and the 6 extra
are a SECOND clear of the same KXHIGHDEN-26JUL30 ladders at **07:45:07**
— not a top-of-hour instant. The clear is therefore repeatable rather
than once-per-market, which the fix already handles (its idempotence test
covers exactly this) but which falsifies reading the burst as strictly
hourly. PRACTICAL RULE, joining `underlying_sign_p` /
`flagged_day_weighted` / `new_share_vs_all` /
`cross_bucket_overlap.groups` / `top_day_share` / connection-scoped-`seq`
/ own-(category,horizon): **handling a book-clearing frame for FILLS does
not handle it for VALUE. An empty book means no counterparty, not a
worthless position — any equity, PnL or `max_drawdown` read from a
shadow run between 494a2ac (07-31 08:20) and d07d8e8 (07-31 14:23) marks
every position in an expired market at zero for up to ~24h, and the
`mid is None -> 0.0` fallback is side-blind wherever it still fires.**
NEXT PASS: 08-01 ~02:15 UTC is weather bracket independent run #5, the
first carrying `underlying_sign_p` natively — per the retired data gate
the unit to pool is leaning UNDERLYINGS (5 over / 7 under, k=12, p=0.387
so far), not runs; the 08-01 11:10 UTC sweep re-opens the atlas gate for
a second reading of the replicated longshot-fade narrowing. Also open:
whether the ~05:00 clear of the 26JUL31 ladders lands cleanly under
d07d8e8 — that is the first clear the mark fix will see live, and shadow
currently holds positions in 30 such markets. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-07-31 08:35 UTC (THE CLASS-B VOID-ROW PREDICTION IS
CONFIRMED AND THE FRAME TYPE NAMES THE HOURLY BURST — AND CHASING WHAT
THAT FRAME *MEANS* FOUND THE SENTINEL BEING RECORDED BUT NOT ACTED ON:
REPLAY CARRIES A PHANTOM LADDER PAST EVERY CLEARED BOOK. Gate check
first, hard rather than estimated: atlas is data-gated until the 07-31
11:10 UTC kalshi sweep (`systemctl list-timers`: `hyxlab-sweep` next
06:10 CDT, ~2h54m out, last fired 07-30); weather bracket ran 07-31
02:16 UTC so independent run #5 is 08-01 ~02:15; econ needs >=336h (next
~08-10); QA fired 07-31 07:00 UTC. The one runnable item is the
discriminating instant the last three passes named — **07-31 04:59 UTC,
now ~3h in the past.** **THE PREDICTION HOLDS, AND CLASS B IS
IDENTIFIED.** The hourly bursts recurred exactly on schedule at
04:59:18, 05:59:18 and 06:59:18 UTC and every one of them landed as
`kind='void'` rows carrying a type — **`orderbook_snapshot`**, 30 rows
across the three bursts. So the discarded-frame explanation is complete
for the class that produces all the volume, no `SeqTracker`-invisible
loss exists, and there is no data-integrity finding. The type also names
what the burst IS: every market is a **26JUL30** ticker — the PREVIOUS
day's expired weather ladders (KXHIGHNY/MIA at 04:59, KXHIGHCHI/AUS at
05:59, KXHIGHDEN at 06:59). Kalshi wipes an expired daily market's book
at the top of the hour by sending a snapshot with empty levels. Note
this falsifies the 07-30 08:35 pass's supporting claim that "snapshots
appear only at connection start" — these are mid-run, interleaved with
live deltas (seq 499265 is a KXHIGHDEN-26JUL31 delta sitting between two
void rows). Harmless to that pass's conclusion, but the log was carrying
it. **THE FINDING, FOUND BY ASKING WHAT THE RECORDED FRAME MEANS RATHER
THAN STOPPING AT "THE HOLE IS CLOSED", AND IT IS THE LOAD-BEARING
HALF**: an empty snapshot is not an absence of information, it is a full
absolute image saying **the ladder is now empty** — and it is the one
book image that cannot be expressed as rows, because the writer emits
one row per level and there are no levels. 22c9556 made the archive
record that the frame arrived; nothing made replay act on it.
`BookReplayer.apply` fell through to `if e.kind != "delta": return None`,
so a void row was a complete no-op and the pre-clear ladder replayed
forever. **PROBED BEFORE BUILDING, against the live archive**: for all
30 markets the last non-void row PRECEDES the void instant and there are
**zero** non-void rows after it, so the clear is terminal — and replaying
the shipped code over them left **30 of 30 carrying a phantom ladder,
1328 phantom levels in total**, e.g. `KXHIGHNY-26JUL30-B79.5` showing a
6-level NO ladder with **79 contracts resting at 0.99** for a book that
had been wiped and never traded again. That is a fill-simulation hazard,
not a display nit: any maker consumer resting against that ladder trades
with a counterparty that does not exist. HARDENING SHIPPED (494a2ac): a
void row whose type is `orderbook_snapshot` now clears the book and
emits the resulting empty top, so consumers SEE the clear; the fix
**clears rather than `invalidate()`s**, and that distinction is the
design call — an empty ladder is a KNOWN state, not broken coverage, so
the market stays seeded and a market whose book opens EMPTY is tracked
from there instead of discarding every delta until the next reconnect
image. Sequenced control acks and legacy `''` rows stay no-ops. Five
regression tests; the load-bearing one asserts the emitted top is empty
on BOTH sides and that a following delta builds from empty rather than
from the phantom ladder, so a fires-but-does-nothing implementation
fails on the NUMBERS, not on a missing emit — plus the discrimination
control (a control ack must NOT wipe a live book, so the tier keys on
frame type rather than on `kind='void'`), the seed-from-empty case, the
idempotence case, and the `replay_snapshots` row-group case (a void row
is a complete image alone and must emit inline, not wait for a finalize
that never comes). Verified by mutation, three separate ones: dropping
the void branch reddens exactly four and leaves the control green;
firing on any `kind='void'` regardless of type reddens exactly the
control; and `invalidate` semantics (unseed instead of clear) reddens
exactly the three that assert the book survives as empty. Suite
351->356, ruff clean, pushed, and **PROMOTED** — and this one is a
DEPARTURE from the standing "sim-side, no promote" call used for atlas
and queuescore: `hyxlab-shadow` is a live daemon running
`simulator.shadow`, which calls `replay_snapshots`, so the code is in
production. Daemon back up 08:20:04 UTC on 494a2ac; stable worktree
verified at that commit. Validated in the real pipeline by replaying the
SHIPPED code over the same 30 markets: **30/30 phantom -> 0/30**.
**AN HONEST LIMITATION ON THE OTHER HALF**: the seed-from-empty gain
cannot act on anything currently in the archive. All 6 class-A void rows
were written 07-30 08:26-11:49, BEFORE 22c9556 recorded frame types, so
they carry `''` and are correctly unattributable. The gain is real but
prospective — measured on the class-A markets, `KXCPI-26OCT-T0.1` had 4
deltas and `KXCPIYOY-26NOV-T3.6` had 102 deltas discarded between the
empty snapshot and the next reconnect image (~5.3h of coverage), and the
next class-A occurrence recovers them. PRACTICAL RULE, joining
`underlying_sign_p` / `flagged_day_weighted` / `new_share_vs_all` /
`cross_bucket_overlap.groups` / `top_day_share` / connection-scoped-`seq`
/ own-(category,horizon): **recording a frame is not handling it. A void
row of type `orderbook_snapshot` is a book-CLEARING event; any replay or
depth reading taken before 494a2ac carries the pre-clear ladder forward
indefinitely for every expired daily market, and `seq` continuity being
green says nothing about whether the book state is real.** NEXT PASS:
the 07-31 11:10 UTC sweep re-opens the atlas gate for the first reading
whose strictest tier is `flagged_day_weighted` — the standing longshot-
fade narrowing is the thing to re-test; 08-01 ~02:15 UTC is weather
bracket independent run #5, the first carrying `underlying_sign_p`
natively, and per the retired data gate the unit to pool is leaning
UNDERLYINGS (5 over / 7 under, k=12, p=0.387 so far), not runs. Also
worth a pass: whether any ARCHIVED report or shadow run was computed
against a phantom ladder. Untracked `strategies/hylshi_fade.py`
re-confirmed present, still correctly left alone per the 07-18
provenance resolution.)**
(prior 2026-07-31 02:35 UTC (24TH WEATHER MAKER BRACKET, INDEPENDENT
RUN #4 — AND CHASING WHAT CERTIFIED IT FOUND THAT NO DIRECTIONAL READING
IN THE ENTIRE 34-RUN BRACKET ARCHIVE IS DISTINGUISHABLE FROM A COIN
FLIP. TWELFTH INSTANCE OF THE UNIT-OF-COUNTING CLASS, THIS TIME AS A
POWER CEILING RATHER THAN A WRONG UNIT. Gate check first, hard rather
than estimated: the prior weather bracket ran 07-30 02:15:23 UTC and this
run fired 02:16:26 UTC, so the >~24h expiry-crossing rule was satisfied to
the minute — and the report confirms it rather than my arithmetic
(`new_share_vs_all: 1.0`, 207/207 against 18 comparable priors, all
26JUL30 markets). Atlas is data-gated until the 07-31 11:10 UTC kalshi
sweep; econ needs >=336h (next ~08-10); QA next fires 07-31 07:00 UTC and
will still be RED on pre-restart residue as predicted; the class-B void-row
discriminating instant (07-31 04:59 UTC) is ~2.5h away and UNCHANGED —
read it at ~08:00+ UTC per the last pass. **THE RUN**: 207 virtual orders
across 8 markets (KXHIGHNY 85, KXHIGHCHI 78, KXHIGHMIA 44), crossing
**129 vs queue [138 pess, 144 opt]** — 9 orders BELOW the pessimistic
floor, `direction_underlying_robust: true` (2 under / 1 over),
`abs_net_by_underlying` 13 against `net_disagreement` -9 so this run is
mostly agreement rather than cancellation. Report:
`reports/maker_bracket/20260731T021626.json`. Read naively that is the
second robustly-UNDER certified-independent reading and would be the
strongest directional evidence in the archive. **IT IS NOT, AND THAT IS
THE FINDING.** `direction_*_robust` is a strict-MAJORITY test over the
leaning units, and a majority is not a measurement: with an ODD number of
leaning units a strict majority ALWAYS exists, so at the default
`--markets 8` — which reaches only ~3 city-days — the tier can only fail
when the aggregate sign contradicts the unit majority. Today's
certification is 2-of-3, whose one-sided sign-test p is **exactly 0.50**.
PROBED BEFORE BUILDING, by rehydrating `orders_detail` from all 34
archived reports: `robust` fires on **24 of 34** runs, **10 of them at p
exactly 0.50**, and **not one run of the 34 reaches p <= 0.05**. The
sharper half: **31 of the 34 were underpowered BY CONSTRUCTION** — the p
they would have produced had every underlying agreed already exceeded
0.05 before any data was read (at k=3 the ceiling is 2^-3 = 0.125). Only
3 runs in the whole archive ever had a reachable ceiling, all econ, and
none attained it. HARDENING SHIPPED (fcbded3): both tiers now carry
`*_sign_p` (one-sided binomial on the leaning units), `*_min_sign_p` (the
run's power ceiling) and `direction_*_significant` at alpha 0.05;
`robust` and the net-split fields are untouched for cross-report
comparability per the divergence-matcher / atlas-day-tier / overlap-tier
precedent — unlike the QA seq headline this is a coarser VALID bound, not
an artifact, so it is kept rather than replaced. The market tier is
routed through `_direction_tier` so both tiers share one implementation.
Five regression tests; the load-bearing one asserts BOTH halves on the
production shape (3 city-days, 1 over / 2 under: `robust` must still read
True — proving it is the thing being corrected, not something already
strict — while `sign_p` reads 0.50 and `min_sign_p` reads 0.125, so the
ceiling is shown to be a property of the run's WIDTH and not of how it
leaned), plus the power-ceiling case (a UNANIMOUS 3-underlying run is
still not significant), the discrimination control (6 unanimous
underlyings give 0.015625 and DO certify, so the tier is not merely
always-false), the market tier, and the no-direction case. Verified by
mutation, three separate ones: collapsing `significant` to the bare
majority reddens exactly the three tier tests and leaves the
discrimination control green; dropping the aggregate-direction guard on
`sign_p` reddens exactly the undirected test; reporting `min_sign_p` as
the observed p rather than the ceiling initially SURVIVED — both ceiling
fixtures were unanimous, so ceiling and observed coincided — which is why
the non-unanimous assertion was added, and it now reddens exactly that
test. Suite 346->351, ruff clean, pushed. No promote — queuescore is
sim-side, no timer runs it (verified against `scripts/systemd/`).
Validated in the real pipeline by replaying the SHIPPED function over the
whole archive report-by-report, reproducing the probe exactly. The
archived `20260731T021626.json` is deliberately NOT rewritten to carry
the new fields — reports are immutable inputs to `independence_vs_prior`,
same call as the 07-29 event-tier and 07-30 `new_share_vs_all` decisions;
the next run carries it. **WHAT THIS DOES TO THE STANDING DATA GATE, AND
IT RETIRES IT AS WRITTEN**: the gate was "accumulate certified-independent
weather brackets and re-test the over-award lean at n>=8 RUNS". That
counts the wrong unit one more time — each run's direction is itself a
coin flip, so eight of them is eight coin flips. Pooling the leaning
underlyings across the four certified-independent runs instead gives **5
over / 7 under, k=12, p=0.387** — no direction, and note it is the honest
instrument that says so rather than a majority vote. Reaching even 50%
power against a 60/40 bias needs ~78 pooled underlyings: ~26 more runs at
3 city-days, or ~16 at `--markets 15` (measured against the live archive:
top-15 reaches 5 city-days, top-23 reaches 8). Widening top-N changes the
scored population and therefore STARTS A NEW comparability series rather
than extending this one — that is a real cost and the reason it is named
here as a decision rather than silently applied. **A CORRECTION TO THIS
LOG'S OWN NARRATIVE, surfaced by replaying the strictest tier over the
history**: the 07-29 pass wrote that "two certified-independent readings
now exist and NEITHER shows over-award". Run #1 (`20260727T151833`) was
assessed at the MARKET tier because the underlying tier shipped two days
later; at the underlying tier it reads `robust: true` with agg +1 — an
OVER lean. The certified-independent sequence is therefore OVER(p=0.50) /
UNDER(p=0.31) / undirected(p=0.75) / UNDER(p=0.50), not the clean
under-lean the log has been carrying. Across all 24 robust runs the split
is 17 over / 7 under, which superficially revives the raw over-award
tally — and every one of those readings has p >= 0.0625, so it revives
nothing. PRACTICAL RULE, joining `flagged_day_weighted` /
`new_share_vs_all` / `cross_bucket_overlap.groups` /
`direction_underlying_robust` / `top_day_share` / connection-scoped-`seq`
/ own-(category,horizon): **read `underlying_sign_p` and
`underlying_min_sign_p`, not `direction_underlying_robust`, before
calling any bracket over/under verdict — a bare majority of 3 underlyings
is p=0.50, and when `min_sign_p` > 0.05 the run could not have shown a
direction whatever the data did. Reports written before fcbded3 carry no
sign fields; every "robust" directional reading this log has reported,
including today's, is a coin flip.** NEXT PASS: **07-31 04:59 UTC remains
the discriminating instant for class-B void rows** — read at ~08:00+ UTC,
not off the 07:00 QA run which is still red on pre-restart residue; then
the 07-31 11:10 UTC sweep re-opens the atlas gate for the first reading
whose strictest tier is `flagged_day_weighted`. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-07-30 20:55 UTC (THE VOID-ROW PREDICTION IS VERIFIED
FOR ONE HOLE CLASS AND UNTESTED FOR THE ONE THAT PRODUCES ALL THE
VOLUME — AND CHASING IT FOUND THAT THE FIX ITSELF *INVERTED* THE
DETECTION IT CLAIMED TO ADD. Gate check first, hard rather than
estimated: the kalshi sweep last fired 07-30 11:10 UTC and atlas ran
14:20 UTC after it, so atlas is data-gated until the 07-31 11:10 UTC
sweep (`systemctl list-timers`); weather bracket ran 07-30 02:15 UTC and
needs >~24h to cross a daily expiry, so 07-31 ~02:15 (independent run
#4); econ needs >=336h (next ~08-10); QA next fires 07-31 07:00 UTC;
divergence unchanged — shadow run 20260722T081852 still open. Nothing
standing was runnable, so the 08:35 pass's FALSIFIABLE PREDICTION was
probed ~14h early instead of waiting for the 07-31 QA run. **RESULT:
THE MECHANISM IS CONFIRMED, BUT ONLY FOR HALF THE PROBLEM.** Post-
restart the check reads clean — a window starting after the 08:26
restart gives `0 missing seq in 0 hole events` — and the proof that void
rows are what closed it, rather than a coincidentally clean wire, is a
re-run with `kind <> 'void'` excluded: **exactly 4 one-seq holes
reappear, at exactly the 4 void-row timestamps** (09:42:01, 11:49:09).
**BUT THE 72h HOLE HISTORY SPLITS INTO TWO CLASSES AND ONLY ONE HAS
BEEN TESTED.** Class A is connection-start, `seq 34->36` and `36->38`,
one per run — these are the empty-ladder CPI snapshots, and the 6 void
rows written so far are exactly this class (seq 35/37/104,
`KXCPI-26OCT-T0.1` etc). Verified closed. Class B is MID-RUN bursts of
4-12 seq inside a ~50ms window, and it is where every large hole count
in this log came from: 07-28/29/30 all show it at **04:59:19, 05:59:19
and 06:59:19 UTC and nowhere else** (plus two 07-29 outliers at 17:55:19
and 18:07:41), repeatable to the second across days. Since the 08:26
restart the archive has covered 09:00-20:15 UTC — eleven hourly
opportunities and both outlier times — and produced **zero class-B
events and zero class-B void rows**. So the class that produces all the
volume has simply not recurred yet and the prediction is UNTESTED for
it. **The first real test is 07-31 04:59 UTC**, not the QA run: QA at
07-31 07:00 will still be RED regardless, because its 26h window still
reaches back to the pre-restart 07-30 05:59/06:59 holes. The clean read
is 07-31 ~08:00+ UTC. Ruled out the obvious class-B explanation against
the archive rather than assuming — `parse_message` returns on
`typ == "trade"` BEFORE `_void()` exists, so a trade frame on the books
connection would leave a hole the fix cannot close; but the trade rows
whose `seq` fall inside each class-B hole have `recv_ts` scattered
across the whole month, i.e. they are the trades connection's own
counter colliding by coincidence. Confirms the 08:35 pass's separate-
connection finding; class B remains unidentified. **THE FINDING, AND IT
IS THE LOAD-BEARING ONE, FOUND BY ASKING WHO READS THE SENTINEL**:
nothing did. `grep` for `void` outside the writer returns only the
writer. So fc628fc's own stated rationale — "a void row also makes a
Kalshi schema change loud" — was never implemented, and the rows carried
no frame type either, so an empty snapshot, a control ack and an
unrecognised NEW frame type all wrote the identical row. That is not
merely a missing feature, it is an **INVERSION**: before the fix, a
frame type this parser does not understand left a seq hole and turned
the QA seq check RED; after it, that frame writes a void row, the seq
check reads GREEN, and no other check looks. The fix traded a detectable
failure for an invisible one while asserting the opposite. HARDENING
SHIPPED (22c9556): the void row now carries the frame's `type` in
`side` (meaningless for a row archiving no book level, so it is the free
column), and QA gains `void frames are known types`, alarming on any
type outside the benign set (empty `orderbook_snapshot` plus sequenced
control acks). Legacy void rows carry `''` and are counted but
unattributable, so they cannot red the check and they roll out of the
26h window on their own. Four regression tests; the load-bearing one
asserts BOTH halves on one fixture — the seq check must stay SILENT on
an unknown frame type (proving it is blind, not redundant) and the void
check must fire naming the type — plus a discrimination control (the
empty snapshot that actually occurs in production must stay benign, so
the check is not merely always-red) and the legacy-`''` case. Verified
by mutation: reverting `side` to `''` reddens exactly the two
type-recording tests and leaves the control and legacy tests green;
counting `''` as unknown reddens exactly the legacy test. Suite
342->346, ruff clean, pushed, and **PROMOTED** — this is collector-side,
`hyxlab-qa` runs from the stable worktree and `hyxlab-stream` needed the
restart to record frame types (daemon back up 20:21:35 UTC). Live
reading, and note it is deliberately NOT a manufactured green: `PASS
void frames are known types — 6 void frames (6 legacy unattributed); all
known`, alongside the still-red seq check (`30 missing seq in 4 hole
events, 4 unexcused`) which is the expected pre-restart residue. No new
void row had been written in the 2 min after the restart, so attribution
lands on the next occurrence — definitively at 07-31 04:59 UTC if class
B is a void producer at all. PRACTICAL RULE, joining
`flagged_day_weighted` / connection-scoped-`seq` / `new_share_vs_all` /
`cross_bucket_overlap.groups` / `direction_underlying_robust` /
`top_day_share` / own-(category,horizon): **a green seq check is not
evidence the parser understands the wire — read `void frames are known
types` alongside it, because every frame the parser fails to recognise
now lands there silently instead of reddening the seq check. Void rows
written before 22c9556 carry `''` and can never be attributed.** NEXT
PASS: **07-31 04:59 UTC is the discriminating instant** — if class-B
frames appear as void rows carrying a type, the discarded-frame
explanation is complete and the type NAMES what the hourly burst is; if
they appear as seq holes again, they are loss `SeqTracker` cannot see
and that is the first data-integrity finding. Read it at ~08:00+ UTC,
not off the 07:00 QA run, which is still red on pre-restart residue.
Also due: 07-31 ~02:15 weather bracket (independent run #4, first
carrying `new_share_vs_all` natively) and the 07-31 11:10 sweep
re-opening the atlas gate — the first atlas reading whose strictest tier
is `flagged_day_weighted`. Untracked `strategies/hylshi_fade.py`
re-confirmed present, still correctly left alone per the 07-18
provenance resolution.)**
(prior 2026-07-30 14:25 UTC (ATLAS ON THE FIRST INCREMENT THAT
TOUCHED EVERY (CATEGORY, HORIZON) — THE STRICT TIERS DROPPED, AND
CHASING THAT FOUND THE ELEVENTH INSTANCE OF THE UNIT-OF-COUNTING CLASS
*INSIDE THE DAY TIER ITSELF*. THE STANDING SIGNATURE CLAIM SURVIVES BUT
LOSES HALF ITS DIRECTION. Gate check first: the kalshi sweep fired 11:10
UTC (3h prior, confirmed against `systemctl list-timers`), so the atlas
gate named last pass had OPENED and atlas was the one runnable standing
report. QA next fires 07-31 07:00 UTC, so the void-row prediction from
the 08:35 pass is still GATED and unchecked; weather bracket next ~07-31
02:15 (independent run #4); econ needs >=336h (next ~08-10); divergence
unchanged — shadow run 20260722T081852 still open. **THE RUN IS THE
FIRST GENUINELY BROAD INCREMENT IN THE LOG**: every (category, horizon)
gained, including `Financials|24h` +97 — the population the 07-29 pass
called "structurally frozen", which moved for the first time. Headline
flagged 91->91 (28 groups, flat), but the strict tiers FELL: robust
67->63 (28->25 groups), day-robust **16->13** (11->8 groups). Three
Financials mid-decile buckets were demoted (1h d5, 6h d5, 6h d6), all
because their gap SHRANK on a ~10% increment. Report:
`reports/atlas/20260730T141519.json`. **PROBED BEFORE BUILDING**, by
breaking Financials 6h d5 down by settlement day: the entire increment
is 07-29, 106 markets at implied 0.543 that realized **0.028**. And the
day column is near-bimodal — 07-28 reads 0.966, 07-23 reads 0.015,
07-21 reads 0.982 — i.e. the index went one way that day and every
mid-decile ladder resolved with it. That is the day-correlation the day
tier was built for, and it fired correctly. **BUT LOOKING AT THE TIER'S
OWN ARITHMETIC IS WHERE THE FINDING IS**: `flagged_day_robust` is
`wilson(realized * days, days)` (atlas.py:302) — it takes its SAMPLE
SIZE from days but its POINT ESTIMATE from markets, since `implied` and
`realized` are both market-weighted. So a 106-market day outvotes a
1-market day 106:1 in the mean while both count as a single draw in n.
The tier uses the day model for the variance and the market model for
the mean, which is precisely the correlation it exists to bound.
MEASURED over the 13 day-robust survivors: re-weighting both sides so
each day contributes once shrinks **Financials 24h d8 from +0.1289 to
+0.0208 (6.2x)** and **Economics 1h d6 from +0.1453 to +0.0444 (3.3x)**;
no survivor flips sign. Note the direction — the defect is NOT uniformly
conservative, it INFLATES a gap wherever the largest days happen to
agree with the signature. HARDENING SHIPPED (8a6ac3c): every bucket now
carries `implied_day_weighted`, `realized_day_weighted` and a
`flagged_day_weighted` tier — the day tier with the same unit on both
sides. `flagged_day_robust` and the market-weighted `implied`/`realized`
are untouched for cross-report comparability, per the divergence-matcher
/ day-tier / overlap-tier / bracket-concentration precedent; unlike the
QA seq headline this one is a coarser VALID measure, not an artifact, so
it is kept rather than replaced. Four regression tests on a fixture with
the production shape (one 300-market all-yes day plus 40 five-market
days settling at exactly the implied 0.60): the load-bearing one asserts
the day-weighted gap is +0.0098 where the market-weighted gap is +0.24,
so a bug-preserving implementation fails on the NUMBER, not on a missing
key — verified by mutation, swapping `avg(day_*)` for the
market-weighted sum reddens exactly that test and the field-level one
and leaves the others green; the discrimination control (the same 0.84
realized spread evenly over 50 days must KEEP the flag, so the tier
measures day balance rather than being merely always-stricter); and tier
nesting. Suite 338->342, ruff clean, pushed. No promote — atlas is
sim-side, no timer runs it (verified against `scripts/systemd/`).
**THE RESULT, AND IT IS THE LARGEST NARROWING THE STANDING CLAIM HAS
TAKEN**: 13 day-robust -> **5 day-weighted (8 -> 4 groups)**, report
`reports/atlas/20260730T142000.json`. Counter-signature survivors:
still **ZERO**, so the claim itself holds. But every one of the 5
survivors is a LOW decile with a NEGATIVE gap — Economics 1h d2/d3/d4,
Financials 1h d2, Financials 6h d2 — and **every high-decile survivor is
demoted** (Financials 24h d7/d8, 6h d7/d8, Economics 1h d6). So at the
strictest tier available only the LONGSHOT half of the favorite-longshot
signature survives: longshots realize below implied. The "favorites
realize above implied" half was carried by large days and does not
survive day-weighting. That is a directional narrowing that matters for
any strategy lead — the tradeable half of this signature in this archive
is FADING LONGSHOTS, not backing favorites. It is not a verdict: 4
groups is a small sample and no pre-registration exists. PRACTICAL RULE,
joining `cross_bucket_overlap.groups` / own-(category,horizon) /
`top_day_share` / `direction_underlying_robust` / `new_share_vs_all` /
connection-scoped-`seq`: **read `flagged_day_weighted`, not
`flagged_day_robust`, as an atlas bucket's strictest surviving tier —
the day tier's market-weighted mean overstates a high-decile gap by up
to ~6x. Reports written before 8a6ac3c carry no day-weighted field at
all, and every "day-robust" high-decile finding this log reported is
weaker than it reads.** NEXT PASS: the 07-31 07:00 UTC QA run is the
falsifiable void-row prediction from the 08:35 pass — green means the
discarded frames were the whole explanation, still-red means real loss
`SeqTracker` cannot see, which would be the first data-integrity
finding; the 07-31 ~02:15 UTC weather bracket is independent run #4, the
first carrying `new_share_vs_all` natively. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-07-30 08:35 UTC (THE QA SEQ CHECK FAILED FOR THE FIRST
TIME SINCE IT WAS HARDENED — AND THE FAILURE IS AN ARTIFACT OF THE FIX,
NOT A DAEMON FAULT. TENTH INSTANCE OF THE UNIT-OF-COUNTING CLASS, THIS
TIME INSIDE THE REPAIR SHIPPED FOR THE SIXTH. Gate check first: atlas
still data-gated until the 07-30 11:10 UTC kalshi sweep (the timer last
fired 07-29 06:10 CDT, confirmed against `systemctl list-timers`); the
weather bracket ran 07-30 02:15 UTC so independent run #4 is 07-31
~02:15; econ needs >=336h (next ~08-10); divergence unchanged — shadow
run 20260722T081852 still open. QA DID fire 07-30 07:00 UTC and it is
the one fresh standing report: **`FAIL book seq contiguous or
gap-marked — 70 seq holes over 8 connection runs, 14 gap rows, 1
unexcused runs`**, the first non-PASS the check has ever produced.
**FINDING 1 — THE FAIL IS THE OPEN RUN, AND THE CHECK IS STILL
VACUOUS FOR EVERY OTHER RUN.** Replayed the shipped query at the exact
QA instant: the single unexcused run is run 7, the connection that was
still LIVE at 07:00:03 with no terminating gap row yet. Re-running the
identical check at 08:15 reads it EXCUSED, because its reconnect landed
at 07:00:18 — the alarm self-cleared without anything being fixed. The
mechanism is that a556b31 scoped excusal to the RUN: a gap row
overlapping any part of a run pardons every hole in it. Every completed
run ends in a logged reconnect whose gap row touches the run's endpoint,
so **every completed run is pardoned wholesale** — verified on the live
archive, run 6's 22 holes occur at 17:55:19 and 18:07:42 and are
pardoned by the 21:29:36 reconnect, 3.4h later. So the only run that can
ever fail is the open one, and it fails spuriously. The 07-29 pass's
reassuring `0 unexcused runs` is void for the same reason. A second,
independent scoping defect sits beside it: the gap query pulls EVERY gap
row in the window, so a polymarket or kalshi-trades reconnect excuses a
kalshi-books hole — 3 of the 13 rows in this window are foreign.
**FINDING 2, FOUND BY ASKING WHAT A HOLE IN `book_events` ACTUALLY
MEANS, AND IT IS THE LOAD-BEARING ONE.** Under point-scoped,
channel-scoped excusal the honest reading is **70 missing seq in 9 hole
events, ALL unexcused** — not one gap row covers a single real hole;
they all sit at run boundaries where seq resets, which is not a hole at
all. But the daemon is not losing data: **`SeqTracker` observes every
frame on the wire and writes a `seq_gap` row plus forces a reconnect on
any discontinuity, and there is not one `seq_gap` row in the 26h
window** — all 13 are `reconnect`/`daemon_start`. The wire was
contiguous. So those frames ARRIVED and parsed to zero archived rows:
`parse_message` returns `([], [])` for control frames and for an
`orderbook_snapshot` whose ladders are empty, and their seq is consumed
and never recorded. Ruled out the alternatives against the archive
rather than assuming: the missing seq are absent under every sid (Kalshi
is sid=1 only, poly is sid NULL), they are not trades (the trades
channel is a separate connection, its seq was 2.3M while books was
138k), and they are not mid-run snapshots (snapshots appear only at
connection start — 441 frames at 17:29, 400 at 07:00, zero in between).
**A hole in `book_events` is therefore not evidence of loss, and the
check has been asserting a property of the WIRE by counting ARCHIVED
ROWS — which differ by exactly the frames the parser discards.**
HARDENING SHIPPED, both halves: (f91517b) excusal is now scoped to the
HOLE's own interval and to the owning venue/channel, and the reported
unit becomes unexcused hole events plus missing-seq count — the old
headline is an artifact, not a coarser valid measure, so it is replaced
outright rather than preserved for comparability, same call as a556b31
and unlike the atlas/queuescore tier precedent; (fc628fc) every frame
carrying sid/seq that archives no book row is now written as a
`kind='void'` row, so the archive records the full wire sequence.
Replay filters on snap/delta and ignores void; the sentinel also makes a
Kalshi schema change loud, since a new frame type swallowed by
`parse_message` would otherwise just silently thin book capture. Five
regression tests: the run-terminating-gap case and the foreign-channel
case (both RED on the old code); void rows for a sequenced control frame
and for an empty snapshot; and the integration one that shows a void row
closing the hole it explains. Suite 333->338, ruff clean, pushed, and
**PROMOTED** — this is collector-side, `hyxlab-qa` runs from the stable
worktree and `hyxlab-stream` needed the restart to pick up the parse
change (daemon back up 08:26:18 UTC). **THE CHECK IS DELIBERATELY RED
RIGHT NOW AND THAT IS THE POINT**: run against the live archive it reads
`58 missing seq in 8 hole events, 8 own-channel gap rows, 8 unexcused`,
reproducing the probe exactly. No historical hole carries a void row, so
QA will FAIL until the 26h window rolls past the 08:26 restart. **THIS
IS A FALSIFIABLE PREDICTION, NOT A KNOWN-GOOD STATE**: if the discarded
frames are the whole explanation, void rows fill the holes and QA goes
green by ~07-31 10:30 UTC on its own. If it stays red past that, the
remaining holes are real loss that `SeqTracker` cannot see, which would
be a genuine data-integrity finding and the first evidence of one.
PRACTICAL RULE, joining connection-scoped-`seq` / `new_share_vs_all` /
`cross_bucket_overlap.groups` / `direction_underlying_robust` /
`top_day_share` / own-(category,horizon): **a gap in archived `seq` is
not a gap on the wire — read `seq_gap` gap rows for wire loss, and treat
`book_events` seq holes as unexplained frames until a void row accounts
for them. Every "unexcused runs" figure this log reported on 07-29 is
void.** NEXT PASS: check whether QA cleared on its own at 07-31 07:00
UTC per the prediction above; the 07-30 11:10 UTC sweep opens the atlas
gate for the first reading measured against a reproducible `implied`
baseline with survivors read as `groups`; the 07-31 ~02:15 UTC weather
bracket is independent run #4, the first carrying `new_share_vs_all`
natively. Untracked `strategies/hylshi_fade.py` re-confirmed present,
still correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-07-30 02:35 UTC (23RD WEATHER MAKER BRACKET, THE
INDEPENDENT RUN #3 THE LAST FIVE PASSES BUILT TOWARD — AND THE PREDICTED
SIGNAL DOES NOT APPEAR. NINTH INSTANCE OF THE UNIT-OF-COUNTING CLASS,
THIS TIME IN THE INDEPENDENCE CERTIFICATE ITSELF. Gate check first: the
prior weather bracket ran 07-29 02:15:53 UTC and the run fired 02:15:11
UTC today, so the >~24h expiry-crossing rule was satisfied to the second
— and the report confirms it rather than my arithmetic (`new_share: 1.0`,
251/251, all 26JUL29 vs the prior run's 26JUL28). Atlas is data-gated
until the 07-30 11:10 UTC kalshi sweep; econ needs >=336h (next ~08-10);
QA next fires 07:00 UTC; divergence unchanged — shadow run
20260722T081852 still open. **THE RUN, AND THE PREDICTION FAILS
CLEANLY**: 251 virtual orders across 8 markets (KXHIGHNY 99, KXHIGHMIA
93, KXHIGHCHI 59), crossing **153 vs queue [147 pess, 167 opt]** —
INSIDE the bounds, not the second under-award the last pass named as the
first real directional signal in 23 runs. At the strictest tier it is
also UNDIRECTED: 3 underlyings, 1 over (CHI +9) / 1 under (MIA -3) / 1
tied (NY 0), `direction_underlying_robust: **false**` — and note the two
tiers openly disagree this run (`direction_market_robust: true` off 4
over / 3 under / 1 tied), which is the event tier earning its keep on
live data for the first time. `top_underlying_net_share` 0.75, and
`abs_net_by_underlying` 12 against `net_disagreement` 6, so half the
aggregate is cancellation. Report:
`reports/maker_bracket/20260730T021523.json`. **SO THE INDEPENDENT
SEQUENCE IS NOW n=3 AND CARRIES NO DIRECTION**: #1 07-27 15:18 (agg +1,
undirected), #2 07-29 02:15 (agg -9, robustly UNDER), #3 today (agg +6,
inside, undirected). The archive's raw +16/-6 over-award tally still has
zero support from any reading allowed to count, and the n>=8 data gate
stands unchanged — one over, one under, one inside is exactly what noise
looks like. **THE FINDING, FOUND BY ASKING WHAT "INDEPENDENT" CERTIFIES**:
`independence` compares this run's orders against the SINGLE most recent
comparable report (`queuescore.py:112`, a `break`). But the scored market
set is only the top-N by print count (`select_markets`), and that set
CHURNS — a strike that drops out of one run's top-N and returns in the
next reads as fresh evidence against the immediate prior while an older
run already counted it. "New since the last run" is not "never scored
before". PROBED BEFORE BUILDING, by rehydrating `orders_detail` from all
34 archived reports and diffing pairwise-vs-prior against
vs-union-of-all-priors: **07-24 econ reads `new_share` 0.265 where the
honest figure is 0.137 (1.93x), 07-26 econ reads 0.206 vs 0.115
(1.79x)**, and the mechanism is confirmed order-by-order — 262
`KXCPI-26JUL-T-0.1` orders on 07-24 and 198 `KXCPIYOY-26JUL-T3.5` orders
on 07-26 were absent from the immediate prior's top-N but present in an
older run. A small second mechanism sits underneath it: order placement
is a stateful arm/cooldown walk seeded from the window start
(`next_arm = since`), so a different `since` re-phases orders within a
market that both runs scored — 2-3 orders per run, same direction,
negligible. **WHAT IT DOES NOT TOUCH**: every weather run reads 1.000 on
both tiers, so runs #1/#2/#3 are certified independent at the strictest
tier available and nothing above is weakened. It is the ECON track that
was over-credited — and note the 07-27 pass hand-computed the right
11-14% figure, so the defect is that the SHIPPED instrument disagrees
with that pass's own analysis and a future pass reading the field would
have over-credited econ novelty. HARDENING SHIPPED (eae740c):
`independence` now carries `priors_compared`, `orders_new_vs_all` and
`new_share_vs_all`, unioning every comparable prior; `new_share` and
`prior_report` untouched for cross-report comparability per the atlas
day-tier / overlap-tier and bracket concentration-tier precedent. Three
regression tests: the top-N churn case (a strike scored by an older run
but dropped by the immediate prior must read 1.0 on `new_share` and 0.2
on `new_share_vs_all` — so a bug-preserving implementation fails on the
number, not just on a missing key); the discrimination control (a run
sharing nothing with any prior stays 1.0 on BOTH tiers, so the tier is
not merely always-lower); and series-scoping of the union, since pulling
weather orders into an econ union would suppress real econ novelty. Suite
330->333, ruff clean, pushed. No promote — queuescore is sim-side, no
timer runs it (verified against `scripts/systemd/`). Validated in the
real pipeline by replaying the SHIPPED function over the whole archive
report-by-report, reproducing the probe exactly; today's run reads
`new_share_vs_all` 1.000 against all 17 comparable priors. The archived
`20260730T021523.json` is deliberately NOT rewritten to carry the new
fields — reports are immutable inputs to `independence_vs_prior`, same
call as the 07-29 event-tier decision; the next run carries it.
PRACTICAL RULE, joining `direction_underlying_robust` /
`cross_bucket_overlap.groups` / `observations_by_category_horizon` /
`top_day_share` / connection-scoped-`seq`: **read `new_share_vs_all`,
not `new_share`, before calling a bracket an independent reading —
pairwise novelty overstates econ novelty by up to ~1.9x. Reports written
before eae740c carry only the inflated field.** NEXT PASS: the 07-30
11:10 UTC sweep opens the atlas gate for the first reading whose drift
is measured against a reproducible `implied` baseline (post-7462e5c) and
whose survivor counts are read as `groups`; the 07-31 ~02:15 UTC weather
bracket is independent run #4, the first to carry `new_share_vs_all`
natively, and the first that could put any direction back on the board.
Untracked `strategies/hylshi_fade.py` re-confirmed present, still
correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-07-29 20:45 UTC (EVERY STANDING REPORT GATED, SO THE
UNIT-OF-COUNTING LENS WENT WHERE IT HAD NEVER BEEN POINTED — *ACROSS*
ATLAS BUCKETS — AND FOUND TWO THINGS, THE SECOND ONE UNDERMINING THE
DRIFT METHOD ITSELF. Gate check first: atlas ran 14:18 UTC and is
data-gated until the 07-30 11:10 UTC kalshi sweep (confirmed hard, not
estimated: all 260 buckets' `n` are unchanged since that report);
weather bracket ran 07-29 02:15 UTC and per the spacing rule needs
>~24h to cross a daily expiry, so 07-30 02:15; econ needs >=336h (next
~08-10); QA fired 07:00 UTC; divergence unchanged — shadow run
20260722T081852 still open. Nothing runnable, so ladder rung 4.
**FINDING 1, AND IT IS THE EIGHTH INSTANCE OF THE CLASS**: atlas has
three correlation tiers (n -> clusters -> days) and every one of them
bounds correlation *within* a bucket. Nothing bounded it BETWEEN
buckets, and the horizon dimension duplicates evidence by construction
— a market enters up to five horizon buckets and its `result` is the
SAME in all of them. PROBED BEFORE BUILDING: 89,371 settled markets
produce 205,602 bucket observations (2.3x reuse), Climate and Weather
1h d0 and 6h d0 share **98.8%** of the smaller bucket's markets, and
among the 16 day-robust survivors Financials 24h d8 / 6h d8 share 58%.
So every headline this log has reported for weeks — 87/93/91 flagged,
59/67 robust, 12/16 day-robust — is a count of BUCKETS presented as a
count of findings. HARDENING SHIPPED (705273e): `cross_bucket_overlap`
unions survivors sharing >=30% of the smaller bucket's markets and
reports `groups`. **91 flagged -> 28 groups, 67 robust -> 28, 16
day-robust -> 11.** Share is measured against the SMALLER bucket
because a small bucket wholly inside a large one is fully redundant
however small a fraction of the large one it is; `groups` is a LOWER
bound and `buckets` an UPPER bound, since union-find is transitive and
pairwise-linked adjacent deciles collapse even where the chain's ends
share nothing (the Commodities d0-d4 group). Same standing as the day
tier: a bound, not an estimate. Headline/bucket fields untouched for
comparability per the divergence-matcher / atlas-day-tier /
bracket-concentration precedent. **THE STANDING CLAIM SURVIVES ITS
STRICTEST TEST YET**: 11 distinct day-tier findings, still ZERO
counter-signature, min |gap| 0.083. **FINDING 2, FOUND WHILE BUILDING
FINDING 1, AND IT HITS THE METHOD RATHER THAN A NUMBER**: the last
several passes all work by diffing two atlas reports and chasing the
drift, which assumes an identical run on unchanged data gives identical
numbers. It did not. `implied` was `avg(mid)`, and DuckDB accumulates a
float average in a parallelism-dependent order. Measured over 8
back-to-back runs of the same query on the same connection: **238 of
260 buckets returned a different raw `implied`, and 3 flipped their
reported 4th decimal** (Climate/Weather 1h d2 0.2371<->0.2372,
Climate/Weather 7d d3 0.3637<->0.3638, Science and Tech 72h d2
0.2612<->0.2613) because their exact mean sits on a rounding boundary —
and `flagged` was likewise non-deterministic for any bucket whose
implied sat on a Wilson endpoint. No past conclusion is affected (the
phantom is 1e-4 against a smallest-ever-chased drift of 0.033) but a
future pass would have chased it, and the 14:18 report demonstrably
captured a coin-flip: re-running on identical data prints Science and
Tech 72h d2 as 0.2612 where that report says 0.2613. FIXED (7462e5c):
`implied` is summed in exact DECIMAL, which is order-independent — 8
identical runs are now bit-identical on the live archive. `realized`
was left as `avg()` on measurement, not assumption: a sum of exact
1.0/0.0 doubles is exact regardless of order and it never varied.
Nondeterminism needs production scan sizes and cannot be provoked on a
fixture, so its tests assert the mechanism (no float `avg` in the
implied projection, `--` comments stripped so the fix's own comment
cannot satisfy the check — it did on the first try) plus exact
correctness on a hand-picked mid triple whose float and exact means are
different doubles. Overlap tier tests: same 250 markets at 1h and 6h
must read as ONE group; two disjoint flagged buckets must stay two (so
the tier discriminates rather than collapsing everything); and the
smaller-bucket denominator, verified by mutation — flipping min to max
reddens only that test. Suite 325->330, ruff clean, pushed. No promote
— atlas is sim-side, no timer runs it (verified against
`scripts/systemd/`). Verification report:
`reports/atlas/20260729T203821.json`, explicitly a pipeline
verification and NOT a reading, since no bucket gained an observation
since 14:18. PRACTICAL RULES, joining the `top_day_share`,
`direction_underlying_robust`, connection-scoped-`seq` and
own-(category,horizon) rules: **(a) read
`cross_bucket_overlap.tiers.<tier>.groups`, not the length of the
survivor list, as an atlas tier's sample size — the flagged count
overstates findings by ~3x; (b) atlas reports written before 7462e5c
have 4th-decimal `implied` noise, so any pre-07-29-20:38 drift below
~1e-3 is uninterpretable.** NEXT PASS: the 07-30 02:15 UTC weather
bracket is independent run #3 and the first to carry the event tier
natively — two under-award events in a row at the strictest tier would
be the first real directional signal in 23 runs; the 07-30 11:10 UTC
sweep then opens the atlas gate for the first reading whose drift is
measured against a reproducible baseline. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-07-29 14:20 UTC (ATLAS ON THE WEDNESDAY-SETTLEMENT
INCREMENT — THE HEADLINE DRIFT IS REAL AND SIGNATURE-CONFIRMING, BUT
THE BUCKET THE LAST PASS KILLED IS BIT-IDENTICAL AND
`settled_by_category` CANNOT SEE IT. SEVENTH INSTANCE OF THE
UNIT-OF-COUNTING CLASS, THIS TIME IN THE ANTI-NON-READING GUARD
ITSELF. Gate check first: the 11:10 UTC kalshi sweep fired 3h prior, so
the atlas data-gate named last pass had OPENED and atlas was the
runnable report (prior run 07-28 14:19 UTC). Weather bracket ran 02:15
UTC today and per the spacing rule needs >~24h to cross a daily expiry
— next independent reading 07-30, unchanged; econ needs >=336h (next
~08-10); QA fired 07-29 07:00 UTC; divergence unchanged — shadow run
20260722T081852 still open. THE RUN: **93->91 flagged, 63->67 robust,
12->16 day-robust**, and it is the informative Financials increment the
07-28 pass predicted (`settled_by_category` Financials +1,113). All
four newly day-robust buckets are Financials (1h d5, 6h d1/d6/d7). The
standing claim survives its largest test yet: of all **16** day-tier
survivors, **ZERO are counter-signature** (deciles <=4 all negative
gap, >=5 all positive) and every one carries |gap| >= 0.083. Top-3 gap
drift on 163 common n>=100 buckets is Financials 1h d5 (0.039), 1h d6
(0.036), 1h d7 (0.033) — an order of magnitude BELOW the prior run's
0.210, and all in the signature-widening direction, so there is no
anomalous drift to chase. Report:
`reports/atlas/20260729T141801.json` (pre-fix run
`20260729T141518.json`). THE FINDING, AND IT IS THE SAME BUG CLASS FOR
THE SEVENTH TIME: the 07-28 pass's motivating bucket — Financials 24h
d5, `top_day_share` 0.61, the one the settlement-day tier was built to
kill — reads **bit-identical** this run: n 293->293, clusters 82->82,
days 27->27, gap +0.1502->+0.1502, every digit. So does 24h d4 and 24h
d6. Under the 07-27 guard this looks like fresh confirmation, because
`settled_by_category` says Financials gained 1,113 markets. PROBED
BEFORE BUILDING, by summing bucket `n` per (category, horizon) across
both reports: the increment landed **1h +1,036 / 6h +986 / 24h -1**.
Cause is structural, not calendar: a market enters the horizon-h bucket
only if it carries a candle h before its close (`BUCKET_SQL` pts CTE),
and Financials is same-day KXDJI/KXINXU index ladders that open and
close inside one session — they can never have a candle 24h before
their own close. Financials 24h is a nearly-frozen 5,187-observation
population that daily atlas runs do not re-test at all. The 07-27 guard
answers "did this CATEGORY gain evidence" while the bucket key is
(category, HORIZON, decile) — right question, wrong granularity, and it
fails precisely on the category it was written for. HARDENING SHIPPED
(8a49b17): the fingerprint now carries
`observations_by_category_horizon`, summed from `buckets` rather than
re-queried, so it describes exactly the population the buckets are
built from and cannot drift from `BUCKET_SQL`'s candle gates the way a
second query over `markets` can. `settled_by_category` untouched for
cross-report comparability, per the divergence-matcher / atlas-day-tier
/ bracket-concentration precedent. Two regression tests on a fixture
with the production shape (a short-lived market reaching only 1h vs one
reaching 24h): the horizon split must be visible where the category
total is blind to it, and the per-horizon counts must equal the summed
bucket population. Red on both (KeyError), green with. Suite 323->325,
ruff clean, pushed. No promote — atlas is sim-side, no timer runs it.
Verified in the real pipeline: the shipped report reproduces the
hand-computed Financials|24h 5187 exactly. PRACTICAL RULE, joining the
`top_day_share`, `direction_underlying_robust` and connection-scoped-
`seq` rules: **before calling any atlas bucket reading a confirmation,
diff `observations_by_category_horizon` for that bucket's OWN
(category, horizon) — not the category total. Financials 24h in
particular is structurally frozen and its readings are not independent
evidence at any cadence.** The day-tier verdict on Financials 24h d5
therefore stands on ONE reading, not two. NEXT PASS: the 07-30 weather
bracket (>24h from 02:15 today) is independent run #3 and the first to
carry the event tier natively — two under-award events in a row at the
strictest tier would be the first real directional signal in 22 runs.
Untracked `strategies/hylshi_fade.py` re-confirmed present, still
correctly left alone per the 07-18 provenance resolution.)**
(prior 2026-07-29 08:30 UTC (THE QA SEQ-CONTINUITY CHECK HAS BEEN
REPORTING A GARBAGE NUMBER AND COULD NOT FAIL — SAME UNIT-OF-COUNTING
BUG CLASS, SIXTH INSTANCE, NOW IN THE DAILY DATA-QUALITY GATE ITSELF.
Gate check first: atlas data-gated until the 07-29 11:10 UTC kalshi
sweep (not yet fired at 08:15); weather bracket ran 02:15 UTC (~6h) and
per the spacing rule needs >~24h to cross a daily expiry, so the next
independent reading is 07-30; econ needs >=336h (next ~08-10);
divergence unchanged — shadow run 20260722T081852 still open. QA DID
fire 07-29 07:00 UTC and was the one fresh standing report: all-PASS,
but **`book seq contiguous or gap-marked — 1468944 seq holes, 418 gap
rows`** against the 07-27 log line's `14 seq holes`. A 100,000x jump
that still PASSES is the drift worth chasing. PROBED BEFORE BUILDING,
against the live stream archive: the 26h window holds 6,547,930 book
events under exactly **one distinct sid**, with `seq` going BACKWARDS
to 1 at eight separate instants (07-28 06:43, 08:04, 09:20, 14:31,
21:40, 22:40, 07-29 06:00, 07:01). Kalshi's `seq` is CONNECTION-scoped
and restarts at 1 on every reconnect, while the server hands `sid=1`
to the first subscription of each new connection — a fact the file's
own reconstruction comment 20 lines below already recorded (`seq is NOT
usable as an ordering key here: it is subscription-scoped and resets on
every reconnect`) while the check above it grouped by sid alone. So the
query welded 9 disjoint connection runs into one min..max range and
called the interleaving holes. It is not merely wrong, it is
UNSTABLE: re-running the identical query at 08:16 instead of 07:00 gave
**0** holes, because the artifact is entirely a function of where the
window truncates the leading run (a run clipped to seq 800k..1.39M
followed by a restart at 1 fabricates ~600k phantom holes; a window
whose runs happen to tile 1..max densely fabricates none). Every seq
hole figure in this log's QA lines is therefore uninterpretable noise,
including the reassuring `14`. SECOND, INDEPENDENT DEFECT in the same
three lines: the pass condition was `holes == 0 or gaps > 0` — ANY gap
row anywhere in the 26h window excused ANY number of holes, and
production carries 392 of them. The check could not fail in production
regardless of daemon behaviour; it has been decorative since it was
written. HARDENING SHIPPED (a556b31): holes are now measured strictly
inside a connection run (ordered by time, a backwards `seq` opens a new
run via a windowed reset-cumsum), and a hole is excused only by a gap
row whose `[started_at, ended_at]` interval OVERLAPS that run. Unlike
the atlas/queuescore tier precedent the old headline is NOT preserved
for comparability — it is an artifact, not a coarser valid measure, so
it is replaced outright and the historical values are void. Three
regression tests: window-truncated run at seq 50..52 followed by a
fresh run at 1..3 must stay silent (old code: 47 phantom holes, RED);
a real hole with a non-overlapping gap row 10h away must trip (old
code: excused, RED); the mirror control, a real hole with a gap row
spanning the run's own interval, still excused (green both ways, so the
tier discriminates rather than killing everything). Suite 320->323,
ruff clean, pushed, and **PROMOTED** — unlike the last four passes this
is collector-side and the `hyxlab-qa` timer runs it from the stable
worktree; verified in that worktree post-promote. THE CORRECTED
READING, and the daemon is fine: **44 seq holes over 9 connection runs,
392 gap rows, 0 unexcused runs.** Every real hole is covered by a gap
row the SeqTracker logged, which is exactly the healthy signature. The
alarming 1.47M was never a data-loss event. PRACTICAL RULE, joining the
atlas `top_day_share` and bracket `direction_underlying_robust` rules:
**`seq` is only comparable within one connection — any query that
groups Kalshi book events by `sid` alone is counting across reconnects.
Read `unexcused runs`, not the raw hole count.** NEXT PASS: the 07-29
11:10 UTC sweep opens the atlas gate (Wednesday settlements, so an
informative Financials increment), and the 07-30 weather bracket is
independent run #3 — the first to carry the event tier natively; two
under-award events in a row at the strictest tier would be the first
real directional signal in 22 runs. Untracked
`strategies/hylshi_fade.py` re-confirmed present, still correctly left
alone per the 07-18 provenance resolution.)**
(prior 2026-07-29 02:30 UTC (22ND WEATHER MAKER BRACKET — THE
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
  bounded windows); apply bounds if it ever OOMs. **A THIRD class
  found 2026-08-07 02:15 before it fired**: the hourly metadata
  refresh loads the FULL markets table, which the 08-02 breadth
  widening grew to 486k rows / ~430MB (+13k rows/day) — swap-reload
  double-holds it and the daemon reached HWM 966MB of 1G. FIXED
  b962b5c (filtered load + held-market pinning, ~60MB); running
  process bridged with a --runtime MemoryMax=2G until its next
  restart. Pattern across all three: any per-poll or per-reload state
  proportional to the ARCHIVE (equity curve ∝ snapshots, metadata ∝
  markets table) eventually outgrows a fixed cgroup cap — bound state
  by what the daemon can ACT on, not by what the archive holds.

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
