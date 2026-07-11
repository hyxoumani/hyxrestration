# LLM Trading Agent Orchestration — Project Context

> **Status: architecture revised April 2026 during scoping.**
> Sections 1, 2.1, 2.4, 5.2, 6, 7, 8 reflect post-scoping decisions (Python-first, SQLite-primary, agriculture specialist, swing 1–5d, vertical-slice build). Sections 2.2, 2.3, 2.5, 3, 4, 9 preserve original design reasoning and reference material.
>
> Operating mode: **sponsor model** — Claude makes architecture/build decisions autonomously, documents reasoning. User retains authorization on resource-touching decisions (real money, hard budget, ethical limits).

## 1. Project Overview

A local LLM-driven system for autonomous **swing-trading of US agricultural equities**. Specialized models (LoRA adapters on a shared 7B base, plus a time-series model and rule-based modules) coordinate to ingest market data + news + domain signals, generate directional signals, and execute trades — with a nightly DPO training loop that improves performance from trade outcomes over time.

**Niche:** Agriculture specialist, tentatively **variant B (crop-side)** — fertilizer + seeds + processors + equipment + ag-commodity ETFs. Final sub-niche lock after Phase 0 domain learning.

**Trading style:** Swing 1–5 day holds. Long-only at start (add shorts post-T1). Paper-only until explicit authorization.

**Goal tier:** **T2** — OOS walk-forward Sharpe beats SPY across ≥3 regimes. Tiered kill gates at M3/M6/M12 (T0/T1/T2). Auto-rollback on any DPO promotion that regresses validation OOS Sharpe.

**Hardware:** RTX 5090 (32GB VRAM), 32GB system RAM, home lab (Raspberry Pi, Jetson Nano, RTX 5090).

**Core constraints:**
- Single GPU multiplexes inference and fine-tuning (shapes scheduling once training is live, slice 10+)
- Solo operator → one-language stack preferred; add cross-language complexity only when a specific subsystem demands it
- Vertical-slice build: minimum tool per slice, add complexity only when actual limits are hit

---

## 2. Architecture

### 2.1 Agent Specialization

Specialist roles, each with the simplest implementation that fits its job. The "all 5 as LoRA adapters" framing from the original doc was wrong for this problem: OHLCV pattern recognition is better served by a purpose-built time-series model than a 7B LLM, and risk enforcement is a deterministic rule, not a learned policy.

| Agent | Role | Input | Output | Implementation |
|-------|------|-------|--------|----------------|
| **News/Sentiment** | Ag news, earnings calls, USDA narrative → return-predictive sentiment | Ag news feeds, earnings call transcripts, USDA report text | Sentiment score + direction + confidence per ticker | FinBERT for slice 1 (baseline); LoRA adapter on Qwen 2.5 7B Instruct from slice 5 onward |
| **Domain** | Weather, drought, commodity prices, USDA supply/demand → ag conditions signal | NOAA + US Drought Monitor, CBOT commodity prices, USDA WASDE/NASS text, ag policy | Ag conditions per ticker + regime tag | Rule-based + lookups initially (slice 3); may collapse into Sentiment adapter or become its own LoRA later |
| **Technical** | OHLCV pattern recognition on ag equities + commodity futures | Rolling OHLCV windows + derived features (returns, realized vol, RSI, etc.) | Directional probability + confidence over 1–5 day horizon | **PatchTST / TCN** (PyTorch). Standard supervised training on forward-return labels — not DPO |
| **Macro** | Fed language, yield curve, USD, inflation → regime tilt relevant to ag | FOMC text, CPI/NFP/GDP releases, DXY, 2s10s, VIX | Regime classification + sector tilt | Rule-based initially (slice 6); upgrades to LoRA adapter only if rules insufficient |
| **Risk** | Position sizing, concentration limits, drawdown circuit breakers | Signals from other agents + current portfolio state | Position recommendation respecting all limits | **Deterministic rules in Python** — not an LLM. Three-line policies don't need fine-tuning |
| **Meta** | Final trade/no-trade decision + sizing | All agent signals + portfolio state | Execute / hold / exit + size | Confidence-weighted aggregator initially (slice 7); evolves to LoRA adapter once DPO preference-pair data from live outcomes accumulates (slice 10+) |

