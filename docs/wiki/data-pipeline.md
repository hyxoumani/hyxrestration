# Data pipeline (hyxlab archive)

One DuckDB file (`data/hyxlab.duckdb`, gitignored, **irreplaceable — needs
off-box backup**) fed by collectors/sweeps; read by sim and research.

## Tables

Append-only facts: `candles` (hourly price+bid/ask OHLC, volume, OI),
`snapshots` (live top-of-book), `nws_forecasts` (fetched_at = as-issued
time), `observations` (climate-report highs). Reference: `markets`
(metadata + settlement result), `series` (category/fee metadata),
`sweep_log`, `watermarks`, `schema_meta`.

## Key decisions

- **Naive-UTC timestamps everywhere** via `store._naive_utc()`. DuckDB
  silently converts tz-aware inserts to BOX-LOCAL time otherwise
  (machine-dependent corruption). Migration 1 fixed legacy rows,
  verified against unix ground truth.
- **Idempotent inserts**: `insert_new()` anti-join on natural keys; any
  backfill/sweep re-run is safe. (Fixed a real dup-on-rerun defect.)
- **Single writer**: DuckDB blocks even read_only connects while a
  writer is open. All scheduled writers flock `data/writer.lock`;
  ad-hoc reads must wait for the 5-min collector (seconds).
- **Provenance**: every signal row carries when it became knowable
  (forecast runtime, vintage release, poll time). The no-lookahead
  boundary is enforced by this column, not convention.

## Running pieces

- `hyxlab-collect.timer` (systemd user, 5 min): `collect --once` —
  Kalshi focus top-of-book, NWS, Polymarket pairs (pairs still empty).
- `hyxlab-sweep.timer` (daily 06:10 UTC): `sweep --days 2` incremental,
  category allowlist (8 categories ≈ 2,240 series; sports/entertainment/
  politics excluded — they dominate settle volume).
- Initial 60-day retention capture COMPLETE 2026-07-07: 35,144 markets,
  2.6M candles. `python -m hyxlab.sweep --doctor` = health check.
- Backfills: `python -m hyxlab.backfill` (Kalshi candles + IEM).
- Migrations: `python -m hyxlab.migrate` (numbered, schema_meta-gated).

## Gotchas

- `pgrep -f` matches your own command string — quote patterns or match
  the python binary path; this caused false "sweep alive" reads.
- Long background jobs: use `python -u` (buffered stdout hid 4h of
  sweep progress).
- Kalshi candle bid/ask closes are unsynchronized within the hour →
  crossed/sentinel quotes (1.3% of candles). Excluded at replay by the
  gate in `candles_as_snapshots` — see [simulation-honesty](simulation-honesty.md).
- Data written before 2026-07-06 tz fix was box-local; already migrated.

## Next (planned, user-approved)

Stream daemon (B7): both venues' WS → `book_events`/`stream_trades` +
gap log; then trade-tape retro-pass (races retention); then ALFRED/GDELT
ingestion behind a `FeatureView` as-of API.

## Related
- [venues](venues.md) — sources and their limits
- [hyxlab-architecture](hyxlab-architecture.md) — where this layer sits
