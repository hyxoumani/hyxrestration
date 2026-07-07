# hyxlab v2 — technical design: components of a robust testing environment

**Author: Claude (developer role, decision-making delegated). 2026-07-06, rev 3.**
Status: PROPOSAL — component-level design with per-component specs.
Build starts at B1 (§11) unless redirected.

## 0. What "robust" means here, concretely

A testing environment is robust when a strategy result can only be wrong in
ways we have already priced in. Five engineering properties, each owned by
components below:

- **P1 Provenance** — every datum carries when it became knowable; nothing
  reaches a strategy except through an as-of gate. (C2, C4)
- **P2 Determinism** — same data + same params ⇒ identical results; every
  run reproducible from its manifest. (C5, C6)
- **P3 Bounded optimism** — fill assumptions explicit, pluggable, and every
  result ships with its sensitivity to them. (C5, C6)
- **P4 Self-verification** — the environment mechanically tests its own
  accounting, lookahead-resistance, and data quality. (C7)
- **P5 Durability** — capture is idempotent, gap-aware, and beats the
  venue's ~60–90-day retention (verified: older Kalshi markets are purged
  and cannot be re-downloaded). (C1, C8)

## 1. Component map

```
            ┌──────────────────────────────────────────────────────────┐
            │ C1 CONNECTORS      kalshi | polymarket | nws | iem |     │
            │                    alfred | gdelt | alpaca-news          │
            └────────────┬─────────────────────────────────────────────┘
                         ▼
            ┌──────────────────────┐      ┌──────────────────────────┐
            │ C2 ARCHIVE STORE     │◀─────│ C8 SCHEDULER/SWEEP       │
            │ DuckDB + parquet     │      │ the only writer          │
            └──────────┬───────────┘      └──────────────────────────┘
                       ▼
            ┌──────────────────────┐
            │ C4 ALIGNMENT         │  the only time-sensitive read path
            └──────┬───────┬───────┘
                   ▼       ▼
     ┌──────────────┐   ┌───────────────────────────────┐
     │ C3 RESEARCH  │   │ C5 SIM ENGINE                 │
     └──────────────┘   └──────────────┬────────────────┘
                                       ▼
                        ┌───────────────────────────────┐
                        │ C6 RUN HARNESS & MANIFESTS    │
                        └──────────────┬────────────────┘
                                       ▼
                        ┌───────────────────────────────┐
                        │ C7 ENVIRONMENT SELF-TESTS     │
                        └───────────────────────────────┘
```

---

## 2. C1 — Connectors (`hyxlab/venues/`)

### Responsibilities / non-responsibilities

Fetch remote data → typed records. **No** storage, no scheduling, no
retry-policy decisions of their own beyond per-request backoff. Sessions
injected so every connector is testable against canned responses (the
existing IEM test pattern).

### Shared plumbing: `hyxlab/venues/_http.py`

```python
@dataclass(frozen=True)
class RateBudget:
    requests_per_s: float        # steady-state politeness ceiling
    burst: int = 1

def get_json(session, url, *, params=None, timeout=30, max_tries=6) -> Any
def get_text(session, url, *, params=None, timeout=60, max_tries=6) -> str
```
Backoff: on 429/5xx sleep `Retry-After` if present else `2**attempt`, then
raise after `max_tries` (extracted from today's `_candles_with_backoff`,
which stays as a thin wrapper). Connectors declare their budget as a module
constant; **C8 enforces pacing** — call sites never `sleep()`.

### Uniform record contract

Every emitted record has a `knowable_at: datetime` (UTC): the earliest
moment a live trader could have possessed it. Mappings:

| Source | knowable_at | Notes |
|---|---|---|
| kalshi candles | candle `end_period_ts` | quotes during the hour are NOT knowable until period end at this grain |
| kalshi snapshots (live) | poll time | |
| nws live forecast | poll time | |
| IEM MOS archive | model runtime | validated vs CLI truth via MAE gate |
| ALFRED vintage | release **datetime** (see below) | `realtime_start` is date-granular; refine with the release-calendar endpoint + known 08:30 ET print times; if unresolvable, use `realtime_start 23:59 ET` (pessimistic) |
| GDELT article | `seendate` (monitored time) | ±15 min honesty → daily-horizon strategies only (validity check enforces) |
| alpaca news | `created_at` | Benzinga wire timestamp |

A source that cannot provide an honest knowable_at is not ingested (P1).

### Per-connector specs

