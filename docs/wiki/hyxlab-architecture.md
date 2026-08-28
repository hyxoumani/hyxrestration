# hyxlab architecture

Strategy-testing lab for prediction markets. Full component spec:
`docs/plans/hyxlab-v2/proposal.md` (C1–C8); contract validation:
`docs/plans/hyxlab-v2/data_contracts.md`.

## Layers

Physical package split (2026-07-09): four root packages with the import
boundary test-enforced by `tests/test_boundaries.py` — `collector/` and
`simulator/` never import each other; `strategies/` may import
simulator + kernel; `hyxlab/` is the shared kernel (models, store,
streamstore, fees, migrate, watchlist, stations). Simulator runner
entrypoints (`run_sim`, `run_backtest`, `shadow`, `simui/server`) are
the only sim modules allowed to wire in `strategies`; the engine stays
strategy-agnostic.

```
collector/ (venues/*, sweeps, streamd, qa)  →  archive store (hyxlab/store.py, DuckDB)
        ↓ scheduled by systemd timers (collect 5min, sweep daily)
alignment (Context / FeatureView planned)   ← the ONLY time-sensitive read path
        ↓
sim engine (simulator/sim.py) ←→ strategies (strategies/*)
        ↓
harness manifests (simulator/harness.py → data/runs/)  +  self-tests (tests/)
```

## Module map

- `hyxlab/models.py` — typed records (Snapshot, MarketInfo, Order incl.
  open/close + GTC/IOC, Cancel, Fill, Forecast, EconVintage, NewsItem).
- `collector/venues/` — kalshi, polymarket, nws, iem, alfred, alpaca_news
  (pure fetch→records; sessions injected; fixtures in tests/fixtures/);
  kalshi_ws + polymarket_ws (WS auth/payloads/parsers, no sockets).
- `hyxlab/store.py` — schema, naive-UTC, insert_new dedup, watermarks,
  candles_as_snapshots (with crossed-candle gate), mirror tripwire. Also
  the repo's ONLY `duckdb.connect` (`test_sidecar_discipline.py`), which
  makes it the chokepoint where every attach gets a private spill
  directory (`private_spill`), a cgroup-derived buffer budget
  (`cgroup_memory_limit`) and a bounded spill (`spill_cap`) — in that
  ORDER, since the spill bound is a multiple of the memory limit.
- `hyxlab/memcap.py` — DuckDB's `memory_limit` taken from the cgroup, not
  from host RAM (EXP-1374). DuckDB defaults to 80% of `/proc/meminfo`,
  which a `MemoryMax=`-capped service does not have; streamd's own
  startup `last_recv_ts` read peaked at 2899 MB against a 2048 MB cap and
  was OOM-killed. Half the cap, spills nothing, and a no-op where no
  cgroup cap binds. Guard: `tests/test_memcap_discipline.py`.
- `hyxlab/spillcap.py` — how much DISK a spilling attach may take
  (EXP-1375). DuckDB's `max_temp_directory_size` defaults to "90% of
  available disk space", 1.26 TB of the volume the archives and the
  collector share. Measured first: the sliced walk spills 45 MiB over
  72 h / 26.0M rows, and the queries that need more DIE in memory rather
  than spilling (atlas at 0.1 s having written nothing) — the largest
  spill by a query that SUCCEEDED is 266 MiB. So the bound rescues
  nothing; it replaces a number that is the disk. The tighter of 8x
  `memory_limit` and 25% of free space. Guard:
  `tests/test_spillcap_discipline.py`.
- `Store.markets()` — market metadata keyed (venue, market_id), and the
  largest Python-heap allocation in the repo: unfiltered it is 1.87M
  MarketInfo objects, **1.32 GiB resident / 1.56 GiB traced peak**
  (measured 2026-08-27, EXP-1378; 486k rows / ~430 MB three weeks
  earlier), sized by the ARCHIVE rather than by anything a caller
  chooses. No `memory_limit` reaches it — it is the heap the cgroup
  kills the process for, and simui's page view under `MemoryMax=1G` was
  oom-killed in 1.257 s on it. Bound the load: `market_ids=` for a
  caller that knows its id set (a replay's set is the ids `book_events`
  carries over its window — 714 of 1.87M for 3 h), `alive_days=` for a
  daemon, `include=` to pin a held position past either. Guard:
  `tests/test_markets_load_discipline.py` — every call bounds or is one
  of five enumerated offline one-shots.
- `hyxlab/streamstore.py` — stream archive (own DuckDB: book_events,
  stream_trades, stream_gaps; buffered flush bursts).
- `collector/streamd.py` — stream daemon (asyncio, reconnect/re-seed/
  gap-marking; systemd `hyxlab-stream.service`).