**Net LoRA adapter count at steady state: 2** (Sentiment, eventually Meta). Macro and Domain *may* become adapters if rules prove insufficient — a scope decision made later based on evidence.

### 2.2 LoRA Adapter Mechanics

A LoRA adapter decomposes the weight update as:

```
W_new = W + A × B
```

Where W is the frozen base weight matrix (4096×4096), A is 4096×r, B is r×4096, and r (rank) is small (8–32). This means ~131K trainable parameters per layer at rank 16 vs. 16.7M for full fine-tuning. A full adapter is ~10–50MB.

**QLoRA** quantizes the base model to 4-bit (~3.5GB VRAM for 7B) and trains LoRA matrices in full precision on top.

**Key empirical finding:** Fine-tuning weight deltas are already low-rank in practice. The effective rank of (W_finetuned - W_original) is typically 4–64, so the rank constraint costs very little precision.

### 2.3 Adapter Serving — Two Approaches

#### Approach A: Hard Adapter Swapping (Start Here)

1. Load quantized base model (~3.5GB)
2. Load agent's LoRA adapter (~10–50MB), merge: W_eff = W + A × B
3. Run inference
4. Unmerge, load next agent's adapter
5. Repeat

Swap cost: ~50–100ms per switch. Serial — one agent at a time.

#### Approach B: X-LoRA / MoE-LoRA (Upgrade Path)

Load all 5 adapters simultaneously (~250MB total). A learned routing network outputs per-token scaling weights across adapters:

```
W_eff = W + α₁(A₁×B₁) + α₂(A₂×B₂) + ... + α₅(A₅×B₅)
```

Routing is per-layer and per-token — the model leans on Sentiment adapter for earnings tokens, Macro adapter for yield curve data, within the same forward pass. Only the routing network is trained; base model and all adapters stay frozen.

**Tradeoff:** Eliminates adapter swap latency and simplifies the Rust scheduler, but reduces interpretability — you lose discrete per-agent audit trails.

**Relevant techniques:**

- **X-LoRA** — Dense gating of LoRA experts via learned scaling. Drop-in for HuggingFace. Most implementable.
- **LoRA-Mixer** — Routes experts into attention projection matrices for finer-grained token-level specialization.
- **LoRA-Switch** — Fused CUDA kernel for token-wise adapter routing. Solves the 2.5x latency overhead from fragmented kernel calls in naive MoE-LoRA.
- **S-LoRA / Unified Paging** — Virtual-memory-style unified pool for adapter weights + KV cache. Eliminates memory fragmentation when swapping adapters. The right abstraction for the PyO3 bridge even if not using S-LoRA directly.

### 2.4 Communication Layer

**Initial (slices 1–10): Python-native, in-process.**

- `asyncio` for concurrent IO (data ingestion, broker API calls, HTTP fetches)
- In-process dataclasses / function calls for inter-agent signal passing — no message bus needed when all agents live in one process
- Each agent emits a structured signal (direction, magnitude, confidence, reasoning summary) persisted to SQLite for audit
- Meta agent / aggregator reads signals from SQLite (or in-memory state) and writes the final decision to SQLite

**Why not Rust initially:** Swing cadence (decisions per session, not per second) removes any latency argument for Rust. A Rust/Python boundary via PyO3 would cost weeks of build time for zero functional benefit at this stage. Adding Rust makes sense only when a specific subsystem demonstrates an actual bottleneck — most likely the GPU scheduler once we need preemptive training-vs-inference routing (slice 11+, optional).

