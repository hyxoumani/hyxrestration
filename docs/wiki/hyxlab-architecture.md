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
  candles_as_snapshots (with crossed-candle gate), mirror tripwire.
- `hyxlab/streamstore.py` — stream archive (own DuckDB: book_events,
  stream_trades, stream_gaps; buffered flush bursts).
- `collector/streamd.py` — stream daemon (asyncio, reconnect/re-seed/
  gap-marking; systemd `hyxlab-stream.service`).
- `collector/sweep.py` — exchange-wide archival sweep + `--doctor`.
- `collector/trades_backfill.py` — trade-tape retro-pass (races retention).
- `collector/qa.py` — daily data-quality checks (`hyxlab-qa.timer`).
- `simulator/bookreplay.py` — stream events → ms-fidelity Snapshot stream
  (gap-honest, complete-image emission, mirror-derived asks).
- `simulator/sim.py` — event loop (`step()`/`finalize()`/`run()`), order
  lifecycle, runtime invariants, latency model (`latency=Δ`; Δ=0 = legacy).
- `simulator/shadow.py` — Tier-3 shadow harness (`hyxlab-shadow.service`):
  live Simulator on a stream-archive tail, ledger-only fills per run_id.
- `simulator/simui/` — interactive market-replay UI (`python -m
  simulator.simui`, localhost:8877): archived event groups replay like a
  live Kalshi event page; user + strategy orders fill through the real
  Simulator (ManualTrader queue → step()). session.py (ReplaySession;
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
  Wilson flags). `simulator/iterate.py` — DSR, purged folds,
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