**`kalshi.py`** (exists; additions in bold)
- `get_markets(**filters)` paginated; `get_candlesticks(series, ticker, start, end, period)`.
- **`get_series_list()`** — `GET /series` paginated → all series tickers +
  categories; input to the C8 sweep enumeration.
- Budget: markets 5 rps; candlesticks 2 rps (empirical — the documented
  30 rps public limit does not hold for candlesticks; we observed 429s).

**`alfred.py`** (new)
- `get_vintages(series_id, start, end) → list[EconVintage]` via
  `fred/series/observations?realtime_start=&realtime_end=`;
  `get_release_datetimes(release_id)` via `fred/release/dates`.
- Initial series set: `CPIAUCSL`, `CPILFESL` (CPI), `ICSA` (claims),
  `PAYEMS` (payrolls), `UNRATE` (U-3), `DFEDTARU/L` (Fed target bounds).
- Free API key (env `FRED_API_KEY`); budget 2 rps.

**`gdelt.py`** (new; contract corrected per data_contracts.md)
- DOC 2.0 API only for narrow daily topic queries on a dedicated slow
  lane: **≥5 s between requests** (verified hard per-IP limit) and empty
  `{}` responses treated as throttling (retry with long cooldown) — the
  API signals limits via plain-text bodies, not status codes.
- High-volume/backfill path: **bulk 15-min GKG files**
  (`data.gdeltproject.org/gdeltv2/`, no rate limit, ~5 MB zipped per file)
  with filter-and-discard ingestion — grep theme codes, keep matches,
  never archive raw (~480 MB/day otherwise). Tone comes from GKG; the
  artlist mode does not carry it.
- Dedup key: `url_hash = sha256(canonical_url)[:16]`.
- Queries are **per-market-category topic templates** stored in
  `hyxlab/queries/gdelt.json` so ingestion is reproducible, not ad-hoc.

**`alpaca_news.py`** (new, thin)
- Port `hyx/news.py` (Phase-0 client, creds in `.env`) to emit `NewsItem`.
  Historical range queries back to ~2015 for finance-relevant symbols.

**`polymarket.py` / `nws.py` / `iem.py`** — as today; polymarket stays
collect-forward (no public book history), IEM is complete for its role.

### Failure modes & handling
Connector exceptions carry (source, endpoint, params); C8 logs and
isolates per task — one source failing never blocks others. Schema drift
(venue renames a field) surfaces as a typed-mapping KeyError in exactly one
module; golden fixture tests (C7) catch it before a silent `None` cascade.

### Tests
Per connector: fixture-response parsing test (exists for kalshi/iem),
knowable_at correctness test, and one recorded-response regression fixture.

---

## 3. C2 — Archive store (`hyxlab/store.py`)

### Table classes and rules

**Append-only facts** — never UPDATE, dedup on natural key at insert:

```sql
-- existing: snapshots, candles, nws_forecasts, observations
CREATE TABLE IF NOT EXISTS econ_vintages (
    series_id    VARCHAR NOT NULL,     -- 'CPIAUCSL'
    obs_date     DATE    NOT NULL,     -- period the value describes
    value        DOUBLE,
    knowable_at  TIMESTAMP NOT NULL,   -- release datetime (vintage)
    PRIMARY KEY (series_id, obs_date, knowable_at)
);
CREATE TABLE IF NOT EXISTS news_items (
    source       VARCHAR NOT NULL,     -- 'gdelt' | 'alpaca'
    url_hash     VARCHAR NOT NULL,
    published_at TIMESTAMP,
    knowable_at  TIMESTAMP NOT NULL,
    title        VARCHAR,
    tone         DOUBLE,               -- NULL for alpaca
    topics       VARCHAR,              -- comma list, from query template
    PRIMARY KEY (source, url_hash)
);
```

**Reference state** — upserts allowed: `markets` (latest metadata +
result), `sweep_log` (C8), `schema_meta(version INTEGER)`.

Natural-key dedup for facts that lack a PK today (candles, snapshots):
`insert_new(table, rows, key_cols)` does an anti-join insert via a temp
table — re-running any backfill or sweep is safe (P5). This fixes a real
current defect: re-running `backfill.py` duplicates candles.

### Timestamps
All naive-UTC via `_naive_utc()` (already implemented — DuckDB silently
converts tz-aware to box-local otherwise). One legacy migration: data
written before the fix is uniformly box-local (America/Chicago); migration
001 shifts it. Migrations = numbered SQL/python files, applied by
`schema_meta.version`, reusing the `hyx/db/migrate.py` pattern.

