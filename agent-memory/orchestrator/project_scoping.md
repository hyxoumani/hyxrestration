# Project Scoping — LLM Trading Orchestration

## Status: SCOPING COMPLETE (April 2026)

Strategic and architectural decisions locked. Build sequence defined as 11 vertical slices in `llm_trading_orchestration.md` §7. Phase 0 domain learning queued behind slice 3 (domain signal ingest).

## Operating mode

**Sponsor model.** User delegates strategy / architecture / build decisions to Claude. Claude makes decisions, documents reasoning, and executes. User retains authorization on resource-touching decisions only (real money, hard budget thresholds, ethical limits).

Cross-session memory: `/home/devs/.claude/projects/-home-devs-workspace-hyxrestration/memory/sponsor_mode.md`

Do NOT relapse to advisor mode. The user pushed back twice on multi-choice "pick A/B/C" framing. Continued deferral is a regression.

## Foundational thesis (locked)

**L01 — Load-bearing advantage claim:** Cross-modal information aggregation on a small under-covered universe, accumulating *encoded* domain knowledge that compounds over time, at swing time horizons that fall in institutional capacity / mandate gaps.

Three components, each load-bearing — lose any one and the thesis weakens significantly:

1. **Cross-modal integration on small universe** — small universe (= attention budget per name) enables daily integration of news + earnings + USDA + NOAA + commodity + macro per ticker. Quant funds are siloed by mandate; this multi-source integration on small N is a genuinely LLM-specific capability that *is* new in 2026.
2. **Encoded domain knowledge that compounds** — accumulated ag expertise becomes a moat IF systematically encoded into agent prompts, training labels, rules, and feature engineering. Year-3 > year-1. Raw knowledge in markdown ≠ alpha.
3. **Time-horizon arbitrage** — 2–10 day swing falls in the structural gap between HFT (<1s) and institutional value/momentum (months). Underserved because HFT can't trade at this cadence and PMs can't trade at this turnover.

The thesis is **testable**: if real, eval shows positive OOS Sharpe vs SPY with ablations confirming each agent contributes. If not, we kill at M6/M12 per S06.

Every downstream decision (strategy, niche, architecture, build sequence) gets re-checked against L01.

## Strategic decisions (locked)

| ID | Decision | Reasoning |
|----|----------|-----------|
| S01 | Tier: **T2** (beats SPY on OOS walk-forward Sharpe across ≥3 regimes) | Matches user motivation (learning + edge belief); eval infra must be built alongside agents, not after |
| S02 | Trading style: **Swing 1–5 day holds** | Matches LLM cadence (decisions per session, not per second), sidesteps PDT, ~500–1500 trades/yr at this pace gives T2 statistical power |
| S03 | Niche: **Agriculture specialist**; tentatively **variant B (crop-side)** | LLM edge sharpest in multi-source text-heavy domains with slower cycles; ag is under-covered vs mega-caps; user interest confirmed |
| S04 | Long/short: **Long-only at start** | Simpler; add shorts post-T1 after Risk + slippage modeling mature |
| S05 | Headline metric: **12-mo rolling OOS Sharpe on walk-forward (24-mo train / 1-mo test / monthly roll)** | Standard, comparable, supports tier gates; secondaries tracked: max DD, Sortino, Brier, turnover, active share vs SPY |
| S06 | Kill criteria: **Tiered M3/M6/M12 gates at T0/T1/T2** + continuous auto-rollback on any DPO promotion that regresses validation OOS Sharpe | Prevents sunk-cost continuation; enables autonomous learning within safe bounds |
| S07 | Real money: **Paper-only until explicit user authorization** | User-resource decision; default is safe |
| S08 | Long-only hedging: **Accept non-ag noise (USD, rates, broad beta) initially**; revisit if noise dominates returns in eval | Deferred complexity; don't over-design |

## Architectural decisions (locked) — see llm_trading_orchestration.md §6 for full table