- `collector/sweep.py` — exchange-wide archival sweep + `--doctor`.
- `collector/trades_backfill.py` — trade-tape retro-pass (races retention).
- `collector/qa.py` — daily data-quality checks (`hyxlab-qa.timer`).
- `simulator/bookreplay.py` — stream events → ms-fidelity Snapshot stream
  (gap-honest, complete-image emission, mirror-derived asks). Holds
  `stream_events`, THE ONE walk over `book_events`: 6h slices, each sorted
  separately, so peak memory is flat in window length rather than linear
  (measured 2026-08-27 at a 512 MiB engine limit over 72 h / 23.4M rows:
  unsliced peaks at 1826 MiB of spill, sliced spills nothing). Every
  caller goes through it — reports, the L2 backtest AND the shadow
  daemon's boot seed (mistake #37). `lo_inclusive=True` is for SEED
  callers only: their `lo` is a gap's `ended_at`, and a seq_reset gap ends
  AT the reconnect image that re-seeds the book, which the default
  half-open `(lo, hi]` would drop. The SLICE bounds what the engine
  sorts; `EVENT_CHUNK` bounds what PYTHON holds, and until 2026-08-28 it
  did not: at 200,000 rows one batch is **125.5 MiB of materialised
  tuples and BookEvents** — 184.0 of a 3 h `run_l2`'s 194.7 MiB traced
  peak, 15x the equity curve the ladder had named (EXP-1380). Measured
  at **660 bytes/row**, linear from 1k to 200k, with the walk FLAT in
  time (17.3-19.2 s) and IDENTICAL in answer (270,402 snapshots,
  sha `9d52d498c6fd0e4f`, at every chunk size) — so the constant is a
  BUDGET divided by a measured row cost, not a tuned row count. 5,000
  rows / 4 MiB; `run_l2` end to end 194.7 -> 83.7 MiB. This is the walk
  `hyxlab-shadow` (`MemoryMax=1G`) seeds through at every boot. Guard:
  `tests/test_event_batch_discipline.py`.
- `simulator/sim.py` — event loop (`step()`/`finalize()`/`run()`), order
  lifecycle, runtime invariants, latency model (`latency=Δ`; Δ=0 = legacy).
- `simulator/shadow.py` — Tier-3 shadow harness (`hyxlab-shadow.service`):
  live Simulator on a stream-archive tail, ledger-only fills per run_id.
  Boot seeds books through `bookreplay.stream_events`, bounded ABOVE at
  the anchor (an unbounded seed and the first poll both apply anything
  that lands in between — a delta counted twice is a book that never
  existed). Its `stream_conn` lowers `memory_limit` to DUCK_MEM and then
  re-derives `spill_cap`: it is the one site that moves the limit after
  passing the connect chokepoint, and the cap is a multiple of the limit
  in force (verified live under `MemoryMax=1G`: 512 MiB / 4.0 GiB).
- `simulator/simui/` — interactive market-replay UI (`python -m
  simulator.simui`, localhost:8877): archived event groups replay like a
  live Kalshi event page; user + strategy orders fill through the real
  Simulator (ManualTrader queue → step()). Runs from stable under
  `MemoryMax=1G` and is the one daemon `promote.sh` never auto-restarts
  (a restart drops a live paper session) — it prints a NOTICE instead,
  so "not restarted" is not spelled "not mentioned". session.py
  (ReplaySession; every metadata load is bounded to the ids it renders;
  seek = flat restart; chunked advance proven ≡ one-shot sim.run),
  server.py (websockets clock, guarded — errors log+pause, never die
  silently), static/index.html (single-file Kalshi-style UI with WS
  auto-reconnect). Design: `docs/plans/simui/plan.md`.
- `collector/poly_sweep.py` — Polymarket archival sweep (daily timer).
- `simulator/strategy.py` — Strategy ABC (+ `requires` capability
  declaration) + Context (hides settlements, as-of forecasts,
  open_orders for Cancel).
- `simulator/capabilities.py` — strategy↔data capability contract
  (vacuous backtests raise instead of returning zero).
- `hyxlab/fees.py` — parabolic models, per-series `kalshi_model()`.
- `simulator/harness.py` — run manifests (git rev, params, fingerprint).
- `hyxlab/migrate.py` — numbered migrations.
- `collector/signals.py` — daily ALFRED+GDELT pull (`hyxlab-signals.timer`;
  value-diffed vintages, watermarked GKG grid; fresh session per ALFRED
  attempt — timeouts wedge keep-alive connections).
- `collector/backup.py` — read-lock-consistent 7-slot archive backups
  (`hyxlab-backup.timer`; HYXLAB_BACKUP_DIR for off-box).
