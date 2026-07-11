# Deep review — 2026-07-11

Scope: full read of `collector/`, `simulator/`, `strategies/`, `hyxlab/`,
venue clients, simui entry points, systemd/promote scripts, and repo
hygiene. Tests (159) pass and ruff is clean at HEAD (`5769132`).

Overall: the core is in good shape — the accounting invariants, gap
discipline, no-lookahead sanitization, import-boundary test, and the
verified fee models are all genuinely careful work. The findings below
are ranked by how much they threaten the two things the lab says it
cares about most: never losing unrecoverable data, and never letting the
sim flatter a strategy.

---

## High

### H1. Writer-lock discipline is inconsistent across archive writers

The `data/writer.lock` flock protocol is honored only by
`collector/poly_sweep.py` and `collector/trades_backfill.py`. The other
two writers of `data/hyxlab.duckdb` ignore it **and** hold the DuckDB
file lock across long stretches of network I/O:

- `collector/collect.py:87` opens `Store` once in `main()` and keeps the
  connection open through every REST call of the cycle.
- `collector/sweep.py:216` holds the connection for the **entire
  multi-hour sweep** (`run_sweep` interleaves REST fetches and inserts
  on one open connection).

Consequences:

- `poly_sweep._flush` (`collector/poly_sweep.py:40`) opens `Store(db)`
  with **no retry**, and the periodic flush call at
  `collector/poly_sweep.py:129` sits *outside* the per-market
  `try/except`. If a flush lands while the collector cycle (or the
  daily Kalshi sweep) holds the lock, `duckdb.connect` raises and the
  whole ~7 h poly sweep dies mid-run, losing the un-flushed batch and
  the rest of the enumeration.