### Episode export (feeds C6/C7)

```python
def export_episode(dest: Path, *, venues, series, start, end) -> EpisodeMeta
```
Writes one parquet per table filtered to the window + `episode.json`:
row counts, min/max ts per table, and `content_hash` = sha256 over
parquet bytes in sorted table order. Episodes are the frozen, portable,
hashable unit that manifests (C6) and golden tests (C7) reference.
`Store.from_episode(path)` opens one read-only.

### Concurrency
DuckDB = single writer. All writes flow through the C8 scheduler process.
Sim/research open `duckdb.connect(path, read_only=True)` — concurrent
readers are safe. CLI guards: `collect.py`/`backfill.py`/`sweep.py` take a
`flock` on `<db>.lock` so a stray manual run cannot corrupt a scheduled one.

### Tests
Dedup-on-rerun (insert twice ⇒ count once), migration up-from-legacy
fixture, episode hash stability (export twice ⇒ same hash), read-only
enforcement (write on RO connection raises).

---

## 4. C3 — Research layer (`hyxlab/research/`)

Deterministic report scripts — not notebooks — reading **only** through C4
(research and sim can never disagree about what was knowable).

### `calibration_atlas.py`
The pattern detector. For each settled market and each horizon bucket
h ∈ {1h, 6h, 24h, 72h, 7d before close}:
1. Take the last candle mid at `close_time − h` (skip if none).
2. Bucket by (venue-category, price decile, h).
3. Per bucket: implied p̄ = mean mid, realized r = mean(result==yes),
   Wilson 95% interval on r, n.
4. Emit `atlas.json` + markdown table; **flag** buckets where p̄ falls
   outside the Wilson interval with n ≥ 200 — those are candidate
   inefficiencies (e.g. the favorite-longshot signature appears as
   realized > implied in the 90–99¢ decile).
Report is versioned by episode content hash — atlas claims are always
attributable to a dataset.

### `event_study.py`
Input: an event stream (econ release datetimes from ALFRED, news-burst
timestamps from `news_items` aggregates) + a market filter.
Method: for each event, sample the market's mid at fixed offsets
(−24h, −1h, −5m, +5m, +1h, +6h, +24h) from candles/snapshots; report mean
path and dispersion vs a matched-baseline path (same markets, random
non-event times, same offsets). Gates: n ≥ 30 events per cell, report
suppressed below that. Output: drift table per (event type × category).
Purpose: measure whether signals lead prices at horizons we can trade —
the go/no-go input for any news strategy.

### Tests
Golden mini-atlas on a fixture episode with hand-computed buckets;
event-study offsets verified against a synthetic series with a known step.

---

## 5. C4 — Alignment layer (`hyxlab/features.py`)

The single read gate for time-sensitive data (P1).

### Interface

```python
class SignalIndex:
    """Per-key time-sorted values with O(log n) as-of lookup."""
    def asof(self, key: tuple, ts: datetime) -> Value | None

class FeatureView:
    @classmethod
    def from_store(cls, store: Store) -> FeatureView   # builds all indexes once
    def forecast_high(self, station, target_date, ts) -> int | None
    def econ_latest(self, series_id, ts) -> EconObs | None       # latest vintage at ts
    def econ_series_asof(self, series_id, ts, n) -> list[EconObs] # last n obs as known at ts
    def news_window(self, topic, ts, window) -> NewsAgg           # count, mean_tone
```

