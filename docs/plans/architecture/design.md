# Architecture Design

> **STATUS: SUPERSEDED — UNDER CO-DESIGN.**
> This draft was written unilaterally and is being rewritten one decision at a time, with each architectural choice discussed and locked before being committed. Sections below should be treated as a starting position for discussion, NOT as locked decisions. Current discussion: process model (§1). Once a section is agreed, it gets re-written here as locked.

Concrete design layer below the high-level decisions in `llm_trading_orchestration.md` (§§1, 2.1, 2.4, 5.2, 6, 7, 8). This document covers process model, repo layout, SQLite schema (DDL), configuration, conventions, error handling, concurrency model, observability, and slice-1 cuts.

When in doubt: this doc is implementation-level; the main doc is project-level. If they conflict, the main doc wins on *what* and this doc wins on *how*.

---

## 1. Process Model

System grows from one ad-hoc script (slice 1) to three long-running daemons (slice 8+). The slice introducing each transition is noted explicitly.

| Slice | Process count | Run model |
|------|---------------|-----------|
| 1–2 | 0 long-running | On-demand scripts: `python -m hyx.slice1` |
| 3–6 | 0 long-running | Cron / systemd timers for scheduled ingest; interactive scripts for analysis |
| 7 | 0 long-running | Same as 3–6, plus end-to-end signal→decision script |
| 8+ | **3 long-running** | `hyx-ingest`, `hyx-trader`, `hyx-train` as systemd services |

### Steady-state (slice 8+) processes

- **`hyx-ingest`** — async loop. Pulls Alpaca OHLCV (1d + 1h), Alpaca News, USDA WASDE (monthly), NOAA Drought Monitor (weekly), CBOT commodity prices (daily). Writes to SQLite. Single writer for all `*_data` tables.
- **`hyx-trader`** — main decision loop. Triggered by data arrival (poll-based; no event bus). Runs Sentiment → Domain → Technical → Macro → Risk filter → Meta → submits orders to Alpaca paper. Single writer for `signals`, `decisions`, `executions`, `fills`, `outcomes`.
- **`hyx-train`** — nightly cron-triggered (not always-on). Reads outcomes, generates DPO preference pairs, fine-tunes adapters, validates, promotes-or-rolls-back. Single writer for `adapters`, `training_runs`.

All three share `audit_log` (append-only, low contention).

### Boot sequence

1. Process starts → loads config (`.env` + YAML) → opens SQLite connection in WAL mode
2. `db.migrations.apply_pending()` — runs any unapplied DDL files
3. Process-specific init (Alpaca client, model loader, etc.)
4. Main loop (`hyx-ingest`/`hyx-trader`) or one-shot (`hyx-train`)
5. SIGTERM → finish current cycle → close SQLite cleanly → exit 0

### Inter-process coordination

**No message bus.** Coordination is via SQLite + polling:
- `hyx-trader` polls SQLite for new OHLCV / news rows since its last seen `id`
- `hyx-train` runs once nightly via cron; reads `outcomes` since last run
- Lock contention is avoided by single-writer-per-table convention (§9)

Polling cadence is generous (every 1m for trader, every 30s during market hours for ingest checks) — swing trading doesn't need event-driven precision.

---

## 2. Repo Layout