**Lock-free channels (crossbeam), Tokio, MPMC routing — all deferred.** Potentially interesting as a learning exercise for a specific subsystem later; not load-bearing for the critical path. See §6 deferred upgrades and §7 slice 11 (optional).

**LoRA adapters vs. prompt-based subagents** (unchanged rationale):

- LoRA: Zero prompt tokens for specialization (full context window available), consistent behavior (specialization in weights, not context), faster inference
- Prompts: Use lightweight prompts only for runtime parameters (which tickers, risk tolerance, output format)
- Best practice: LoRA for domain specialization (what the agent knows), prompts for task parameters (what to do right now)

### 2.5 Decision Aggregation

Two approaches from the research:

1. **Majority voting** — Accounts for most gains attributed to multi-agent debate (Choi et al., NeurIPS 2025). Don't over-engineer consensus.
2. **Learned confidence-weighted aggregation (A-HMAD)** — Heterogeneous specialized agents with a consensus optimizer that learns reliability weights per agent per market regime. 4–6% improvement over naive voting. Better fit for trading where agent reliability is context-dependent (Sentiment agent may be reliable on earnings days but useless during macro shocks).

**Recommendation:** Start with confidence-weighted voting. A small trained module (even logistic regression) learns reliability weights. Hiding confidence scores between agents prevents over-confidence cascades.

---

## 3. GPU Scheduling

### 3.1 Single GPU Design

A Rust-based priority scheduler manages the 5090 as a shared resource:

- **Real-time priority:** Inference during market hours (9:30 AM – 4:00 PM ET, plus pre/post-market)
- **Batch priority:** QLoRA fine-tuning overnight, weekends, and in gaps between inference batches
- Preemption: training jobs checkpoint frequently and yield to inference requests

### 3.2 Dual GPU Design (Future)

If a second 5090 is added:

- **GPU 1 (Inference):** Five Q4-quantized 7B models at ~4GB each = ~20GB, with headroom for KV cache and batching
- **GPU 2 (Training):** Dedicated DPO training with no inference contention
- Scheduler simplifies from priority-based time-slicing to workload-type routing

### 3.3 VRAM Budget (Single 5090, 32GB)

| Component | VRAM |
|-----------|------|
| Base model (Q4) | ~3.5GB |
| 5 LoRA adapters | ~250MB |
| KV cache (inference) | ~2–8GB depending on batch/seq length |
| QLoRA training overhead | ~8–12GB |
| **Total inference** | **~6–12GB** |
| **Total training** | **~12–16GB** |

### 3.4 System RAM Constraint (32GB)

- Inference: ~8–12GB system RAM (model server processes + Rust orchestrator). Comfortable.
- DPO fine-tuning: Tighter. Gradient checkpointing and optimizer states push RAM usage. This is the tightest bottleneck — tighter than VRAM.

---

## 4. Training Pipeline

### 4.1 Training Method: DPO (Direct Preference Optimization)

Trade outcomes naturally generate preference pairs: given a market state, decision A was profitable, decision B was not. DPO eliminates the need for a separate reward model by using these pairs directly.

- Collect trade outcome pairs during market hours
- Train overnight (no online reward model needed)
- Only LoRA adapter weights are updated — base model stays stable

### 4.2 Training Loop Options

| Option | Description | Pros | Cons |
|--------|-------------|------|------|
| **A: Nightly Batch** | Collect all outcomes during day, train overnight | Simple scheduling, no GPU contention, clean separation | Adapters always ≥1 day stale |
| **B: Continuous + Preemption** | Background training, preempted by inference | Faster adaptation, intraday feedback | Complex scheduler, overfitting risk |
| **C: Weekend Batch + Nightly Incremental** | Full retrain weekends, lightweight DPO nightly | Stable base + daily adaptation | Incremental drift if not regularized |

**Recommendation:** Start with Option A. Get the nightly pipeline working end-to-end. Migrate to Option C once stable.

