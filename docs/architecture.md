# Architecture

Canonical architecture for the hyxrestration project — a local LLM-driven autonomous swing-trading system for US agricultural equities. Reflects all decisions co-designed through 2026-04-20.

**Source-of-truth hierarchy:**
- This file — current architecture in narrative form
- [`decisions.md`](decisions.md) — scannable decision index with IDs (L01, S*, A*, ST*, T*)
- `llm_trading_orchestration.md` — original architecture proposal; **partially stale** (pre-co-design)
- `docs/plans/architecture/design.md` — early implementation draft; **largely superseded** (three-daemon model abandoned)

---

## 1. Project overview

### 1.1 What we're building

A local system that runs on a single workstation (RTX 5090, 32GB RAM) and autonomously makes swing-trading decisions on a small universe of US agricultural equities.

**One cycle, end-to-end:**

1. Pull fresh market data, news, weather/crop reports, macro indicators
2. Produce signals per ticker from several specialist agents
3. Aggregate signals + portfolio state into candidate orders (deterministic code)
4. An LLM reviews candidates against qualitative context (news, events) and passes / vetoes / adjusts
5. Approved orders go to a paper brokerage (provider TBD at slice 8)
6. Everything logged to a local DuckDB file

Later (conditional stretch): overnight DPO training on accumulated trade outcomes.

### 1.2 Scope constraints

- **Niche:** Agriculture specialist, variant B — crop-side (fertilizer + seeds + processors + equipment + ag-commodity ETFs). Final sub-niche re-lockable after Phase 0 domain substrate (~slice 3).
- **Universe:** 14 tickers — `NTR, MOS, CF, CTVA, FMC, ADM, BG, DE, AGCO, CNH, DBA, CORN, WEAT, SOYB`. (CNH = continuation ticker for what was CNHI prior to the Jan 2024 Iveco spin-out; yfinance carries the restated adj-close series back to 2013 under CNH only.)
- **Trading style:** Swing 1–5 day holds. Long-only at start. Shorts only post-T1.
- **Tier target:** T2 — OOS walk-forward Sharpe beats SPY across ≥3 regimes.
- **Paper-only:** Until explicit user authorization. Minimum bar for real money: 3+ months consistent paper AND T2 gate passed.
- **Single operator, single machine:** No distributed systems, no message buses, no container orchestration.

### 1.3 Operating mode

**Sponsor model.** User delegates strategy / architecture / build decisions. Claude decides, documents, executes. User retains authorization on resource-touching decisions only (real money, hard budget, ethical limits). Architectural and technical-design decisions are co-designed — discuss → converge → write, not unilateral.

### 1.4 Load-bearing thesis (L01)

**Edge claim:** Cross-modal information aggregation on a small under-covered universe, accumulating *encoded* domain knowledge that compounds over time, at swing time horizons that fall in institutional capacity / mandate gaps.

Three components, each load-bearing:

1. **Cross-modal integration on small universe** — 14 tickers × daily integration of news + earnings + USDA + NOAA + commodities + macro per ticker. Quant funds are siloed by mandate; multi-source integration on small N is a genuinely LLM-specific capability.
2. **Encoded domain knowledge that compounds** — accumulated ag expertise becomes a moat only if systematically encoded into prompts, training labels, rules, and feature engineering. Year-3 > year-1. Raw markdown ≠ alpha.
3. **Time-horizon arbitrage** — 2–10 day swing falls between HFT (<1s) and institutional value/momentum (months). Underserved because HFT can't trade at this cadence and PMs can't trade at this turnover.

Testable: OOS walk-forward Sharpe vs SPY with per-agent ablations. If false, kill at M6/M12 gates.

---

## 2. System architecture

### 2.1 Runtime model

**Scheduled batch, no daemons, no events.**

The system is not event-driven. It is not reactive. It is not always-running. Each slice is a Python script invoked manually (later possibly via cron). One script at a time, one LLM pipeline at a time, one writer at a time.

The original project proposal had three long-running daemons (`hyx-ingest`, `hyx-trader`, `hyx-train`). Abandoned — over-engineered for a single-operator swing-cadence system.