```
hyxrestration/
├── pyproject.toml              # Python project + deps + tool config
├── README.md
├── CLAUDE.md
├── llm_trading_orchestration.md   # Project-level architecture
├── .env.example                # Template; real .env gitignored
├── .gitignore
│
├── docs/
│   ├── plans/
│   │   ├── architecture/
│   │   │   └── design.md       # This file
│   │   └── slice-{N}.md        # Per-slice implementation specs (slice-1.md next)
│   ├── sessions/               # Session summaries (auto-generated)
│   └── wiki/                   # Context-keeper synthesized knowledge
│
├── hyx/                        # Main Python package
│   ├── __init__.py
│   ├── config.py               # Config loading (.env + YAML)
│   ├── types.py                # Shared dataclasses (Signal, Decision, etc.)
│   ├── audit.py                # Audit log helpers
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── conn.py             # SQLite connection mgmt (WAL setup, pragmas)
│   │   ├── migrations.py       # Migration runner
│   │   └── migrations/         # Numbered DDL files
│   │       ├── 001_initial.sql
│   │       ├── 002_signals.sql
│   │       └── ...
│   │
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── alpaca_md.py        # Market data
│   │   ├── alpaca_news.py      # News
│   │   ├── usda.py             # WASDE/NASS (slice 3)
│   │   ├── noaa.py             # Drought monitor (slice 3)
│   │   └── cbot.py             # Commodity prices (slice 3)
│   │
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── sentiment.py        # FinBERT (slice 1) → Qwen+LoRA (slice 5)
│   │   ├── technical.py        # PatchTST (slice 4)
│   │   ├── domain.py           # Rules + lookups (slice 3)
│   │   ├── macro.py            # Rules (slice 6)
│   │   ├── risk.py             # Deterministic limits (slice 7)
│   │   └── meta.py             # Aggregator (slice 7)
│   │
│   ├── broker/
│   │   ├── __init__.py
│   │   ├── alpaca.py           # Alpaca paper client wrapper (slice 8)
│   │   └── execution.py        # Order placement, monitoring (slice 8)
│   │
│   ├── eval/
│   │   ├── __init__.py
│   │   ├── backtest.py         # Replay engine (slice 9)
│   │   ├── walkforward.py      # Walk-forward driver (slice 9)
│   │   ├── metrics.py          # Sharpe, Sortino, Brier, etc. (slice 9)
│   │   └── baselines.py        # Random, SPY, SMA, agent-solo (slice 9)
│   │
│   ├── training/
│   │   ├── __init__.py
│   │   ├── dpo.py              # DPO loop (slice 10)
│   │   ├── pairs.py            # Preference pair generation (slice 10)
│   │   ├── adapters.py         # LoRA load/save/promote (slice 5+)
│   │   └── validation.py       # Promote/rollback gate (slice 10)
│   │
│   └── slice1/                 # Per-slice entry points
│       ├── __init__.py
│       └── run.py              # `python -m hyx.slice1.run`
│
├── adapters/                   # LoRA checkpoints (gitignored)
│   └── {agent}/v{date}_{sha}.safetensors
│
├── data/                       # SQLite + raw blobs (gitignored)
│   ├── hyx.db
│   ├── hyx.db-wal
│   ├── hyx.db-shm
│   └── blobs/                  # Large news/filing text not stored inline
│
├── configs/
│   ├── dev.yaml
│   └── prod.yaml               # Created when deployment formalizes
│
├── scripts/                    # CLI tools
│   ├── ingest.py               # python scripts/ingest.py --once
│   ├── trade.py
│   ├── train.py
│   ├── backtest.py
│   └── migrate.py              # Apply pending migrations manually
│
└── tests/
    ├── conftest.py
    ├── fixtures/               # Small SQLite DBs with known data
    ├── unit/                   # Fast, mocked IO
    ├── integration/            # Real SQLite, real models, no broker
    └── replay/                 # Deterministic replay against fixtures
```

**Why this shape:**
- One Python package (`hyx/`) with subpackages by concern (db, ingest, agents, broker, eval, training)
- Per-slice entry points in `hyx/sliceN/` keep slice scope visible — easy to see "what's in slice 1" by looking at one directory
- `scripts/` are thin CLI wrappers over `hyx/` modules — easy to invoke from cron/systemd
- `adapters/` and `data/` separate from code — gitignored, easy to back up independently
- Migrations as numbered SQL files — auditable, no ORM-coupling

---

## 3. SQLite Schema (full DDL)

All timestamps are ISO 8601 UTC strings (e.g. `2026-04-18T14:30:00.000Z`). All tickers uppercase. All percentages as fractions (0.0–1.0, not 0–100).

### 3.1 Universe & instruments

```sql
CREATE TABLE universe (
    ticker      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    asset_type  TEXT NOT NULL CHECK (asset_type IN ('equity', 'etf', 'futures')),
    subsector   TEXT,           -- 'fertilizer', 'equipment', 'processor', etc.
    sector      TEXT NOT NULL DEFAULT 'agriculture',
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    added_at    TEXT NOT NULL,
    removed_at  TEXT,
    notes       TEXT
);
```

### 3.2 Market data

```sql
CREATE TABLE ohlcv (
    ticker       TEXT NOT NULL,
    ts           TEXT NOT NULL,    -- bar START time, UTC
    timeframe    TEXT NOT NULL CHECK (timeframe IN ('1m', '5m', '15m', '1h', '1d')),
    open         REAL NOT NULL,
    high         REAL NOT NULL,
    low          REAL NOT NULL,
    close        REAL NOT NULL,
    volume       REAL NOT NULL,
    vwap         REAL,
    trade_count  INTEGER,
    source       TEXT NOT NULL,    -- 'alpaca'
    ingested_at  TEXT NOT NULL,
    PRIMARY KEY (ticker, ts, timeframe)
);

CREATE INDEX idx_ohlcv_ts          ON ohlcv(ts, timeframe);
CREATE INDEX idx_ohlcv_ticker_ts   ON ohlcv(ticker, ts);
```

