# hyxrestration

Local LLM-driven autonomous swing-trading system for US agricultural equities.

Architecture, decisions, and roadmap live under [`docs/`](docs/):
- [`docs/architecture.md`](docs/architecture.md) — canonical architecture
- [`docs/decisions.md`](docs/decisions.md) — scannable decision index
- [`docs/phase0_testing.md`](docs/phase0_testing.md) — pre-slice-1 falsification tests

## Setup

Requires Python 3.11+ (3.14 on the dev box) and an NVIDIA GPU with CUDA support.
PyTorch cu128 wheels are required for RTX 5090 (Blackwell sm_120).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel

# Core deps (no torch)
pip install duckdb python-dotenv yfinance alpaca-py numpy pandas tqdm pytest ruff transformers

# PyTorch with CUDA 12.8 wheels (Blackwell support)
pip install torch --index-url https://download.pytorch.org/whl/cu128
```

## Credentials

**OHLCV** is pulled from yfinance and needs no account.

**News** uses the Alpaca Benzinga feed (symbol-tagged, ~2021+ depth) and
requires a free paper-trading account — data-only, not a broker commitment.
Broker selection is deferred to slice 8.

```bash
cp .env.example .env
# edit .env: ALPACA_KEY, ALPACA_SECRET   (news only — see A10 in decisions.md)
```

You can run `hyx.slice1 --skip-news` to exercise the OHLCV + report path
before provisioning Alpaca.

## Running slice 1

```bash
# Full run (requires Alpaca credentials for news)
python -m hyx.slice1

# OHLCV-only (no credentials needed)
python -m hyx.slice1 --skip-news
```

First run backfills 5 years of DE OHLCV from yfinance and ~4 years of DE
news from Alpaca (Benzinga history starts ~2021). Subsequent runs are
incremental via `fetch_state` cursors.

Outputs:
- `data/hyx.duckdb` — all operational state
- `reports/slice1/YYYY-MM-DD.{md,csv}` — per-run report