- `collector/venues/gdelt.py` + `collector/queries/gdelt.json` — bulk
  15-min GKG filter-and-discard, prefix-matched topic templates.
- `simulator/features.py` — FeatureView as-of gate (P1): econ vintage
  semantics, news windows, forecast index; Context delegates.
- `simulator/divergence.py` — shadow-vs-replay report (exact
  convergence proven post-fixes; qty-weighted v2 matching).
- `simulator/queuebounds.py` + `simulator/queuescore.py` — FIFO maker
  queue-position bounds and the crossing-rule calibration bracket.
- `simulator/atlas.py` — calibration atlas (implied vs realized,
  Wilson flags; plus a cluster-robust flag tier with n = distinct
  (series, close_time) ladders — the correlation worst case; pre-regs
  size evidence on `clusters`, not n). Consecutive atlas runs are only
  independent evidence for a bucket that GAINED observations in
  between — diff `data_fingerprint.observations_by_category_horizon`
  for the bucket's own (category, horizon) before counting a flat
  bucket as a confirming reading. `settled_by_category` is the coarser
  07-27 version and is NOT sufficient: it answers at category level
  while the bucket key is (category, horizon, decile). Two ways a
  reading is a replay rather than evidence — (a) calendar: index-ladder
  categories (Financials = KXDJI/KXINXU) add zero settled markets over
  a weekend, so weekend re-runs replay verbatim (07-27); (b)
  structural: a market only enters the horizon-h bucket if it carries a
  candle h before close, and same-day index ladders never do, so
  **Financials 24h is a frozen population that no cadence re-tests** —
  the 07-29 sweep added +1,113 Financials markets, 1h +1,036 / 6h +986
  / 24h -1, leaving every Financials 24h bucket bit-identical (07-29).
  `simulator/iterate.py` — DSR, purged folds,
  family_report (B5 core). `simulator/pair_candidates.py` — cross-venue
  leads (user-gated activation). `simulator/run_favlong.py` — the
  killed pre-reg's runner (record).
- `scripts/autoloop.sh` — 6-hourly bounded headless development
  iteration (`hyxlab-autoloop.timer`, flock-guarded).
- Entrypoints: `collect`, `sweep`, `backfill`, `signals`, `backup`,
  `run_sim`, `run_backtest`, `run_favlong`, `divergence`, `queuescore`,
  `atlas`, `pair_candidates`, `streamd`.

## Key decisions

- **Tier ladder**: candles (kill-only) → live book replay → shadow
  orders. A strategy's credential is which tier it survived.
- **Venue separation** is first-class; cross-venue strategies consume
  two explicit legs with hand-verified resolution-rule pairs.
- **Debug frontend**: simui (2026-07-08) is the foundation — a local
  single-page replay terminal where the user paper-trades archived
  markets and watches strategies do the same, all through the real
  Simulator (honesty: results blanked, seek restarts flat, manual
  orders ride the latency model). Decision-replay overlays and doctor
  views layer on it later.
- **Streaming (B7, promoted)**: WS daemons are the only as-if-live
  source; both venues' handshakes proven (Kalshi needs RSA auth).
- **Collection/sim split (2026-07-07, user-approved infra-first)**:
  logical boundary enforced by tests/test_boundaries.py; physical
  deployment separated (daemons run from the `stable` worktree via
  `scripts/promote.sh`). Full package split deferred to the Pi
  migration, where collection moves to the Pi and the DB sync doubles
  as off-box backup.
- No LLM in the signal path until deterministic signals prove out.
- GPU is irrelevant here — everything is network/IO-bound; portable to
  a Pi (repo + venv + duckdb + .secrets + 2 systemd timers).

## Build state (2026-07-12)

B1 archive+sweep ✅, B2 sim v2 ✅, B3 self-tests ✅, all gates ✅,
B7 stream daemon ✅ LIVE, B3.5 Kalshi tape ✅, stable deployment +
import boundary ✅, daily QA ✅, BookReplayer + latency fills ✅,
shadow harness ✅ LIVE, simui replay terminal ✅ (suite green,
2026-07-08), divergence ✅ (shadow≡replay exact), maker queue bounds ✅,
B4 signals ✅, B5 core ✅, B6 atlas ✅, fav-long v1 pre-reg FAIL (spread
decides), pair leads ✅, backups + simui service + autoloop ✅
(2026-07-12). Next: Tier-2 maker fav-long registration (data-gated) and
event study (data-gated); user gates in status.md.

## Related
- [data-pipeline](data-pipeline.md) · [simulation-honesty](simulation-honesty.md)
- [strategy-verdicts](strategy-verdicts.md) · [venues](venues.md)