### 3.3 News

```sql
CREATE TABLE news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,    -- 'alpaca', 'yahoo', 'reuters', ...
    source_id     TEXT,             -- source's own ID for dedup
    published_at  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    url           TEXT,
    headline      TEXT NOT NULL,
    summary       TEXT,
    body_path     TEXT,             -- path under data/blobs/ if body is large
    UNIQUE (source, source_id)
);

CREATE TABLE news_tickers (
    news_id  INTEGER NOT NULL,
    ticker   TEXT NOT NULL,
    PRIMARY KEY (news_id, ticker),
    FOREIGN KEY (news_id) REFERENCES news(id) ON DELETE CASCADE
);

CREATE INDEX idx_news_published    ON news(published_at);
CREATE INDEX idx_news_tickers_tk   ON news_tickers(ticker);
```

### 3.4 Domain data (slice 3+)

```sql
CREATE TABLE usda_wasde (
    ts              TEXT NOT NULL,    -- report release date
    commodity       TEXT NOT NULL,    -- 'corn', 'soybeans', 'wheat', 'cotton', ...
    field           TEXT NOT NULL,    -- 'production', 'ending_stocks', 'stocks_to_use_ratio', ...
    marketing_year  TEXT NOT NULL,    -- e.g. '2025/26'
    value           REAL,
    unit            TEXT,
    PRIMARY KEY (ts, commodity, field, marketing_year)
);

CREATE TABLE drought_monitor (
    ts       TEXT NOT NULL,    -- weekly Tuesday release
    region   TEXT NOT NULL,    -- state code or 'US'
    d0_pct   REAL,             -- abnormally dry
    d1_pct   REAL,             -- moderate
    d2_pct   REAL,             -- severe
    d3_pct   REAL,             -- extreme
    d4_pct   REAL,             -- exceptional
    PRIMARY KEY (ts, region)
);

CREATE TABLE commodity_prices (
    ts              TEXT NOT NULL,
    commodity       TEXT NOT NULL,
    contract_month  TEXT,             -- e.g. 'JUL26' or NULL for spot
    price           REAL NOT NULL,
    settlement      INTEGER NOT NULL DEFAULT 1,  -- 1 = settlement, 0 = intraday
    source          TEXT NOT NULL,
    PRIMARY KEY (ts, commodity, contract_month)
);
```

### 3.5 Signals (each agent's output)

```sql
CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    agent           TEXT NOT NULL,    -- 'sentiment', 'technical', 'domain', 'macro', 'meta'
    agent_version   TEXT NOT NULL,    -- e.g. 'finbert-v1' or 'sentiment-v20260418_a1b2c3d'
    direction       REAL NOT NULL CHECK (direction BETWEEN -1.0 AND 1.0),
    confidence      REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    horizon_days    INTEGER,
    reasoning       TEXT,             -- nullable for non-LLM agents
    metadata_json   TEXT              -- agent-specific extras (JSON)
);

CREATE INDEX idx_signals_ts          ON signals(ts);
CREATE INDEX idx_signals_ticker_ts   ON signals(ticker, ts);
CREATE INDEX idx_signals_agent_ts    ON signals(agent, ts);
```

### 3.6 Decisions (Meta's output, slice 7+)

```sql
CREATE TABLE decisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    action             TEXT NOT NULL CHECK (action IN ('open_long', 'open_short', 'close', 'hold')),
    target_size_pct    REAL,    -- as fraction of equity
    target_price       REAL,    -- limit entry
    stop_price         REAL,
    take_profit_price  REAL,
    horizon_days       INTEGER,
    confidence         REAL,
    rationale          TEXT,
    inputs_json        TEXT     -- snapshot of signal IDs that produced this decision
);

CREATE INDEX idx_decisions_ts          ON decisions(ts);
CREATE INDEX idx_decisions_ticker_ts   ON decisions(ticker, ts);
```

### 3.7 Executions & fills (slice 8+)

```sql
CREATE TABLE executions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id      INTEGER NOT NULL,
    submitted_at     TEXT NOT NULL,
    broker           TEXT NOT NULL,     -- 'alpaca_paper', 'alpaca_live'
    broker_order_id  TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty              REAL NOT NULL,
    order_type       TEXT NOT NULL,     -- 'market', 'limit', 'bracket'
    limit_price      REAL,
    stop_price       REAL,
    take_profit_price REAL,
    tif              TEXT NOT NULL,     -- 'gtc', 'day', 'ioc', 'fok'
    status           TEXT NOT NULL,     -- 'submitted', 'partial', 'filled', 'cancelled', 'rejected'
    last_update_at   TEXT NOT NULL,
    error            TEXT,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);

CREATE INDEX idx_executions_decision     ON executions(decision_id);
CREATE INDEX idx_executions_broker_id    ON executions(broker_order_id);

CREATE TABLE fills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id  INTEGER NOT NULL,
    ts            TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           REAL NOT NULL,
    price         REAL NOT NULL,
    commission    REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (execution_id) REFERENCES executions(id)
);

CREATE INDEX idx_fills_execution  ON fills(execution_id);
```