### 2.2 Process topology

```
[ manual invocation / cron ]
           │
           ▼
  python -m hyx.slice{N}
           │
     ┌─────┴─────────────────────────────────┐
     ▼                                       ▼
 Pull from external APIs          Read existing DuckDB state
 (yfinance, Alpaca news,
  USDA, NOAA, CBOT, FRED,
  local GPU inference)
     │                                       │
     └─────────────┬─────────────────────────┘
                   ▼
            DuckDB write (INSERT OR IGNORE)
                   │
                   ▼
          MD/CSV report + audit_log entries
```

One process, one DuckDB file, one writer at a time. No concurrency, no locking complexity.

### 2.3 Agent topology

Split Meta architecture (A11). Each component has the simplest implementation that fits its job.

| Component | Role | Implementation | Trains? |
|---|---|---|---|
| **Sentiment agent** | News headline → per-ticker sentiment | FinBERT (slices 1–4) + Qwen 2.5 7B zero-shot (slice 5+). Both scorers logged side-by-side in the same table. | Pre-trained only in core path. Sentiment LoRA = conditional stretch (slice 5b). |
| **Domain agent** | Ag-conditions per ticker | Rules + lookups over USDA / NOAA / CBOT tables | No |
| **Technical features** | Derived price/volume features for Quant Meta + Risk | Pandas CTEs over OHLCV, inlined at slice 6 where Risk/Meta consume them. No standalone agent. | No — no model at all. (Originally planned as PatchTST; retired because autoregressive OHLCV prediction is single-modal and weak; L01 is about cross-modal integration.) |
| **Macro agent** | Market regime + ag-sector tilt | Rules over VIX / 2s10s / DXY / FOMC calendar | No |
| **Risk module** | Position sizing + hard safety rails | Deterministic Python: 20%/ticker cap, 5 concurrent max, daily halt, monthly halt, ATR stops | No |
| **Quant Meta** | Numerical aggregation → candidate orders | Deterministic code: normalize → weighted aggregate → threshold → Kelly size → risk filter. Hand-picked weights v1, upgraded to 5-parameter logistic regression at slice 9+. | Logreg trains on outcomes; no ML before slice 9. |
| **LLM Meta** | Qualitative review of candidate orders | Qwen 2.5 7B Instruct + structured prompt. Reads candidate orders + news + events → `pass` / `veto` / `adjust` + reasoning trace. | No. Meta LoRA + DPO = conditional stretch (slice 10b). |

**Why split Meta:** LLMs are bad at precise numerics (arithmetic, comparisons at scale, weighted aggregation). Splitting puts the right tool on each half of the job — deterministic code for the math, Qwen for qualitative judgment on news/events/context. The LLM never computes Kelly sizing or enforces position limits.

**Signal flow:**

```
Sentiment agent  ─┐
Domain agent      │
Technical feats   ├──► Quant Meta ──► candidate_orders ──► LLM Meta ──► broker orders
Macro agent       │      + Risk                               │
Portfolio state  ─┘                                           ▼
                                                       reasoning trace
                                                      (DPO data later)
```

### 2.4 Technology stack

All decisions co-designed 2026-04-19 / 2026-04-20.

| Layer | Choice | Slice |
|---|---|---|
| Language | Python 3.14 (system default on dev box; 3.11+ acceptable) | 1 |
| Package management | `pip + venv + requirements.txt` | 1 |
| Database | DuckDB (single file, primary for everything) | 1 |
| Schema evolution | Numbered SQL migrations + `schema_migrations` tracker + ~50-line runner | 1 |
| Concurrency | None — sequential loops | 1 |
| ML framework | PyTorch via HuggingFace `transformers` | 1 |
| Base LLM | Qwen 2.5 7B Instruct (Apache 2.0, digit-level tokenization) | 5 |
| Sentiment baseline | FinBERT `yiyanghkust/finbert-tone` | 1 |
| OHLCV source | `yfinance` (free, no account, 10+ yr depth, adj-close built-in) | 1 |
| News source | Alpaca Benzinga feed via `alpaca-py` (free account, symbol-tagged, ~2021+) | 1 |
| Broker | **TBD at slice 8.** No broker integration in slices 1–7 or 9. | 8 |
| Secrets | `.env` + `python-dotenv`, gitignored | 1 |
| Retry | 3× exponential backoff (1s / 2s / 4s), then crash | 1 |
| Logging | `print()` for humans + DuckDB `audit_log` append-only table | 1 |
| Reports | Markdown + CSV per run at `reports/slice{N}/YYYY-MM-DD.{md,csv}` | 1 |
| GPU | NVIDIA RTX 5090 (32GB VRAM, Blackwell sm_120) — needs PyTorch cu128+ wheels | 1 |
| CUDA runtime | Bundled with PyTorch pip wheel (no system CUDA toolkit required) | 1 |
| Lint / format | `ruff` (hook-enforced via `auto-format.sh`) | 1 |
| Test framework | `pytest` | 1 |

