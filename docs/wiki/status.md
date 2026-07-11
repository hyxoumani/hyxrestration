# Status & next steps (living page)

Updated: **2026-07-11** (physical package split shipped 07-09:
`collector/` / `simulator/` / `strategies/` / `hyxlab` kernel, systemd
units vendored in `scripts/systemd/`, promote.sh installs them. QA
negative-levels root cause found and fixed 07-11: flush() dropped its
batch when a reader held the file lock — 18 silent 15s archive holes
Jul 9–11, now retro-gap-marked; QA reconstruction was also unsound
(max(seq) vs subscription-scoped seq) — rewritten time-ordered. All QA
green. Next: shadow-vs-replay divergence report, ~2.9k shadow fills
banked.)
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
[simulation-honesty](simulation-honesty.md)). 150 tests green.

**Falsification record**: weather v1 pre-reg FAIL (−$425, fees decide).

## Execution queue (sim platform, user-approved)

1. **Cross-venue pair candidates report** (last B3.5 checkbox) —
   generate Kalshi↔Poly topic matches; pairs activate only after USER
   verifies resolution rules.
2. ~~Shadow harness (Tier-3) v1~~ LIVE 2026-07-08: `hyxlab-shadow.service`
   — persistent Simulator tailing the stream archive (books seeded from
   history, trading strictly from the stream head), same latency model
   as backtests, fills/equity per run_id in `data/hyxshadow.duckdb`.
   Probe strategy running. **Next iteration**: maker queue-position-bound
   scoring + shadow-vs-replay divergence report (the calibration
   haircut).
3. **B4 FeatureView + signal feeds** (ALFRED vintages, GDELT, econ
   release calendar) — built together; the as-of API is the consumer.
4. **B5 iteration machinery** — purged walk-forward, sweeps, DSR
   deflation with family-wide trial counting.
5. **B6 calibration atlas** + event study v1.
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

## Standing user items (non-blocking)

**Off-box backup** (all three DuckDB files — highest-value 30 min; needs
a destination from the user; stakes rose: 10M+ unrefetchable rows, and
the poly sweep holds a multi-hour write lock);
`sudo timedatectl set-ntp true` (box ~20s fast; daemon logs the step);
rotate Kalshi API key;
Phase 0 write-up (pending prose artifact); micro-probe budget decision
(parked until explicitly authorized); **simui as a systemd unit?** —
currently dies with the dev session that launched it (user to confirm).

## Small follow-ups (agent-actionable)

- ~~Sweep-shrink tripwire~~ DONE 2026-07-11 (QA: last completed day vs
  prior-week peak, 0.5 threshold; reachability check is now lock-aware
  so the multi-hour poly sweep no longer false-alarms).
- Cross-venue pair candidates report (queue item 1) is mostly
  mechanical and can ride along with other work.

## Watch items (not yet alarming)

- **Poly swept universe declining organically** ~5%/day (8.7k Jul 2 →
  5.7k Jul 10, smooth, predates the keyset change — resolve-churn, not
  enumeration breakage). Tripwire threshold chosen so drift stays
  quiet; investigate only if the trend accelerates or the sweep
  runtime (now ~12h, was ~7h) keeps growing against a shrinking
  universe.

## Hard rules in force

Zero capital without pre-registered Tier-2+ PASS **and** explicit user
authorization. No retro-rescues of failed strategies. Probe before
build. Every new store writer ships with a stored-timestamp assertion
(mistakes #10). Vacuous backtests must refuse to run.