### 3.8 Outcomes (closed positions, slice 8+)

```sql
CREATE TABLE outcomes (
    decision_id      INTEGER PRIMARY KEY,
    closed_at        TEXT NOT NULL,
    holding_days     REAL NOT NULL,
    entry_price      REAL NOT NULL,
    exit_price       REAL NOT NULL,
    pnl_dollars      REAL NOT NULL,
    pnl_pct          REAL NOT NULL,
    pnl_vs_spy_pct   REAL,    -- alpha over SPY in same window
    notes            TEXT,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);
```

### 3.9 Adapters & training (slice 5+ for adapters; slice 10+ for training)

```sql
CREATE TABLE adapters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT NOT NULL,
    version         TEXT NOT NULL,    -- 'v20260418_a1b2c3d'
    path            TEXT NOT NULL,    -- 'adapters/sentiment/v20260418_a1b2c3d.safetensors'
    base_model      TEXT NOT NULL,    -- 'Qwen2.5-7B-Instruct'
    created_at      TEXT NOT NULL,
    promoted_at     TEXT,             -- NULL if never promoted to live
    rolled_back_at  TEXT,             -- NULL if not rolled back
    val_metrics_json TEXT,
    notes           TEXT,
    UNIQUE (agent, version)
);

CREATE INDEX idx_adapters_agent_promoted  ON adapters(agent, promoted_at);

CREATE TABLE training_runs (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at               TEXT NOT NULL,
    ended_at                 TEXT,
    agent                    TEXT NOT NULL,
    method                   TEXT NOT NULL CHECK (method IN ('qlora', 'dpo')),
    base_adapter_id          INTEGER,
    new_adapter_id           INTEGER,
    training_examples_count  INTEGER,
    val_sharpe_before        REAL,
    val_sharpe_after         REAL,
    promoted                 INTEGER NOT NULL DEFAULT 0,
    rollback_reason          TEXT,
    log_path                 TEXT,
    FOREIGN KEY (base_adapter_id) REFERENCES adapters(id),
    FOREIGN KEY (new_adapter_id)  REFERENCES adapters(id)
);
```

### 3.10 Audit log (all slices, all processes)

```sql
CREATE TABLE audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    process       TEXT NOT NULL,    -- 'ingest', 'trader', 'train', 'cli'
    level         TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR')),
    component     TEXT NOT NULL,    -- 'alpaca_md', 'meta', 'sentiment', etc.
    event         TEXT NOT NULL,    -- 'fetch_ohlcv', 'decision_made', 'order_submitted', etc.
    payload_json  TEXT
);

CREATE INDEX idx_audit_ts             ON audit_log(ts);
CREATE INDEX idx_audit_process_event  ON audit_log(process, event);
CREATE INDEX idx_audit_level          ON audit_log(level) WHERE level IN ('WARN', 'ERROR');
```

### 3.11 Schema versioning

```sql
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    filename    TEXT NOT NULL
);
```

### 3.12 Slice 1 cuts

Slice 1 uses only:
- `universe` (one row: DE)
- `ohlcv` (DE only, 1d timeframe)
- `news`, `news_tickers` (DE-tagged news)
- `signals` (FinBERT outputs only)
- `audit_log`
- `schema_version`

Migrations 001-002 cover these. Later migrations introduce the rest as their slice arrives.

---

## 4. Configuration

### 4.1 Layered

1. **`.env`** — secrets only (API keys, tokens). Not committed. Loaded via `python-dotenv`.
2. **`configs/{HYX_ENV}.yaml`** — non-secret config (universe, schedules, thresholds). Committed.
3. **CLI flags** — per-invocation overrides (e.g., `--once`, `--ticker=DE`).
4. **Environment variables `HYX_*`** — runtime overrides for ops (e.g., `HYX_LOG_LEVEL=DEBUG`).

Resolution order (last wins): YAML → env vars (`HYX_*`) → CLI flags.

### 4.2 `.env.example` (committed template)

```
# Alpaca paper trading
ALPACA_KEY_ID=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets

# Hyx runtime
HYX_ENV=dev
HYX_DATA_DIR=./data
HYX_ADAPTER_DIR=./adapters
HYX_LOG_LEVEL=INFO

# Optional: Telegram alerting (slice 8+)
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=
```

