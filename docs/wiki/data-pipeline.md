# Data pipeline (hyxlab archive)

Two DuckDB files (gitignored, **irreplaceable — need off-box backup**):
`data/hyxlab.duckdb` (polled archive; collectors/sweeps write, sim reads)
and `data/hyxstream.duckdb` (WS stream archive; the stream daemon is its
sole writer — separate file because DuckDB's single-writer rule would
deadlock a long-lived daemon against the 5-min collector).

## Tables

Append-only facts: `candles` (hourly price+bid/ask OHLC, volume, OI),
`snapshots` (live top-of-book), `nws_forecasts` (fetched_at = as-issued
time; 5 watchlist stations on the collect cycle, 580k rows, the ground
truth `strategies.WeatherNWS` trades against — and unrecoverable, since
NWS publishes only the CURRENT forecast), `observations` (climate-report
highs; no live writer since 2026-07-06). Reference: `markets`
(metadata + settlement result), `series` (category/fee metadata),
`sweep_log`, `watermarks`, `schema_meta`.

**Which tables QA watches is DERIVED, not listed**
(`tests/test_qa_table_coverage.py`, 2026-09-04). The CREATE TABLE set in
`store.py` is compared against the SQL parsed out of `collector/qa.py`,
and a table QA never reads fails the suite unless it is declared in
`UNREAD` with a reason. This exists because the same defect landed twice:
`breadth_snapshots` went unread for a month, and the derivation's first
run immediately found the second one, `nws_forecasts` (580,270 rows,
written in the same 5-min cycle as `snapshots`, never named in qa.py).
Four tables are declared unread today — `series` and `watermarks` (sweep
bookkeeping, witnessed by "sweep ran in last 36h"), `trades` (watched
harder, per-market, through `trades_swept`), and `observations` (no live
writer; newest row 2026-07-06). **Per-WRITER attribution is not
derivable**, and that is pinned rather than assumed: every archive write
goes through `store.py`, so all 8 unit import closures contain it and
closure attribution would assign all 16 tables to all 8 writers. What the
test does NOT claim: "read" is weaker than "checked for freshness"
(`markets` is read only through `close_time`, a business column), so it
catches the defect that has happened twice, not staleness in general.

**That stronger question is derived too** (`tests/test_qa_staleness_
coverage.py`, 2026-09-04). Per table it asks whether qa.py ever asks the
age of the table's INGEST STAMP — declared in `STAMP`, and required to be
a real column of that table's DDL — crediting only the two unambiguous
shapes: a `_check_freshness(table, col)` call, or `max(<col> ...) FROM
<table>`. The recency-window shape (`<col> > ? - INTERVAL`) is
deliberately NOT credited, because its two live users behave oppositely
on an empty window: "sweep ran in last 36h" counts and FAILS at zero,
while the poly swept-universe tripwire guards on `len(runs) >= 2` and
SKIPS at zero — exactly what a dead sweep produces. Nothing static
separates them, so both tables carry a declared `WITNESS` instead and the
derivation errs toward naming a table that IS watched, never toward
missing one that is not. A join may not lend one table another's stamp
(pinned directly, since qa.py has no multi-table `max()` query today).
Its first run named **`candles`** — the only live-written table with
neither an age check nor a witness.

`candles` is the one archive table with **no ingest stamp at all**:
`end_ts` is the candle's period end and the sweep walks settled history,
so `max(end_ts)` moves BACKWARDS on a healthy run. `sweep ran in last
36h` is not a witness for it: `sweep.py` logs `status='ok'` on the MARKET
count, so a candlestick endpoint returning `{"candlesticks": []}` under
HTTP 200 (renamed field, dropped `period_interval`) inserts nothing while
the trade tape riding in the same loop keeps landing — sweep_log fills
with ok entries, `trades_swept` stays fresh, everything reads green. The
nws shape exactly: two payloads in one loop, one silent failure. So
`candle ingest landing (36h)` asks the writer's own log for the stamp the
table lacks — `sum(sweep_log.n_candles)` over the same 36h window,
nested under `ok_sweeps` (no sweep, no candle to expect, and the cause is
already named once). The `> 0` budget is a DEATH threshold on purpose and
the measurement says it can be no tighter: over 30 days of hourly-stepped
36h windows the sum runs median 312,296 and min 5,726 (the 2026-08-20 box
outage) — a 55x benign spread that no percentile budget survives.
Production 2026-09-04: 211,257 candles over 3,872 ok sweeps.

**The third derivation asks whether the check is EMITTED at all**
(`tests/test_qa_silent_guards.py`, 2026-09-05). Coverage proves the
question is asked; it does not prove the answer is loud, and a check
behind a guard is not asked at all when the guard is false. So every
named `check()` in qa.py is compared against the guards that gate it, and
the guard is classified from its own SQL rather than declared:

- **MONOTONE** — the guard's names trace only to an UNWINDOWED whole-table
  count (`n_breadth`, `n_nws`, `n_poly`, `n_news`). Nothing in this repo
  deletes archive rows (derived by the same file; venue retention purges
  upstream, we only accumulate), so such a guard means "was this feed ever
  turned on", is false only on a deployment that never enabled the writer,
  and never reverts. Silence is the right answer to a deliberate choice.
- **WINDOWED** — anything else. It empties on a dead writer, which is when
  the check it gates matters most, so it must be DECLARED with what stays
  loud: an emitting `else` branch (derived — the `orelse` must really
  emit), or another check that reds on the same input.

Its first run named **`poly swept universe not shrinking`** — the one
guard in qa.py computed from a windowed read (`len(runs) >= 2`, the last
10 days of `poly_market_stats`), and it had no else branch: the tripwire
simply stopped being printed, with no line to miss. Two ways in, both
silent before 2026-09-05: (a) the stats half of the poly walk goes inert
(renamed column, raised insert) while the CLOB prices half keeps landing,
so `poly prices fresh` stays green and `runs` empties; (b) the universe
collapses BELOW the 500-market floor and stays there ten days, so the
guard that exists to make the shrink RATIO meaningful consumes the
collapse it was protecting against. The check written because a silent
universe shrink went unnoticed could itself go missing unnoticed.

The fix is the `qa_signals_fetch` shape against a different witness:
`poly prices fresh (< 30h old)` decides it, because the same sweep writes
both tables. Fresh prices with no settled stats run = the stats writer is
inert -> **FAIL** (`TRIPWIRE INERT`), once `SKIP_MAX_AGE_H` has passed
since the tripwire last actually MEASURED something. Stale prices = the
sweep itself is down, already reported one line above -> **SKIP**, since
two red lines for one cause is the `candles`/`ok_sweeps` rule. The 36h
grace is loose on the measurement: over the 30 days to 2026-09-05 the
trailing-10d window held median 9 and minimum 9 settled runs at every one
of 721 hourly steps — never once below the 2 the guard needs — and the
widest gap between consecutive run starts over all 64 runs is 26.4h.
`ok_sweeps` is the other declared guard, and it needs no else: `sweep ran
in last 36h` counts the same window and fails at zero.

**The fourth derivation asks whether the SECTION stays**
(`tests/test_qa_early_returns.py`, 2026-09-05). The third covers a check
gated by an `if` and says nothing about a check whose section RETURNS
before reaching it — a guard skips one check, an early exit drops the
rest of the function. So every `return` in a `qa_*` section must have an
EMITTER (`check`/`_check_freshness`/`_check_continuity`/`_reachable`/
`print`) on its path — a preceding statement in its own or an enclosing
block, or the TEST of an enclosing `if`, which is what credits the two
`_reachable` exits, whose only emitter lives inside the condition. "On
its path" is path-sensitive on purpose: an emitter inside a nested `if`
fires on one branch while the return stays silent on the other, and
crediting it would have read this pass's own fix as complete.

Its first run named **`qa_econ_pull_live`'s empty-table return** — the
one exit in qa.py that emitted nothing at all. Its guard IS monotone by
the third derivation, so on a deployment that never enabled the econ pull
silence is right; but monotone is an argument about the GUARD, not about
what else can empty the table. `signals.main` calls `record_fetch`
BEFORE the locked DuckDB write, so a fetch that succeeds and a write that
does not leaves ok series in the sidecar and nothing in `econ_vintages`,
and `diff_vintages` cannot explain it away — on an empty table every
fetched observation is new. An econ pull broken from the day it was
installed printed no line, and everything econ-side downstream read
"cannot decide" (`pull_age_d` is None, so `qa_signals_fetch` skips too).

Decided against the sidecar, in the `qa_signals_fetch` shape: ok series
fetched within `ECON_PULL_GAP_BUDGET_D` + empty table = the fetch works
and the write does not -> **FAIL** (`INGEST NEVER LANDED`); no sidecar
run = the feed was never enabled -> quiet, the declared case; a run older
than the budget = the pull is stopped and `qa_signals_fetch` already says
so once -> quiet. `read_signals_fetch` is now the single parser of that
file, because the two questions asked of it (is a series being dropped /
is the ingest landing at all) must not disagree about what it says.

**All four derivations read qa.py as TEXT; the fifth asks whether what
it prints is READ** (`tests/test_qa_prior_run.py`, EXP-1383, 2026-09-05).
Measured across the tree, the answer was nothing: `hyxlab-qa.service` is
`Type=oneshot` with no `OnFailure=`, no `ExecStopPost=`, no `Restart=`
and no notifier; there is no `systemctl is-failed`, no `--failed` and no
other reader of a unit's state anywhere; `read_batch_runs` is the only
journal consumer in the project and `BATCH_RUN_BUDGET_H` covers
`hyxlab-sweep`, `hyxlab-tradepass` and (since 2026-10-17) `hyxlab-poly-sweep`
only (the fade arm reads `KALSHI_BATCH_UNITS`, which excludes poly); `autoloop.sh` never invokes
qa. So `sys.exit(1)` set a `failed` state nothing queried, and the `NOT a
full pass` line exits 0 — for that one even systemd's own state read
clean. `test_nothing_outside_qa_reads_qa` keeps the premise honest: if a
real consumer is ever added, it fails and this section is rewritten.

The consequence is structural, not a matter of taste: the only consumer
available to QA is the NEXT QA run, so `qa_prior_run` is made one. It
runs LAST, is handed this run's own findings, and reports the two states
nothing else in the project can see:

  * **the run did not HAPPEN** — the record is older than
    `QA_RUN_GAP_BUDGET_H` = 36h. The timer is daily at 10:00Z with
    `Persistent=true`, so consecutive records sit 24h apart and one
    missed slot is 48h; 36h is the midpoint, tripping on a single miss
    with 12h of slack for a late start or a boot catch-up.
  * **something went unread and HEALED** — a name the prior run FAILED
    or SKIPPED that is green today. The freshness checks are
    instantaneous (EXP-1359), so a defect that repairs itself between
    two 10:00Z runs leaves yesterday's line in a journal nobody reads
    and today's run green.

**Only the healed set, and the production steady state is what decided
it.** `collect-skips` reports UNVERIFIED — hence SKIPPED — on every run
of a healthy box, so "the prior run was partial" is the NORMAL verdict
here and an arm that fired on it would FAIL every night forever. A name
that is still open today is reported by today's own line and bounded by
its own clock (`SKIP_MAX_AGE_H`); repeating it here would be a second
opinion about a live state. What is repeated nowhere is the name that
went quiet. The record carries `_own_findings()` — this run's failures
MINUS its own prior-run report — so a healed failure is named exactly
once and the naming does not re-arm off its own echo; the filter is
re-read after the check rather than reused from before it, so the
exclusion is live rather than an artifact of statement order (that was
one of the two mutants the first shape survived).

**What it cannot do**, stated so the next pass does not assume it: report
its own non-execution. No in-process check can, which is why the gap arm
reads the record's AGE instead of trusting that a run occurred — a
crash-looping QA is named by the first run that completes, and until one
does, no reader inside this process exists to name it. The record lives
under the `run` key of `reports/qa/sections.json`; an unparseable or
older-format one reads as "no prior run" rather than failing on the day
the check ships.

