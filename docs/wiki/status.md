# Status & next steps (living page)

Updated: **2026-07-07 late** (infra-first: stable deployment + boundary
enforced; B3.5 retro-pass RUNNING overnight).
Cold-start order: this page → [hyxlab-architecture](hyxlab-architecture.md)
→ `docs/sessions/2026-07-07-09.md` (full operational handoff, gitignored).

## Where the project is

- **Lab built and pushed** (main @ `8186b5a`): archive store + migrations,
  exchange-wide sweep, sim v2 (full order lifecycle + runtime accounting
  invariants), fee models (per-series), self-test rig, manifests, wiki.
  73 tests green; test-gate enforces on every stop.
- **Archive banked**: 35,144 markets / 2.6M hourly bid/ask candles across
  8 Kalshi categories (60-day retention capture complete — data Kalshi
  has since begun purging). Plus 27k archived MOS forecasts, 1.8k climate
  observations. `data/hyxlab.duckdb` — **needs off-box backup**.
- **Automation live**: systemd timers — collector every 5 min, sweep
  daily 06:10 UTC (watermarked, idempotent, flock-guarded).
- **Both live streams proven**: Polymarket WS (no auth, book+deltas);
  Kalshi WS (RSA auth working, creds in `.secrets/`; exchange-wide trade
  firehose ~105 ev/s observed).
- **First full falsification cycle complete**: WeatherNWS v1
  pre-registered → FAIL (−3.0% ROI; fees decide the sign) → confirmed
  worse (−$425) after the crossed-candle gate. See
  [strategy-verdicts](strategy-verdicts.md).

## Session findings worth remembering

The durable technical findings live in [venues](venues.md),
[data-pipeline](data-pipeline.md), [simulation-honesty](simulation-honesty.md);
failures + root causes in [mistakes](mistakes.md). Headlines: Kalshi's
mirrored single book makes intramarket arb impossible there; retention
purge makes self-archiving the moat; candle bid/asks can be crossed
(1.3% excluded at replay); fees, not signal, killed weather v1.

## Execution queue (user-approved order)

1. ~~Correctness gates finale~~ DONE 2026-07-07: mirror-invariant
   tripwire (`Store.mirror_violations`, in doctor, 0 live violations) +
   capability guard (`hyxlab/capabilities.py`; vacuous backtests raise
   `VacuousBacktestError`). See [simulation-honesty](simulation-honesty.md).
2. ~~Stream daemon (B7)~~ LIVE 2026-07-07: `hyxlab-stream.service`
   (Restart=always) → `data/hyxstream.duckdb` via `streamd.py`. Kalshi
   trade firehose + watchlist books (hourly ticker refresh, seq-gap →
   reconnect+re-seed, gap log); Poly books idle until pairs land. First
   minute live: ~17.5k rows, 0 gaps. Doctor covers both archives.
   Watch: disk growth (est. low-single-GB/day), box uptime now matters.
**Direction change (user, 2026-07-07): data plumbing & infra first,
strategies after.** Landed same evening: collection/sim import boundary
(tests/test_boundaries.py) + stable deployment worktree
(`scripts/promote.sh` — the only shipping path for collection code).

3. **Trade tape (B3.5)** — Kalshi side RUNNING (hyxlab-tradepass
   transient unit, 35,179 settled markets oldest-first; probed retention
   boundary: closed ≤2026-05-01 already purged, ~64-day window). Sweep
   now captures prints for newly settled markets. Still pending:
   Polymarket prints/volume sampling + first hand-verified cross-venue
   pairs.
4. **Daily stream QA job** — promote the audit (seq holes, negative
   books, latency, disk) into a scheduled check.
5. **BookReplayer + latency-aware fills** → **shadow harness (Tier-3
   paper trading)** — one arc, shares stream-replay plumbing.
6. B4 signals (ALFRED vintages, GDELT, FeatureView) → B5 harness (purged
   walk-forward, sweeps, DSR) → B6 calibration atlas → debug frontend.
7. Strategy work (favorite-longshot pre-reg etc.) AFTER the plumbing.

## Standing user items (non-blocking)

**`sudo timedatectl set-ntp true`** — box clock is ~20 s fast, NTP off
(stream audit finding; daemon logs the correction as a clock_step gap);
`git restore .claude/skills/compact/SKILL.md`; DuckDB off-box backup
(both files); rotate Kalshi API key (transited chat); optional FRED key
(helps B4); Pi migration whenever (fully portable); box uptime matters —
the stream daemon is live and its data unrecoverable.

## Hard rules in force

Zero capital without pre-registered Tier-2+ PASS **and** explicit user
authorization. No retro-rescues of failed strategies. Phase 0 write-up
remains the pending prose artifact.
