# Venues: Kalshi & Polymarket

Two separate markets by design (user-confirmed principle). Cross-venue
strategies are explicit consumers of both, never a blurring.

## Book structure (load-bearing difference)

- **Kalshi = one mirrored book per market.** Buying NO *is* selling YES:
  `no_ask ≡ 1 − yes_bid` (verified: 0 violations across all live data).
  Consequence: YES+NO<$1 intramarket arb is **impossible by venue design**.
- **Polymarket = independent YES/NO token books** (separate CLOB token
  ids). Intramarket rebalance arb is possible there only.

## Data availability

| Data | Kalshi | Polymarket |
|---|---|---|
| Quote history | hourly candles w/ bid+ask OHLC, **~60–90d retention then purged** | none (last-price series only) |
| Trade tape | REST endpoint, same retention | data API + **permanent on-chain (Polygon)** |
| Book depth/deltas | live WS only, never historical | live WS only, never historical |
| Order-level flow | never exposed | never exposed |

**Self-archiving is the moat**: old Kalshi events survive as empty shells;
markets/candles are unrecoverable. Streams are unrecoverable everywhere.

## APIs & limits (verified 2026-07-06/07)

- Kalshi REST `api.elections.kalshi.com/trade-api/v2`: public reads, no
  auth. `/series` returns all ~11,170 series in ONE response (category,
  fee_type, fee_multiplier). Candlesticks endpoint 429s hard: ~2 rps safe
  with Retry-After backoff (documented 30 rps does not hold).
- Kalshi WS `.../trade-api/ws/v2`: **auth required** — RSA-PSS(SHA256)
  over `ts_ms + "GET" + path`, headers KALSHI-ACCESS-{KEY,TIMESTAMP,
  SIGNATURE}. Creds: `.env` + `.secrets/kalshi.pem`. `trade` channel =
  exchange-wide firehose (~105 ev/s); book/ticker channels need
  market_tickers.
- Polymarket WS `ws-subscriptions-clob.polymarket.com/ws/market`: no
  auth; send `{"type":"market","assets_ids":[...]}`; initial `book` per
  token then `price_change` deltas; ~5 connections/IP.
- Polymarket Gamma/CLOB REST: public; Gamma has market-level `volume`.
  **Gamma `/markets` rejects offset > 2000** (appeared 2026-07-08,
  hours after that day's sweep enumerated 4,200 markets — would have
  silently halved the next sweep). Deep listing goes through
  `/markets/keyset`: chain `after_cursor` ← response `next_cursor`,
  same order/filter params plus server-side `volume_num_min`
  (`iter_markets_by_volume` does this; params from
  gamma-api.polymarket.com/openapi.json).

## Fees (verified against schedules + /series metadata)

Both parabolic: `factor × count × P × (1−P)`, peak at 50¢.
- Kalshi taker 0.07 (ceil-to-cent on total). Makers **FREE** on
  `quadratic` series (11,040/11,170); ¼-taker on
  `quadratic_with_maker_fees` (130). Resolve via `fees.kalshi_model()`.
- Polymarket US: taker 0.05, maker REBATE −0.0125.
- Fee wall: taker-taker cross-venue near 50¢ ≈ 3¢/share — kills naive arb.

## Other data sources

- **IEM** (mesonet.agron.iastate.edu): archived NWS climate reports
  (settlement truth) + MOS forecasts as-issued, decades deep.
- **ALFRED**: point-in-time econ vintages, keyless via
  `alfredgraph.csv?id=X&vintage_date=Y` (`vd=` is silently ignored!).
- **GDELT**: 1 req/5s hard limit, throttles via **empty {} with HTTP
  200**; bulk 15-min GKG files are the high-volume path (filter-and-
  discard, ~480MB/day raw).
- **Alpaca news**: Benzinga wire back to 2016, creds in `.env`.

## Related
- [data-pipeline](data-pipeline.md) — how these get archived
- [simulation-honesty](simulation-honesty.md) — mirror theorem as a gate