### 4.3 TT-LoRA MoE Pattern (Advanced)

Expert adapters remain frozen after initial training (no inter-task interference or catastrophic forgetting). A sparse MoE router, trained separately, dynamically selects the specialized adapter per input at inference time. New adapter versions can be trained overnight without destabilizing others.

### 4.4 Validation Gate

Adapters are only promoted to the live inference path if held-out performance improves. Walk-forward validation against held-out market data prevents deploying degraded adapters.

---

## 5. Data Architecture

### 5.1 Data Requirements

1. **Market data:** OHLCV bars (1m, 5m, 1h, daily), tick data if sub-minute
2. **Alternative data:** News articles, SEC filings (EDGAR), social sentiment, Fed transcripts
3. **Computed features:** Technical indicators, correlation matrices, volatility surfaces
4. **Agent audit trail:** Every agent's input, output, confidence, and final decision + outcome
5. **Training artifacts:** DPO pairs, adapter checkpoints, validation metrics over time

### 5.2 Storage

| Store | Use | Slice introduced |
|-------|-----|------------------|
| **SQLite (WAL mode)** | **Primary.** Embedded, concurrent writers natively via WAL, first-class Python (`sqlite3` stdlib) and Rust (`rusqlite`) bindings. All operational state: market data, news, signals, decisions, executions, outcomes, audit trail. | Slice 1 |
| **Filesystem** | LoRA adapter checkpoints, large blobs (news/filings text), training artifacts. Organized by `adapters/{agent}/v{YYYYMMDD}_{git_sha}.safetensors`. | Slice 5 |
| **DuckDB (read-only analytical layer)** | Added when analytical query workload (backtests, walk-forward eval) outgrows SQLite. DuckDB's `sqlite_scanner` extension queries SQLite tables directly — **no migration, no dual-write**. | Slice 9 |
| **Parquet partitioned by ticker/year** | Added only if SQLite + DuckDB combo hits actual size limits (~10GB+ per table). At tens-of-GB total over years, likely never needed. | Deferred indefinitely |

**Why SQLite over DuckDB as primary:** The original doc optimized for analytical workload, but the first many slices are OLTP-ish — small, frequent writes from multiple processes (data ingest, inference, Meta, audit). SQLite in WAL mode handles concurrent writers natively; DuckDB is single-writer and would require funneling all writes through one process or accepting file-lock contention. Analytical performance only becomes relevant at slice 9 (eval harness), by which point DuckDB reads SQLite files in place — zero migration cost.

**Skip TimescaleDB / Postgres / ClickHouse** — all add operational complexity (running a server process) that isn't justified at single-machine scale.

### 5.3 Data Sources

| Source | Data |
|--------|------|
| Alpaca / Polygon / IBKR | Real-time + historical market data |
| EDGAR | SEC filings |
| Financial news APIs | News sentiment |
| FOMC transcripts | Fed language, macro signals |

---

## 6. Technology Stack

### 6.1 Initial stack (slices 1–10)

| Layer | Technology | Purpose | Slice introduced |
|-------|-----------|---------|------------------|
| Core | **Python 3.11+** | Orchestration, data pipeline, scheduler, trade execution — all in one language | Slice 1 |
| Async runtime | **`asyncio`** | Concurrent IO for data ingestion and API calls | Slice 2 |
| Inference | **HuggingFace `transformers`** | Model loading + inference, including LoRA hard-swap | Slice 5 |
| Fine-tuning | **QLoRA via `peft` + `bitsandbytes`** | 4-bit quantized LoRA fine-tuning on the 5090 | Slice 10 |
| Base model | **Qwen 2.5 7B Instruct** | Shared base for all LoRA adapters (Apache 2.0, best-in-class 7B benchmarks, digit-level tokenizer) | Slice 5 |
| Time-series model | **PatchTST (PyTorch)** | Technical agent — replaces LLM for OHLCV pattern recognition | Slice 4 |
| Training method | **DPO via `trl`** | Preference optimization on trade outcome pairs | Slice 10 |
| Primary data store | **SQLite (WAL mode)** | All operational state | Slice 1 |
| Analytical query layer | **DuckDB (read-only over SQLite)** | Backtest / walk-forward eval aggregations | Slice 9 |
| Brokerage | **Alpaca** (paper → live only with explicit user authorization) | Market data + trade execution | Slice 2 (data), Slice 8 (execution) |
| Hardware | **RTX 5090 (32GB VRAM)** | Single GPU for inference + training | Slice 5 |
| Secrets | **`.env` + `python-dotenv`** (gitignored) | Dev-time secrets | Slice 1 |
| Observability | **Structured log lines to SQLite `audit_log`** | Append-only decision trace; queried via SQL | Slice 1 |