### 4.3 `configs/dev.yaml` (full)

```yaml
database:
  path: ${HYX_DATA_DIR}/hyx.db
  wal_mode: true
  busy_timeout_ms: 5000
  foreign_keys: true

universe:
  # Slice 1
  - ticker: DE
    name: Deere & Company
    asset_type: equity
    subsector: equipment
  # Slice 2 expansion
  - ticker: NTR
    name: Nutrien Ltd
    asset_type: equity
    subsector: fertilizer
  - ticker: MOS
    name: Mosaic Co
    asset_type: equity
    subsector: fertilizer
  - ticker: CF
    name: CF Industries
    asset_type: equity
    subsector: fertilizer
  - ticker: CTVA
    name: Corteva
    asset_type: equity
    subsector: seeds_agchem
  - ticker: FMC
    name: FMC Corp
    asset_type: equity
    subsector: seeds_agchem
  - ticker: ADM
    name: Archer-Daniels-Midland
    asset_type: equity
    subsector: processor
  - ticker: BG
    name: Bunge Global
    asset_type: equity
    subsector: processor
  - ticker: AGCO
    name: AGCO Corp
    asset_type: equity
    subsector: equipment
  - ticker: CNHI
    name: CNH Industrial
    asset_type: equity
    subsector: equipment
  - ticker: DBA
    name: Invesco DB Agriculture Fund
    asset_type: etf
    subsector: ag_commodity_basket
  - ticker: CORN
    name: Teucrium Corn Fund
    asset_type: etf
    subsector: ag_commodity_corn
  - ticker: WEAT
    name: Teucrium Wheat Fund
    asset_type: etf
    subsector: ag_commodity_wheat
  - ticker: SOYB
    name: Teucrium Soybean Fund
    asset_type: etf
    subsector: ag_commodity_soy

ingest:
  ohlcv:
    timeframes: ['1d', '1h']
    history_years: 10        # initial backfill
    schedule_cron: "0 17 * * 1-5"   # daily 5pm ET-ish (post market close)
  news:
    sources: ['alpaca']
    schedule_cron: "*/15 13-21 * * 1-5"  # every 15min during US market hours UTC

agents:
  sentiment:
    model_id: ProsusAI/finbert     # slice 1; replaced at slice 5
    confidence_threshold: 0.6
  technical:
    model_path: ${HYX_ADAPTER_DIR}/technical/current.pt
    horizons_days: [1, 3, 5]
  meta:
    aggregation: confidence_weighted   # slice 7; LoRA at slice 10+

risk:
  max_pct_per_ticker: 0.20
  max_concurrent_positions: 5
  daily_loss_halt_pct: -0.05
  monthly_loss_halt_pct: -0.10
  kelly_fraction: 0.25
  max_gross_exposure: 1.00          # no leverage initially

execution:
  broker: alpaca_paper
  default_order_type: bracket
  entry_offset_bps: 10
  atr_stop_multiplier: 2.0
  atr_window_days: 20
  reward_risk_ratio: 2.0
  tif_entry: gtc
  tif_exit: day

logging:
  console_level: INFO
  audit_db: true
  audit_levels: ['INFO', 'WARN', 'ERROR']

eval:
  walkforward:
    train_window_months: 24
    test_window_months: 1
    roll_step_months: 1
  baselines: ['random', 'spy_buy_hold', 'sma_crossover_50_200', 'each_agent_solo']
  headline_metric: rolling_oos_sharpe_12mo
```

---

## 5. Type Conventions

- **Timestamps** — always UTC, ISO 8601 with milliseconds. Stored as TEXT, parsed on read. Never use `time.time()` floats in storage.
- **Tickers** — uppercase, no spaces. Validated at ingest boundary.
- **Prices** — Python `Decimal` for storage and arithmetic involving money; `float` only inside numerical models (TCN inputs, etc.). Convert at boundaries.
- **Percentages** — fractions in 0.0–1.0 (e.g., `0.20` for 20%, never `20`).
- **Cash** — dollars, `Decimal`, 2 places.
- **IDs** — INTEGER auto-increment from SQLite.
- **JSON blobs** — store as TEXT, parse with `json.loads` on read; validate shape on write only at process boundaries.
- **Enums** — Python `Enum` types in code, plain TEXT in DB with `CHECK` constraints to enforce domain.

### Shared dataclasses (`hyx/types.py`)

Use Python 3.11+ dataclasses with `slots=True` for performance. Pydantic only introduced if validation needs grow (probably slice 5+ when LLM outputs need validation).

