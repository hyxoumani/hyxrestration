# simui — interactive market-replay simulator (paper-trading UI)

Status: building (2026-07-08 session). Data tier: stream archive
(Kalshi ms-fidelity books) first; candle tier and Polymarket later.

## What it is

A local, Kalshi-style web UI where an archived market **replays as if
live**: the order book ladder ticks, the trade tape scrolls, the price
chart draws — and the user (plus optionally strategies) can buy/sell
into the replay. All fills come from the real `Simulator` (latency
model, accounting invariants, capability guard), so what the profile
shows is exactly what a backtest would have scored. This grows into the
planned "debug frontend" slot (decision replay is a session with a
strategy attached).

## Architecture

```
hyxstream.duckdb ──(read-only, load once per session)──┐
hyxlab.duckdb  ──(MarketInfo, sanitized: result="")──┐ │
                                                     ▼ ▼
             ReplaySession (hyxlab/simui/session.py)
             ├─ events/trades/gaps for ONE event group, in memory
             ├─ BookReplayer (persistent; full-depth via .depth())
             ├─ Simulator(latency=Δ) ── ManualTrader("you") + strategies
             └─ advance(to_ts) → frame  |  seek(ts) → re-seed, flat sim
                                ▼
             WS server (hyxlab/simui/server.py, websockets 16)
             ├─ owns the replay clock (wall tick × speed)
             ├─ GET /  → static/index.html   (127.0.0.1 only)
             └─ /ws: frames out (~8 Hz); play/pause/speed/seek/
                order.place/order.cancel in
                                ▼
             single-file UI (static/index.html, vanilla JS)
             ladder · chart · tape · order ticket · profile · transport
```

## Honesty rules (inherited, not new)

- User orders enter through a `ManualTrader` Strategy whose queue is
  drained by `sim.step()` — same latency + fill path as any strategy;
  the decision-time quote is never fillable.
- `MarketInfo.result` is blanked in session state: many captured
  markets have since settled, and showing the result would leak the
  answer to the human trading the replay. Sessions end at end-of-data,
  positions marked to last mid (no settlement payout inside a session).
- Seek re-seeds book state from history WITHOUT stepping the sim
  (shadow's anchor logic): you can't carry a portfolio backwards in
  time; seeking resets it flat.
- Books unknown until first full snapshot image / after any
  stream_gap — `replay_snapshots` semantics unchanged.

## Non-goals (v1)

Polymarket replay (BookReplayer is Kalshi-only), candle-tier replay,
persistence of session portfolios, rewind-with-portfolio, multi-user.
