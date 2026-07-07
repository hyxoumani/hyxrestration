"""hyxlab — strategy-testing lab for prediction markets.

Backbone for testing prediction-market strategies against recorded data
before any capital decision. Three layers:

1. **Collect** (`hyxlab.collect`): polls public, auth-free endpoints —
   Kalshi markets (top-of-book included), Polymarket CLOB books, NWS
   forecasts — into a DuckDB file. Run it for days/weeks to build the
   dataset strategies replay against.

2. **Simulate** (`hyxlab.sim`): replays stored snapshots in time order
   through `Strategy` objects, fills orders against displayed quotes with
   verified venue fee models (`hyxlab.fees`), and settles positions at
   market resolution. No lookahead: strategies only see snapshots and
   forecasts fetched before the current timestamp.

3. **Strategies** (`hyxlab.strategies`): baselines under test —
   intramarket YES+NO rebalancing, cross-venue arb, NWS-based weather
   trading. Each is a candidate to be falsified, not a recommendation.

Known v1 simplifications (documented, revisit before trusting results):
- Snapshot-based, not full order flow. Fill model is optimistic for
  takers (assumes displayed size is still there) and conservative for
  makers (fills only when the opposite touch strictly crosses the limit).
- Buy-only orders (buying NO expresses short-YES); positions are held to
  settlement. Market-making strategies need a richer engine — v2.

Usage:
    python -m hyxlab.collect --once          # one collection cycle
    python -m hyxlab.collect --interval 300  # poll every 5 minutes
    python -m hyxlab.run_sim                 # replay stored data through baselines
"""
