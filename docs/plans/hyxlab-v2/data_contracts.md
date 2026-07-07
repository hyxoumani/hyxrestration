# Data-contract validation — live probe results (2026-07-06)

Every planned v2 source probed live before build; response shapes captured
as fixtures in `tests/fixtures/` and pinned by `tests/test_hyxlab_connectors.py`
(55-test suite green). Prototype parsers exist for the two new sources
(`hyxlab/venues/alfred.py`, `hyxlab/venues/alpaca_news.py`).

## Verified per source

| Source | Verdict | Shape / depth verified | Quirks found |
|---|---|---|---|
| Kalshi `/series` | ✅ | **All 11,170 series in one unpaginated response**; fields: ticker, category, fee_type, fee_multiplier, frequency, settlement_sources, tags | Categories: Entertainment 2,462, Sports 2,295, Politics 2,021, Elections 1,422, Financials 608, Economics 589, Weather 286, … Fee fields matter (below) |
| Kalshi settled markets | ✅ | Global feed pages fine, but first 1,000 of last-48h settled = **one esports parlay series** | Sweep must enumerate per category/series, not page the global feed |
| Kalshi candlesticks | ✅ (earlier today) | price + yes_bid/yes_ask OHLC hourly; settled markets within retention | Endpoint rate limit ≪ documented 30 rps (429s; empirical ~2 rps safe with backoff) |
| ALFRED keyless CSV | ✅ | `alfredgraph.csv?id=CPIAUCSL&vintage_date=2024-01-15` → series **as of that date** (ends 2023-12, pre-release of Jan data ✓) | `vd=` param silently ignored — must be `vintage_date=`. Keyless = 1 request/vintage; free FRED key upgrade optional, not blocking |
| Alpaca news | ✅ | **Depth to 2016-01-01** with existing .env creds; fields: created_at, updated_at, headline, summary, content, symbols, source, url; next_page_token pagination | knowable_at = created_at (wire time) |
| GDELT DOC API | ⚠️ usable, hostile | — | Hard limit **1 request / 5 s** per IP with lingering cooldown; empty `{}` responses when throttled (not an error code!) |
| GDELT bulk files | ✅ | `data.gdeltproject.org/gdeltv2/lastupdate.txt` → 15-min export/mentions/gkg CSV zips, no rate limit | GKG ≈ 5 MB zipped / 15 min ⇒ ~480 MB/day: **filter-and-discard** ingestion (grep themes, keep matches), never archive raw |

## Design corrections fed back into the proposal

1. **Kalshi maker fees were wrong in our model.** `/series` exposes
   `fee_type`: 11,040 series are `quadratic` (makers pay **zero**) vs 130
   `quadratic_with_maker_fees`; `fee_multiplier` is 1 except 13 fee-free
   series. Our `KALSHI` FeeModel charges makers 0.0175 everywhere —
   pessimistic for most series. Fix: per-series fee resolution from series
   metadata (C5/fees). *Today's weather FAIL is unaffected: taker-only.*
2. **Sweep enumeration: category allowlist, not denylist.** Launch set:
   Economics, Financials, Climate and Weather, Companies, Commodities,
   Science and Technology, Health, World (~2,240 series) — excludes the
   ~8,200 sports/entertainment/politics series that dominate settle volume.
   Revisitable; the series list is one cheap call.
3. **GDELT connector redesigned**: DOC API only for narrow daily topic
   queries on a dedicated ≥5s-spaced lane, treating empty `{}` as throttle
   (retry with cooldown); bulk GKG files as the high-volume path with
   filter-and-discard. Tone comes from GKG (the artlist mode doesn't
   carry it — proposal's NewsItem.tone mapping corrected).
4. **ALFRED knowable_at**: vintage dates are date-granular ⇒ stamp
   pessimistic 23:59 ET (never lets a backtest see data early); tighten
   later via the release calendar if release-time precision starts to
   matter.

## Fixtures (committed, network-free tests)

- `kalshi_series.json` — one Economics + one Weather series object
- `kalshi_market_settled.json` — settled KXHIGHNY market incl. result
- `alfred_cpiaucsl_20240115.csv` — point-in-time vintage sample
- `alpaca_news.json` — two 2025 items (content truncated)
- GDELT artlist fixture **pending** first successful pull (throttled today);
  bulk-path `lastupdate.txt` format documented above.
