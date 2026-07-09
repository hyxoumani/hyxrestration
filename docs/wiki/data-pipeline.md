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
  verified against unix ground truth.
- **Idempotent inserts**: `insert_new()` anti-join on natural keys; any
  backfill/sweep re-run is safe. (Fixed a real dup-on-rerun defect.)
- **Single writer**: DuckDB blocks even read_only connects while a
  writer is open. All scheduled writers flock `data/writer.lock`;
  ad-hoc reads must wait for the 5-min collector (seconds) — but the
  **poly sweep holds the archive open for HOURS** (~7h observed
  2026-07-08 walking 4k+ markets). Sim-side readers must degrade
  gracefully and retry lazily (simui's `ensure_metadata` pattern),
  never block on it.
- **Enumeration tripwire (TODO)**: the Gamma offset-cap regression
  (see [venues](venues.md)) would have silently halved the poly sweep;
  it was caught by a lucky dead probe, not QA. The sweep should alarm
  when the enumerated universe shrinks sharply vs. the archive count.
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

- `pgrep -f` matches your own command string — quote patterns or match
  the python binary path; this caused false "sweep alive" reads.
- Long background jobs: use `python -u` (buffered stdout hid 4h of
  sweep progress).
- Kalshi candle bid/ask closes are unsynchronized within the hour →
  crossed/sentinel quotes (1.3% of candles). Excluded at replay by the
  gate in `candles_as_snapshots` — see [simulation-honesty](simulation-honesty.md).
- Data written before 2026-07-06 tz fix was box-local; already migrated.

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