### 6.2 Deferred upgrades — build only when a concrete bottleneck is hit

| Upgrade | Replaces | Trigger |
|---------|----------|---------|
| **vLLM** | `transformers` for inference | Multi-adapter hot-swap latency becomes critical OR backtest inference throughput bottlenecks eval cycles |
| **Rust for specific subsystems** (scheduler first candidate) | Python for that subsystem only | Concrete latency/safety/concurrency need emerges. Most likely slice 11+ as a learning exercise; may never be strictly required for swing |
| **X-LoRA / LoRA-Switch** | Hard adapter swap | Adapter swap cost + mid-pipeline chaining become the actual bottleneck (current swap is ~50–100ms — fine for our cadence) |
| **DuckDB as primary** | SQLite for writes | Analytical workload dominates OLTP (unlikely at our scale) |
| **Parquet + object storage** | SQLite for history | Per-table size > ~10GB (unlikely for years) |
| **systemd services + containers** | Plain Python processes | More than ~3 long-running processes to manage |
| **`age` / `sops`** | `.env` | Deployment formalizes beyond dev machine |

### 6.3 Explicitly not using

- **PyO3 / Rust-Python bridge** — no bridge needed when everything is Python
- **Tokio / crossbeam** — `asyncio` suffices for swing cadence
- **TimescaleDB / Postgres / ClickHouse** — SQLite suffices at single-machine scale
- **Ray / Dask / Celery** — overkill for a single operator and single machine
- **Kafka / Redis / external message buses** — SQLite audit log + in-process function calls suffice
- **Kubernetes / Helm / orchestrators** — single machine
- **Web dashboard framework** (Grafana, Streamlit, custom) — SQL + Jupyter notebook suffice until they don't

---

## 7. Build Sequence — Vertical Slices

Each slice is an **end-to-end thread that ships independently.** No slice depends on future-slice components. This replaces the original 7-monolithic-milestone order, which required building most of the system before any of it produced value.

### Slice 1 — Proof of wiring (Python only, one ticker)

- Pull OHLCV for DE from Alpaca free tier → SQLite
- Pull news for DE (Alpaca News API or Yahoo) → SQLite
- Score each headline with FinBERT → SQLite
- SQL query prints today's news + sentiment + price for DE
- **Deliverable:** `python -m hyx.slice1` runs end-to-end and prints the joined view
- **Explicitly NOT in slice 1:** Rust, LoRA, DPO, broker execution, scheduler, multi-ticker, Qwen, asyncio

### Slice 2 — Multi-ticker ag universe

- Expand to variant B set (~12–15 tickers: NTR, MOS, CF, CTVA, FMC, ADM, BG, DE, AGCO, CNHI + commodity ETFs like DBA, CORN, WEAT, SOYB)
- Parallel ingestion via `asyncio`
- Universe membership tracked in a SQLite table
- **Deliverable:** daily report across the universe

### Slice 3 — Basic domain signals (non-LLM)

