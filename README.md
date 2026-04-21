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
pip install duckdb python-dotenv alpaca-py numpy pandas tqdm pytest ruff transformers

# PyTorch with CUDA 12.8 wheels (Blackwell support)
pip install torch --index-url https://download.pytorch.org/whl/cu128
```

Copy `.env.example` to `.env` and fill in credentials:

```bash
cp .env.example .env
# edit .env: ALPACA_KEY, ALPACA_SECRET
```

## Running slice 1

```bash
python -m hyx.slice1
```

First run backfills 5 years of DE OHLCV and ~4 years of DE news (bounded by
Alpaca news history starting 2021). Subsequent runs are incremental.

Outputs:
- `data/hyx.duckdb` — all operational state
- `reports/slice1/YYYY-MM-DD.{md,csv}` — per-run report
