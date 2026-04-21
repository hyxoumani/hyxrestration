# Decisions Log

Scannable index of locked decisions for the hyxrestration project. Each row is the decision + one-line reasoning + pointer to the full rationale.

**Sources:**
- **This file** — canonical current state. Supersedes the narrative docs where they conflict.
- `llm_trading_orchestration.md` — narrative architecture doc. **Partially stale post-2026-04-20 co-design** (slice 4 reshuffled, Meta split, SQLite→DuckDB). Kept for historical reasoning.
- `agent-memory/orchestrator/project_scoping.md` — scoping memory. Same caveat.
- `docs/plans/architecture/design.md` — implementation-level draft. **Largely moot** (three-daemon model abandoned; one script per slice is the new model).

**Status legend:** ✅ locked · 🟡 tentative (re-lock on evidence) · 🔄 under co-design · ⏳ deferred · 🔒 blocked on user auth

---

## Operating mode

**Sponsor model** ✅ — User delegates strategy / architecture / build decisions to Claude. Claude decides, documents, executes. User retains authorization on resource-touching decisions only (real money, hard budget, ethical limits). Architectural / technical-design decisions are co-designed (discuss → converge → write), not unilateral.

Cross-session memory: `/home/devs/.claude/projects/-home-devs-workspace-hyxrestration/memory/sponsor_mode.md`

---

## Foundational thesis

| ID | Decision | Status |
|----|----------|--------|
| **L01** | **Load-bearing advantage claim:** Cross-modal information aggregation on a small under-covered universe, accumulating *encoded* domain knowledge that compounds, at swing time horizons that fall in institutional capacity / mandate gaps. Three components — all load-bearing: (1) cross-modal integration on small universe, (2) encoded domain knowledge that compounds, (3) 2–10d time-horizon arbitrage between HFT and PM cadences. Testable: OOS Sharpe vs SPY with per-agent ablations. | ✅ |

---

## Strategic decisions

(Came out of scoping discussion with user; legitimately locked.)

| ID | Decision | Status |
|----|----------|--------|
| S01 | Tier: **T2** (OOS walk-forward Sharpe beats SPY across ≥3 regimes) | ✅ |
| S02 | Trading style: **Swing 1–5 day holds** | ✅ |
| S03 | Niche: **Agriculture specialist**, variant B (crop-side: fertilizer + seeds + processors + equipment + ag-commodity ETFs) | 🟡 (final sub-niche lock after Phase 0 domain learning, ~slice 3) |
| S04 | Long/short: **Long-only at start**; add shorts post-T1 once Risk + slippage modeling mature | ✅ |
| S05 | Headline metric: **12-mo rolling OOS Sharpe** on walk-forward (24mo train / 1mo test / monthly roll). Secondaries: max DD, Sortino, Brier, turnover, active share vs SPY | ✅ |
| S06 | Kill criteria: **Tiered M3/M6/M12 gates at T0/T1/T2** + continuous auto-rollback on any DPO promotion that regresses validation OOS Sharpe | ✅ |
| S07 | Real money: **Paper-only until explicit user authorization** (min bar: 3+ mo consistent paper + T2 gate passed) | 🔒 |
| S08 | Long-only hedging: **Accept non-ag noise** (USD, rates, broad beta) initially; revisit if noise dominates returns in eval | ✅ |

---

## Architectural decisions

Items marked 🔄 were written into the project doc but never actually co-designed with the user; flagged for later discussion.

