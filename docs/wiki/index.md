# Project Wiki

Knowledge base maintained by the context-keeper. Pages are synthesized from
session findings, reviewer feedback, experiments, and architectural decisions.

**Start here → [Status & next steps](status.md)** (living page: build
state, session findings, execution queue).

## Current focus: hyxlab (prediction-market strategy lab)

- [hyxlab architecture](hyxlab-architecture.md) — layers, module map, build state, tier ladder
- [Data pipeline](data-pipeline.md) — DuckDB archive, timers/sweep ops, provenance, gotchas
- [Venues: Kalshi & Polymarket](venues.md) — book structures, APIs/limits, fees, retention, signal sources
- [Simulation honesty](simulation-honesty.md) — no-lookahead, fill biases, invariants, correctness gates
- [Strategy verdicts & queue](strategy-verdicts.md) — dead/rejected/queued strategies, pre-reg rules

## Process

- [Mistakes log](mistakes.md) — root-caused failures with escalation tiers

## Historical (falsified L01 thesis — do not build on)

- `docs/phase0_postmortem.md`, `docs/architecture.md` — Phase 0 record;
  the methodology (pre-reg, BH-FDR, honest nulls) is the portable part.
