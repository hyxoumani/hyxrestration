# Phase 0 — Alpaca news depth check (§7.1)

**Run date:** 2026-04-22
**Current commit SHA:** (this commit)
**Pre-registration:** Tests pre-registered at 7d6ef52 (PRE_REGISTERED=true).
**Spec:** phase0_data_sources.md §7.1 — action item #1 before running Test 2+3.

## Summary

Alpaca News API delivers the coverage Phase 0 needs. One ticker (CNH) falls below
the §7.1 "< 5/month = NOISE" threshold and is excluded from the Test 2+3 regression
matrix per the §7.1 escape clause. Seven of the eight pre-registered regression
tickers are in the USABLE (5-20/mo) or STABLE (≥20/mo) buckets over the Test 2+3
pre-reg window (2021-2024).

## Alpaca auth

- Paper account: ACTIVE, $100k cash / $200k buying power.
- Rate limit observed: no 429s on 60+ pages of bulk pulls at default pacing.
- Endpoint: `data.alpaca.markets/v1beta1/news`, Benzinga-sourced, matches the
  schema in `phase0/news_loader.py`.

## Historical depth — corrected from assumption

**Our assumption (phase0_data_sources.md §3.2):** news starts ~January 2021.
**Measured:** news exists back to at least 2015-01. Alpaca's public docs were
accurate; our Phase 0 doc was conservatively wrong.

Year-by-year probe on a single ticker (DE) returned non-empty article batches for
every sampled year 2015-2021. The per-year totals below across all 10 ag tickers
confirm this at scale.

This is not currently blocking anything — Test 2+3's 2021-2024 window stands as
the pre-reg window — but it means an extended-window re-run (2015-2024) is a
future option without a data-sourcing obstacle.

## Per-year article totals across 10 ag tickers

| Year | Articles | Peak ticker |
|---|---:|---|
| 2015 | 432 | DE (134) |
| 2016 | 512 | DE (184) |
| 2017 | 428 | DE (148) |
| 2018 | 549 | DE (165) |
| 2019 | 594 | DE (208) |
| 2020 | 633 | DE (173) |
| 2021 | 740 | DE (191) |
| 2022 | 1079 | DE (311) |
| 2023 | 1047 | DE (244) |
| 2024 | 877 | DE (222) |

Coverage roughly doubles from 2015→2022. Benzinga has been expanding ag sector
coverage over the decade.

## §7.1 depth check — 2021-2024 pre-reg window

Per-ticker article counts over 48 months (multi-tag articles counted once per
tagged ticker — the aggregation decision documented in phase0_data_sources.md
§3.3 gotcha).

| Ticker | Total | Avg/mo | Bucket | In regression? |
|---|---:|---:|---|:--:|
| DE | 968 | 20.2 | **STABLE** | ✅ |
| MOS | 457 | 9.5 | USABLE | ✅ |
| ADM | 448 | 9.3 | USABLE | ✅ |
| CF | 354 | 7.4 | USABLE | ✅ |
| NTR | 342 | 7.1 | USABLE | ✅ |
| BG | 322 | 6.7 | USABLE | ✅ |
| AGCO | 300 | 6.2 | USABLE | ✅ |
| **CNH** | **27** | **0.6** | **NOISE** | ❌ (excluded) |

Total unique articles in window: 3,032.

### §7.1 go/no-go

§7.1 says:
> If more than ~3 of those 8 fall under 5/month, Test 2+3's statistical power
> is worse than assumed and the test design needs revision.

Measured: **1 of 8 regression tickers under 5/month** (CNH at 0.6). Below the
~3 threshold. Test 2+3 design does not need revision.

## CNH exclusion — not a pre-reg violation

§7.1 explicitly authorizes per-ticker exclusion based on depth-check outcome:
> Outcome determines how many of the 10 tickers actually participate in Test
> 2+3's regression matrix.

So the exclusion is a pre-registered operation, not a post-hoc adjustment.

**Effect on the equipment category:** (DE, AGCO, CNH) → (DE, AGCO). Equipment
loses one of its three tickers. DE dominates equipment-category sentiment by
volume anyway (968 articles vs AGCO's 300), so practical impact on the
category-level aggregate is small. CNH at 27 articles over 4 years would
contribute < 3% of equipment-category events even if included.

**Mechanism of exclusion:** CNH's missing sentiment flows through as NaN in
the event panel; `_run_one_regression`'s `.dropna(subset=[..., "event_sentiment"])`
silently drops those rows. No code edit to `TICKER_CATEGORIES` is required —
the panel-construction path handles it. Documenting the expected behavior here
for audit trail.

## Processors category notes

Processors = (ADM, BG) — only two tickers in the pre-reg design. Both are in
the USABLE bucket. No change.

## Other observations worth flagging

1. **CTVA** (tracked but not in the regression matrix): 540 articles in
   2021-2024 window avg 11/mo. Would be a viable addition to the matrix if
   we later revise §2.3's ticker-category assignments. Not doing that now —
   would be a pre-reg violation requiring the marker.

2. **Multi-tag articles are common.** Total tagged-ticker count (sum of
   per-ticker counts) will exceed total unique articles — standard for
   symbol-tagged news feeds. phase0_data_sources.md §3.3 pre-registered the
   "count once per tagged ticker" aggregation.

3. **Same-day freshness lag.** A probe for "DE news in the last 5 days"
   returned 0 articles on 2026-04-22 (the run date). This is consistent with
   Alpaca's free-tier "end default = current time minus 15 minutes" for
   real-time access, and our query spanned several days. Not material for
   Test 2+3 which regresses 2021-2024 historical data only.

## Implications for Test 2+3 iter 3

- Proceed as pre-registered in §2.3 / §2.4 with CNH dropped via §7.1.
- Expected sample size: ~3,000 sentiment-scored articles → ~15k event-ticker-
  horizon rows in the panel, similar to iter 1's synthetic runs. Regression
  cells will have n = 150-400 depending on direction tercile + horizon split.
- Sentiment coverage rate at any given WASDE event will be ~30-60% depending
  on ticker (4-day window × per-ticker density). Missing-sentiment events
  drop from the regression, not fail it.