| ID | Decision | Status |
|----|----------|--------|
| A01 | **Python primary**, no Rust initially. Rust only for specific subsystems if a concrete bottleneck emerges (scheduler = optional slice 11). | ✅ co-designed 2026-04-19 |
| A02 | **DuckDB primary** for operational state. Single-writer reality (one LLM pipeline at a time) removes SQLite's concurrency edge; DuckDB gives analytical speed from day 1. Single file, trivial backup. Parquet deferred indefinitely. | ✅ co-designed 2026-04-20 |
| A03 | HuggingFace `transformers` for inference initially; vLLM only if hot-swap or backtest throughput bottlenecks | 🔄 |
| A04 | Base model: Qwen 2.5 7B Instruct (Apache 2.0, strong 7B benchmarks, digit-level tokenizer) | 🔄 |
| A05 | **Agent roster (revised post-A11 Meta split):** Sentiment (LoRA), Domain (rules + data), **Technical as inline features only — no standalone agent**, Macro (rules), Risk (deterministic Python), Meta (split per A11). Net steady-state LoRAs: 2 (Sentiment + eventual Meta LoRA post-DPO). | 🔄 |
| A06 | **Vertical-slice build** — ship slice 1 fast, structure emerges from real usage. Replaces original 7-monolithic-milestone plan. | ✅ co-designed 2026-04-20 |
| A07 | Concurrency: **sequential loops, no asyncio**. 14 tickers × ~1s each is fast enough without async. Revisit if/when a slice demonstrates a real need. | ✅ co-designed 2026-04-20 (supersedes prior `asyncio`-default) |
| A08 | Hard adapter swap initially (~50–100ms swap); X-LoRA / LoRA-Switch deferred until adapter chain latency matters | 🔄 |
| A09 | DPO via `trl` on LoRA adapters; trade outcomes as preference pairs | 🔄 |
| A10 | ~~Alpaca paper brokerage for data + execution~~ **Superseded 2026-04-21.** Split into three: (a) **yfinance** for daily OHLCV (free, no account, 10+ yr depth incl. adj close); (b) **Alpaca Benzinga feed** for symbol-tagged ag news (free account; data-only, not a broker commitment); (c) **broker TBD at slice 8** — evaluated against execution-quality criteria (fees, fills, short locate, bracket orders) when we actually need execution. Rationale: "single-provider consistency" was the original argument but feed-divergence only matters at microseconds for daily-bar swing trading, and slice 9 eval is against historical DB, not live broker fills. | ✅ co-designed 2026-04-21 |
| **A11** | **Meta architecture: split into two layers.** **Quant Meta** = deterministic code (normalize agent signals → aggregate with hand-picked weights, upgrade to tiny logreg when outcome data exists → Kelly-size → risk-filter → candidate orders table). **LLM Meta** = Qwen reads candidate orders + news + upcoming events → `pass` / `veto` / `adjust` per candidate + reasoning trace (becomes DPO training data later). LLMs are bad at precise numerics; split puts the right tool on each half of the job. | ✅ co-designed 2026-04-20 |

---

## Strategy (system-level)

| ID | Decision | Status |
|----|----------|--------|
| ST01 | **Push-based continuous prediction** — system continuously predicts per ticker; opens/closes when Meta judges threshold met. Replaces T1–T5 theory-of-edge taxonomy as artificial | 🔄 |
| ST02 | **Prediction shape v1** = `(direction, confidence)` per ticker per agent at 3-day horizon. Single horizon, no probability calibration. v2 = calibrated probability + Brier. v3 = multi-horizon (1d/3d/5d). v4 = distribution (mean+var) with Kelly sizing | 🔄 |
| ST03 | **Strategic decisions are LLM-decided by Meta**, not hardcoded rules (anti-overtrading, hold duration, mid-hold management, hedging, event-window response, cash-vs-invested) — now *partially* overridden by A11 Meta split: quantitative aggregation is code, qualitative judgment is LLM | 🔄 (needs revision per A11) |
| ST04 | **Hardcoded safety rails** (not subject to LLM judgment): 20%/ticker cap, 5 concurrent positions max, daily -5% halt, monthly -10% halt, long-only at start, ATR server-side stops, no real money without authorization | 🔄 |
| ST05 | **Meta v1 = Qwen 2.5 7B Instruct base + structured prompt (no LoRA)** — now applies to LLM Meta only; Meta v2 adds LoRA via DPO at slice 10+ | 🔄 (needs revision per A11) |
| ST06 | **Meta inputs:** agent signals + portfolio state + recent decision history + relevant context (events, time-of-day, recent outcomes). Concrete schema at slice 6–7 | 🔄 |
| ST07 | **Meta outputs:** Quant Meta → candidate orders table `(ticker, direction, size, entry_limit, stop_price, target_price, composite_score, contributors)`. LLM Meta → pass/veto/adjust per candidate + reasoning trace. | 🔄 (revised per A11) |
| ST08 | Meta prompt provides **priors as guidance, not constraints** (default 1–5d holds, once-per-day cycle, no rapid open/close on same ticker, cash is valid output); DPO tunes priors over time | 🔄 |