| ID | Decision | Original doc said | Why changed |
|----|----------|-------------------|-------------|
| A01 | **Python primary**, no Rust initially | Rust orchestrator + Python ML via PyO3 | Swing cadence removes latency need; solo operator benefits from one language; Rust added later only for specific subsystems (scheduler, optional slice 11) |
| A02 | **SQLite (WAL) primary**; DuckDB as analytical read layer at slice 9; Parquet deferred indefinitely | DuckDB primary | SQLite handles concurrent writers natively via WAL; OLTP workload dominates early slices; DuckDB's `sqlite_scanner` reads SQLite files directly at slice 9 — zero migration cost |
| A03 | **HuggingFace `transformers`** for inference initially | vLLM / llama.cpp | Simpler for slices 5–8; upgrade only if hot-swap or backtest throughput bottlenecks |
| A04 | **Base model: Qwen 2.5 7B Instruct** | Not specified | Best 7B benchmarks, Apache 2.0, digit-level tokenizer helps numerical reasoning, first-class `transformers` + `peft` support |
| A05 | **Agent roster revised** — News/Sentiment (LoRA), Domain (rules→LoRA later), Technical (PatchTST), Macro (rules→LoRA later), Risk (rule-based deterministic), Meta (aggregator→LoRA after DPO data) | Five LoRA adapters | Technical is better served by TCN/PatchTST than 7B LLM; Risk is deterministic (three-line policies don't need fine-tuning); Domain added for ag specialization; Meta becomes LoRA only when DPO data accumulates. **Net LoRA count at steady state: 2** (possibly 4 if Macro/Domain also become LoRA — decided on evidence) |
| A06 | **Vertical-slice build** (11 slices, each shippable) | 7 monolithic milestones | Each slice ships end-to-end; YAGNI; real feedback per slice; no cart-before-horse architecture |
| A07 | **`asyncio`** for concurrency | Tokio + crossbeam | Swing cadence doesn't need Rust-tier concurrency |
| A08 | Hard adapter swap initially; X-LoRA deferred | Same | Consistent with original |
| A09 | DPO via `trl` on LoRA adapters | Same | Consistent with original |
| A10 | Alpaca paper brokerage | Same | Consistent with original |

## Strategy decisions (locked)

| ID | Decision | Reasoning |
|----|----------|-----------|
| ST01 | **Push-based continuous prediction** strategy. System continuously predicts per ticker; opens/closes when LLM Meta judges threshold met | Maximizes trade frequency for stat-sig in 12mo to T2; aligned with L01 component 1 (continuous cross-modal integration); replaces earlier T1-T5 theory-of-edge framing as artificial taxonomy |
| ST02 | **Prediction shape v1** = `(direction, confidence)` per ticker per agent at **3-day horizon**. Single horizon, no probability calibration | Simplest verifiable shape; graduates to v2 calibrated probability + Brier scoring, v3 multi-horizon (1d/3d/5d), v4 distribution (mean+var) with Kelly sizing — each version triggered by tier-gate evidence |
| ST03 | **Strategic decisions are LLM-decided by Meta, not hardcoded rules.** Anti-overtrading, hold duration, mid-hold management (add/scale/close partial), hedging composition, event-window response, cash-vs-invested — all judged by Meta per cycle | Aligned with L01 (encoded domain knowledge in prompts/reasoning), enables DPO learning over time, avoids hyperparameter overfitting from rule tuning |
| ST04 | **Hardcoded safety rails (Risk module + broker):** max 20%/ticker, max 5 concurrent positions, daily -5% halt, monthly -10% halt, long-only at start, ATR-based server-side stops at Alpaca, no real money without explicit user authorization | Existential limits; non-negotiable; never subject to LLM judgment. Risk module enforces in code; broker enforces stop execution |
| ST05 | **Meta v1 = Qwen 2.5 7B Instruct base + structured prompt (no LoRA)** | Need decisions before DPO data exists; base model has enough world knowledge for defensible v1 decisions. Meta v2 adds LoRA via DPO at slice 10+ |
| ST06 | **Meta inputs:** agent signals + portfolio state + recent decision history + relevant context (upcoming events, time of day, recent outcomes) | Provides reasoning substrate; concrete schema designed in slice 7 |
| ST07 | **Meta outputs:** structured JSON decisions per ticker — `(action, size, exit_conditions, reasoning_trace)` | Auditable; reasoning trace required because Meta's reasoning becomes DPO training data later (L01 component 2) |
| ST08 | Meta prompt provides priors as *guidance*, not constraints: default 1-5d holds, default once-per-day cycle, default no rapid open/close on same ticker, cash is valid output | Priors guide naive Meta v1; DPO tunes them over time as outcomes accumulate |

## Trading-mechanics decisions (locked)

| ID | Decision |
|----|----------|
| T01 | Position sizing: quarter-Kelly on Meta confidence |
| T02 | Max position per ticker: 20% of equity |
| T03 | Max concurrent positions: 5 |
| T04 | Daily loss circuit breaker: -5% → halt new positions 24h |
| T05 | Monthly drawdown breaker: -10% → halt, manual restart required |
| T06 | Order type: bracket (limit entry ± 10 bps, server-side ATR stop at broker, 2:1 take-profit default) |
| T07 | TIF: GTC for entries (24h), market-close for urgent exits |
| T08 | Backtest slippage model: midpoint + 2 bps + simulated 100ms latency |
| T09 | Observability: structured log lines to SQLite `audit_log` (append-only); SQL queries answer "what happened?"; Telegram bot on failure triggers (implemented per failure class as encountered) |
| T10 | Secrets: `.env` + `python-dotenv` (gitignored) for dev; formalize later if deployment grows |

## Deferred decisions (revisit in-slice)

- **Final ag sub-niche** (variant A/B/C/D) — B tentative; final lock after Phase 0 (slice 3-ish gives the substrate)
- **Paid-data budget** — $0 assumed; propose + request when concrete ROI argument exists
- **Ethical limits on instruments** — none assumed; user to flag if any
- **Per-failure-class default actions** — general principle is "safe = halt, not liquidate"; specifics added per slice as failure modes are identified
- **Kalshi/Polymarket overlay** — deferred; revisit post-slice-6 (Macro agent) as potential cheap edge-falsification harness for the Macro signal

## Blocked on user authorization (only these)

- **Real-money activation** (default: never; no fallback; always requires explicit OK)
- **Hard budget threshold for paid services** (default $0; user can revise)
- **Ethical trading constraints** (default none; user to flag)

All other decisions: Claude makes and documents per sponsor model.

## Active build state

Architecture doc: `llm_trading_orchestration.md` (revised April 2026)

Next build step: spec slice 1 in detail (file layout, SQLite schema, Alpaca endpoints, FinBERT setup), then build it.

## References

- Primary architecture: `llm_trading_orchestration.md`
- Sponsor-mode memory: `/home/devs/.claude/projects/-home-devs-workspace-hyxrestration/memory/sponsor_mode.md`
- Wiki (to be populated by context-keeper): `docs/wiki/`