**Explicitly not used:** Rust, PyO3, Tokio, asyncio, SQLite, Postgres, TimescaleDB, Parquet, Ray / Dask / Celery, Kafka / Redis, Docker / Kubernetes, message buses, web dashboards, uv / poetry (fine tools, just not what we picked), vLLM / llama.cpp (until `transformers` bottlenecks).

---

## 3. Data architecture

### 3.1 Storage

Single DuckDB file at `data/hyx.duckdb`. Gitignored. All operational state — raw ingest, computed signals, agent outputs, decisions, executions, outcomes, audit log. One file, trivial backup (`cp`).

DuckDB's columnar format gives fast analytics when the eval harness lands at slice 9 — no additional storage layer required.

### 3.2 Schema evolution

Numbered SQL migration files at `hyx/db/migrations/NNN_description.sql`. A `schema_migrations(id, applied_at)` table records what's been run. The runner (`hyx/db/migrate.py`) reads the migrations directory, diffs against the tracker, applies unapplied files in numeric order.

Each slice owns its DDL additions. New tables land in the owning slice's migration file.

### 3.3 Ingestion pattern

**Incremental + idempotent by default.** Each run pulls everything new since the last successful fetch, writes with `INSERT OR IGNORE` on natural keys. Rerunning the same script the same day is a no-op.

**Natural keys:**
- `ohlcv_daily`: `(ticker, date)`
- `news`: `news_id`
- `news_sentiment`: `(news_id, model)` — lets FinBERT and Qwen zero-shot coexist
- `macro_signals`: `date`
- `ag_conditions`: defined at slice 3

A `fetch_state(source, ticker NULL, last_fetched_at)` table tracks last-seen timestamps so incremental pulls resume correctly. **First-run backfill: 5 years for OHLCV** (yfinance has 10+ years so this is a conservative default); **news backfill is bounded by Alpaca's Benzinga history, which started ~2021** (effectively ~4y). CLI override: `--backfill-since=YYYY-MM-DD`.

### 3.4 Error policy

**Crash loudly** on persistent failure = non-zero exit + stderr message + `audit_log` entry with `level='error'`. Transient failures (network, 5xx) are retried 3× with exponential backoff (1s / 2s / 4s). Rerunning recovers via idempotent ingest.

Slice-specific data-quality checks (gap detection, sanity bounds, corporate actions) added per-slice as failure modes are observed. No pre-committed quality regime.

### 3.5 Reproducibility

**Deferred to slice 9.** The eval harness will force the question. Options when we get there: (A) nightly `cp` snapshots, (B) `as_of` / `retrieved_at` columns on revision-prone tables, (C) something else. Pre-committing now is premature.

Slices 1–4 data is mostly immutable: OHLCV bars don't retroactively change, Alpaca news IDs are stable, VIX/DXY/yields are historical fact. USDA is the one revision-prone source — its revision policy is decided at slice 3.

### 3.6 Data sources

