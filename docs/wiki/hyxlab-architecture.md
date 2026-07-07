# hyxlab architecture

Strategy-testing lab for prediction markets. Full component spec:
`docs/plans/hyxlab-v2/proposal.md` (C1–C8); contract validation:
`docs/plans/hyxlab-v2/data_contracts.md`.

## Layers

```
connectors (venues/*)  →  archive store (store.py, DuckDB)
        ↓ scheduled by systemd timers (collect 5min, sweep daily)
alignment (Context / FeatureView planned)   ← the ONLY time-sensitive read path
        ↓
sim engine (sim.py) ←→ strategies (strategies/*)
        ↓
harness manifests (harness.py → data/runs/)  +  self-tests (tests/)
```

## Module map

- `hyxlab/models.py` — typed records (Snapshot, MarketInfo, Order incl.
  open/close + GTC/IOC, Cancel, Fill, Forecast, EconVintage, NewsItem).
- `hyxlab/venues/` — kalshi, polymarket, nws, iem, alfred, alpaca_news
  (pure fetch→records; sessions injected; fixtures in tests/fixtures/);
  kalshi_ws + polymarket_ws (WS auth/payloads/parsers, no sockets).
- `hyxlab/store.py` — schema, naive-UTC, insert_new dedup, watermarks,
  candles_as_snapshots (with crossed-candle gate), mirror tripwire.
- `hyxlab/streamstore.py` — stream archive (own DuckDB: book_events,
  stream_trades, stream_gaps; buffered flush bursts).
- `hyxlab/streamd.py` — stream daemon (asyncio, reconnect/re-seed/
  gap-marking; systemd `hyxlab-stream.service`).
- `hyxlab/sweep.py` — exchange-wide archival sweep + `--doctor`.
- `hyxlab/sim.py` — event loop, order lifecycle, runtime invariants.
- `hyxlab/strategy.py` — Strategy ABC (+ `requires` capability
  declaration) + Context (hides settlements, as-of forecasts,
  open_orders for Cancel).
- `hyxlab/capabilities.py` — strategy↔data capability contract
  (vacuous backtests raise instead of returning zero).
- `hyxlab/fees.py` — parabolic models, per-series `kalshi_model()`.
- `hyxlab/harness.py` — run manifests (git rev, params, fingerprint).
- `hyxlab/migrate.py` — numbered migrations.
- Entrypoints: `collect`, `sweep`, `backfill`, `run_sim`, `run_backtest`,
  `streamd`.

## Key decisions

- **Tier ladder**: candles (kill-only) → live book replay → shadow
  orders. A strategy's credential is which tier it survived.
- **Venue separation** is first-class; cross-venue strategies consume
  two explicit legs with hand-verified resolution-rule pairs.
- **Debug frontend** (planned, user-scoped): a debugging tool, not a
  dashboard — decision replay ("what did the strategy see at ts"),
  market timeline, doctor. Single-file read-only local web app.
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

## Build state (2026-07-07)

B1 archive+sweep ✅, B2 sim v2 ✅, B3 self-tests ✅, crossed-candle
gate ✅, mirror tripwire + capability guard ✅, B7 stream daemon ✅
LIVE (96 tests). Next: trade tape (B3.5) → debug frontend → B4 signals
→ B5 walk-forward/DSR → B6 calibration atlas → first pre-reg strategy.

## Related
- [data-pipeline](data-pipeline.md) · [simulation-honesty](simulation-honesty.md)
- [strategy-verdicts](strategy-verdicts.md) · [venues](venues.md)