- USDA WASDE ingest (monthly PDF → parsed fields in SQLite)
- NOAA Drought Monitor ingest (weekly)
- CBOT commodity daily settlements (free source)
- Cross-reference: e.g., drought area weighted by company revenue geography
- **Deliverable:** ag-conditions-per-ticker view in SQLite

### Slice 4 — Technical agent (PatchTST)

- Train PatchTST on 10 years OHLCV for the ag universe (supervised, forward-return labels)
- Emit directional probability + confidence over 1/3/5-day horizons
- Log as signals to SQLite
- **Deliverable:** technical-signal time-series per ticker

### Slice 5 — Qwen + LoRA Sentiment (replaces FinBERT)

- Fine-tune Qwen 2.5 7B Instruct with QLoRA on an ag-news-labeled corpus (news text + forward 1-day return labels, seeded with FinBERT sentiment then overwritten with return-based labels)
- Hard-swap LoRA adapter at inference time via `transformers`
- A/B compare OOS against FinBERT baseline on the same eval slice
- **Deliverable:** Sentiment signal backed by our own adapter; A/B results logged

### Slice 6 — Macro agent (rule-based)

- Rules: VIX band, 2s10s slope, DXY trend, FOMC calendar carve-outs
- Regime classification + ag-sector tilt recommendation
- Pure Python; upgrade to LoRA only if rules prove insufficient
- **Deliverable:** regime signal in SQLite; backtest regime-carve-out comparison

### Slice 7 — Risk + Meta modules

- **Risk** (deterministic Python): max 20% per ticker, max 5 concurrent positions, quarter-Kelly on Meta confidence, daily -5% halt, monthly -10% halt
- **Meta** (confidence-weighted aggregator): combines Sentiment + Technical + Domain + Macro → final direction + size
- Still paper-logging-only (no execution yet)
- **Deliverable:** end-to-end signal → decision pipeline

### Slice 8 — Paper-trade execution (Alpaca)

- Alpaca paper API integration
- Bracket orders: limit entry ± 10 bps, ATR-based stop (server-side at Alpaca), 2:1 take-profit
- Full audit trail in SQLite `executions` + `fills`
- GTC for entries, market-close for urgent exits
- **Deliverable:** system submits, tracks, and closes paper orders autonomously

### Slice 9 — Eval harness + walk-forward validation

- Backtest engine (replay from SQLite history)
- Walk-forward: 24-mo train / 1-mo test / monthly roll
- Baselines logged every run: random, SPY buy-hold, SMA crossover, each agent solo
- Headline metric: 12-mo rolling OOS Sharpe; secondaries: max DD, Sortino, Brier, turnover, active share vs SPY
- **DuckDB added here** as analytical read layer over SQLite (via `sqlite_scanner`)
- **Deliverable:** tier-gate report showing T0/T1/T2 status

### Slice 10 — DPO training loop