- Conversely, `collect.py` has no connect retry at all: any collector
  cycle that starts while `sweep.py` is mid-run just crashes (the timer
  masks this as a failed run every 5 min for the sweep's duration).

Fix direction: one rule for everybody. Either (a) all `hyxlab.duckdb`
writers take the flock and touch the DB only in open→write→close
bursts (sweep.py is the big offender — it can buffer per-series and
flush like poly_sweep does), or at minimum (b) add the same 5×2s
connect-retry that `sweep --doctor` and QA already use to
`collect.main`, `poly_sweep._flush`, and `Store.__init__` call sites.

### H2. Shadow: any venue's gap row blanks Kalshi book state, and Kalshi books re-seed only on reconnect

`ShadowRunner._read_new` (`simulator/shadow.py:201`) selects
`stream_gaps` with **no venue filter**, and `replay_snapshots` applies
every gap by invalidating **all** books (`simulator/bookreplay.py:198`,
documented as conservative because gap rows aren't per-market — but
they *do* carry a venue column). A single Polymarket reconnect gap
therefore blanks every Kalshi book in the shadow sim, and those books
stay unknown until the next Kalshi `orderbook_snapshot` — which only
arrives on a Kalshi reconnect (hourly ticker-set change or an error),
i.e. potentially hours of self-inflicted blindness per poly flap.

The status page already measured the cost: 57% of shadow fills sit in a
gap's 65-minute "re-seed shadow". Filtering gaps to
`venue IN ('kalshi', '*')` in both the shadow tail query and the
divergence replay (`simulator/divergence.py:88` — keep the two
consistent, as today) is a one-line change per site that should
substantially raise shadow coverage without weakening honesty
(polymarket replay isn't even implemented — `bookreplay.py` refuses
non-Kalshi events).

### H3. Silent pagination caps in the Kalshi client — the exact Gamma-offset failure class

`mistakes.md` and the QA universe-shrink tripwire exist because Gamma
silently truncated an enumeration. The Kalshi REST client has the same
latent shape, with no tripwire:

- `kalshi.get_markets` (`collector/venues/kalshi.py:53`) stops after
  `max_pages` (default 10 → 2,000 markets; sweep passes 50) with no
  signal when a cursor was still present.
- `kalshi.get_trades` (`collector/venues/kalshi.py:85`) caps at 100
  pages → 100k prints per market, then `trades_backfill` happily marks
  the market `status='ok'` in `trades_swept` — a *permanently* truncated
  tape recorded as complete.

Fix: when the page loop exhausts `max_pages` with a non-empty cursor,
at least log loudly; for `get_trades`, return the truncation fact so
`trades_swept.status` can record `'truncated'` instead of `'ok'`.

### H4. Sim fills full order qty against a *zero* displayed size

`Simulator._executable_qty` (`simulator/sim.py:153`): `avail <= 0` is
treated as "size unknown → take full order qty". But a real quote with
displayed size 0 reaches this path: `kalshi.to_snapshot` maps missing
size fields to `0.0` via `_fp` (`collector/venues/kalshi.py:230`), and
poly `pair_snapshot` returns `(price, 0.0)` shapes too. A taker order
then fills its **entire qty against no displayed liquidity** — an
optimistic-fill hole beyond the documented "cap at displayed size"
bias. The unknown-size case is already representable (candle snapshots
use `inf`): treat `avail == 0` with a non-None price as *no* liquidity
(qty 0), and reserve the "cap at order qty" behavior for `inf`/None.

---

## Medium

### M1. `sweep.py` failure paths exit 0, and the ad-hoc lock file can go stale forever

- Both abort paths — stale `data/hyxlab.duckdb.lock`
  (`collector/sweep.py:229`) and "archive busy" (`collector/sweep.py:222`)
  — `return` with exit code 0, so systemd records a *successful* run
  that did nothing; only the 36 h QA check eventually notices.
- The lock is a bare `touch()`/`exists()` file: a SIGKILL/OOM/power
  loss between touch and the `finally` leaves it behind and every
  subsequent sweep aborts (silently, per the point above) until someone
  removes it by hand. An `fcntl.flock` on the file (self-releasing on
  process death, like poly_sweep uses) or a PID-stamped lock with a
  staleness check would remove the failure mode entirely.

### M2. `sweep_series` skips markets with missing open/close time but advances the watermark past them

`collector/sweep.py:93-96`: `continue` with no log, then
`set_watermark(series, max_close)` — those markets' candles/tape are
never fetched and never will be (the watermark has moved past their
close). Probably rare, but it's a permanent, unlogged hole in a system
whose whole design is "mark honestly what you missed". Log it and/or
exclude their close times from the watermark advance.

### M3. StreamStore retry buffer is unbounded

`hyxlab/streamstore.py:137` correctly holds the batch on flush failure
(mistakes #12 fix), but if a reader wedges the file for a long time the
in-memory buffer grows without limit (~105 ev/s on the trade firehose),
and an eventual daemon OOM loses everything at once — the exact outcome
the retry was built to avoid. Consider a cap that, when exceeded,
spills to a sidecar file (or at minimum logs `pending` size in the
flusher loop so the journal shows the buildup before the OOM).

### M4. One malformed API response kills the whole collector cycle

`collect_once` (`collector/collect.py:48,63,72`) catches only
`requests.RequestException`. A non-JSON body, an error-shaped payload
(`KeyError: 'ticker'` in `to_snapshot`), or a `json.JSONDecodeError`
propagates and aborts the cycle — the remaining series/stations for
that 5-minute tick are skipped. Polymarket's Gamma API demonstrably
returns error *objects* with HTTP 200 (see `iter_markets_by_volume`'s
own defense), so this is a live failure class. Broaden the per-series
catch to `Exception` (log + count), since the loop's job is isolation.

### M5. Long-lived shadow runs never refresh market metadata

`ShadowRunner.poll_once` (`simulator/shadow.py:212`) only retries
loading markets while the dict is *empty*. On a multi-day run, markets
listed after start have no `close_time` (resting orders in them never
expire, `_maker_check_and_expire` needs `info.close_time`) and no
settlement results at `finalize()`. A cheap periodic re-load (e.g.
hourly, with the existing lock-tolerant `_try_load_markets`) fixes it.

### M6. Poly enumeration stops silently on repeated Gamma errors

`iter_markets_by_volume` (`collector/venues/polymarket.py:97-105`):
no `raise_for_status`, and after one 5 s retry the keyset walk just
`break`s — a partial universe is returned with no marker. The QA
shrink-tripwire catches it a day later; a same-run signal (log line +
an `"incomplete"` flag or raised exception the sweep can record in
`sweep_log`) closes the gap. `trades_tail` has the same silent-stop
shape (`polymarket.py:184` treats any non-list body as end-of-data).

### M7. Sim marks unquoted positions at zero

`Simulator._mark` (`simulator/sim.py:303`) returns 0.0 for an
unresolved market with no observed snapshot/mid. Positions entered via
`ctx.last` quotes always have one, but after a `latency`-mode fill in a
market that then goes quiet, equity/max-drawdown carry an artificial
full-loss mark. Consider marking at last trade/fill price instead of 0,
or at least noting the bias where max_drawdown is consumed.

### M8. Divergence matcher can't see partial-fill splits

`compare` (`simulator/divergence.py:117`) requires exact qty equality
and takes the *first* (not nearest) fill inside the 60 s window. If
shadow fills 5 and replay fills 3+2 (or vice versa) they count as
unmatched on both sides, deflating the match rate that the status page
reports as a headline number. Fine for v1, but worth a note in the
report JSON so the number isn't over-read.

---

## Hygiene / drift

1. **Legacy `hyx/` package is live but ungoverned.** It's the old
   equity/news "Slice 1" pipeline (yfinance + Alpaca + FinBERT) — not
   in CLAUDE.md's four-package overview, not in
   `tests/test_boundaries.py`'s `ALLOWED` map, and it duplicates
   infrastructure (`hyx/db/migrate.py` vs `hyxlab/migrate.py`,
   `data/hyx.duckdb`). Phase 0 got an explicit "closed historical
   record" fence; `hyx/` should get the same treatment (move under
   `phase0/`-style quarantine or delete) — otherwise it's an open
   invitation to build on dead code.
2. **Stray root file** `llm_trading_orchestration.md` — file it under
   `docs/` or remove.
3. **CLAUDE.md drift**: says "150 tests" (159 pass); the `## Metric`
   section is still all `_TBD_`; the Commands section lists only the
   `hyxlab-collect`/`hyxlab-sweep` timers while six units exist
   (qa, poly-sweep, stream, shadow are only discoverable via the wiki).
   `status.md` repeats "150 tests green".
4. **`Order` accepts any strings.** A typo'd `side="Yes"` flows through
   silently: positions keyed under `"Yes"`, `_mark` compares
   `info.result == side` → never matches → marked/settled at 0. A
   `__post_init__` assert on `side/action/tif` is one line each and
   turns a silent accounting distortion into an immediate error.
5. **`streamd.open_tickers` total failure at startup** → subscribes to
   an empty book set and stays dark until the hourly refresh; worth a
   shorter retry when the initial set comes back empty.
6. **QA book reconstruction edge** (`collector/qa.py:117`): snapshot
   baselines are keyed per `(market, side)`; an image where one side is
   legitimately empty leaves that side anchored to an *older* image,
   and deltas spanning the newer reset can sum negative → false alarm.
   Low likelihood, but it's a tripwire, so false positives cost trust.
7. **`requirements.txt` vs `scripts/requirements-stable.txt`** — two
   hand-maintained lists with no check they agree on the packages the
   daemons import; promote.sh's smoke-import catches missing ones, but
   version skew between dev-test and stable-run is invisible.

---

## What's notably good (keep doing this)

- Runtime accounting invariants that hard-abort (`sim.py` I1/I2/I3) —
  an accounting bug can't masquerade as PnL.
- Gap discipline end-to-end: startup gaps, seq gaps, clock steps,
  retro-marked flush failures, and a replayer that refuses to emit
  half-applied snapshot images.
- The result-sanitizing `Context.market()` and capability guard — two
  distinct classes of self-deception (lookahead, vacuous backtests)
  structurally blocked.
- Fee models with dated, source-cited verification; the mirror
  invariant as pipeline-corruption tripwire (not "opportunity").
- `tests/test_boundaries.py` — the package split is enforced, not
  aspirational; promote.sh refuses to ship red.
- The mistakes log with escalation tiers; H3 above is just that
  discipline applied to one more client.

## Suggested priority order

1. H1 (lock discipline) — it can kill a 7 h sweep today.
2. H2 (venue-filtered gaps) — directly recovers shadow coverage the
   status page says is the current calibration bottleneck.
3. H3 + M6 (silent truncation signals) — cheap, matches house doctrine.
4. H4 + hygiene #4 (sim optimistic-fill hole, Order validation).
5. M1/M2 (sweep exit codes, lock, watermark holes).
6. Everything else opportunistically.