- Implementation: dict key → sorted arrays + `bisect` (generalizes the
  Context forecast index that fixed today's O(n·m) scan). Built once per
  run; lookups are O(log n).
- `news_window` uses per-topic prefix-sum arrays over time buckets so a
  trailing-window aggregate is two binary searches, not a scan.
- **Vintage semantics** (the subtle one): `econ_latest('CPIAUCSL', ts)`
  returns the value *as revised most recently before ts* for the most
  recent period whose release ≤ ts. Both dimensions (period, revision) are
  as-of. This is what ALFRED's realtime columns encode; the index stores
  (obs_date, knowable_at) → value and resolves max-obs_date-then-max-
  knowable_at ≤ ts.
- `Context` (strategy-facing) delegates to a `FeatureView` and adds
  positions/fees/quotes; `ctx.market()` keeps hiding settlement results.
  C3 consumes `FeatureView` directly.

### Tests
As-of correctness at boundary timestamps (== ts inclusive, +1µs exclusive);
vintage resolution across a revision (pre-revision ts sees old value);
news window at bucket edges; a property test: for random ts, results never
reference a knowable_at > ts (this doubles as the P1 unit-level proof).

---

## 6. C5 — Simulation engine (`hyxlab/sim.py`)

### Order model (extends today's buy-only v1)

```python
@dataclass(frozen=True)
class Order:
    venue: str; market_id: str
    side: Literal["yes", "no"]
    action: Literal["open", "close"]      # close = sell out of a position
    qty: float
    limit_price: float | None = None      # None ⇒ marketable
    tif: Literal["GTC", "IOC"] = "GTC"
    order_id: int = 0                     # assigned by engine
```

Lifecycle state machine:

```
submit ─▶ marketable? ── yes ─▶ FILLED (taker, ≤ displayed size; IOC drops remainder)
             │ no
             ▼
           RESTING ──▶ later snapshot crosses limit ─▶ FILLED (maker)
             │                                          (partial ok, stays resting)
             ├─▶ strategy cancel(order_id) ─▶ CANCELED
             └─▶ market close_time reached ─▶ EXPIRED
close action: sells `qty` of an existing position at the bid (taker) or
via resting ask (maker); engine rejects closes exceeding position (no
shorting — buying the opposite side is the sanctioned equivalent).
```

### FillPolicy plugin interface

```python
class FillPolicy(Protocol):
    def taker_fill(self, order, snap) -> Fill | None
    def maker_check(self, resting, snap) -> Fill | None
```
- `TouchFill` (default): today's semantics — fill at touch, capped at
  displayed size (∞ at candle grain); maker fills only on strict cross at
  the limit price.
- `HaircutFill(inner, ticks=1, size_frac=0.5)`: decorator; worsens price
  by N ticks and scales available size. Never silently applied — a C6
  sweep dimension (P3).
- `WalkBookFill` (post-B7): consumes stored depth levels; price = volume-
  weighted across levels, size = cumulative displayed.
- Later: `HazardMakerFill` — probabilistic maker fills à la homerun's Cox
  model, once we archive our own book/trade tape to train it.

### Accounting: runtime invariants, not conventions

State: `cash`, `positions[(strategy, venue, mkt, side)] → (qty, cost)`,
`fees_paid`. After **every** event the engine asserts:

```
I1  cash == −Σ(open flows) + Σ(close flows) + Σ(settle payouts) − fees_paid
I2  ∀ position: qty ≥ 0
I3  at settlement of market m: Σ_strategies pnl(m) == payout(m) − cost(m) − fees(m)
```
Violation ⇒ `SimAccountingError`, run aborted, no metrics emitted. An
accounting bug must be impossible to mistake for PnL (P4). Equity curve =
cash + Σ qty·mark; mark = settlement value if resolved, else last mid,
else last trade price (in that order, per market).

### Event loop

```
for snap in snapshots (strict ts order, stable tiebreak venue+market):
    ctx.observe(snap)
    engine.expire_and_maker_check(snap)      # resting orders vs this market
    for strat in strategies:
        for order in strat.on_snapshot(snap, ctx):
            engine.submit(strat, order, snap)
    invariants.check()
settle_all(); invariants.check(); metrics()
```
Determinism requirements: no wall-clock reads, no dict-iteration-order
dependence (strategies applied in registration order; fills logged with a
monotonic sequence number). Complexity: O(#snapshots × #strategies) with
O(log n) feature lookups — today's 75K-snapshot replay runs in seconds;
an exchange-wide year at hourly grain (~10⁷ snapshots) stays practical,
with per-category filtering as the first knob if it isn't.

### Out of scope, permanently documented
Reactive market impact and queue-position modeling. Impact is *bounded*
(P3) via caps/haircuts/persistence-filters; what replay can't know is
delegated up the tier ladder (Tier 3 = shadow orders against live books).

### Tests
Every lifecycle edge (partial maker fill, IOC remainder drop, expiry at
close, close-exceeds-position rejection); invariant property test on random
order streams (see C7); regression: all 50 existing tests keep passing —
v1 behavior is the TouchFill special case.

---

## 7. C6 — Run harness & manifests (`hyxlab/harness.py`)

### Manifest schema (`runs/<run_id>/manifest.json`)

```json
{
  "run_id": "2026-07-06T21_weather-v1_a3f9",
  "git_rev": "6ad51ba+dirty",
  "episode": {"path": "...", "content_hash": "sha256:..."},
  "strategies": [{"class": "WeatherNWS", "params": {"sigma": 2.7}}],
  "fill_policy": {"class": "TouchFill", "haircut": null},
  "window": {"start": "...", "end": "..."},
  "seed": 0,
  "trial_context": {"sweep_id": null, "n_trials_in_family": 1},
  "metrics": {...},
  "fills": "fills.parquet"
}
```
`run_id` embeds a short hash of (episode hash, params); re-running is
idempotent. `trial_context` is the anti-p-hacking hook: sweeps stamp every
child run with the family size so DSR (below) can deflate honestly.

### Experiment shapes

- `run(episode, strategies, fill_policy) → Manifest` — the primitive.
- `sweep(episode, strategy_cls, param_grid, fill_grid) → SweepReport`:
  cartesian product, parallel via read-only connections (safe, C2);
  report includes best/median/worst and **Deflated Sharpe** of the best
  variant given `n_trials` (Bailey & López de Prado formula; inputs:
  candidate SR, trials, skew, kurtosis of returns).
- `purged_walk_forward(episode, folds, embargo_days)`: folds partition on
  market **close dates**; training data for fold k excludes markets whose
  close falls within `embargo_days` of the test fold's span (adjacent-day
  weather/econ regimes leak through naive splits). Emits per-fold + pooled
  manifests. Calibrated strategies expose `fit(train_view) → params` to
  make the train/test boundary mechanical rather than conventional.
- `size_sensitivity(run)` — standard post-processor on every run: PnL
  across `max_qty × haircut` grid; output table lands in the manifest.
  The flat region of the curve is the believable capacity claim.
- `persistence_filter(fills, k)` — labels each fill with how many
  consecutive prior snapshots showed the same executable opportunity;
  reported PnL is broken out by k ∈ {1, 2, 3+}. Race-shaped edges (k=1
  only) are flagged as unclaimable.

### Tests
Manifest determinism (same inputs ⇒ same run_id and metrics hash), DSR
against published worked example, embargo correctness (a market closing
inside the embargo appears in neither train nor test).

---

## 8. C7 — Environment self-tests (`tests/` + `tests/golden/`)

What makes the environment itself trustworthy:

1. **Golden episodes** (committed, small): `golden/weather-week/` (one real
   NYC week incl. settlement, exported via C2) and `golden/synthetic-arb/`
   (hand-built candles with known extractable PnL to the cent). CI runs
   both through the engine and compares against pinned manifests. Any
   engine change that moves golden PnL fails until the pin is consciously
   updated in the same commit — behavior drift becomes reviewable.
2. **Adversarial peeker**: a strategy whose `on_snapshot` actively attempts
   lookahead — reads `ctx.market().result`, queries features at `ts + ε`,
   tries mutating Context/FeatureView internals, holds references across
   snapshots. Asserts every channel returns nothing/raises. Today's
   settlement-leak bug becomes a permanent regression class.
3. **Determinism probe**: golden episode twice in one process + once in a
   fresh process ⇒ identical metric hashes (catches dict-order and
   wall-clock leaks).
4. **Accounting property test** (hypothesis): random order streams against
   random book paths; invariants I1–I3 must hold for every generated
   sequence; shrinking gives minimal counterexamples.
5. **Data-quality gates as declared checks**: each signal source registers
   a `ValidityCheck` run by C6 before any backtest reads it —
   MOS: day-ahead MAE ∈ [1, 5]°F and exact-match < 60% (the gate that
   already caught Miami); ALFRED: vintage monotonicity (revisions never
   retro-dated); GDELT: dedup rate sanity + seendate within window; sweep:
   no gaps (from `sweep_log`) inside the episode span. A failed gate
   aborts before PnL exists, mirroring the pre-registration discipline in
   code.

---

## 9. C8 — Scheduler & sweep (`hyxlab/scheduler.py`, `hyxlab/sweep.py`)

### Sweep algorithm (the P5 payload)

```
daily:
  series = kalshi.get_series_list()                  # all categories
  for s in series (minus denylist):
      settled = get_markets(s, status=settled, min_close_ts=now−48h)
      new = anti-join vs sweep_log                   # idempotent
      upsert market metadata + results
      for m in new: pull hourly candles open→close; insert_new
      sweep_log += (series, day, n_markets, n_candles, 'ok'|error)
```
Enumeration is a **category allowlist** over the series list (verified:
one unpaginated call returns all 11,170 series with categories). Launch
set: Economics, Financials, Climate and Weather, Companies, Commodities,
Science and Technology, Health, World — ~2,240 series. The ~8,200
sports/entertainment/politics series that dominate settle volume (probed:
the first 1,000 of a 48h global settled page were a single esports parlay
series) are excluded at launch; the allowlist is one line in
`watchlist.json` and revisitable — Kalshi's deletions are not.

Series metadata also carries `fee_type`/`fee_multiplier` — the sweep
stores them, and the fee model resolves per-series: makers pay zero on
`quadratic` series (11,040 of 11,170) and ¼-taker only on
`quadratic_with_maker_fees` (130). Our v1 always-charge-makers model was
pessimistic for most series (taker-only results, incl. the weather FAIL,
unaffected).

### Scheduler
One long-lived process owning the single DB writer handle:

```
tasks = [ (sweep, daily@06:00 UTC), (collector_focus_topofbook, 5 min),
          (nws_pull, 1 h), (alfred_pull, daily), (gdelt_pull, daily) ]
loop: run due tasks sequentially; per-task try/except → sweep_log/error;
      one task failing never blocks the loop.
```
Deployment: systemd unit (or `--foreground` for tmux). `flock` on the DB
guards against concurrent manual writers (C2).

### `--doctor`
Prints: last success + age per task, sweep gaps by series/day, row deltas
vs yesterday, DB/parquet size, failed-task tail. One command answers "has
the archive been healthy while I wasn't looking."

### Storage projection
Candles ≈ 100 B/row; even 10⁴ markets/day × ~30 rows ≈ 30 MB/day raw,
~5 MB parquet-compressed ⇒ single-digit GB/year. No rotation needed; the
constraint is API pacing, not disk.

---

## 10. Cross-cutting: what is explicitly NOT in v2

- Reactive market impact / agent-based simulation (bounded instead; tier
  ladder covers the residual).
- LLM-in-the-signal-path (deterministic signals first; local Qwen feature
  extraction is a later experiment on top, pinned model, airgapped).
- Polymarket historical books (no public source; collect-forward only).
- Any live/capital execution path. Shadow-order support (Tier 3) is a
  separate, later design with its own review.

## 11. Build order

| # | Component slice | Effort | Rationale |
|---|---|---|---|
| B1 | C8 sweep + sweep_log + C2 `insert_new` dedup + series denylist | ~½ day | Retention clock is running (P5); also fixes the backfill-rerun dup defect |
| B2 | C5 order lifecycle (sells/close, cancel, IOC, partial maker) + runtime invariants; C6 `run()` + manifests | ~1 day | Unblocks non-hold strategies; locks P2/P3 foundations |
| B3 | C7 golden episodes, adversarial peeker, determinism probe, accounting property test | ~½ day | Cheap now, expensive to retrofit; pins B2 behavior |
| B3.5 | **Trade tape** (user-prioritized 2026-07-06): `trades` table (venue, market_id, ts, yes_price, count, taker_side); sweep pulls Kalshi `GET /markets/trades` per settled market alongside candles; retro-pass over already-archived markets while still inside retention | ~½ day | Executions with size + aggressor side: fill-model calibration (Cox-style maker fills), volume-at-price, and it's purgeable data — same retention clock as candles |
| B4 | C1 alfred/gdelt(/alpaca port) + C2 tables + C4 FeatureView | 1–2 days | Signal layer with honest knowable_at |
| B5 | C6 sweep/purged-WF/size-sensitivity/persistence + DSR | ~1 day | Iteration machinery with anti-p-hacking built in |
| B6 | C3 calibration atlas + event study v1 | ~1 day | First pattern reports over the growing archive |
| B7 | C8 depth collector (WS book levels) + C5 WalkBookFill | background | Impact: bounded → modeled |

Strategy work (longshot bias, econ prints, weather v2, news-lag) consumes
the environment; each arrives via its own pre-registration when its
supporting slice lands, and is intentionally not specified in this doc.

## 12. Risks

- **Sim optimism bounded, not eliminated** until B7+; tier ladder + P3
  outputs are the mitigation; no Tier-1/2 PASS green-lights capital.
- **Venue API drift/retention change** — B1 first; connectors thin and
  isolated so breakage is local and loud (typed mappings, fixture tests).
- **News timestamp fidelity** (±15 min) caps news strategies at daily
  horizons — encoded as GDELT's ValidityCheck, not a convention.
- **Multiple-testing risk grows with iteration speed** — trial counts in
  manifests + DSR + pre-registration keep the honesty budget explicit.
- **Scope risk**: v2 is ~5 focused build-days across 7 slices; each slice
  lands independently useful, so a pause after any B-step leaves a working,
  more-capable lab than the day before.