| Source | Data | Cadence | Account required | First slice |
|---|---|---|---|---|
| yfinance | Daily OHLCV bars + adjusted close | Daily | no | 1 |
| Alpaca (Benzinga) | Ag-sector news, symbol-tagged | Daily | free | 1 |
| USDA QuickStats (REST API) | Crop production, yield, planting, WASDE | Monthly + ad-hoc | free | 3 |
| NOAA Drought Monitor | US drought intensity by state | Weekly CSV | no | 3 |
| CME / CBOT | Corn, soybean, wheat futures settlements | Daily CSVs | no | 3 |
| FRED | VIX, 2s10s spread, DXY, FOMC calendar | Daily | free | 4 |

All free. Paid-data budget is $0 until concrete ROI argument emerges.

**Decoupling note:** Alpaca is a *news source* in this architecture, not a broker commitment. The original proposal (A10 in `decisions.md` pre-2026-04-21) used Alpaca for both data and execution under "single-provider consistency." That benefit is thin for daily-bar swing trading — feed divergence between broker and data vendor only matters at microsecond timescales, and our eval harness (slice 9) validates against historical DB data, not live broker fills. Broker choice is deferred to slice 8 and evaluated on execution-quality criteria (fee tiers, fill quality, short locate, tax reporting) rather than being locked in now.

### 3.7 On-disk layout

```
hyxrestration/
├── data/                        # gitignored
│   └── hyx.duckdb
├── reports/                     # gitignored
│   └── slice{N}/YYYY-MM-DD.{md,csv}
├── hyx/                         # source
│   ├── slice{N}.py
│   ├── config.py
│   ├── db.py
│   └── db/migrations/
│       └── NNN_*.sql
├── docs/
│   ├── architecture.md          # this file
│   ├── decisions.md
│   └── plans/
├── .env.example                 # committed template
├── .env                         # gitignored
├── requirements.txt
└── README.md
```

HuggingFace models (FinBERT, Qwen) use the default user-level cache at `~/.cache/huggingface/`.

---

## 4. Quant Meta design

### 4.1 Inputs

For each ticker on each date:
- **Sentiment:** directional probability + confidence (one row per scorer: FinBERT, Qwen zero-shot)
- **Domain:** ag-conditions scalar + drought-exposure scalar
- **Technical features:** `return_1d/5d/20d`, `realized_vol_5d/20d`, `atr_14d`, `rsi_14d`, `trend_slope_20d`, `vol_ratio_today_vs_20d`, `distance_from_ma_50d`
- **Macro:** regime tag + `ag_tilt` scalar

Plus portfolio state: current positions, cash, daily P&L, monthly P&L.

### 4.2 Pipeline

Deterministic, ~200 lines of code:

1. **Normalize** each agent signal to `[-1, +1]`, scaled by agent confidence
2. **Aggregate** into a composite score — weighted sum. V1 weights (hand-picked): `0.4 sentiment + 0.25 technical + 0.2 domain + 0.15 macro`. Replaced by 5-parameter logistic regression at slice 9+ once outcome data exists.
3. **Filter** by threshold: candidate if `|composite| > 0.30`
4. **Size** via quarter-Kelly (T01): `size = min(0.20, (|composite| - 0.30) × confidence / vol_20d × 0.25)`
5. **Risk constraints** — max 20%/ticker (T02), max 5 concurrent (T03), daily -5% halt (T04), monthly -10% halt (T05). *Numerical defaults under co-design; need user sign-off.*
6. **Stop / target** — stop at `entry - 2×ATR`, target at `entry + 2×(entry - stop)` for 2:1 reward-to-risk (T06)
7. **Emit** to `candidate_orders` table

### 4.3 Output schema

```
candidate_orders(
  date, ticker, direction ∈ {long, short, hold},
  size_pct, size_dollars,
  entry_limit, stop_price, target_price,
  composite_score, confidence,
  contributors JSON,     -- {sentiment: +0.37, technical: +0.21, ...}
  created_at
)
```

One row per ticker per cycle. `direction='hold'` rows include a `reason` (below threshold, concentrated, etc.) — visible in reports, skipped by execution.

---

## 5. LLM Meta design

### 5.1 Role

Reads the candidate orders table + recent news + upcoming events (earnings dates, FOMC, USDA release calendar) + portfolio state. For each candidate, outputs one of:

