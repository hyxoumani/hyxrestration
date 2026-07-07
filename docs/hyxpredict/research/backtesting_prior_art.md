# Prior art: backtesting infrastructure & concepts for prediction markets

**Researched 2026-07-06.** What exists, what to steal, what to skip — mapped
to hyxlab's roadmap. Sources linked inline.

## 1. Existing prediction-market backtesting systems

| System | What it is | What to take |
|---|---|---|
| [homerun](https://github.com/braedonsaunders/homerun) (AGPL-3.0) | Open-source Polymarket/Kalshi platform: strategies as Python classes, L2 book replay from self-archived WebSocket deltas, **Cox proportional-hazards maker-fill model** (queue depth/spread/time-to-resolution covariates), trade-vs-cancel decomposition, measured-latency injection, walk-forward + parameter sweeps, shadow mode, **backtest/shadow/live PnL triangulation** | The concepts (AGPL — study, don't copy code): Cox fill model is the right-sized upgrade for maker fills; PnL triangulation is the right way to validate a simulator; their 0.5s × 25-level book archiving confirms our forward-collector priority (and that ours is too coarse for MM strategies) |
| [PredictionMarketBench](https://arxiv.org/abs/2602.00133) (arXiv 2602.00133) | SWE-bench-style benchmark: portable "episodes" built from Kalshi order books/trades/lifecycle/settlement; deterministic event-driven replay with maker/taker semantics and fees; evaluates classical + LLM agents | **Episode format** (self-contained, portable dataset + config per experiment — our "experiment manifest" idea, done properly). Headline finding matches ours independently: *naive agents lose to transaction costs; fee-aware strategies stay competitive* |
| [evan-kolberg/prediction-market-backtesting](https://github.com/evan-kolberg/prediction-market-backtesting) | NautilusTrader custom adapters for Polymarket | Reference if we ever outgrow our sim; NautilusTrader is the heavyweight standard |
| Marketlens (via [awesome-prediction-market-tools](https://github.com/aarora4/Awesome-Prediction-Market-Tools)) | Commercial tick-level historical Polymarket book/trade data + backtest API | The gap-filler if we ever need Polymarket book history (public APIs don't have it). Paid; defer |
| [PolyBackTest](https://polybacktest.com/resources/backtesting/page/6) / [PolySimulator](https://polysimulator.com/backtesting) | Hosted low-fidelity backtesters | Nothing — below our fidelity bar already |

Takeaway: our architecture (record → deterministic replay → fee-aware fills
→ settlement) is the same shape the serious projects converged on. Where
we're behind: maker-fill realism (Cox model), book depth in the collector,
and a walk-forward/manifest harness.

## 2. Validation methodology (the quant canon)

- **Deflated Sharpe Ratio** ([Bailey & López de Prado](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)):
  corrects reported performance for selection under multiple testing +
  non-normal returns. Our Phase-0 BH-FDR habit is the same instinct; DSR is
  the standard tool once we compare multiple strategy variants.
- **Purged / Combinatorial Purged Cross-Validation**
  ([overview](https://en.wikipedia.org/wiki/Purged_cross-validation),
  [comparison study](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)):
  CPCV beats single walk-forward on Probability of Backtest Overfitting.
  Direct application: **WeatherNWS v2 should use purged walk-forward, not
  the naive first-half/second-half split** I sketched — purging matters
  because adjacent days share weather regimes (serial correlation leaks
  across a naive split boundary).
- Look-ahead bias is enough of an industry problem that there's a
  [2026 paper on LLM-assisted lookahead detection](https://arxiv.org/pdf/2605.24564).
  Our plumbing-level enforcement (Context hides settlements/future
  forecasts) is the right defense; keep it structural, never conventional.

## 3. Fill-realism ladder (literature version of our tiers)

Static replay (us, Tier 1–2) → **probabilistic fill models** on replay
(Cox hazards — homerun) → interactive agent-based simulation with market
impact ([ABIDES](https://arxiv.org/pdf/2006.05574),
[LOB simulation review](https://arxiv.org/html/2402.17359v1),
["reality gap" paper](https://arxiv.org/abs/2603.24137)). The
research frontier (world-agent models, diffusion simulators) exists
because *replay cannot price your own impact*. At our size the Cox-style
middle rung is the ceiling worth building to; ABIDES-class is overkill
until a strategy needs to trade size that moves books.

## 4. Strategy concepts the literature hands us for free

- **Favorite-longshot bias** — the most robust documented inefficiency in
  prediction markets: longshots systematically overpriced, favorites
  underpriced ([QuantPedia synthesis of 20 studies](https://quantpedia.com/systematic-edges-in-prediction-markets/),
  [domain-calibration study, arXiv 2602.19520](https://arxiv.org/html/2602.19520v1)).
  Exploit: buy NO on longshots / YES on favorites, fee-aware.
  **This is model-free — no forecasts needed — and testable against our
  existing 75K-candle backfill today.** Natural next pre-registration, and
  it exercises the lab beyond weather.
- **Calibration varies by domain and horizon** (same 2602.19520): political
  prices compressed toward 50¢; sports well-calibrated under a week. A
  per-category calibration map built from our own archive is both a
  strategy input and a publishable artifact.
- **[Makers or Takers: The Economics of the Kalshi Prediction Market](https://www2.gwu.edu/~forcpgm/2026-001.pdf)**
  (GWU working paper) — venue-economics study of exactly our venue; read
  before designing any maker-side strategy.
- Historical inter-exchange arbitrage was real but seconds-lived and
  fee-fragile (QuantPedia; consistent with our §2 fee-wall math and the
  IMDEA study).

## 5. Resulting infrastructure priorities (proposed)

1. **Experiment manifests / episodes** (PredictionMarketBench pattern):
   every backtest emits params + data window + git state + metrics JSON;
   datasets addressable and re-runnable.
2. **Purged walk-forward harness** — required before any calibrated (v2)
   strategy; naive splits leak.
3. **Sells/exits in the sim** — prerequisite for longshot-bias and MM
   strategies (enter/exit, not only hold-to-settlement).
4. **Favorite-longshot backtest** on existing data — first cross-family
   test of the lab; new pre-registration.
5. **Collector depth upgrade** (order book levels via WebSocket, finer
   cadence) — feeds a future Cox fill model; homerun proves the approach.
6. DSR/PBO statistics once we run multi-variant comparisons.
