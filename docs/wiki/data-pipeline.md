# Data pipeline (hyxlab archive)

Two DuckDB files (gitignored, **irreplaceable — need off-box backup**):
`data/hyxlab.duckdb` (polled archive; collectors/sweeps write, sim reads)
and `data/hyxstream.duckdb` (WS stream archive; the stream daemon is its
sole writer — separate file because DuckDB's single-writer rule would
deadlock a long-lived daemon against the 5-min collector).

## Tables

Append-only facts: `candles` (hourly price+bid/ask OHLC, volume, OI),
`snapshots` (live top-of-book), `nws_forecasts` (fetched_at = as-issued
time), `observations` (climate-report highs). Reference: `markets`
(metadata + settlement result), `series` (category/fee metadata),
`sweep_log`, `watermarks`, `schema_meta`.

Stream archive (`streamstore.py`): `book_events` (kalshi snap qty =
absolute level, delta qty = SIGNED change; poly delta qty = new ABSOLUTE
size; poly market_id = CLOB token id), `stream_trades`, `stream_gaps`
(closed intervals of broken coverage — reconnects, Kalshi seq jumps,
daemon downtime; replay must treat books as unknown inside a gap until
the next snapshot re-seeds).

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
- `hyxlab-qa.timer` (daily 07:00 UTC): data-quality checks, both
  archives; FAIL lines land in the journal with exit 1.
- `hyxlab-stream.service` (long-running, Restart=always, live since
  2026-07-07): `python -u -m hyxlab.streamd` — Kalshi exchange-wide
  trade firehose (~105 ev/s) + orderbook_delta for watchlist series'
  open markets (re-resolved hourly, reconnect re-seeds books); Poly
  books for the top-50 volume markets' tokens + any watchlist pairs
  (hourly refresh). Flushes every 15 s;
  `--smoke N` for a bounded live test. **Watch disk**: observed rate
  extrapolates to low-single-GB/day; parquet rotation is the lever if
  it bites. Box uptime now matters — stream data is unrecoverable.
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
  off-box (standing user item).
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