---

## Trading mechanics

Numerical defaults carried forward from scoping; not yet co-designed with user.

| ID | Decision | Status |
|----|----------|--------|
| T01 | Position sizing: quarter-Kelly on Meta confidence | 🔄 |
| T02 | Max position per ticker: 20% of equity | 🔄 |
| T03 | Max concurrent positions: 5 | 🔄 |
| T04 | Daily loss circuit breaker: -5% → halt new positions 24h | 🔄 |
| T05 | Monthly drawdown breaker: -10% → halt, manual restart | 🔄 |
| T06 | Order type: bracket (limit entry ± 10 bps, server-side ATR stop at broker, 2:1 take-profit default) | 🔄 |
| T07 | TIF: GTC for entries (24h), market-close for urgent exits | 🔄 |
| T08 | Backtest slippage model: midpoint + 2 bps + simulated 100ms latency | 🔄 |
| T09 | Observability: structured log lines to DuckDB `audit_log` (append-only); MD/CSV report per run; Telegram bot on failure triggers, implemented per failure class | ✅ (log shape + report convention locked 2026-04-20) |
| T10 | Secrets: `.env` + `python-dotenv` (gitignored) for dev | ✅ (locked 2026-04-20) |

---

## Build sequence (revised 2026-04-20)

Slice 4 pulled forward from Technical to Macro (Technical collapsed to inline features at slice 6 per A11). Meta split into slice 6 (Quant) + slice 7 (LLM).

| Slice | Deliverable | Status |
|-------|-------------|--------|
| 1 | DE baseline — daily OHLCV + news + FinBERT + DuckDB + MD/CSV report | ✅ spec locked 2026-04-20 |
| 2 | Universe expansion — 14 ag tickers, sequential loop | ✅ spec locked 2026-04-20 |
| 3 | Domain context — USDA QuickStats + NOAA Drought + CBOT commodity settlements | ✅ spec locked 2026-04-20 |
| 4 | Macro (rules) — VIX, 2s10s, DXY, FOMC → regime + ag-tilt | ✅ spec locked 2026-04-20 |
| 5 | Qwen + LoRA Sentiment (replaces FinBERT); A/B OOS vs baseline | 🔄 |
| 6 | Risk + Quant Meta — technical features inlined; deterministic aggregation → candidate orders | 🔄 |
| 7 | LLM Meta — Qwen reads candidate orders + news + events → pass/veto/adjust + reasoning trace | 🔄 |
| 8 | Paper-trade execution via chosen broker (bracket orders; provider TBD — see A10) | 🔄 |
| 9 | Eval harness + walk-forward validation (DuckDB is already primary; no layer addition needed) | 🔄 |
| 10 | DPO training loop on Sentiment LoRA; shadow-deploy + auto-rollback | 🔄 |
| 11 | (Optional) Rust GPU scheduler — skip unless Python insufficient or explicit learning exercise | 🔄 |
| 12+ | As-needed: vLLM, X-LoRA, variant-C pivot, expansion beyond ag, live trading (on authorization) | 🔄 |

### Constraint on slices 1–4

**No model training. No trading decisions. No Meta. No Risk checks.** Pure ingest + compute substrate. FinBERT in slice 1 is pre-trained inference, not training.

### Locked specs — slices 1–4

**Slice 1 — DE baseline**
- Command: `python -m hyx.slice1` (manual, daily)
- Pull: OHLCV daily via yfinance (no account); news for DE via Alpaca Benzinga feed (free account)
- Score: FinBERT (GPU inference) over headlines
- Write: DuckDB + `reports/slice1/YYYY-MM-DD.{md,csv}`
- Schema: `ohlcv_daily(ticker, date, O/H/L/C, adj_close, volume)` PK (ticker, date) · `news(news_id, published_at, headline, summary, url, source)` PK news_id · `news_tickers(news_id, ticker)` PK (news_id, ticker) — many-to-many so one article tagging NTR+MOS+CF stays one `news` row · `news_sentiment(news_id, model, label, score, score_pos, score_neg, score_neu, scored_at)` PK (news_id, model) — `model` column lets FinBERT and slice-5 Qwen zero-shot coexist without migration
- Files: `hyx/slice1.py`, `hyx/db.py`, `hyx/config.py`
- Config: `.env` → `ALPACA_KEY`, `ALPACA_SECRET`
- Failure: crash loudly