- **`pass`** — no concerning qualitative context; order submitted
- **`veto`** — explicit reason (e.g., "SEC investigation news just landed," "earnings tomorrow," "ticker gapped pre-market"); order skipped
- **`adjust`** — size override (e.g., "earnings in 2 days, cut size to 50%"); order submitted modified

Plus a free-text **reasoning trace** per candidate, logged to DuckDB. The trace is load-bearing because it becomes DPO training data if we promote Meta LoRA (slice 10b).

### 5.2 Implementation

Qwen 2.5 7B Instruct, base model + structured prompt. Loaded from HuggingFace cache. GPU inference on the 5090, ~10–30 seconds per cycle for the full candidate set.

No fine-tuning in slices 5–10. Meta LoRA via DPO is slice 10b, conditional stretch.

### 5.3 Output schema

Designed at slice 7. Lean: `decisions(candidate_order_id, llm_action, adjusted_size_pct NULL, reasoning TEXT, decided_at)`.

---

## 6. Execution

Slice 8. Paper brokerage TBD — candidates: Alpaca, IBKR, Tradier, tastytrade. Evaluation criteria: fee model, fill quality on small-cap ag equities, short locate availability (post-T1), bracket-order support, tax-reporting export. Bracket orders with server-side ATR stops. Full audit trail in `executions` + `fills` tables. GTC for entries (24h), market-close for urgent exits (T07).

Slippage model for backtest: midpoint + 2 bps + simulated 100ms latency (T08).

Concrete schema and failure modes designed at slice 8.

---

## 7. Evaluation

Slice 9. Walk-forward backtesting — 24 months train / 1 month test / monthly roll. Baselines logged every run: random, SPY buy-hold, SMA crossover, each agent solo.

Headline metric: 12-month rolling OOS Sharpe. Secondaries: max drawdown, Sortino, Brier (prediction calibration), turnover, active share vs SPY.

**Tier gates:**
- **T0 (M3):** system runs end-to-end, produces paper trades, no disastrous failure modes
- **T1 (M6):** OOS Sharpe beats random and SMA-crossover baselines
- **T2 (M12):** OOS Sharpe beats SPY across ≥3 regimes

Fail → kill. DuckDB is already primary, so no storage layer addition at slice 9.

---

## 8. Training

### 8.1 What trains, what doesn't

| Model | Trained? | When | Mechanism |
|---|---|---|---|
| Qwen 2.5 7B Instruct base | Never | — | Used as-is from HuggingFace |
| FinBERT | Never | — | Pre-trained, inference-only baseline |
| Quant Meta weights | Yes, trivially | Slice 9+ | 5-param logistic regression on (signals → forward-return) data |
| Sentiment LoRA | **Conditional stretch** | Slice 5b | QLoRA on ag-news corpus with forward-return labels. Triggered only if Qwen zero-shot beats FinBERT and we want further gains. |
| Meta LoRA | **Conditional stretch** | Slice 10b | DPO via `trl` on accumulated trade-outcome preference pairs. Triggered only if slice 9 eval shows LLM Meta making systematic errors a targeted fine-tune would fix. |

Neither stretch goal blocks the core path (slices 1–9).

### 8.2 Stretch — Sentiment LoRA (slice 5b)

Training data: ag-news corpus (Alpaca Benzinga feed + supplementary) with forward-return labels. Method: QLoRA via `peft` + `bitsandbytes` on Qwen 2.5 7B. Validation: A/B OOS vs FinBERT on held-out slice.

### 8.3 Stretch — Meta LoRA (slice 10b)

Training data: preference pairs synthesized from accumulated paper-trade outcomes. Method: DPO via `trl`. Validation gate: new adapter only promoted if OOS Sharpe on held-out slice ≥ prior version with statistical confidence. Shadow-deploy for 5 days before full promotion. Auto-rollback on post-promotion 7-day rolling regression.

---

## 9. Build sequence

Vertical-slice build (A06). Each slice ships end-to-end. Core path is 9 slices (1–9) plus conditional stretch goals.