```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

class Action(str, Enum):
    OPEN_LONG = 'open_long'
    OPEN_SHORT = 'open_short'
    CLOSE = 'close'
    HOLD = 'hold'

@dataclass(slots=True, frozen=True)
class Signal:
    ts: datetime
    ticker: str
    agent: str
    agent_version: str
    direction: float          # -1.0 to 1.0
    confidence: float         # 0.0 to 1.0
    horizon_days: int | None
    reasoning: str | None
    metadata: dict | None

@dataclass(slots=True, frozen=True)
class Decision:
    ts: datetime
    ticker: str
    action: Action
    target_size_pct: float | None
    target_price: Decimal | None
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    horizon_days: int | None
    confidence: float | None
    rationale: str | None
    input_signal_ids: list[int]
```

---

## 6. Logging & Audit

### 6.1 Two streams

1. **Operational logs** — stderr, JSON-structured per line, captured by systemd journal in production. Format:
   ```json
   {"ts":"2026-04-18T14:30:00.000Z","level":"INFO","process":"ingest","component":"alpaca_md","event":"fetch_complete","ticker":"DE","bars":78}
   ```
   Used for live debugging, performance analysis, transient errors.

2. **Audit log** — SQLite `audit_log` table, append-only, written by `hyx.audit.log_event(...)`. Captures events that matter for trade reconstruction:
   - Every `signal_emitted`
   - Every `decision_made`
   - Every `order_submitted`, `order_filled`, `order_cancelled`, `order_rejected`
   - Every `adapter_promoted`, `adapter_rolled_back`
   - Every `circuit_breaker_tripped`
   - Every `error` at WARN/ERROR level

**Convention:** if a future regulator/auditor (or future Claude) needs to answer "why did the system buy DE on 2026-04-18 at 10:32 ET?" the `audit_log` + `signals` + `decisions` + `executions` + `fills` tables alone should answer it without needing operational stderr logs.

### 6.2 Log line schema (operational)

Required fields: `ts`, `level`, `process`, `component`, `event`. Optional: any kwargs (`ticker`, `error`, `latency_ms`, etc.).

Helper: `hyx.audit.log(level, component, event, **kwargs)` writes to BOTH stderr and `audit_log` if level >= configured threshold.

---

## 7. Error Handling Philosophy

**Rule of thumb: safe default is HALT, do not act.**

| Failure | Default response |
|---------|------------------|
| Inference failure | Don't emit signal; log; continue with other agents |
| All-agents failure | No decision; no orders submitted; alert |
| Data ingest failure | Retry with backoff; if persistent, halt new decisions (stale-data guard) |
| Broker API failure (read) | Retry with backoff; existing positions stay (broker has them) |
| Broker API failure (write/order) | Halt new orders; alert; manual intervention required |
| Training failure | Don't promote new adapter; keep current; log |
| Validation gate failure | Don't promote; keep current; alert |
| Disk full | Halt all writes; switch to read-only; alert |
| SIGTERM | Finish current cycle; close SQLite cleanly; exit 0 |
| Unhandled exception in main loop | Log to stderr + audit_log; alert; exit non-zero (let systemd restart) |

**Active actions (liquidating, opening, promoting) require positive confirmation:**
- Liquidating positions only via explicit human input or hard rule trigger (`-5%` day, `-10%` month)
- Promoting adapters requires validation gate pass (statistical significance vs prior)
- Changing risk limits requires committed YAML change

Exceptions are caught at module boundaries (`ingest`, `agents`, `broker`, `training`). Inside modules, let exceptions propagate to make bugs visible.

### 7.1 Stale-data guard

Trader checks: max age of last OHLCV row per ticker before making any decision. If > 6 hours old (intraday) or > 2 trading days (overnight), skip that ticker's decisions for the cycle. Alert.

### 7.2 Position-of-record

**Source of truth for positions = the broker, not our DB.** On every trader cycle start: pull positions from Alpaca, reconcile against `executions`/`fills`. Discrepancy = halt + alert.

This protects against: missed fills due to network blips, manual interventions on broker side, corrupted local state after a crash.

---

## 8. Concurrency Model

### 8.1 Within a process

- `asyncio` for IO-bound concurrency (HTTP fetches, broker calls). One event loop per process.
- Synchronous SQLite calls (the `sqlite3` stdlib module is sync) — wrapped in `asyncio.to_thread()` when called from async context, OR run on a single thread with sync calls if the process is sync-by-default (slice 1).
- No threading for CPU work beyond what `transformers`/`torch` does internally.

### 8.2 Across processes (single-writer-per-table)