- Preference-pair generation from trade outcomes (same market state, different decisions, different forward returns)
- Nightly DPO on Sentiment LoRA (later Meta)
- Validation gate: new adapter only promoted if OOS Sharpe on held-out slice ≥ prior version with statistical confidence
- Shadow-deploy new adapters for 5 days (compute signals but don't trade) before full promotion
- **Auto-rollback** if post-promotion 7-day rolling performance drops > threshold vs pre-promotion
- **Deliverable:** system improves (or correctly does not regress) over 2+ weeks of monitoring

### Slice 11 (optional) — Rust GPU scheduler

- Rewrite the inference/training scheduler in Rust with Tokio
- Python inference/training processes stay as-is; Rust binary manages GPU time slices
- Motivation: learning value + preemptive training-vs-inference routing
- **Skip unconditionally unless:** (a) the Python scheduler proves insufficient, or (b) user explicitly wants the learning exercise
- **Deliverable:** Rust binary managing GPU time; Python services talk to it via simple IPC

### Slice 12+ — As-needed

- vLLM for inference (if hard-swap throughput bottlenecks backtests)
- X-LoRA / LoRA-Switch (if adapter chain latency matters)
- Variant C fertilizer-specialist pivot (if variant B plateaus)
- Expansion beyond ag (only after T1 reached and T2 looks plausible)
- Live trading (**only on explicit user authorization** — never by default)

---

## 8. What Not to Build Yet

### Infrastructure deferred

- **Web dashboard** (Grafana, Streamlit, custom) — SQL + Jupyter notebook suffice for monitoring until they demonstrably don't
- **Rust anywhere in the critical path** — Python suffices through slice 10; Rust scheduler is an optional slice 11 learning exercise, not load-bearing
- **PyO3 / cross-language bridges** — No bridge needed while everything is Python
- **X-LoRA / LoRA-Switch** — Hard-swap is sufficient; upgrade only if swap cost becomes the bottleneck
- **vLLM / llama.cpp** — `transformers` is sufficient through slice 8. Upgrade when multi-adapter serving or backtest inference throughput bottlenecks
- **DuckDB as primary store** — Add only as analytical read layer at slice 9
- **Parquet partitioned storage** — Deferred indefinitely; revisit only if SQLite table size > ~10GB
- **systemd services / Docker / containers** — Plain Python processes work until we have more than ~3 long-running services
- **Message bus / Redis / Kafka** — In-process function calls + SQLite audit log suffice
- **Second GPU** — Profile first; buy only if single-5090 VRAM is the demonstrated bottleneck

### Trading scope deferred

- **Real money** — Paper-only until explicit user authorization. Minimum bar: 3+ months of consistent paper performance AND T2 tier gate passed
- **Options / futures** — Equities only until the full pipeline is stable and T1 gate passed
- **Short-selling** — Long-only until Risk module + slippage modeling are mature (post-T1)
- **Sub-minute trading** — Swing only; no intraday
- **Universes beyond ag** — Specialist focus locks during Phase 0; broadening considered only after T1

### Scope-creep traps to avoid early

- **Comprehensive unit test coverage of modules we haven't designed yet** — Tests for slice N ship with slice N
- **"Final" data schemas** — SQLite schemas evolve with slices; document changes in the ADR log (§6), don't over-design up front
- **Notification / alerting systems** — Add per-failure-class as we hit them, not preemptively
- **Backfilling years of historical data** for features not yet used — Ingest on-demand when a slice needs it
- **Premature abstraction** — Three similar lines is better than a "generic" helper that anticipates needs we haven't validated

---

## 9. Reading List

### LoRA Foundations
1. **"LoRA: Low-Rank Adaptation of Large Language Models"** — Hu et al., 2021
2. **"QLoRA: Efficient Finetuning of Quantized LLMs"** — Dettmers et al., 2023

### Multi-Adapter Systems
3. **"S-LoRA: Serving Thousands of Concurrent LoRA Adapters"** — Sheng et al., 2023. Unified Paging concept for adapter + KV cache memory management.
4. **"X-LoRA: Mixture of LoRA Experts"** — Buehler & Buehler, 2024. Dense gating of LoRA experts with learned scaling. Drop-in HuggingFace implementation.
5. **"LoRA-Switch"** — Kong et al., ICLR 2025. Fused CUDA kernel for token-wise adapter routing. Fixes the latency overhead of naive MoE-LoRA.

### Training
6. **"DPO: Direct Preference Optimization"** — Rafailov et al., 2023. Core training method — trade outcomes as preference pairs.
7. **"Training Language Models to Follow Instructions with Human Feedback"** (InstructGPT) — Ouyang et al., 2022. Context on the RLHF pipeline DPO simplifies.

### Agent Decision-Making
8. **"Debate or Vote"** — Choi et al., NeurIPS 2025. Majority voting accounts for most multi-agent gains. Don't over-engineer consensus.
9. **"Adaptive Heterogeneous Multi-Agent Debate (A-HMAD)"** — 2025. Learned reliability-weighted aggregation across heterogeneous specialists.

---
