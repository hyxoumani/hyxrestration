# hyxrestration

Prediction-market strategy-testing lab (`hyxlab/`). Archives Kalshi and
Polymarket market data into DuckDB, replays it through a no-lookahead
simulator, and kills strategies via pre-registered backtests. No capital
is deployed until a strategy passes a pre-registered test.

## Functionality

```
connectors (hyxlab/venues/*)  →  archive store (store.py, DuckDB)
        ↓ scheduled by systemd timers (collect 5min, sweep daily)
alignment (Context / FeatureView)   ← the only time-sensitive read path
        ↓
sim engine (sim.py) ←→ strategies (strategies/*)
        ↓
harness manifests (harness.py → data/runs/)  +  self-tests (tests/)
```

- **Data collection** — REST connectors for Kalshi, Polymarket, NWS, IEM,
  ALFRED, and Alpaca news (`hyxlab/venues/`), plus WebSocket stream
  daemons (`streamd.py`) that archive live order-book events and trades
  with gap-marking and reconnect/re-seed. A 5-minute collect timer and a
  daily exchange-wide sweep keep the DuckDB archive current;
  `qa.py` runs daily data-quality checks.
- **Replay** — `bookreplay.py` turns archived stream events into a
  millisecond-fidelity snapshot stream (gap-honest, complete-image
  emission).
- **Simulation** — `sim.py` is an event-loop simulator with full order
  lifecycle (open/close, GTC/IOC, cancels), a latency model, venue fee
  models (`fees.py`), and runtime invariants that enforce no-lookahead.
  Strategies see the market only through `Context`, which hides
  settlements and serves forecasts as-of.
- **Strategies** — subclass the `Strategy` ABC (`strategy.py`) and
  declare required data capabilities; backtests on data that can't
  exercise the strategy raise instead of silently returning zero.
  Verdicts and pre-registration rules: `docs/wiki/strategy-verdicts.md`.
- **Tier ladder** — candles (kill-only) → live book replay → shadow
  orders (`shadow.py` runs strategies live against the stream-archive
  tail, ledger-only). A strategy's credential is which tier it survived.
- **simui** — interactive market-replay terminal: archived event groups
  replay like a live Kalshi event page and you paper-trade against them;
  manual and strategy orders both fill through the real simulator.
- **Reproducibility** — every run writes a manifest (git rev, params,
  data fingerprint) via `harness.py`; schema changes go through numbered
  migrations (`migrate.py`).

## Setup

Python 3.11+ (3.14 on the dev box). Everything is network/IO-bound — no
GPU needed; the whole lab is portable to a Raspberry Pi.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install duckdb requests websockets cryptography python-dotenv pytest ruff
```

### Credentials

Public REST data (market discovery, candles, books) needs no account.
The Kalshi WebSocket stream requires an API key: put the key ID in
`.env` and the RSA private key under `.secrets/` (both gitignored):

```bash
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=.secrets/kalshi.pem
```

Without these, the Kalshi stream channels idle; everything else works.

## Usage

```bash
# Archive health check
.venv/bin/python -m hyxlab.sweep --doctor

# One collection cycle (normally run by the 5-min systemd timer)
.venv/bin/python -m hyxlab.collect --once

# Replay a pre-registered backtest
.venv/bin/python -m hyxlab.run_backtest

# Interactive market-replay UI (paper trading) → http://127.0.0.1:8877
.venv/bin/python -m hyxlab.simui

# Tests and lint
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check hyxlab tests
```

Scheduled operation uses systemd user units run from a `stable` worktree
deployed via `scripts/promote.sh`: `hyxlab-collect` (5 min),
`hyxlab-sweep` (daily), `hyxlab-qa` (daily), `hyxlab-stream` and
`hyxlab-shadow` (daemons). The DuckDB archive is single-writer — ad-hoc
runs may briefly wait on the collector's flock.

## Documentation

Start with [`docs/wiki/index.md`](docs/wiki/index.md) — architecture,
data pipeline, venue notes, simulation-honesty invariants, and strategy
verdicts. Design spec: `docs/plans/hyxlab-v2/proposal.md`.

## Historical: Phase 0

`phase0/` and `hyx/` are the closed record of a falsified thesis
(LLM-driven swing trading of US agricultural equities). Do not build on
them; the portable part is the methodology (pre-registration, BH-FDR,
honest nulls). See `docs/phase0_postmortem.md`.
