# Status & next steps (living page)

Updated: **2026-07-07** (gates finale + B7 stream daemon LIVE; B3.5 next).
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
3. **Trade tape (B3.5)** — Kalshi retro-pass over 35k archived markets
   (races retention), Polymarket prints + volume, first hand-verified
   cross-venue pairs.
4. **Debug frontend** — decision replay / market timeline / doctor;
   single-file read-only local web app.
5. B4 signals (ALFRED vintages, GDELT, FeatureView) → B5 harness (purged
   walk-forward, sweeps, DSR) → B6 calibration atlas → **pre-registered
   favorite-longshot backtest** (first strategy through the full lab).

## Standing user items (non-blocking)

`git restore .claude/skills/compact/SKILL.md`; DuckDB off-box backup;
rotate Kalshi API key (transited chat); optional FRED key (helps B4);
Pi migration whenever (fully portable); box uptime matters once the
stream daemon runs.

## Hard rules in force

Zero capital without pre-registered Tier-2+ PASS **and** explicit user
authorization. No retro-rescues of failed strategies. Phase 0 write-up
remains the pending prose artifact.