| Slice | Deliverable | Status |
|---|---|---|
| 1 | DE baseline — OHLCV via yfinance + Alpaca news + FinBERT → DuckDB + MD/CSV report | ✅ spec locked |
| 2 | Universe expansion to 14 ag tickers, sequential loop | ✅ spec locked |
| 3 | Domain context — USDA / NOAA / CBOT → `ag_conditions` | ✅ spec locked |
| 4 | Macro (rules) — VIX / 2s10s / DXY / FOMC → `macro_signals` | ✅ spec locked |
| 5 | Qwen zero-shot sentiment alongside FinBERT (same table, `model` column) | ✅ lean locked |
| 5b | Sentiment LoRA (conditional stretch) | Deferred |
| 6 | Risk + Quant Meta with technical features inlined. Candidate orders. Paper-log only. | 🔄 (features/threshold/Kelly locked; aggregation weights + risk numbers open) |
| 7 | LLM Meta — Qwen + prompt, pass/veto/adjust + reasoning trace | 🔄 |
| 8 | Paper-trade execution via chosen broker (bracket orders) | 🔄 |
| 9 | Eval harness + walk-forward validation | 🔄 |
| 10b | DPO training loop (conditional stretch, was slice 10) | Deferred |
| 11 | (Optional) Rust GPU scheduler | Deferred, possibly never |
| 12+ | As-needed: vLLM, X-LoRA, variant-C pivot, live trading (on authorization) | Deferred |

**Constraint on slices 1–4:** no model training, no trading decisions, no Meta, no Risk checks. FinBERT in slice 1 is pre-trained inference. Pure ingest + compute substrate.

---

## 10. Explicitly not being built

**Infrastructure:**
- Rust in the critical path (slice 11 optional)
- PyO3 / cross-language bridges
- vLLM / llama.cpp (until `transformers` bottlenecks)
- X-LoRA / LoRA-Switch (until hard-swap latency matters)
- Web dashboards (SQL + notebooks suffice)
- systemd / containers / Docker (plain Python is enough)
- Message buses / Redis / Kafka
- Second GPU (profile first)
- Parquet partitioned storage
- DuckDB-as-analytical-layer-over-SQLite (moot — DuckDB is primary)

**Trading scope:**
- Real money (paper-only until authorization; minimum 3 mo paper + T2 gate)
- Options / futures (equities only until T1)
- Short-selling (long-only until Risk + slippage modeling mature, post-T1)
- Sub-minute trading (swing only)
- Universes beyond ag (specialist focus locks until T1)

**Scope-creep traps:**
- Comprehensive test coverage of modules not yet designed — tests ship with their slice
- "Final" data schemas upfront — schemas evolve slice-by-slice
- Notification / alerting systems — per-failure-class as we hit them
- Historical backfills for unused features — on-demand per slice
- Premature abstraction — three similar lines beats a "generic" helper anticipating unvalidated needs

---

## 11. Open decisions

Items still under co-design — tracked by ID in [`decisions.md`](decisions.md):

- **A03, A04, A10** — likely fine, worth explicit confirmation
- **A05** — agent roster details post-Meta-split
- **A08, A09** — only matter at stretch slices 5b / 10b
- **ST01–ST08** — system-level strategy (prediction shape, priors)
- **T01–T08** — trading-mechanics numerical defaults; **T02–T05 need explicit user sign-off** (risk preference)
- **Slice 6** — aggregation weights (#2) and risk numbers (#5) block slice 6 build
- **Slice 7** — prompt design, I/O schemas
- **USDA revision policy** — decided at slice 3

---

## 12. User-authorization boundaries

| Topic | Default | To unblock |
|---|---|---|
| Real-money activation | Never | Explicit user OK. Minimum: 3 mo paper + T2 gate. No fallback. |
| Hard budget for paid services | $0 | User revises |
| Ethical trading constraints | None assumed | User flags any |

---

## References

- L01 full rationale: `agent-memory/orchestrator/project_scoping.md`
- Original architecture proposal (partially stale): `llm_trading_orchestration.md`
- Early implementation draft (largely superseded): `docs/plans/architecture/design.md`
- Decision index: `docs/decisions.md`
- Sponsor-mode memory: `/home/devs/.claude/projects/-home-devs-workspace-hyxrestration/memory/sponsor_mode.md`