SQLite WAL allows multiple writers, but contention degrades performance. We adopt a discipline of **one process owning each table's writes**:

| Table(s) | Owner |
|----------|-------|
| `ohlcv`, `news`, `news_tickers`, `usda_wasde`, `drought_monitor`, `commodity_prices`, `universe` | `hyx-ingest` |
| `signals`, `decisions`, `executions`, `fills`, `outcomes` | `hyx-trader` |
| `adapters`, `training_runs` | `hyx-train` |
| `audit_log` | All processes (append-only, one INSERT per event, low contention) |
| `schema_version` | Whichever process applies migrations (typically `hyx-ingest` first) |

All processes can READ all tables freely (WAL allows readers in parallel with writer).

### 8.3 SQLite pragmas (set at connection open in `hyx.db.conn`)

```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")     # NORMAL is safe with WAL; FULL is slower
conn.execute("PRAGMA foreign_keys=ON")
conn.execute("PRAGMA busy_timeout=5000")      # 5s before raising SQLITE_BUSY
conn.execute("PRAGMA mmap_size=268435456")    # 256 MB memory-mapped I/O for reads
conn.execute("PRAGMA cache_size=-64000")      # 64 MB cache (negative = KB)
```

---

## 9. Data Flow (full system, slice 8+)

```
External sources                              
─────────────────                              
                                              
Alpaca ─┐    USDA ─┐    NOAA ─┐    CBOT ─┐    
  (md)  │  (WASDE) │ (drought)│ (prices) │    
        ▼          ▼          ▼          ▼    
       ┌──────────────────────────────────┐   
       │         hyx-ingest               │   
       │  (asyncio, single writer)        │   
       └──────────────┬───────────────────┘   
                      │ INSERT                
                      ▼                       
       ┌──────────────────────────────────┐   
       │    SQLite (WAL mode)             │   
       │                                  │   
       │  ohlcv, news, usda_wasde,        │   
       │  drought_monitor, commodity_prices│  
       └──────────────┬───────────────────┘   
                      │ SELECT                
                      ▼                       
       ┌──────────────────────────────────┐   
       │         hyx-trader               │   
       │                                  │   
       │  ┌──────────┐  ┌──────────┐      │   
       │  │Sentiment │  │Technical │      │   
       │  │ (Qwen)   │  │(PatchTST)│      │   
       │  └────┬─────┘  └────┬─────┘      │   
       │  ┌────┴─────┐  ┌────┴─────┐      │   
       │  │ Domain   │  │ Macro    │      │   
       │  │ (rules)  │  │ (rules)  │      │   
       │  └────┬─────┘  └────┬─────┘      │   
       │       └──────┬──────┘            │   
       │              ▼                   │   
       │         ┌────────┐               │   
       │         │  Risk  │               │   
       │         │ filter │               │   
       │         └────┬───┘               │   
       │              ▼                   │   
       │         ┌────────┐               │   
       │         │  Meta  │               │   
       │         └────┬───┘               │   
       └──────────────┼───────────────────┘   
                      │ INSERT                
                      ▼                       
       ┌──────────────────────────────────┐   
       │    SQLite (WAL mode)             │   
       │                                  │   
       │  signals, decisions,             │   
       │  executions, fills, outcomes     │   
       └──────────────┬───────────────────┘   
                      │ submit_order          
                      ▼                       
                ┌──────────┐                  
                │  Alpaca  │                  
                │  Paper   │                  
                └────┬─────┘                  
                     │ webhook / poll fills   
                     │                        
                     ▼                        
              (back to hyx-trader            
               via fills polling)            
                                              
                                              
       ┌──────────────────────────────────┐   
       │         hyx-train                │   
       │   (nightly cron, single-shot)    │   
       │                                  │   
       │  1. Read outcomes since last run │   
       │  2. Generate DPO pairs           │   
       │  3. Run QLoRA + DPO on agent     │   
       │  4. Validate vs held-out slice   │   
       │  5. Promote or rollback          │   
       └──────────────┬───────────────────┘   
                      │ INSERT                
                      ▼                       
       ┌──────────────────────────────────┐   
       │    SQLite + filesystem           │   
       │                                  │   
       │  adapters/, training_runs        │   
       └──────────────────────────────────┘   

All three processes write to audit_log throughout.
```

For slice 1 the picture collapses to: `Alpaca → hyx.slice1.run → SQLite → SQL query`. No daemons, no broker execution, no training. Same SQLite schema (subset).

---

## 10. Ingest Idempotency

All ingest writes use `INSERT OR IGNORE` (or `INSERT ... ON CONFLICT DO NOTHING`) backed by primary keys / unique constraints (§3). Re-running ingest after a crash never duplicates.

