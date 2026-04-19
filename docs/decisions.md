# Decisions Log

Scannable index of locked decisions for the hyxrestration project. Each row is the decision + one-line reasoning + pointer to the full rationale. Full reasoning lives in the source docs — don't duplicate it here.

**Sources:**
- `llm_trading_orchestration.md` — project architecture doc (full prose)
- `agent-memory/orchestrator/project_scoping.md` — scoping memory (extended rationale)
- `docs/plans/architecture/design.md` — implementation-level draft (superseded, under co-design)

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

| ID | Decision | Status |
|----|----------|--------|
| A01 | **Python primary**, no Rust initially. Rust only for specific subsystems if a concrete bottleneck emerges (scheduler = optional slice 11) | ✅ |
| A02 | **SQLite (WAL) primary** for all operational state; **DuckDB read-only analytical layer** added at slice 9 via `sqlite_scanner`; Parquet deferred indefinitely | ✅ |
| A03 | **HuggingFace `transformers`** for inference initially; vLLM only if hot-swap or backtest throughput bottlenecks | ✅ |
| A04 | **Base model: Qwen 2.5 7B Instruct** (Apache 2.0, strong 7B benchmarks, digit-level tokenizer) | ✅ |
| A05 | **Agent roster:** Sentiment (LoRA), Domain (rules→maybe LoRA), Technical (PatchTST, not LLM), Macro (rules→maybe LoRA), Risk (deterministic Python), Meta (aggregator→LoRA post-DPO). **Net steady-state LoRAs: 2** (possibly 4) | ✅ |
| A06 | **Vertical-slice build** — 11 slices, each ships end-to-end (replaces 7-monolithic-milestone order) | ✅ |
| A07 | **`asyncio`** for concurrency; no Tokio / crossbeam | ✅ |
| A08 | **Hard adapter swap** initially (~50–100ms swap); X-LoRA / LoRA-Switch deferred until adapter chain latency matters | ✅ |
| A09 | **DPO via `trl`** on LoRA adapters; trade outcomes as preference pairs | ✅ |
| A10 | **Alpaca paper** brokerage for data + execution | ✅ |

---

## Strategy (system-level)

| ID | Decision | Status |
|----|----------|--------|
| ST01 | **Push-based continuous prediction** — system continuously predicts per ticker; opens/closes when Meta judges threshold met. Replaces T1–T5 theory-of-edge taxonomy as artificial | ✅ |
| ST02 | **Prediction shape v1** = `(direction, confidence)` per ticker per agent at 3-day horizon. Single horizon, no probability calibration. v2 = calibrated probability + Brier. v3 = multi-horizon (1d/3d/5d). v4 = distribution (mean+var) with Kelly sizing | ✅ |
| ST03 | **Strategic decisions are LLM-decided by Meta**, not hardcoded rules (anti-overtrading, hold duration, mid-hold management, hedging, event-window response, cash-vs-invested) | ✅ |
| ST04 | **Hardcoded safety rails** (not subject to LLM judgment): 20%/ticker cap, 5 concurrent positions max, daily -5% halt, monthly -10% halt, long-only at start, ATR server-side stops, no real money without authorization | ✅ |
| ST05 | **Meta v1 = Qwen 2.5 7B Instruct base + structured prompt (no LoRA)**; Meta v2 adds LoRA via DPO at slice 10+ | ✅ |
| ST06 | **Meta inputs:** agent signals + portfolio state + recent decision history + relevant context (events, time-of-day, recent outcomes). Concrete schema at slice 7 | ✅ |
| ST07 | **Meta outputs:** structured JSON per ticker — `(action, size, exit_conditions, reasoning_trace)`. Reasoning trace required because it becomes DPO training data | ✅ |
| ST08 | Meta prompt provides **priors as guidance, not constraints** (default 1–5d holds, once-per-day cycle, no rapid open/close on same ticker, cash is valid output); DPO tunes priors over time | ✅ |

---

## Trading mechanics

| ID | Decision | Status |
|----|----------|--------|
| T01 | Position sizing: **quarter-Kelly on Meta confidence** | ✅ |
| T02 | Max position per ticker: **20% of equity** | ✅ |
| T03 | Max concurrent positions: **5** | ✅ |
| T04 | Daily loss circuit breaker: **-5% → halt new positions 24h** | ✅ |
| T05 | Monthly drawdown breaker: **-10% → halt, manual restart** | ✅ |
| T06 | Order type: **bracket** (limit entry ± 10 bps, server-side ATR stop at broker, 2:1 take-profit default) | ✅ |
| T07 | TIF: **GTC for entries (24h), market-close for urgent exits** | ✅ |
| T08 | Backtest slippage model: **midpoint + 2 bps + simulated 100ms latency** | ✅ |
| T09 | Observability: **structured log lines to SQLite `audit_log`** (append-only); Telegram bot on failure triggers, implemented per failure class | ✅ |
| T10 | Secrets: **`.env` + `python-dotenv`** (gitignored) for dev | ✅ |

---

## Build sequence

| Slice | Deliverable | Status |
|-------|-------------|--------|
| 1 | Proof of wiring: DE-only OHLCV + news + FinBERT → SQLite; `python -m hyx.slice1` prints joined view | ✅ (spec next) |
| 2 | Multi-ticker ag universe (~12–15 tickers) via asyncio | ✅ |
| 3 | Domain signals: USDA WASDE + NOAA Drought + CBOT; ag-conditions-per-ticker view | ✅ |
| 4 | Technical agent: PatchTST trained on 10y OHLCV; directional probability at 1/3/5d | ✅ |
| 5 | Qwen + LoRA Sentiment (replaces FinBERT); A/B OOS vs baseline | ✅ |
| 6 | Macro agent (rule-based): VIX band, 2s10s, DXY, FOMC calendar | ✅ |
| 7 | Risk + Meta modules; full signal → decision pipeline, still paper-logging only | ✅ |
| 8 | Paper-trade execution via Alpaca bracket orders | ✅ |
| 9 | Eval harness + walk-forward validation; DuckDB analytical layer added | ✅ |
| 10 | DPO training loop on Sentiment LoRA; shadow-deploy + auto-rollback | ✅ |
| 11 | (Optional) Rust GPU scheduler — skip unless Python insufficient or explicit learning exercise | ✅ |
| 12+ | As-needed: vLLM, X-LoRA, variant-C pivot, expansion beyond ag, live trading (on authorization) | ✅ |

Full slice specs: `llm_trading_orchestration.md` §7.

---

## Under co-design (implementation-level architecture)

Sections of `docs/plans/architecture/design.md` currently being rewritten one at a time — each gets discussed and locked before re-committing.

| Section | Topic | Status |
|---------|-------|--------|
| §1 | Process model (3 daemons at slice 8+, SQLite polling, no message bus) | 🔄 in discussion |
| §2 | Repo layout | 🔄 |
| §3 | SQLite schema / DDL + single-writer-per-table convention | 🔄 |
| §4 | Config (`.env` + YAML layering) | 🔄 |
| §5 | Error handling / retry / halt policy per failure class | 🔄 |
| §6 | Concurrency model (asyncio scope, single-writer rules) | 🔄 |
| §7 | Observability (`audit_log` shape, Telegram hooks) | 🔄 |
| §8 | Slice-1 cuts (minimum-viable DE-only thread) | 🔄 |

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

---

## Blocked on user authorization

| Topic | Default | Required action |
|-------|---------|-----------------|
| Real-money activation | Never | Explicit user OK; no fallback |
| Hard budget threshold for paid services | $0 | User revises |
| Ethical trading constraints | None | User flags |
