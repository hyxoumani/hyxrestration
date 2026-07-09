"""hyxlab — shared kernel for the prediction-market strategy lab.

The lab is split into four top-level packages with a test-enforced
import boundary (tests/test_boundaries.py):

- `collector/` — data capture: venue connectors, sweeps, stream
  daemons, QA. Runs unattended 24/7 from the stable worktree; its
  failure loses unrecoverable data.
- `simulator/` — replay + simulation: sim engine, Strategy ABC,
  capability contracts, book replay, shadow harness, simui.
- `strategies/` — strategies under test. Each is a candidate to be
  falsified, not a recommendation. May import simulator + kernel.
- `hyxlab/` (this package) — the shared kernel both sides may import:
  typed records (`models`), DuckDB archive (`store`), stream archive
  (`streamstore`), fee models (`fees`), migrations (`migrate`),
  watchlist, and station metadata.

Neither collector nor simulator may import the other; both go through
this kernel only.

Usage:
    python -m collector.collect --once   # one collection cycle
    python -m collector.sweep --doctor   # archive health
    python -m simulator.run_backtest     # pre-registered backtest replay
    python -m simulator.simui            # market-replay UI (localhost:8877)
"""