For data that needs updates (e.g., revised USDA WASDE numbers republished), use `INSERT OR REPLACE` with explicit `revision_count` semantics — handled per-source in `hyx/ingest/{source}.py`.

For news, dedup is on `(source, source_id)`; if a source lacks a stable ID, fall back to URL hash or `(source, published_at, sha256(headline))`.

---

## 11. Migration Strategy

- Numbered SQL files in `hyx/db/migrations/NNN_description.sql`.
- `hyx.db.migrations.apply_pending(conn)` reads `schema_version` table, applies any unapplied files in order, records `(version, applied_at, filename)`.
- Migrations are append-only — to fix a bad migration, write a new one that reverses it. Never edit committed migrations.
- Plain SQL — no Alembic, no Django ORM coupling. Each migration is one transaction.

### Initial migrations

- `001_initial.sql` — `universe`, `ohlcv`, `audit_log`, `schema_version`
- `002_news.sql` — `news`, `news_tickers`
- `003_signals.sql` — `signals`
- `004_domain.sql` — `usda_wasde`, `drought_monitor`, `commodity_prices` (slice 3)
- `005_decisions.sql` — `decisions` (slice 7)
- `006_execution.sql` — `executions`, `fills`, `outcomes` (slice 8)
- `007_training.sql` — `adapters`, `training_runs` (slice 5/10)

Each shipped with the slice that introduces it.

---

## 12. Testing Conventions

- `pytest` for everything. Test files mirror module structure: `hyx/agents/sentiment.py` ↔ `tests/unit/agents/test_sentiment.py`.
- **`tests/unit/`** — fast (<1s each). No real IO. Mock Alpaca, mock model calls. SQLite in-memory.
- **`tests/integration/`** — real SQLite on disk, real model loads, no broker calls. Slower; run before commits, not on every save.
- **`tests/replay/`** — deterministic replay against frozen SQLite snapshots in `tests/fixtures/`. Used for "did this signal/decision change?" regression tests.
- Per-slice gating: tests for slice N ship with slice N. No mandatory coverage threshold; aim for tests that would actually have caught real bugs.

---

## 13. Slice 1 Cuts (preview; full spec in `docs/plans/slice-1.md`)

**Schema used:** `universe`, `ohlcv`, `news`, `news_tickers`, `signals`, `audit_log`, `schema_version`. Migrations 001–003.

**Modules used:**
- `hyx.config` — load .env + dev.yaml (subset)
- `hyx.db.conn`, `hyx.db.migrations`
- `hyx.ingest.alpaca_md` — fetch DE 1d OHLCV
- `hyx.ingest.alpaca_news` — fetch DE news
- `hyx.agents.sentiment` — FinBERT score per headline
- `hyx.audit` — log events
- `hyx.types` — `Signal` dataclass
- `hyx.slice1.run` — entry point

**Not in slice 1:** Rust, LoRA, DPO, broker execution, scheduler, multi-ticker, Qwen, asyncio, Risk module, Meta module, walk-forward eval, training pipeline.

**Deliverable:** `python -m hyx.slice1.run` produces a SQL query result joining today's news + sentiment + price for DE.

---

## 14. Open Design Questions (revisit at the slice that hits them)

| Question | Slice that forces resolution |
|---------|------------------------------|
| Exact USDA WASDE PDF parsing approach (regex vs PDFplumber vs LLM-extract) | Slice 3 |
| Drought-area-to-ticker weighting heuristic | Slice 3 |
| FinBERT vs FinGPT vs scratch-trained baseline for slice 1 sentiment | Slice 1 (defaulting to FinBERT) |
| DPO pair generation strategy (which decisions pair with which) | Slice 10 |
| Adapter promotion statistical-significance threshold | Slice 10 |
| Whether Macro becomes an LLM adapter (vs staying rule-based) | Slice 6 evidence dictates |
| Whether Domain becomes its own LoRA vs collapsing into Sentiment | Slice 5–6 evidence dictates |
| Position reconciliation cadence with Alpaca (every cycle? hourly?) | Slice 8 |
| Whether to shadow-deploy new adapters before promotion | Slice 10 (default: yes, 5 days) |
| Rust scheduler trigger conditions | Slice 11 (default: skip) |

---

## 15. Cross-references

- Project-level architecture: `llm_trading_orchestration.md`
- Scoping decisions: `agent-memory/orchestrator/project_scoping.md`
- Sponsor mode (cross-session): `~/.claude/projects/-home-devs-workspace-hyxrestration/memory/sponsor_mode.md`
- Per-slice specs: `docs/plans/slice-{N}.md` (slice-1.md next)