Four tables have no ingest stamp (`candles`, `trades`, `observations`,
`watermarks`, plus `schema_meta` which has no time column at all) and
four carry a witness rather than their own check: `markets` (written in
the SAME transaction from the SAME fetch as the snapshots that witness it
— `collect.py`'s per-series try builds `cyc.infos` and `cyc.kalshi_snaps`
from one `get_markets` pair, so unlike the nws pull it cannot go stale
while "collector fresh" passes), `series`/`watermarks` (the 06:10Z sweep),
`poly_market_stats` (same walk as `poly_prices`, whose freshness is
checked), and `sweep_log` (its own count-window check, which is stronger
than a `max()` since it ignores `truncated` rows).

`breadth_snapshots` (EXP-928) is the archive's only EXCHANGE-WIDE quote
history and deserves its own line: `snapshots` covers the 23-series
watchlist, so a series family we have never studied has no price a
strategy could have traded at, and can be evaluated retrospectively ONLY
out of this table. Same columns as `snapshots` plus `volume_24h` and
`rank`; one `ts` per cycle, ~1,000 rows. 8.26M rows since 2026-08-03.

Stream archive (`streamstore.py`): `book_events` (kalshi snap qty =
absolute level, delta qty = SIGNED change; poly delta qty = new ABSOLUTE
size; poly market_id = CLOB token id), `stream_trades`, `stream_gaps`
(closed intervals of broken coverage — reconnects, Kalshi seq jumps,
daemon downtime; replay must treat books as unknown inside a gap until
the next snapshot re-seeds).
**POLY L2 IS ABSENT, NOT THIN, BEFORE 2026-09-24.** The `price_change`
parser read the venue's documented list key (`changes`, asset on the
frame) instead of the wire's (`price_changes`, asset per ENTRY), so every
delta parsed to nothing from the first day of capture: 242,828,734 poly
`book_events` over 07-08 → 09-24, **zero** of them `delta`. What is
archived is only the reconnect/refresh `book` seed, ~35 per token per
day — 13.6% of poly trades in the last 14 days have a book frame within
2s of them, median 26s, p90 23 min. No verdict rests on it
(`simulator.capabilities` already declares polymarket
`INDEPENDENT_NO_BOOK`), but no analysis may read that span as a book
either. Fixed + instrumented 09-24 (mistakes #82): poly now writes
`kind='void'` rows for any frame that archives no level, `streamd`
publishes `poly_void` apart from `poly_events`, and QA's void check is
per-venue with `price_change` deliberately NOT benign.
**Kalshi weekly maintenance, Thursdays ~06:00-10:00Z**: 26-38 `dead_air`
gaps, each followed by a reconnect that comes back silent. Kalshi trades
fall to ~2k in the 07Z hour, against 200-400k/h on either side. Replay and
event studies must exclude this window rather than read it as illiquidity.
On 09-17 the server also sent a books `unsubscribed` ack mid-window (QA
treats it as a benign control frame).

This was first recorded 09-17 as "every Thursday **since 08-13**", which
was an artefact of looking at recent weeks only. Re-measured the same day
over the whole 71-day stream retention: the storm is on **all 11 Thursdays
in the record**, back to 07-09 — what changed on 08-13 is its SIZE (~50
lost minutes on 07-09, ~450 on 08-27), not its existence. Its hour bounds
are the record's, not a published schedule: storms open in hour 06 or 07
every time, and the one latest-bleeding (07-30) opened its last gap inside
hour 09 — hence the 4h span QA excuses. **When a weekly pattern is found in
the recent window, re-measure it over the whole retention before writing
down a start date; "since <date>" is a claim about the data's edge as often
as about the world.**

**The venue's own schedule, read 2026-10-15** (`GET /trade-api/v2/exchange/
schedule`, no auth): `standard_hours.thursday` is two sessions, 00:00-03:00
and 05:00-24:00, i.e. a published weekly closure **03:00-05:00 ET** (07:00-
09:00Z in EDT), and `maintenance_windows` is empty. Confirmed live the same
morning: `exchange/status` read `trading_active: false` on all five exchange
indices at 08:59:22Z and `true` on all five at 09:00:04Z. The closure is
total, not a WS artefact: REST snapshots kept landing (587 markets per 5-min
cycle, 0 errors) with `volume` frozen on EVERY market from the 07:05Z cycle
to 09:00Z, on 10-01 and 10-08 as well (22-23 frozen cycles each), while
Polymarket flowed at ~50k events per 5 min throughout. So a replay across
the window sees flat books and flat volume, not missing rows -- which is
why it must be EXCLUDED rather than read as illiquidity. The record-derived
06-10Z excuse brackets the ET window with an hour each side; the hours are
ET, so **after DST ends (2026-11-01) the closure sits at 08:00-10:00Z** and
its reopen bleed (~5-6 min of reconnect gaps) lands in the first minutes of
the NEXT 10:00Z QA slot, outside the excuse and against the 30-min budget.
First Thursday to read: 2026-11-05. **2026-10-22 (Thursday) added a second failure mode inside the
closure:** REST `/markets` returned `HTTP 503` on every walk from 07:12:00Z
to 08:00:15Z (0 x 503 in the same window on 10-08 and 10-15), which aborted
the 06:10Z kalshi sweep at 07:14Z (designed exit 75, 714/4042 series,
watermarks intact, 10-23 is the catch-up) and failed breadth 10x; the
collector completed every tick with 4 errors / ~530 of 597 snaps. One
observation; if it recurs on 10-29 the Thursday sweep needs a closure-aware
pause rather than the abort. The streamd journal under
`--since "7 days ago"` starts mid-storm on the matching weekday, so a
per-day gap count over that range under-counts the oldest Thursday -- the
10-15 pass nearly chased a 100-vs-8 "drift" that was this truncation.

**`stream_gaps` volume is budgeted by QA** (`kalshi capture gaps within
budget outside venue maintenance`, 30 min per channel per 26h). Every other
reader spends a gap row as an EXCUSE — it suppresses a seq hole, skips a
market in the book reconstruction, bounds a shadow/replay window — so
before this check degrading capture made the whole check surface QUIETER
instead of louder. Outside the maintenance window the per-channel load is
0.0-5.6 min/day; the budget was calibrated by replaying the check over all
71 historical 10:00Z slots (fails 3, all genuine capture loss, and none of
the 11 storms). Minutes INSIDE the maintenance window are printed every run
but never budgeted, so the storm's own drift stays readable.

## Key decisions

- **Naive-UTC timestamps everywhere** via `store._naive_utc()`. DuckDB
  silently converts tz-aware inserts to BOX-LOCAL time otherwise
  (machine-dependent corruption). Migration 1 fixed legacy rows,
  verified against unix ground truth. **A migration fixes ROWS, not the
  CODE PATH**: `poly_market_stats`, added after migration 1, reacquired
  the bug and sat `America/Chicago` behind the whole archive until
  2026-08-23 (migration 2 + a writer-level test). Every writer taking an
  aware `now` needs `_naive_utc` AND a test asserting it.
- **Idempotent inserts**: `insert_new()` anti-join on natural keys; any
  backfill/sweep re-run is safe. (Fixed a real dup-on-rerun defect.)
- **Single writer**: DuckDB blocks even read_only connects while a
  writer is open. All scheduled writers flock `data/writer.lock`;
  ad-hoc reads must wait for the 5-min collector (seconds) — but the
  **poly sweep holds the archive open for HOURS** (~7h observed
  2026-07-08 walking 4k+ markets). Sim-side readers must degrade
  gracefully and retry lazily (simui's `ensure_metadata` pattern),
  never block on it.
- **The sim-side databases are owned by a LOCK, not by DuckDB**
  (EXP-1372, 2026-08-26). `hyxstream.duckdb` and `hyxshadow.duckdb` are
  each written by one daemon, and "the daemon owns it" was a claim with
  nothing behind it: both writers open the file, write, and CLOSE it
  (`StreamStore.flush` every ~15s, `ShadowLedger.persist` per ~20s poll),
  so DuckDB's exclusive file lock exists only for those milliseconds.
  **Measured: two StreamStore writers on one file, 20 and 15 flushes,
  ZERO declines** — a second `streamd` is not refused, it INTERLEAVES,
  and `book_events`/`stream_trades` have no key and no dedupe anywhere,
  so a duplicated delta replays as a wrong book in data neither venue
  will serve again. `streamd` and `shadow` now hold `<db>.owner.lock`
  (file-scoped, `hyxlab.lockid`) for their whole life and exit 75 naming
  the holder; enumerated in `tests/test_owned_db_discipline.py`.
- **Collector cycle profile, measured (2026-08-05, 213 `timings=`
  cycles spanning 0–2 concurrent Kalshi-API writers)**: fetch is a
  ~29s pagination floor (median with NO other consumer; the watchlist's
  own `get_markets` paging) plus a contention tax — median 44s / p90
  ~65s with sweep+tradepass both running. Write is a flat ~3.6s
  (post-EXP-963). The real tail is **flock wait, not fetch**: during
  2-writer mornings ~half the cycles wait >1s on the archive lock, and
  the sweep holds one ~6-min continuous stretch around 07:28–07:34Z
  (observed both 08-04 and 08-05) that costs exactly one collect cycle
  per day — skipped, billed to `data/collect_skips.jsonl` with holder
  attribution, and inside `qa_collect_skips`' 3/24h budget. One
  sweep-window skip/day is the normal signature; more is drift.
- **Enumeration tripwire** (`collector/qa.py::qa_archive`, "poly
  swept universe not shrinking"): the Gamma offset-cap regression
  (see [venues](venues.md)) would have silently halved the poly sweep;
  it was caught by a lucky dead probe, not QA. It reads
  `poly_market_stats`, NOT `poly_prices`: the stats table holds one row
  per market per sweep RUN stamped with that run's start, so a `ts`
  group IS one run and `count(DISTINCT market_id)` IS the enumerated
  universe. The 2026-07-11 version read `poly_prices` (a CLOB PRINT
  time) and therefore counted markets that TRADED, which decays toward
  the floor by construction as later sweeps backfill history — it
  measured maturation, not enumeration. Now: last COMPLETED run vs the
  prior 10 days' peak, 0.75x, with a 20h settle window excluding the
  in-flight walk. 0.75 because the enumeration series is flat
  (16,391-16,952 over the nine runs to 2026-08-22, a 3.4% band), so the
  check now catches a quarter of the universe going missing rather than
  only a halving.
- **Provenance**: every signal row carries when it became knowable
  (forecast runtime, vintage release, poll time). The no-lookahead
  boundary is enforced by this column, not convention.

## Deployment (stable worktree — since 2026-07-07)

All three systemd units run from `/home/devs/workspace/hyxrestration-stable`,
a git worktree pinned to the `stable` branch with its own venv
(`scripts/requirements-stable.txt`) and symlinks to the dev tree's
`data/`, `.env`, `.secrets`. Dev-tree churn can therefore never break
running capture (daemons restart into whatever code is on disk).
**Ship collection changes ONLY via `scripts/promote.sh`** — it runs the
suite, fast-forwards `stable`, syncs deps, smoke-imports, restarts the
stream daemon. The import boundary (tests/test_boundaries.py) keeps
collection deployable without sim-side churn: collection ↛ sim, sim ↛
collection, both may use the kernel (models, store, streamstore, fees,
migrate, watchlist, stations).

### Shell lint gate (shellcheck, since 2026-09-05)

The deploy path is shell — `promote.sh` sources `restart_decision.sh`,
which decides every daemon restart — and until this date **nothing
checked any of it**. ruff does not read shell, and there was no
shellcheck in the project: 4 operator scripts and 14 `.claude/` hooks
were covered by no gate at all.

    .venv/bin/shellcheck --severity=warning $(git ls-files '*.sh')

pinned via `shellcheck-py` in `requirements.txt` (dev-only — the stable
venv installs no linters), enforced by `tests/test_shell_lint.py`.

**What the first run found**, both in `restart_decision.sh`:

- **SC2148** — no shebang, so shellcheck could not tell the dialect.
  Correct in substance: the file is *sourced*, never executed, so a
  shebang would be a lie. Answered with `# shellcheck shell=bash`, which
  states out loud the assumption promote.sh has always made.
- **SC1010** — `local ts now then` declares a variable named after a
  reserved word. Measured on this box's bash: it *does* localize, so the
  finding was latent, not live. Renamed to `started` regardless; a
  reserved word as an identifier is a parser dare with no upside.

plus **SC2155** in `.claude/evals/run-smoke.sh` (`local X=$(cmd)` masks
the command's exit status, so `set -e` cannot see it fail).

**Why the floor is `warning`, measured.** At shellcheck's default (style
and up) the tree carried 8 findings: the 3 above plus 5 in two families —
SC2001 ×3 ("use `${var//a/b}` instead of sed") and SC2016 ×2
("expressions don't expand in single quotes"). The SC2016 pair is a false
positive **by construction**: both sites are sed/grep *scripts* whose
backticks must not expand, so the only way to satisfy the finding is a
`disable` comment. A gate whose findings are answered by disable comments
teaches the operator to write disable comments. SC2001 has no failure
mode, and one of its sites (`sed 's/^/    /'` over multi-line input) has
no parameter-expansion form at all. The floor is **not** an amnesty:
`BELOW_FLOOR_FAMILIES` names the two excused codes with reasons, and a
*new* below-floor family fails the suite until someone rules on it.

**Scope is discovered, not listed** — the same defect `test_lint_scope.py`
closed for Python, where an enumerated directory list could not fail on
the day `scripts/` appeared. The glob has its own blind spot (a shell
script named without `.sh` is invisible to it), closed by asserting the
naming convention holds rather than by complicating the command: a
tracked file with a shell shebang and no `.sh` extension fails the suite
and names itself. The shebang classifier is unit-tested against strings,
since it matches nothing in the repo today and would otherwise be
untested code passing by matching nothing.

Six mutants red, one test each.

### systemd unit gate (`systemd-analyze verify`, since 2026-09-06)

The shell gate closed the blind spot for `*.sh`. It closed it for `*.sh`
only. The 21 files in `scripts/systemd/` that promote.sh copies verbatim
into `~/.config/systemd/user/` were **parsed by nothing before install**.
`tests/test_systemd_units.py` reads them as *text*: it greps `ExecStart`
for a `-m module` name, `OnCalendar` for a timezone suffix, `Description`
for prose. Every one of those checks passes on a unit systemd itself
refuses to load, and on a unit whose interpreter does not exist.

    SYSTEMD_UNIT_PATH=$PWD/scripts/systemd:/etc/systemd/user:/usr/lib/systemd/user \
      systemd-analyze verify --user scripts/systemd/hyxlab-*

enforced by `tests/test_systemd_verify.py`. Three measurements shaped it.

**The exit code is a liar for warnings.** systemd 260, measured
2026-09-06: `Type=oneshot` + `RuntimeMaxSec=` prints
`RuntimeMaxSec= has no effect in combination with Type=oneshot. Ignoring.`
and exits **0**. Hard errors (missing `ExecStart` binary, unparseable
`OnCalendar`) exit 1. So the class of finding that reads as a *silently
ignored directive* — present, plausible, doing nothing — is exactly the
class a return-code gate lets through. The gate asserts an **empty
report** as well as a zero exit, and
`test_a_warning_only_finding_exits_zero` pins the measurement so that if
systemd ever starts failing on warnings, the extra arm is known to have
become redundant rather than staying load-bearing by accident.

**The tool is not hermetic by default.** `verify --user` resolves through
the full user unit search path, which includes `~/.config/systemd/user` —
and on this box that directory holds an **unrelated project's** units. A
bare run reports two findings belonging to `hylshi-*`. Since the gate
must read stdout (above), it would fail on files this repo does not own.
`SYSTEMD_UNIT_PATH` replaces the search path with the repo's own
directory plus the *system* user-unit dirs; those are required, because
without them every unit fails on `Unit basic.target not found`, an
artefact of the isolation and not a defect in the unit. The isolation is
asserted against a pure function, not against what this box has
installed.

**What it does not check.** `WorkingDirectory=` and `EnvironmentFile=`
pointing at nonexistent paths both verify clean and exit 0 (measured).
The one filesystem fact it *does* check is the `ExecStart` command's
existence — which is the fact that matters, because every hyxlab unit's
ExecStart is an absolute path into the stable worktree's venv
(`…/hyxrestration-stable/.venv/bin/python`) and **nothing in the tree
opened that path**. A stable venv rebuilt, renamed or half-deleted leaves
every daemon failing at start while `git diff` shows perfect units. That
arm is why the suite is deliberately not portable to a box without the
stable worktree: there the failure is correct.

**First run: the vendored units and the installed copies are both clean**
(parity 21/21). The gate's value is prospective — it is the check that
would have caught the 2026-08-03 timer defect's *unparseable* sibling,
and it is the only thing that opens the interpreter path.

Five mutants red: a warning-only finding added to a real unit (rc still
0, caught by the report arm); the user unit dir admitted to the search
path; the documented command stripped of its `SYSTEMD_UNIT_PATH` prefix;
promote.sh widened to a set the gate does not verify; a vendored unit
re-pointed at a missing interpreter.

### Health digest (`collector.health`, since 2026-09-06) — the box's one egress

Five passes in a row built a checker and then found its verdict read by
nothing. This one asks the question those passes deferred three times:
**does any signal leave this box?** Measured 2026-09-06:

- **Nothing notifies.** No smtp, no webhook, no mail, no push client —
  zero hits across `*.py`, `*.sh`, `*.service`, `*.timer`.
  `collector.backup` defaults `--dest` to `data/backups`, and
  `HYXLAB_BACKUP_DIR` (the documented off-box hook, named in the unit's
  own Description) is set nowhere, so even the backups stay on the disk
  they protect.
- **One thing leaves, and it carries only prose.** `scripts/autoloop.sh`
  pushes to `origin main` every six hours. `/data/` and `/reports/` are
  gitignored — rooted, deliberately — so no checker's *output* can ride
  it. What leaves is exactly what an agent typed into `status.md`.

So the smallest honest egress is not a new channel: it is a
machine-written **input** to the one that exists. The autoloop's
cold-start prompt runs `python -m collector.health`, which re-reads what
the manager and qa already wrote down. It **runs no check and opens no
DuckDB** — a health report that took the archive lock to say things look
fine could hurt the thing it watches (ops rule, mistakes #20) — and it
exits 0 unconditionally, because a gate whose failure nothing reads is
the very defect it exists to answer. Preferred to a `WantedBy` on a
checker unit for the readback pass's reason: another reader inside
journald is another unread verdict.

Four properties of systemd's persisted state had to be measured before
they could be reported. Each is a way a naive digest lies:

1. **`Result` is the PRIOR run's while a unit is in flight.** At 09:15Z
   `hyxlab-sweep`, `hyxlab-poly-sweep` and `hyxlab-autoloop` all read
   `ActiveState=activating` with `Result=success` — success belonging to
   yesterday, with today's run still executing. `RUNNING` is its own
   state and quotes no result, so the long multi-hour units (whose
   failures matter most) are never reported in the past tense.
2. **`ExecMainStatus=0` is a default, not a measurement.** Every unit
   that has never exited reports 0 — the daemons up since 08-29 do, and
   so would a unit that has never run. The status is read only when
   `ExecMainExitTimestamp` is populated.
3. **An empty `NextElapseUSecRealtime` is not a broken timer.** systemd
   clears it while the triggered service is active; that is why
   `list-timers` prints `-` for the sweeps. Lateness is asked only of an
   *idle* service, or the digest would cry wolf on every long sweep,
   daily, on a healthy box. The slack (300s) is one full period of the
   fastest timer in the tree (`OnCalendar=*:0/5`).
4. **An uninstalled unit answers quietly.** `systemctl show` on an
   unknown name exits 0 with `LoadState=not-found`. The unit set is
   discovered from `scripts/systemd/`, so a unit committed but never
   promoted reads `UNLOADED` instead of dropping out of the digest.

**The live defect it found on its first run.** `qa.STATE`
(`reports/qa/sections.json`) is a **relative** path, so the dev tree and
the stable worktree keep *separate* records. On 2026-09-06 dev read
`09-05T20:25Z` (a hand run) while production read `09-06T10:00Z` — the
same morning, and the dev copy is the one a naive reader prints.
`qa_record_path` takes the path from the unit's own `WorkingDirectory`
(stripping systemd's optional-`-` prefix). `judge_qa` then adds the arm
qa cannot have: qa reads its record from inside the process that writes
it, so it alone cannot notice that the manager *started* a run whose
record never landed here. The unit's start time and the record's
timestamp are two independent facts about the same run, and the digest is
the only place both are in hand.

qa's parser is reused rather than reimplemented — `_load_state(path)` and
`_prior_run(state)` take the record as a parameter, resolved at **call**
time (a default argument would bind the module-load `STATE` and silently
defeat the suite's monkeypatch; that is how the first version broke 29
existing tests).

**A standing skip is not a partial run (2026-09-07).** The digest's first
QA line read `skipped [...] — NOT a full pass` off any non-empty skip
set — and `collect-skips` is SKIPPED on every run of a healthy box, by
the same design decision recorded above. So the line the autoloop reads
at every cold start carried that phrase every six hours, forever, on
green; and the day a *real* skip appeared it would still read `NOT a full
pass`, differing only in the contents of a list nobody could see change.
That is both halves of the defect: the fatigue `qa_collect_skips`
explicitly refuses to manufacture one layer down and `qa_prior_run`
refuses one layer up, reintroduced by their reader, plus the masking of
the signal it exists to carry.

`qa.STANDING_SKIPS` now names the exempt set, at the one site that
appends it (`COLLECT_SKIP_SECTION` — the literal is gone from all three
of qa's uses, so a rename cannot desync the producer from its reader),
and `judge_qa` partitions the record's skips against it. A standing name
is still **reported** — unread is not the goal — but as a parenthetical
that does not move the verdict: `clean (standing skip: collect-skips)`.
Only a skip qa did *not* expect makes the run partial, and it alone is
listed in the partial set.

The membership criterion, so a fifth skip is not quietly added to the
quiet set: `collect-skips` is the only section deliberately exempt from
`_skip_age_h`. Every other skip name means something could not be
MEASURED — `fade-window` and `batch-run-budget` need a readable journal,
`poly-universe` and `signals-fetch` escalate to FAIL after
`SKIP_MAX_AGE_H` — so their presence is news and this one's is not. A
test reads the `_skipped.append` sites out of `qa.py`'s source and pins
both the set and the constant-not-literal call, so a new standing claim
fails there rather than silencing itself.

**Unit drift — the second section, added 2026-09-06.** The unit-gate
pass named this successor: `test_systemd_units.py` greps the *repo's*
unit files, `test_systemd_verify.py` parses them, `promote.sh` copies
them into `~/.config/systemd/user/` and never looks back. Between two
promotes the installed set is owned by nobody. The obvious check is
`diff scripts/systemd/x ~/.config/systemd/user/x`, and it reports CLEAN
in three of the four states that matter. All four were measured against
a throwaway `hyxprobe-drift.service` — never a hyxlab unit:

| verdict | field | what a file diff sees |
| --- | --- | --- |
| `SHADOWED` | `FragmentPath` | clean — unit lookup walks a search path, so a copy in a higher-priority directory is what loaded. Both diffed files are correct; neither is running. |
| `STALE-IN-MEMORY` | `NeedDaemonReload` | clean — active unit, fragment edited without `daemon-reload`: `show` still reports `Description=probe` while the file says `EDITED BY HAND`. repo == disk, manager runs neither. This is what a promote whose reload failed leaves behind. |
| `DROP-IN` | `DropInPaths` | clean — `<unit>.d/*.conf` with `ExecStart=` + `ExecStart=/bin/echo hijacked` replaced the command outright, fragment byte-identical. A live practice on this box (`hylshi-watchdog.service.d`). |
| `DRIFT` | fragment text | the one state it does catch. |

Two measured complements keep either arm from being redundant: a drop-in
added *before* a reload reads `DropInPaths=` empty with
`NeedDaemonReload=yes`, so each covers the other's blind window; and an
**inactive** unit always reads `NeedDaemonReload=no`, because systemd
garbage-collects the unreferenced unit and re-reads the file on demand —
not a hole, since nothing stale is in memory there, and the text
comparison is the whole answer for those units.

The set is globbed to all **21 files promote installs**, not the 12
services `discover_units` returns: a hand-edited *timer* changes when
everything downstream runs and appears in no service. `INSTALL_DIR` is
asserted against promote.sh's own `cp` line, because if the destination
moved every verdict would silently become `SHADOWED` against the old
path — a checker crying wolf on a healthy box. An uninstalled unit reads
`SKIP`, not a match: `judge` already prints `UNLOADED` for it, and two
lines about one fact train the reader to skim. Twelve mutants red. First
run: **21/21 clean**, so its value is prospective.

Nine mutants red. A tenth defect was found by promote.sh, not by the
pre-commit run: the premise scan read raw text and matched the word
"webhook" in `health.py`'s **own docstring** — the file documenting the
measurement became its counter-example, and only once committed, since
`git ls-files` lists tracked files only. The scan now strips comments and
string literals and asks the question of *code*, which is the same
self-match trap the ops rules name for `pkill -f`.

## Running pieces

- `hyxlab-collect.timer` (systemd user, 5 min): `collect --once` —
  Kalshi focus top-of-book, NWS, Polymarket pairs (pairs still empty).
- `hyxlab-sweep.timer` (daily 06:10 UTC): `sweep --days 2` incremental
  (candles + trade tape per settled market), category allowlist
  (8 categories ≈ 2,240 series). **Sports/entertainment/politics stay
  excluded — USER-CONFIRMED 2026-07-08**: ~8.2k series that dominate
  settle volume, ~10× archive/sweep load, least strategy relevance;
  their live prints are still captured by the stream firehose. The
  allowlist is one line in `sweep.py` if ever revisited.
- `hyxlab-poly-sweep.timer` (daily 05:00 UTC): `poly_sweep` —
  Polymarket metadata + volume/liquidity series + watermarked price
  capture (~60d retention) + trade tails, volume-desc to $10k.
  Since 2026-10-17 every attach it takes on the collector's file (the
  watermark read and one `_flush` burst per batch, both through the
  `_open` wrapper under `charge_hold`) is summed on one `[poly]
  attach_wait: on hyxlab.duckdb N attaches ... held N for Xs` line after
  `done:`; first production line at the 10-18 04:15Z run (the 10-17 run
  started before the promote). `tests/test_hold_discipline.py` now also
  asserts the rule one level up: any unit whose ExecStart module attaches
  a live archive must publish `attach_wait_block` or be named in
  `UNIT_NON_PUBLISHERS` with its argument (only `collector.collect`).
- `hyxlab-breadth.timer` (5 min, offset to :02,:07,... so it does not
  contend with the collector for `data/writer.lock` on the same minute):
  `collector.breadth --once` — one paginated `/markets?status=open` pass
  ranks the open universe by `volume_24h` and IS the snapshot, so top-N
  costs the same requests as top-1. `HYXLAB_BREADTH_CLOSE_WINDOW_H=24` is
  what makes it affordable: the unwindowed universe is >200k markets,
  almost all zero-volume parlay legs. **DEFAULT DISABLED in code —
  installing and enabling the timer IS the enabling act**, because it
  shares Kalshi's rate budget with the live trader. `Persistent=false`:
  a missed cycle is a missed instant, and replaying it at boot would
  stamp a stale quote with a fresh `ts`.
- `hyxlab-tradepass.timer` (daily 06:37 UTC, `AccuracySec=1s`; 06:35 until
  2026-10-17 -- ON the collector's :0/5 tick, which cost the collector's
  06:35Z cycle 9-15 x 429 retries and the band's slowest fetch six days of
  six, see mistakes #125): `collector.trades_backfill`
  — retro-pass over the trade tape, catching the gaps `sweep` leaves when
  a per-market fetch raises HTTPError and is swallowed. Since 2026-10-17
  every attach it takes on the collector's file (the flocked schema
  burst, the proven-empty mark, the pending read, one `_flush` per
  market batch) goes through `held_open`, and the run's whole exclusion
  prints as one `[tradepass] attach_wait: on hyxlab.duckdb N attaches ...
  held N for Xs` line after `done:` (the #96/#102 shape; `signals` was
  the previous instance). Expect `held` = 3 + flushes; the first
  production line, 10-17 06:35Z: 55 markets, 2 flushes, `held 5 for 6.5s
  in total, worst 5.5s`, 0 contended -- 5 = 3 + 2 as predicted; the
  collector's same-minute cycle read `wait_s 0.0` (its write landed after
  the pass closed), so the file cost it nothing and the API cost it 36s.
- `hyxlab-qa.timer` (daily 07:00 UTC): data-quality checks, both
  archives; FAIL lines land in the journal with exit 1. It watches
  tradepass through `qa_tape_coverage` (per-market persistence, plus a
  `TAPE_SWEEP_STALL_H` floor for "the sweeper itself is dead") and its
  wall-clock budget through `qa_batch_run_budget`. Breadth was NOT
  watched at all until 2026-09-04; it now has the collector's own pair —
  `breadth fresh (< 20 min)` and `breadth continuous over last 24h`,
  sharing `_check_continuity` with `snapshots` so a second 5-min writer
  cannot ship with a different window, anchor or budget. Both are guarded
  on the table being non-empty, so a deployment that never enabled
  breadth stays green rather than alarming about a deliberate choice.
  `nws_forecasts` got the same pair on 2026-09-04 (`nws forecasts fresh
  (< 20 min)` / `... continuous over last 24h`), and both halves are now
  shared helpers — `_check_freshness` and `_check_continuity`, the latter
  parameterised on the cycle column so `fetched_at` works like `ts`.
  **`snapshots` freshness is not a witness for the NWS pull**: the pull
  runs inside the collect cycle under a per-station try/except, so an NWS
  outage, a DNS failure or a station rename drops forecasts silently
  while snapshots keep flowing and every other archive check reads green.
  **And the table-wide pair is a check on the luckiest station**: the
  pull is five independent sub-pulls under five excepts, so one station
  failing on every tick leaves `max(fetched_at)` fresh off the other
  four (measured 2026-10-22: AUS pulled nothing for 19 ticks on a
  null-temperature period, mistakes #133). Since 2026-10-22 QA also
  runs `nws stations each fresh (< 20 min old)` — per-station
  `max(fetched_at)` over the stations that pulled inside 24h, stale ones
  named with their age; a station removed from the watchlist ages out of
  the owed set after a day. The parser side: `nws.get_daily_highs` skips
  a daytime period whose `temperature` is null (NWS serves one for the
  running period at times) and prints `[nws] {station}: {period} {date}
  temperature null, skipped` — the journal is the only witness that
  today's high was not pulled, since the archive records no absences.
  **The class, all three per-source loops (Kalshi series / NWS stations /
  Polymarket books):** `qa_collect_source_errors` (section
  `collect-source-errors`) FAILs any source past
  `COLLECT_SOURCE_ERR_MAX_24H` = 5 cycles in 24h, named with its
  exception class. Benign band 1 error/source/day; the 10-22 503 storm
  printed 9 per series and the AUS episode 21, so the 10-23 10:00Z QA
  FAILs this line on both until they age out (AUS 20:25Z 10-23). **Since
  2026-10-23 the archive is the witness:** `collect_cycles` holds one
  row per (cycle, source) -- `ts, source, name, n_rows, wall_s, error`
  (exception class, NULL when the fetch returned) -- written by
  `Store.insert_collect_cycle` INSIDE `write_cycle`'s one transaction
  (both the snapshots and the account, or neither; pinned by
  `tests/test_collect_cycles.py`, the arm that is red against a
  post-commit mutant being "a failing record rolls the snapshots back",
  NOT "a failing snapshot write leaves no record"), and carried through
  the spool (`spool.ROW_MODELS["sources"]`, `hyxlab.models.SourceResult`).
  So a Kalshi series with no open market is `n_rows 0, error NULL` --
  a quiet source, absent from the error map -- and one whose fetch failed
  is `n_rows 0, error 'HTTPError'`. `_archive_checks` reads the map under
  its hold (`read_archive_collect_source_errors`, keyed `nws AUS` /
  `kalshi KXLOWTNYC` / `polymarket books` exactly as the journal reader
  keys it) and the check prefers it, printing `[archive]` -- but ONLY
  when the table's first row predates the window, since a map over part
  of a window read as the whole passes the transition day over an episode
  the journal still holds (10-23 would have read the AUS 21 as clean);
  otherwise the journal scrape judges (`[journal]`), and an unreadable
  journal there is a SKIP. First `[archive]` read: 10-24 10:00Z. ~38 rows per cycle, ~11k/day.
  Not yet judged: a source quiet for a whole day (a watchlist series with
  no open market for 24h) -- the data to judge it now exists.
  **Since 2026-10-23 the Kalshi walk is PACED, not refused:** the
  collector's ~64 `/markets` requests per cycle (32 series x open+settled)
  went out back-to-back at ~18 req/s and drew 3-5 x 429 every cycle for
  48h, the FIRST 429 at 0.84-1.75s into every fetch and each later one
  5.0-6.4s after the previous -- the ladder's 5s fallback sleep plus
  ~0.8s of admitted requests, i.e. a ~15-20 request bucket read from the
  victim's side (#92), so ~85% of a 13.6-34.1s `fetch_s` was back-off
  (status 2026-10-23 03:15Z / 03:45Z, measured off `collect_cycles.wall_s`
  and `data/rate_limit_headers.jsonl` against the cycle walls, nothing
  else walking the key). `kalshi.set_pace(interval_s)` puts a floor
  between request STARTS, applied by `kalshi._pace()` immediately before
  EVERY `sess.get` in the module (the 429 ladder's GET, the trade tape's
  pages, the one-shot `/series`), under one lock so streamd's
  `to_thread` refreshes serialise; `collect --pace` (default
  `kalshi.COLLECT_PACE_INTERVAL_S` = 0.125s, 8 req/s, under every
  documented tier -- Kalshi's public docs say 10-20 read/s and the
  measured bucket fits 20/s) switches it on, so the walk costs ~8s with
  nothing refused. OFF by default (0.0), like `transport_budget=` makes a
  site name its unit: the sweep's 12.5h wall and breadth's 200-page walk
  keep their shape until the collector reading says what a floor costs.
  PER PROCESS by construction (#125): it bounds one process's burst, and
  two paced processes on one tick still sum past the bucket. The cycle
  line's `timings=` carries `pace_s` (seconds of `fetch_s` slept on the
  floor, `kalshi.pace_stats()['slept_s']`, reset per cycle) so the price
  is in the journal, not in the constant. Verification reads: the
  collector's share of the sink's `/markets` 429s (40-50/h before, with
  the sweep idle) should go to ~0, `fetch_s` to ~8-10s, and every series'
  `collect_cycles.wall_s` back to the 0.08-0.15s shape; a tier downgrade
  would show as 429s RETURNING under the floor, not as a silent change.
  `tests/test_kalshi_pacer.py` (8; the every-`sess.get` arm red-verified
  against a tape-site mutant).
  QA COVERAGE IS NOW DERIVED, NOT HAND-KEPT — see below.
- `hyxlab-stream.service` (long-running, Restart=always, live since
  2026-07-07): `python -u -m hyxlab.streamd` — Kalshi exchange-wide
  trade firehose (~105 ev/s quoted since 07-07; re-measured 2026-09-18
  over 174h, median **136/s**) + orderbook_delta for watchlist series'
  open markets (median **96/s**; re-resolved hourly, reconnect re-seeds
  books) — **the two share ONE buffer, so what sizes it is the sum,
  median 249/s, p90 320, busiest hour 433** (`StreamStore.BUFFER_ROWS_PER_S`;
  quoting the trade channel alone mis-sized both buffer constants by 2.4x,
  mistakes #58); Poly
  books for the top-50 volume markets' tokens + any watchlist pairs
  (hourly refresh). Flushes every 15 s;
  `--smoke N` for a bounded live test. **Watch disk**: observed rate
  extrapolates to low-single-GB/day; parquet rotation is the lever if
  it bites. Box uptime now matters — stream data is unrecoverable.
  **The flush backlog is measured** (2026-09-11, `data/stream_stalls.jsonl`
  + `qa_stream_stalls`). A flush that cannot take the archive's write lock
  holds its rows in RAM and retries; past `SPILL_CAP` the oldest go to a
  JSONL sidecar. Measured over the 7 days to 2026-09-11 (101 episodes):
  **median 15 s, 76 of them a single flush, tail 1756/1801/1876 s, and two
  of that tail hit SPILL_CAP** (34,691 and 286 rows spilled). The cause is
  a long-lived READ-ONLY reader — a duckdb read-only handle takes a shared
  lock the writer cannot get past, and `simulator.shadow` is the named
  holder in both. QA fails on the SPILL, never on duration: a stall inside
  the buffer loses nothing and multi-hour readers are legitimate.
  **The sidecar drains in committed chunks** (`DRAIN_CHUNK_ROWS` 100k,
  byte-offset checkpoint `<sidecar>.done`) since 2026-10-07: the
  whole-file drain cost ~750 B/row resident, so a 1.69M-row sidecar
  OOM-looped streamd for 1h53m, 189 restarts each re-running the same drain
  (mistakes #112). Peak at 1.7M rows is now 378 MB, flat in sidecar size.
  **Poly volume is dominated by one market** since the 10-06 20:20Z restart:
  a token pair snapshotting its full book every ~1s, ~580k snap rows/h
  against ~8k/h before. The buffer fill rate is ~2x `BUFFER_ROWS_PER_S`
  while it stays in the top-50, so SPILL_CAP is ~12 min of firehose, not ~27.
- **Stream coverage expansion (branch `coverage-expansion`, 2026-10-23,
  NOT yet promoted).** Books for `kalshi_book_series` (books-only: the REST
  collector never reads the key) beside `kalshi_series`, sharded at
  `kalshi_book_shard_max` markets per connection (`collector/bookcov.py`;
  shards > 0 store `sid + idx * SHARD_SID_STRIDE`); the set follows
  `market_lifecycle_v2` and is edited in place with `update_subscription`,
  so a set change no longer reconnects (REST reconciliation every 15 min is
  the backstop). New exchange-wide tables `stream_ticker` and
  `stream_lifecycle` (KXMVE dropped unless `kalshi_{ticker,lifecycle}_include_mve`),
  gap channels `ticker`/`lifecycle`. Every Kalshi frame is also kept raw in
  `data/raw_frames/<date>/<HH>-<channel>.jsonl.zst` (`recv_ts\tconn\tframe`
  per line, stdlib zstd). The insert path is now a staged CSV + `read_csv`
  (the stable venv has no pandas/pyarrow): FLUSH_ROWS_PER_S 7,100 -> 100,000.
  `BUFFER_ROWS_PER_S`/`PENDING_ALARM`/`SPILL_CAP` still describe the
  pre-expansion mix and must be re-measured after promotion.
- Initial 60-day retention capture COMPLETE 2026-07-07: 35,144 markets,
  2.6M candles. `python -m hyxlab.sweep --doctor` = health check for
  BOTH archives (includes mirror tripwire + stream counts/size).
- Backfills: `python -m hyxlab.backfill` (Kalshi candles + IEM).
- Migrations: `python -m hyxlab.migrate` (numbered, schema_meta-gated).

## Gotchas

- **A batch unit's wall clock does not tell you whether it worked**
  (EXP-1351, 2026-08-22). systemd emits `Consumed ... over ... wall
  clock time` for FAILED runs exactly as for successful ones, so any
  check that reads duration alone is blind to aborts — and blind in the
  dangerous direction, because an abort TRUNCATES wall clock and makes a
  dying unit look further inside its budget than a working one. The
  sweep's designed `ABORT after 25 consecutive series errors` (exit
  75/TEMPFAIL, "venue degraded; watermarks intact, next run resumes")
  is a normal, expected event on a bad venue night, so this is not a
  rare path. `qa.read_batch_runs` now reads the outcome lines that
  precede the cgroup accounting into `BatchRun.ok`; judge budgets on
  healthy runs only.
- **An aborted sweep makes the NEXT run long.** 08-20 aborted 1h21m in
  at 600/3458 series (7,744 of ~46,000 markets); 08-21 then ran 14.64h
  against a 12.5h budget with 2h15m CPU, near double the usual 1h15m,
  carrying the backlog. 08-22 came back at ~9.6h. So a post-abort breach
  is catch-up, NOT a stale constant — re-measuring the budget upward to
  "fix" it bakes a one-off in and blinds the check to real drift. QA
  attributes this automatically now. The tail risk worth watching: the
  sweep starts 06:10Z and the fade window opens 23:00Z, leaving 16.83h;
  the catch-up run used 14.64h of it, so a deeper abort on a heavier day
  could push a catch-up run into the live window.
- **A healthy sweep's wall is the venue's OPEN-MARKET COUNT, not drift
  (measured 2026-10-15 over 14 healthy runs 09-27..10-14):**
  `wall_min = 97 + 8.4 x (markets / 1000)`, r = 0.89, residual sd 23
  min -- 0.50 s per market, which is `CANDLES_PAUSE_S` 0.35 plus
  ~0.15 s of request latency, times one candlesticks request per
  market. The 12.5h budget is therefore crossed at ~77,800 markets,
  and 10-14 swept 74,681 in 12h28m (748.4 min, +28 residual), one
  minute inside it; 10-09..10-13 swept 59-72k in 10.3-11.3h. The count
  is Kalshi's 15-minute/hourly crypto and index ladders -- on 10-14
  KXSOLD/KXSOLE/KXETH/KXETHD ~7.2k each, KXHYPE 4.9k, KXBTC/KXBTCD
  ~4.5k, KXHYPED/KXNASDAQ100U ~2.8k -- so a day the venue lists ~3k
  more of them breaches, and the breach would be filed as a
  `stale_budget` FAIL that keeps failing. Rate limits are NOT the
  driver: 429 retries fell 2,701/day (10-06) to 525 (10-14) while wall
  rose. This answers the question `BATCH_RUN_BUDGET_H`'s comment left
  open in August (8k-budget backlog or steady state?): steady state,
  proportional to the listing count, with ~4% headroom at the 10-14
  count. The constant stays until a healthy breach is read -- a
  breach IS the finding the check exists to print -- and the response
  to one is a per-market cost or a category decision, never a
  budget bump (the fade-window clearance has 4.3h to spare, so the
  bump would be free and would say nothing).
- **Where the sweep's wall goes, per series (measured 2026-10-16 from
  `sweep_log.swept_at` differences -- each series writes its row in its
  own closing burst, so consecutive gaps are per-series wall; the method
  reproduces the 10-15 done line, 655.4 vs 655.7 min).** 10-15 run:
  16 series over 10 min = 420.5 of 655.4 min (64%) for 50,327 of 64,026
  markets (79%); crypto families (BTC/ETH/SOL/BNB/DOGE/XRP/HYPE/CRYPTO)
  399.7 min (61%) for 44,653 markets. The big ladders are a FLAT
  0.43-0.52 s/market (KXETHD 5,440 mkts 40.4 min; KXSOLD/KXSOLE 5,475
  each 40.0; KXETH 38.8; KXBTCD 33.4; KXBTC 30.2; KXPLATINUMH 23.5;
  KXNASDAQ100U 22.4; KXHYPED/KXHYPE 22.2/19.4 truncated at budget) --
  so for them wall IS listing count, as the paragraph above says. The
  exception is the 15-minute family, which costs per PRINT, not per
  market: **KXBTC15M 44-51 min on every run 10-11..10-15 for 91-98
  markets (27.8-31.8 s/market)** -- 13-14 mid-series flushes of ~275k
  trades = ~3.5-4M prints a day for ~96 fifteen-minute markets, 7% of
  the run's wall for 0.15% of its markets; KXETH15M 7-9 min (4.6-5.3
  s/mkt), KXGOLD15M 7-8 (4.7-5.1), KXXRP15M 4.5-6.8 (3.3-4.2), KXSOL15M
  3.7-5 (2.6-3.1), KXDOGE15M 3-4 (2.0-2.6); the family 92-115 min/run
  since 10-11 (23-30 before it). Per run 10-09..10-15: 678 / 639 / 617
  / 653 / 676 / 748 / 655 min. **The 10-11 step is the TRUE cost
  arriving, not a regression**: KXBTC15M cost 0.8-2.0 min on 10-06..
  10-10 with the same 91-96 markets because those were the 429-storm
  runs (28,562 / 20,105 / 17,933 / 2,286 / 2,836 sweep 429s) in which a
  mid-tape 429 restarted the tape, so its ~3,500-page tape never
  completed -- the journal has ZERO `KXBTC15M mid-series flush` lines
  on those five days against 12-13 on 10-03, 10-04 and every day from
  10-11. `b6971c5` (resume the refused page) and `edba964` (pace pages
  at the caller's 0.35s) on 10-10 made the tape complete again from the
  10-11 run, at the paced price: ~3.5M prints / 1,000 per page = ~3,500
  pages x 0.35s = 20 min of pacing alone, ~47 min with the fetches
  (10-04, unpaced, was 21.2 min for the same 12 flushes). VERIFIED
  2026-10-16 from the journal alone: the 10-16 run's 13 `KXBTC15M
  mid-series flush` lines sum to 3,559,822 prints before the closing
  remainder (<250k), i.e. 3.56-3.8M prints / ~3,600-3,800 pages; the
  flushes land every ~3.3 min for ~270k rows = ~1,370 rows/s, so a page
  costs ~0.73s -- the 0.35s pause plus ~0.38s of fetch, about half each.
  The tape's cost is therefore not reducible below ~23 min by any pacing
  change; only skipping or truncating the tape moves it. The five
  storm days' BTC15M prints were recovered by `hyxlab-tradepass`, which
  drained the 51,777-market backlog at 11.9M / 11.3M / 11.5M prints per
  run on 10-10..10-12 and has been at 49-94 pending since 10-13. So a breach has two candidate responses
  and the attribution line tells them apart: a per-market cost (the
  BTC15M tape, ~50 min = 6.7% of the 12.5h budget, more than the ~4%
  headroom the 10-14 run left) or a category decision (the crypto
  ladders, 61% at the flat rate). Neither is taken until a healthy
  breach is read. The sweep now prints `[sweep] wall attribution: top 5
  of N series = X of Y min (Z%): TICKER Mmin/N mkts (R s/mkt), ...` at
  the end of every run and carries the same five in `totals["slowest"]`
  (`collector.sweep.wall_attribution_line`; first production line at the
  10-17 06:10Z run, expected to name KXBTC15M at ~30 s/mkt beside four
  ladders at ~0.45). **10-16 done line: 740.5 min for 73,245 markets**
  (12h20m; 9.5 min under the 750-min budget, the second run in three
  inside 10 min of it; the 10-15 fit `97 + 8.39 x markets/1k` predicts
  711, residual +29 min, 1.3 sd, after +22 on 10-15 -- two positive
  residuals in a row, so the per-market rate may be edging up from 0.50
  s; the 10-17 attribution line is the read). Budget crossing at the
  fit is ~77.8k markets; the 10-14 run swept 74.3k. **Decomposed by
  hand-differencing `sweep_log` over 10-14 / 10-15 / 10-16** (read
  20:58Z 10-16): the 33 ladders (>= 200 markets, BTC15M aside) cost
  528 / 454 / 538 min for 69,536 / 59,847 / 68,773 markets = 0.456 /
  0.456 / **0.469** s/mkt; the ~3,900 small series a flat 170 / 154 /
  156 min at 2.4-2.6 s/series (the fit's intercept); KXBTC15M 49.8 /
  46.9 / 46.6 min at 30.4-30.9 s/mkt. So the 10-16 residual is a +2.9%
  ladder rate on one day, carried by KXSOLE and KXETH (0.44 -> 0.47)
  while KXSOLD/KXETHD held 0.43-0.44 -- one day, not a trend; the
  attribution line reads it daily from 10-17. Budget headroom restated
  on the ladders, which is where the listings grow: 750 - 156 - 47 =
  ~547 min for ladders = ~70k ladder markets at 0.469 s; 10-14 swept
  69.5k of them. ~2% headroom, not the linear fit's ~4%.
- **The sweep's `~N min left` is weighted by the previous run's
  per-series wall** (2026-10-16, `collector.sweep.prior_series_wall` /
  `eta_minutes`; first production line at the 10-17 06:10Z run). The old
  line was series-rate -- remaining series / series per second -- and
  the cost is per market, with the ~7k-market crypto ladders past index
  3000 of ~3950: on 10-16 it read `~330 min left` at 400/3958 (703
  actual) and `~152` at 3100 (190 actual), and three passes projected a
  done time off it and missed by 1-2h. `sweep_log` has one `swept_at`
  per series, in the same sorted order every run, so consecutive
  differences within the previous run are each series' wall (a read
  inside the listing-refresh burst, no extra attach; the largest run in
  a 30h window, so a hand `--series` repair cannot stand in for the
  day). The remainder is priced at those walls -- median for a series
  with none -- scaled by today's pace over the series already done.
  Replayed on the 10-16 run with the 10-15 prior: 717 vs 703 at 400,
  379 vs 378 at 1600, 164 vs 190 at 3100 (the 3 unpriced series there
  were ladders), 34 vs 38 at 3700. The tag on the line names which
  estimate it is: `(prior-run weighted x1.12, 3 unpriced)` or
  `(series-rate)` when no prior exists. An ETA is advisory: a failed or
  stubbed prior read is `{}`, never an aborted run.
  **First production run, 10-17 06:10Z (done 17:28Z, 677.8 min):** the
  weighted line read 55-70 min HIGH from index 800 to 2500 (555 vs 484
  actual at 800, 429 vs 375 at 1500, 255 vs 202 at 2500 -- 13-22%), then
  174 vs 175 at 3100, 73 vs 65 at 3200, 35 vs 36 at 3700, 2 vs 2 at 3800.
  The cause is structural, not a bug: the prior (10-16) had 68,773
  ladder markets and 10-17 had 61,750, and a series' market count is
  learned only when that series is fetched, so the remainder is priced
  at yesterday's listings until the ladders themselves are done. Read
  the line as an upper bound while the ladders are ahead (indices
  ~700-3200), and as the ETA after.
  **10-20 run (done 18:35Z, 744.4 min; mistakes #130):** the scale read
  x1.16-1.19 from 2700 on and every tail line over-read ~15% (`~244` at
  3100 vs 213 actual, `~103` at 3200 vs 88, `~52` at 3700 vs 38) while
  the 3200 -> done tail ran 88 min = 10-19's 88 to the minute. The pace
  was learned on the first 3100 series, which carried the day's growth
  (KXNASDAQ100U 0 -> 2,800 markets in one 29-min leg against a ~2 s
  "no settled markets" prior; the index/rate ladders settle nothing
  into a Monday run), and applied to a tail that had none. A trailing
  window would have been worse (2500 -> 3100 ran x1.45). The error is
  inside the 5-14% replay band -- read the line with that band, and
  never pre-register a band narrower than it.
  **The progress line carries the by-class terms of the series done so
  far** (2026-10-20, `collector.sweep.class_terms_tag`; first production
  line at the 10-21 06:10Z run): `| done: ladders 25/59244 mkts 0.473
  s/mkt, small 141 min, dense 44 min`. Read against yesterday's line at
  the same index: ladder markets up at the same s/mkt is LISTINGS (the
  ETA's scale over-reads the tail), the same markets at a higher s/mkt
  is PACE (429s, a hold). The scale alone cannot tell the two apart.
  **The run names its listings delta on its first line and in the done
  dict** (2026-10-21, `collector.sweep._prior_run` / `listings_delta` /
  `listings_line`, `totals['new_series']` / `['dropped_series']`; first
  production line at the 10-22 06:10Z run): `[sweep] listings: 4031
  targets vs 3963 swept last run | +84 new (KXBEAVERSNOW, ... +72) | -0
  dropped`. The delta is against the prior run's SERIES SET (the wall
  dict drops the run's first series, which would read as new daily);
  no prior run is `None` / "no prior run in the lookback", never
  "everything is new". Why: the 10-21 run's 4,031 targets vs 10-20's
  3,963 were legible only as `68 unpriced` on the ETA tag, and naming
  them cost a contended 8.2s read-only attach on the live archive
  mid-run (held 0.0s). What they were: 84 series the 10-20 run never
  swept -- 54 Financials = nine international index families (FTSE 100,
  Hang Seng, Ibovespa, KOSPI 200, Nikkei 225, Nifty 50, Russell 2000,
  Shanghai Composite, EURO STOXX 50) x six variants (annual close/high/
  low, daily, weekly, monthly close); 14 MVE exotics (KXMVE*); 13 ski-
  resort openings/snowfall; KXBRENT15M (fifteen_min, 0 markets at its
  07:22Z sweep); 2 MVE mentions. Sized against the existing families
  on 10-20: the big index ladders are the HOURLY ones (KXNASDAQ100U
  2,800 / KXINXU 420 / KXDJI 399 markets); the daily range series are
  ~30 (KXINX 30, KXNASDAQ100 30) and weekly/monthly/annual 0-6. So the
  nine new daily-close series are ~30 each at most, ~270 ladder-class
  markets, inside the ETA's 5-14% band, NOT a new 2,800 term.
- **The sweep prints its wall in the three terms the budget model is
  written in** (2026-10-17, `collector.sweep.wall_by_class` /
  `wall_class_line`, `totals['by_class']`; first production line at the
  10-18 06:10Z run): `[sweep] wall by class: ladders (>=200 mkts) 32
  series 478.4 min/61750 mkts (0.465 s/mkt); dense (>=10 s/mkt) 1 series
  51.2 min/96 mkts (32.0 s/mkt) [KXBTC15M]; small 3924 series 148.2
  min/3541 mkts (2.27 s/series); loop 677.8 of 677.8 min` -- that is the
  10-17 run through the function, and it reproduces the hand read to
  the tenth. Classes are name-free: a ladder is >= 200 markets (count
  first, so a 28 s/mkt ladder is still a ladder); dense is >= 10 s/mkt
  on >= 10 markets (measured over the ~100-130 sub-ladder series with
  >= 10 markets on 10-13..10-17: KXBTC15M 27.8-32.0, next 4.7-6.1
  (KXGOLD15M / KXETH15M), a 20 s gap); the rest is small. The rate, not
  a wall: KXSOL15M swept 0 markets on 10-16 and caught up 192 on 10-17
  (606 s at 3.2 s/mkt), which a wall threshold would have classed dense
  for one day and moved 10 min between classes. **The 10-15 linear fit
  `97 + 8.39 x markets/1k` is RETIRED as the predictor**: its +22/+29/
  +32 residuals (10-15/16/17) were its intercept -- the flat block is
  148-170 min plus 44-51 for KXBTC15M, ~200 min, and the fit lumped
  that into its slope, so every run with fewer listings came in over
  it. Five-run decomposition: ladders 28-33 series, 0.456-0.469 s/mkt
  (10-17: 0.465, flat after 10-16's 0.469); small 138-170 min at
  2.27-2.64 s/series; KXBTC15M 44.1-51.2 min (10-17 is the high, 32.0
  s/mkt, 96 markets). Predictor: `small + dense + ladder_mkts x rate`
  = 148 + 51 + 61,750 x 0.465 / 60 = 678 on 10-17. Headroom unchanged:
  750 - 155 - 50 = ~545 min of ladders = ~70k ladder markets at 0.469.
  Weekend note: the FX / metals / commodity 15M series settle few or no
  markets into a Saturday run (10-11: KXEURUSD15M, KXGOLD15M,
  KXSILVER15M, KXUSDJPY15M all 0; 10-17: 14-36; Sunday 10-12: 56-75)
  while the crypto 15M series hold ~92-97 every day -- market hours,
  not a capture hole.
  **Six-run check, 10-15..10-20:** markets 64.0k/73.2k/65.4k/53.0k/
  62.6k/71.6k against elapsed 655.7/740.5/677.8/551.7/652.5/744.4 --
  ~10.4 min per 1k markets, residuals -10/-21/-2/0/+2/0 -- while
  candles ran 322k/208k/249k/90k/126k/311k, uncorrelated. The market
  count is the venue's SETTLEMENTS in (watermark, now]: the crypto
  ladders a flat ~7.2-7.6k each, the index (KXNASDAQ100U ~2,800,
  KXINXU ~420, KXDJI ~400), rate (KX7YRRATEH / KX10YRRATEH ~250) and
  metals/WTI hourly ladders (+330..+610 on a weekday) absent from the
  run that covers a Sunday. The small class is not a flat block either:
  1.79/2.16/2.55 s/series on 2,125/3,217/4,594 markets (10-18/19/20) =
  ~1.2 s/series + ~1.1 s/mkt, within 4 min on all three.
  **Candles are a close-WEEKDAY term, not a drift (measured 10-21 from
  `sweep_log`, 16 runs 10-06..10-21):** a market's candle count is the
  span from its open to its close, and the index ladders close daily at
  20:00Z, so the Monday-close markets (swept by the TUESDAY run) carry
  the weekend: KXNASDAQ100U 110,662 / 116,724 on the 10-20 / 10-13
  Tuesday runs vs 42,690-45,906 on every Wed-Sat run (same 2,800
  markets); KXINXU 26,194-26,411 vs 8,019-8,397 (420); KXGOLDD 2,510-
  2,824 vs 1,209-1,373 (40). The two index ladders alone are ~137-143k
  on a Tuesday, ~39-54k Wed-Sat, 0 Sun/Mon. So the 10-21 done line's
  195,594 vs 10-20's 311,207 (-115,613, 86k of it those two series,
  the rest series that settled nothing into a Wednesday: the weekly
  gas/diesel series, the one-day KX*SHARE/VREQ AI family) is the
  calendar, and `tapes_skipped` FELL 58,851 -> 56,326, so the 10-19
  resolved-tape skip pruned nothing. Compare a run's candles to the
  SAME WEEKDAY of the prior week, never to the prior day.
- **The poly sweep skips the two fetches for a market a PRIOR run
  archived as resolved** (2026-10-19, `Store.poly_resolved_complete` /
  `collector.poly_sweep._fetch_market`; first production run 10-19
  04:15Z; `"skipped_resolved": N` on the done line). Why: the 10-18 run
  took 1,049.8 min (17.57h unit wall, over the 17.5h budget, FAIL as
  pre-registered) and the per-leg regression of its journal (93 progress
  lines, 10-17 and 10-18) put ~80% of the wall in a FIXED per-market
  term (2.7-2.8 s/market), not in content: tail stops cost one request
  each (`trades_tail` has no retry), prices explained nothing, and the
  slow 6,000-7,800 block (5-8 s/market both days, 09:00-11:45Z) matched
  no content term -- time of day, not index. The 10-18 03:30Z backup
  (`data/backups/hyxlab.Sun.duckdb`, read without touching any live
  file) then showed the structure: the enumeration is `closed=false`
  (6,038 open) PLUS `closed=true` with `end_date_min` = now - 7d, and
  Gamma's `endDate` is the NOMINAL end (resolved markets average 17
  days of future endDate), so 12,458 of 18,496 markets (67%) were
  resolved, costing 45,140 of 57,224 price chunks and 23,875 of 35,805
  tail pages (74% of ~93k requests, plus ~0.25 s pacing each; pacing
  alone was ~315 min of the 1,003) for 0 new rows: the 10,179 resolved
  markets dormant 2+ days carried 0 new prints in a 41h window (1
  outlier of 10,179). Also measured and NOT used: 946 markets have no
  price watermark at all (10 empty chunks + 2 s of pauses daily); 2,221
  tokens have watermarks older than 7 days (multi-chunk empty walks);
  Gamma `volumeNum` is unchanged for 12,703 markets but drifts on 695
  resolved ones with no trades, so it is not a trade signal. The skip
  needs BOTH Gamma's `closed` AND `markets.result` set by a prior run:
  the 1,334 markets closed under 2 days carried 716,835 prints -- the
  final tape lands in the run that first sees the close -- and a
  resolved market with a tail stop no later run has out-fetched is
  excluded (`qa.qa_poly_tail_absorbed`'s re-visit oracle is a later
  `poly_market_stats` row, which a skipped market still writes so the
  universe-shrink tripwire stays flat). Expected: ~11,100 markets
  skipped, wall ~7-8h from 17.5h; the ETA line on the first run is
  priced by yesterday's full walls at a tiny pace scale and reads LOW
  for the open markets, then corrects from 10-20. Tail-side levers
  measured for later: newest-first paging confirmed live (page 0 vs
  offset 500), so a watermark stop would cut the open markets' 12,474
  tail pages to 6,833; `REQUEST_PAUSE_S` 0.25 x 3-11 pauses per market.
  **First skip run, read (10-19 04:15Z -> 12:02Z):** `elapsed_min`
  463.1 (7.72h; unit wall 7h47m against 17h34m on 10-18, -56%),
  `skipped_resolved` 10,798 of 12,370 closed (1,572 closed markets still
  fetched: first seen closed today, or owing a tail stop), `n_prices`
  1,015,149 vs 1,002,505 on 10-18 (the skipped markets contributed ~0
  price rows, as the backup measure said), `n_trades` 809,606 vs
  901,182 / 847,020 / 941,184 on 10-18/17/16 (the low end of the band,
  Sunday->Monday; not attributable without the 10-20 backup diff),
  `n_tail_stops` 8 vs 49, errors 6 (4 clob HTTP 500 in one 05:36Z
  minute, 2 ReadTimeouts), CPU 2h34m vs 3h45m. Hold on the collector's
  file: 741 attaches held 6,088.7s total (vs 9,090.6 / 8,998.8), worst
  29.4s, 53 contended / 106.0s slept (vs 32 / 64.0s -- the run now sits
  entirely inside the kalshi sweep's window, 06:10Z on, so more of its
  flushes meet one). PSI 56.8s = 0.20% of 7.78h (vs 0.10-0.11% on
  16-17h runs: same absolute stall, half the wall), peak 6G == cap with
  8.7M reclaim events, oom_kill 0 -- immaterial. **The progress trace
  across the boundary:** trades 91,758 at index 6,000 (the 6,105 open
  markets) and 809,606 at done, so ~718k of the run's trade rows -- 89%
  -- came from the ~1,570 closed-but-fetched markets, the final prints
  before a close, at ~6.7 s/market (190 min over indices 6,000-18,475,
  of which the 10,798 skipped cost ~0) against 2.7 s/market for the
  open segment (275 min / 6,105). Hand-differenced; **so the done line
  now carries `"segments"`** (shipped 10-19 ~14:40Z, first production
  line 10-20 04:15Z): `open` / `closed_fetched` / `closed_skipped`, each
  with `markets`, FETCHED `prices` / `trades` (pre-dedup, counted at the
  fetch, so the skipped segment is 0 by construction; `n_prices` /
  `n_trades` stay the post-dedup insert counts) and `wall_min` (loop
  wall per segment), plus `flush_min` beside them so the four walls sum
  to `elapsed_min` less the enumeration. Read on 10-20: closed_fetched
  markets (expect ~1,300-1,800) and its trades share (expect the
  majority), closed_skipped wall (expect ~0), open s/market vs 2.7.
  The ETA pair on this run, mean HIGH / window LOW from 7,200 on (at
  12,000: `~190 / ~129` for 112 actual; 14,400: `~112 / ~61` for 69;
  16,800: `~43 / ~21` for 29): the pre-registered bracket, held;
  from 10-20 the prior-weighted line prices each market at today's wall.
  QA: the batch-budget check reads the worst completed run in 7d, so
  `hyxlab-poly-sweep.timer 17.57h/17.5h` (the 10-18 run) stays FAIL and
  the digest ATTENTION until that run ages out on 10-25. Re-measured
  2026-10-21 on the first three skip-era done lines (10-19 463.1 min /
  10-20 419.9 / 10-21 452.2; unit walls 7.72 / 7.00 / 7.61h, the last
  carrying 393 fetch errors from the CLOB 500 incident): the budget is
  **8.0h**, the max rounded up to the half hour, the sweep's convention.
  **10-22 (first run on the fixed code, with the 322 unmarked markets
  back in the fetch path): 486.3 min / 8.17h unit wall = FAIL by 10
  min, and the whole excess is in `closed_fetched`** -- 1,604 markets /
  134.0 min vs 1,056 / 88.8 (10-20) and 1,225 / 112.2 (10-21), i.e.
  ~5.0 s per closed market fetched, so the repair cost 322 x 5.0 s =
  27 min; `open` 6,289 / 236.4 min (6,081 / 222.3, 6,168 / 227.8: +~7
  min/day with the universe, ~2.26 s/market), `flush` 115.8 (108.7,
  112.2). 486 - 27 = 459, inside the 420-463 band: **the 8.0h constant
  stands**, the 10-22 line FAILs until it ages out on 10-29, and from
  10-23 the QA breach line carries the segments so the reading is on
  the line (`BatchRun.segments`, mistakes #132). The Kalshi sweep's
  `by_class` dict-repr done line is read the same way (ladder / dense /
  small, `wall_s` rendered as min). The 10-23 line is the
  first clean read on the fixed code: expect closed_fetched ~1,200-1,350
  and elapsed ~455-470 -- note the band's ceiling (463) is now within
  ~5 days of the universe trend's reach of 480 even with nothing
  wrong, which is the drift the check exists to show, not a reason to
  pre-raise it.
  **A market whose fetch RAISED keeps neither its Gamma row nor its
  stats row** (2026-10-21, `collector.poly_sweep.sweep` except branch;
  first production run 10-22 04:15Z). The skip's premise is "a prior
  run saw the market closed AND fetched to that instant", but the loop
  appended the Gamma row (which sets `markets.result` for a closed
  market) and the stats row (the tail oracle's "a run fetched after
  the stop") BEFORE the two fetches and kept both when the fetch raised
  -- so a market first seen closed on an incident day was archived as
  resolved with no prices and no tail, and the next run skipped it for
  good (prices are a ~60-day rolling window, the tail capped at 3,000).
  Measured 2026-10-21: 87 CLOB `500 Server Error` on prices-history
  from 09:04Z (10-18: 0, 10-19: 4, 10-20: 0), 1-7 a minute, still
  landing as the loop crossed the open/closed boundary at 09:17Z;
  `prices_history` has no 5xx retry, so one 500 ends the market's day.
  Now the except branch truncates `infos`/`stats` back to the market's
  entry; the sweep-log row (the ETA's prior) and whatever prices the
  first token put in the batch still land (the watermark is max(ts), so
  a partial token re-reads tomorrow). The 10-21 run itself ran the OLD
  code, and its done line (11:51Z) carried `errors 393` (`HTTPError`
  377, `ReadTimeout` 15, `ConnectionError` 1), 330 of them after the
  09:17Z boundary; 322 of those markets were archived with `result`
  set (306 first seen that day, 24 with any trades). Repaired the same
  day with `collector.poly_unmark_errors` (dry run by default; `--since
  "2026-10-21 09:17" --until "2026-10-21 12:00" --commit`): it parses
  the sweep's `[poly] <Class> at <cond16>:` lines in a UTC window,
  resolves the prefixes exactly as `poly_tail_backfill` does (zero or
  many matches dropped, never guessed; 330/330 unique), and clears
  `result` through one `writer_burst` (`Store.poly_unset_results`; held
  0.2s over two attaches, 0 contended) -- so the 10-22 04:15Z run, the
  first on the fixed code, sees them as unresolved and fetches prices
  from the watermark and the tail from offset 0, and its Gamma row sets
  `result` again. The stats rows stay (no tail stop exists for them to
  absorb, and deleting them would move the universe tripwire). The
  before-values are in `data/poly_unmark_2026-10-21.tsv` (gitignored).
  Not a direct re-fetch: nothing trades on a closed market, their
  `endDate` keeps them in the 7-day closed enumeration window, and the
  API was still returning 500s at the done line. The 63 pre-boundary
  errors were open-segment markets, re-fetched tomorrow by construction.
- **The poly sweep's `~N min left` prints two named estimates**
  (2026-10-17, `collector.poly_sweep.eta_minutes`; first production line
  at the 10-18 04:15Z run): `~404 min left (mean rate; ~330 at the last
  200's rate)`. The mean rate since the start carries the run's slow
  opening to its last line -- 10-17 read `~404` at 11,600/18,497 for 328
  actual and `~60` at 17,400 for 48 -- and three passes pre-registered a
  17.5h budget FAIL off it (the run closed at 1007.2 min, 16.8h). The
  second number prices the remainder at the trailing window's rate.
  **First production read (10-18 04:15Z run): the two converge by
  market 4,800 (`~675 / ~681`) and are both ~100 min LOW there**,
  because the trade-heavy markets sit mid-rank (10-17: trades 91k at
  4,800 -> 770k at 14,200, 10-13 min per 200 through 9,000-14,000 vs 7
  at 4,000; 10-18: 18-24 min per 200 through 6,000-7,200), and by
  10,000 the mean has carried those legs past the point they are done
  and both read ~85 min HIGH (`~509 / ~506` at 14:09:55Z against
  yesterday's 423 min from the same index). A rate from either end of
  the run is wrong in the direction of whatever it has or has not yet
  seen. **Fixed the way the kalshi sweep was (2026-10-18,
  `collector.poly_sweep.prior_market_wall` / `eta_weighted`,
  `poly_sweep_log`; first rows written by the 10-19 04:15Z run, first
  weighted line the 10-20 run):** the loop now records each market's
  own wall -- `(market_id, swept_at, wall_s)`, one row per market per
  run, written in the same `_flush` burst as its prices, keyed so a
  re-flushed batch dedups -- and the next run prices every remaining
  market at its own latest wall within 30h (median for a new one),
  scaled by today's pace over the markets done. The line is TAGGED
  `(prior-run weighted x1.05, 12 unpriced; ~M at the last 200's rate)`
  or, with no prior, `(mean rate; ...)` -- the window estimate stays as
  the pace reading. Per-market, not positional: Gamma's volume-desc
  order moves between runs (10-17's trade-heavy block sat at
  10,200-11,200, 10-18's at 6,200-7,000) and the prior follows the
  market. Until the first weighted line, the done-time projection is
  yesterday's wall from the same index. **Two prices corrected after the
  first weighted run (2026-10-20, mistakes #128/#129):** a market the run
  SKIPS (`skipped_ids`, decided up front) costs 0.0 on both sides of the
  scale whatever its prior says -- a market resolved between the two runs
  was FETCHED yesterday and is skipped today, and 1,671 of those (median
  5.3 s, 132 min unscaled) read `~125 min left` at 11,600 for 75 actual
  while walking the scale 1.44 -> 1.24; and an unpriced CLOSED market is a
  first-seen-closed fetch (1,056 ran 5.0 s each on 10-20, 88.8 min) and
  costs the median prior of yesterday's first-seen-closed set -- today's
  skip members whose prior is above `SKIP_WALL_MAX_S` (0.01 s; the skip
  branch is microseconds, a fetch is a round trip) -- not the open-
  dominated 1.56 s fetched median. Replayed on the 10-20 backup against
  the 10-20 journal the corrected line is within ~10 min through 11,600
  (old: +42; skip-fix alone: -75). First production line: the 10-21
  04:15Z run. **And the per-200 wall rises ~30-40% for ~45 min in
  the kalshi sweep's `KXBTC15M mid-series flush` window** (10-17
  07:57-08:42Z, 10-18 from 07:39Z: ~280k-trade flushes every 3.5 min on
  the same `hyxlab.duckdb`), with flat content per leg and the
  collector's `wait_s` on that file rising in the same minutes -- lock
  contention, not drift; ~12-15 min per poly run, bounded by the leg.

- `pgrep -f` matches your own command string — quote patterns or match
  the python binary path; this caused false "sweep alive" reads.
- Long background jobs: use `python -u` (buffered stdout hid 4h of
  sweep progress).
- Kalshi candle bid/ask closes are unsynchronized within the hour →
  crossed/sentinel quotes (1.3% of candles). Excluded at replay by the
  gate in `candles_as_snapshots` — see [simulation-honesty](simulation-honesty.md).
- Data written before 2026-07-06 tz fix was box-local; already migrated.
- **Identical per-series truncation counts across runs are NOT stall**
  (verified 2026-08-05): fixed-cadence crypto series (KXETH 3600,
  KXETHD 3340, KXSOLD/KXSOLE 3375) settle a deterministic number of
  markets per day, and the windowed budget accepts ~one day's
  production per run, so the truncation count is a constant of the
  series, not a cursor position. Rot is judged from `sweep_log`
  watermark advancement, never from the count: each run must advance
  `max_close` ~18–25h; the steady state is a constant ~2-day lag,
  safe against the 60–90d purge. A watermark that repeats across runs
  is the real stall signature.
- **Venue-side outages trip the sweep's circuit breaker** (2026-08-06):
  Kalshi's `/markets` endpoint degraded for ~1h (503s + a 429 storm
  that outlasted the 4-try exponential backoff) — every series from
  ~550 onward errored while `/markets/trades` kept serving (tradepass
  succeeded mid-window). `run_sweep` now aborts after
  `ABORT_CONSEC_ERRORS` (25) unbroken series failures and `main` exits
  75 so systemd records the failure; watermarks stay untouched, so the
  next timer firing resumes exactly where the aborted run stopped.
  One success resets the count — scattered organic errors never trip
  it (`tests/test_hyxlab_sweep_breaker.py`). An outage run is a
  DELAYED sweep, never a lost one; the recovery signal is the next
  run's larger-than-usual market count, not any repair action.
- **A recovery-scale series can turn the per-series writer burst into a
  multi-minute lock hold** (measured 2026-08-07): KXBTC's post-outage
  backlog buffered ~3.85M trade rows and the single end-of-series
  `writer_burst` held `data/writer.lock` for ~21 min — four consecutive
  collect cycles exited 75, a 20-min capture gap and the QA
  skipped-cycles FAIL. Fixed same day: `sweep_series` flushes its
  buffers mid-series every `FLUSH_ROWS` (250k) rows, keeping each burst
  under collect's 300s open budget. Crash-safety is unchanged — every
  intermediate write is idempotent and the watermark advances only in
  the final burst (`tests/test_hyxlab_writer_lock.py`). The QA FAIL was
  truthful; the budget (3 skips/24h) was not touched.
- **The negative econ-vintage age was by design; the CHECK reading it
  was not** (2026-08-06 explanation, corrected 2026-08-24 / EXP-1360).
  The stamp is correct and stays: `alfred.pessimistic_knowable_at`
  puts vintages at 23:59 US/Eastern on the FETCH date, the signals
  timer fires 04:40Z (already the next ET date), so the stamp lands
  ~04:00Z the following day — up to ~28h ahead of a morning QA run.
  Pessimism only DELAYS knowability, so it can never create lookahead.
  What the 08-06 note got wrong is the conclusion drawn from it: it
  explained the number and left the check measuring it. `now -
  max(knowable_at)` is (ingest staleness − pessimism margin), so
  `econ vintages fresh (< 8 days)` read `age -0.6d` and PASSED, and a
  5-day outage would have read inside a 4-day budget. **A freshness
  measure that can go negative is not measuring freshness.** Replaced
  by `econ pull live (any series, last vintage date)`, which measures
  the vintage DATE recovered from the stamp — cancelling the nuisance
  exactly, because the stamp is a deterministic function of it. See
  mistakes #31.
- **Per-series econ coverage is NOT visible in the archive.**
  `econ_vintages` gains a row only when a value CHANGES, so a monthly
  series ALFRED has dropped and one that simply has not printed yet
  are the same table for a month; and `fetch_alfred` retries three
  times, prints, and moves on with the series absent from its result.
  On 2026-08-24 four of seven series sat 10.2–15.2d stale under a
  green pooled check. The witness is `data/signals_fetch.jsonl`
  (`collector.signals.record_fetch`, one JSON line per pull with each
  series' fetch outcome), read by `qa_signals_fetch`, which decides an
  absent sidecar against the archive rather than trusting it.

- **The July 2026 focus tape has a DAILY ~11:10-11:45Z hole (measured
  2026-10-15, read-only):** over 07-09..08-02 every day with snapshots
  has zero collector cycles between 11:10Z and 11:40Z while the cycles
  on either side are present (07-22: cycles at 11:05 then 11:50; 5-6 of
  the 7 possible cycles in the neighbouring half-hours). Closed from
  08-03 on (6/6 cycles in the window on 08-05 and 08-06), which is the
  08-02 breadth/watchlist widening's first days. The cause is outside
  journal retention (boots start 09-06) and is recorded as UNRESOLVED,
  not guessed. Why it matters: the hole sits exactly on the one window
  the econ-claims registration reads (`[close-60min, close-50min]` on
  a 12:25Z close), so the 07-16/07-23/07-30 `KXJOBLESSCLAIMS` events are
  quoted but NOT scorable, and the registration's sample starts 08-06.
  A strategy that reads one fixed window a day is blind to a daily hole
  at that window in a way a continuity check averaging over the day is
  not -- check the window, not the day.

## Gotchas (stream)

- **Box clock is ~20 s fast; NTP is OFF** (found by the 2026-07-07 stream
  audit: recv_ts − src_ts constant ≈ +19.5 s across trades AND deltas;
  confirmed vs Kalshi HTTP Date). All box-generated timestamps
  (recv_ts, collector snapshot ts) carry this skew until the user runs
  `sudo timedatectl set-ntp true`. Venue-sourced timestamps (src_ts,
  candle end_ts) are true time — prefer src_ts for stream analysis.
  When NTP lands, the daemon's clock tripwire logs the backward step as
  a `clock_step_*` gap row.

- Kalshi WS frames use STRING-DOLLAR fields (`yes_price_dollars`,
  `count_fp`, `price_dollars`, `delta_fp`, `{yes,no}_dollars_fp`) — NOT
  the integer cents older docs suggest. Re-probed live 2026-07-07; the
  first build assumed cents and captured zero rows.
- Polymarket WS has no sequence numbers: disconnects are the only
  detectable gaps; every reconnect logs one and the fresh `book` re-seeds.

## Next (planned, user-approved)

Trade-tape retro-pass B3.5 (races retention); then ALFRED/GDELT
ingestion behind a `FeatureView` as-of API.

## Related
- [venues](venues.md) — sources and their limits
- [hyxlab-architecture](hyxlab-architecture.md) — where this layer sits


## Signal feeds & backups (B4, 2026-07-11/12)

- `hyxlab-atlas.timer` (weekly, Mon 02:48 UTC; since 2026-10-17): the
  calibration atlas (`simulator.atlas`) on the settled corpus of the
  collector's file. Replaces a ~6-day hand cadence (09-26, 10-07,
  10-13, 10-17). Weekly, not daily, because every reading is a LOOK at
  the quoted tier (`quoted_looks`). The hour is the only window in
  which no multi-hour writer of `data/hyxlab.duckdb` is alive (poly
  sweep 04:15Z + up to 17.4h, Kalshi sweep 06:10Z + 12.5h), pinned by
  `tests/test_systemd_units.py::test_the_atlas_runs_while_no_sweep_holds_the_archive`
  against the sweeps' own timers; the seconds follow the tradepass rule
  (mistakes #125). Measured: 3.8s wall, 2.6G peak, one 3.2s hold.
  Reports land in the shared `reports/atlas/` (HYXLAB_REPORTS_DIR); the
  report's own stability fields read the priors from there, so the
  decision rule stands: re-ask pre-registration only when
  `quoted_verdict.family.confirmed_family` is non-empty.
- `hyxlab-signals.timer` (04:40 UTC): ALFRED econ vintages (7 series,
  value-diffed daily — the keyless endpoint restamps knowable_at each
  fetch day, a naive insert would forge vintages) + GDELT bulk GKG
  (15-min grid from the news watermark; filter-and-discard against
  collector/queries/gdelt.json). QA guards the pull's liveness
  (`econ pull live`) and its per-series coverage (`econ series all
  fetched`) SEPARATELY — see the gotcha above for why one check
  cannot do both.
- ALFRED gotcha: a timed-out request wedges the shared keep-alive
  session; every later request on it times out. Fresh session per
  attempt (collector/signals.py).
- `hyxlab-backup.timer` (03:30 UTC): holding a read-only DuckDB attach
  excludes writers → consistent file copy; 7-slot weekday rotation in
  data/backups (local tier); point HYXLAB_BACKUP_DIR at a mount for
  off-box (standing user item). Since 2026-10-16 the attach goes through
  `hyxlab.store.held_attach` (30 x 2.0s, the same budget the hand-rolled
  ladder had), so `hold` on the per-file journal line IS the ledger's
  `held_s` -- timed from the open to the close, recorded in `finally` --
  and each file is followed by a db-scoped `[backup] attach_wait: on
  <file> ...` line through the one renderer. The hand clock it replaces
  started after the open, stopped before the release, and printed
  nothing when the copy raised. `collector/backup.py` is in the pinned
  publisher set (`tests/test_hold_discipline.py`); the
  `test_connect_discipline.py` RETRY allowlist entry was deleted, not
  emptied.
- Poly PRICE day-bucket counts MATURE for many days (later sweeps
  backfill history into past days), so fresh-vs-matured comparisons
  overstate decline without bound — measured 2026-08-23: 10,984 at
  nine days back vs 6,302 at one day back, with nothing wrong. This
  was recorded here in 2026-07 with the conclusion "the shrink
  tripwire's 0.5 threshold absorbs it", which was WRONG: a threshold
  cannot absorb an unbounded drift, and the tripwire was structurally
  pinned just above its floor. Never compare a maturing count across
  ages; use `poly_market_stats`, which is stamped once per run and does
  not mature.