**Slice 2 — Universe expansion**
- Command: `python -m hyx.slice2`
- 14 tickers: NTR, MOS, CF, CTVA, FMC, ADM, BG, DE, AGCO, CNHI, DBA, CORN, WEAT, SOYB
- Sequential loop, no asyncio
- Schema addition: `universe(ticker, added_at, active)`
- Per-ticker rows in report

**Slice 3 — Domain context**
- Command: `python -m hyx.slice3`
- Sources: USDA QuickStats API, NOAA Drought Monitor weekly CSV, CME/CBOT daily commodity settlements (corn/soy/wheat)
- Schema: `ag_conditions(source, date, metric, value, location NULL)`
- Dropped: revenue-geography weighting
- Native cadence per source (monthly / weekly / daily)

**Slice 4 — Macro (rules)**
- Command: `python -m hyx.slice4`
- Inputs: VIX, 2s10s yield spread (FRED), DXY, FOMC calendar
- Deterministic rules → `regime ∈ {risk_on, neutral, risk_off}` + `ag_tilt ∈ [-1, +1]`
- Schema: `macro_signals(date, regime, ag_tilt, vix, yield_curve_bps, dxy, fomc_days_until)`
- No Qwen, no training, pure Python

---

## Implementation-level architecture

Mostly simplified or made moot by the single-pipeline, one-script-per-slice model. `docs/plans/architecture/design.md` preserved for historical reference only.

| Topic | Decision | Status |
|---------|-------|--------|
| Process model | One script per slice, manual invocation. No daemons, no systemd, no message bus. Revisit only if a slice demonstrably needs a long-running process. | ✅ locked 2026-04-20 |
| Repo layout | Starts as `hyx/slice{N}.py` + `hyx/db.py` + `hyx/config.py`. Grows module tree only when a slice forces it. | ✅ locked 2026-04-20 |
| Schema / DDL | Tables defined in their owning slice. No upfront "final" schema. Migrations tracked per slice. | ✅ policy locked |
| Config | `.env` + `python-dotenv`; `.env.example` committed | ✅ locked 2026-04-20 |
| Error handling | Crash loudly in early slices (1–4). Per-failure-class handling added as failure modes are discovered in later slices. | ✅ locked 2026-04-20 |
| Concurrency | Sequential, no asyncio until a slice demonstrably needs it | ✅ locked 2026-04-20 |
| Observability | DuckDB `audit_log` (append-only) + MD/CSV reports per run `reports/slice{N}/YYYY-MM-DD.{md,csv}` | ✅ locked 2026-04-20 |

---

## Deferred (revisit in-slice)

| Topic | Default | Revisit trigger |
|-------|---------|-----------------|
| Final ag sub-niche (variant A/B/C/D) | B tentative | After Phase 0 domain substrate (slice 3-ish) |
| Paid-data budget | $0 | When concrete ROI argument exists |
| Ethical limits on instruments | None assumed | User flags any |
| Per-failure-class default actions | "Safe = halt, not liquidate" | Per slice, as failure modes are identified |
| Kalshi / Polymarket overlay | Not built | Post-slice-6 as potential Macro falsification harness |
| Second GPU | Not bought | Single-5090 VRAM becomes demonstrated bottleneck |
| Variant-C fertilizer pivot | Not taken | Variant B plateaus at tier gate |
| Expansion beyond ag | Not taken | After T1 reached and T2 looks plausible |
| Technical as standalone agent (option B) | Not built (replaced by inline features at slice 6) | Only if eval shows we need the ablation signal |

---

## Blocked on user authorization

| Topic | Default | Required action |
|-------|---------|-----------------|
| Real-money activation | Never | Explicit user OK; no fallback |
| Hard budget threshold for paid services | $0 | User revises |
| Ethical trading constraints | None | User flags |
