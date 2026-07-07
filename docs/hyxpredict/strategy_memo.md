# Strategy memo: if the goal is making money, this is what I'd do

**Written 2026-07-06.** Sponsor asked directly: *"if you are trying to make
money what would you do."* This is the honest ranked answer, based on the
deep-dive re-analysis (`research/deepdive_reanalysis.md`), fresh
investigation (sources in §5), and the constraints that actually bind:
one person + an LLM, US-based, job-search window (time-poor), no
authorized capital, strong local compute but no low-latency
infrastructure.

**Scope note:** this partially reopens the deferred prediction-market
track at the sponsor's explicit request. It does not change the Phase 0
post-mortem's other conclusion: the write-up is still the highest-value
pending artifact, and capital authorization remains a user-only decision.

---

## 0. The frame: what kind of edge is even available to us

The evidence from the deep-dive and the 2026 literature is consistent:
profitable prediction-market participants have exactly three edge types —
information/modeling, structural (market-making/rebates), and speed. Speed
is out (infrastructure arms race we can't win). Structural is
capital-scaled (immaterial at small size). That leaves **modeling edges in
markets where the marginal counterparty is a retail bettor, not a
Susquehanna desk** — small, unglamorous markets with objective settlement
and public data that rewards pipeline discipline over secrets.

That is also the only edge type that survives Phase 0's lesson: any thesis
must be cheaply falsifiable on recorded data *before* commitment. The
testing backbone for that now exists (`hyxlab/` — collectors, fee models,
no-lookahead replay simulator).

## 1. Ranked: what I would actually do

### #1 — Kalshi daily-high weather markets, NWS-systematic (primary candidate)

The thesis: Kalshi's temperature markets (KXHIGHNY/CHI/MIA/AUS/DEN) settle
on the NWS Climatological Report — objective, no oracle risk, no
resolution ambiguity. The best public forecast data (NWS gridpoint/NBM) is
free. NWS 24h forecast highs land within ~3–4°F of the observed high
roughly 80% of the time, while retail flow anchors on round numbers and
misprices bracket tails. Contract sizes fit small capital; the work is
pipelines and calibration — our comparative advantage — not latency.

Why it might already be dead (test, don't assume): at least five public
"weather edge finder" tools shipped in 2025–26, which both validates the
idea and commoditizes its naive version; pro MMs may quote these tight;
and same-day markets structurally know more than a morning forecast (the
market watches the realized intraday high — hyxlab's WeatherNWS therefore
skips same-day by default).

**Falsification plan (zero capital):** run the collector for 3–4 weeks;
replay WeatherNWS against settlements; fit per-city bias/sigma from
forecast-vs-settlement history; pre-register PASS thresholds *before*
looking at settled PnL (Phase 0 discipline — draft: fee-adjusted settled
PnL > 0 over ≥100 next-day trades AND model calibration not worse than
market's at the traded prices). A null here kills the thesis cleanly and
is itself writable.

### #2 — Resolution-rule asymmetry (opportunistic, LLM-native)

Practitioner consensus lists three real retail edges; one is "underpriced
rule asymmetry" — participants who never read the resolution criteria.
Reading hundreds of pages of market rules carefully and flagging
mispricings is literally what an LLM is for. Not systematically
backtestable like #1, so it stays opportunistic: a periodic scan across
open markets, surfacing candidates with the rule text and the market's
price for human-verified action. Build only if #1's pipeline proves out.

### #3 — Cross-venue arb (measurement already running, expected null)

The fee wall (re-analysis §2: ~3¢/share combined taker fees near 50¢)
predicts near-zero surviving opportunities. The CrossVenueArb strategy
doubles as the measurement instrument once matched Kalshi↔Polymarket pairs
are hand-verified and added to the watchlist. Value is the clean number
(and the write-up), not expected profit.

### #4 — Market-making / liquidity rewards (parked)

The only fee-positive seat (Polymarket pays maker rebates; Kalshi charges
makers ~¼ of taker). But returns scale with deployed capital: even
promotional claims (1–3%/mo) put $10K at $100–300/mo before inventory
risk. Revisit only if a capital conversation happens AND the sim gains a
proper quoting engine (v2). Not a solo income plan.

### Rejected outright

- **Latency/oracle arb**: bot-vs-bot infrastructure race; fees designed
  against it; our GPU is irrelevant (network latency, not compute).
- **Copy-trading leaderboards**: survivorship bias + latency decay.
- **Forecasting big political/econ markets**: the marginal counterparty
  is professional; venue conditions falsified this framing in April.
- **Anything requiring capital before a pre-registered PASS on recorded
  data**: that ordering is the entire lesson of Phase 0.

## 2. The honest expected value

Being direct, because the sponsor asked directly: the realistic best case
for #1 is a small, grindy edge — order of hundreds of dollars a month at
small size, decaying as tools proliferate. The distribution's modal
outcome is a null. What is *not* speculative: the falsification pipeline,
the fee-model math, and a clean measured result (either sign) are
portfolio artifacts in a job search targeting quant/data roles — which
remains the highest-EV "make money" move on the table. This track is
designed so its worst case still produces that artifact.

## 3. What's running / how to operate it

```bash
python -m hyxlab.collect --interval 300   # leave running (or --once to test)
python -m hyxlab.run_sim                  # replay stored data through baselines
python -m pytest tests/test_hyxlab_*.py   # 17 unit tests (fees, sim, parsers)
```

Data lands in `data/hyxlab.duckdb` (gitignored). Collector covers 5
Kalshi weather series (all strike brackets, top-of-book), NWS 7-day
forecasts per settlement station (every pull timestamped for no-lookahead
replay), and Polymarket CLOB books for any watchlist pairs. See
`hyxlab/__init__.py` for the v1 fill-model caveats.

## 4. Decision checkpoints

> **Update 2026-07-06:** the Tier-1 historical backtest (built same day
> once Kalshi candle history + IEM forecast archives proved available)
> already resolved checkpoint 1 early: **WeatherNWS v1 FAILED**
> (ROI −3.0%, 1,654 fills, 4/5 cities negative; gross ≈ break-even, fees
> decide). See `prereg_weather_backtest.md` for the full record.
> Thesis #1 in its naive form is dead; sanctioned successors are a
> pre-registered v2 (per-city calibration, split-sample) and the
> econ-print thesis (#2 pipeline unchanged).

1. **~3 weeks of data** (late July 2026): pre-register weather thresholds,
   then evaluate. PASS → per-city calibration + paper-trade live cycle.
   FAIL → write the null, close the track, keep the lab.
   *(Superseded — resolved 2026-07-06 via Tier-1 backtest: FAIL.)*
2. **Any capital step**: user-only decision, needs a pre-registered PASS
   first. Default remains zero capital.
3. **Write-up** (`phase0_postmortem.md` recommendation): still pending,
   still first in line for prose time.

## 5. Sources

- Kalshi API (public data, demo env): [docs.kalshi.com quick start](https://docs.kalshi.com/getting_started/quick_start_market_data)
- Kalshi fee schedule: [kalshi.com/fee-schedule](https://kalshi.com/fee-schedule)
- Weather-market mechanics + settlement: [Kalshi help center](https://help.kalshi.com/en/articles/13823837-weather-markets)
- Weather edge commoditization (examples): [weatheredgefinder.com](https://weatheredgefinder.com/), [kalshiweatheredge.net](https://kalshiweatheredge.net/), [outcomeedge.live](https://outcomeedge.live/weather)
- Polymarket CLOB/Gamma public APIs: [docs.polymarket.com](https://docs.polymarket.com/developers/CLOB/introduction)
- Retail edge taxonomy (cross-platform spreads, thin quotes, rule asymmetry): [zenhodl.net state-of-prediction-markets-2026](https://zenhodl.net/blog/state-of-prediction-markets-2026)
- Fee wall + arb baseline: `research/deepdive_reanalysis.md` §2, [arXiv 2508.03474](https://arxiv.org/abs/2508.03474)
- NWS API: [api.weather.gov](https://api.weather.gov) (free, User-Agent required)
