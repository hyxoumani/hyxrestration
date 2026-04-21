# Phase 0 — Data sources

Concrete data sourcing for the tests defined in [`phase0_testing.md`](phase0_testing.md). All sources free-tier unless explicitly flagged; hard budget = $0 per architecture.md §12.

---

## 1. Summary

| Test | Data needed | Primary source | Cost | Time to ready |
|---|---|---|---|---|
| (prereq) | **Alpaca paper account credentials** | Alpaca signup | $0 | 15 min — **blocks §7.1** |
| Test 1 | Daily OHLC 10 equities + SPY, 2014–2024 | `yfinance` | $0 | 30 min |
| Test 2+3 | Prices | Reuse Test 1 pull | $0 | 0 |
| Test 2+3 | News headlines 2021–2025 | Alpaca news endpoint | $0 | 1 evening |
| Test 2+3 | FinBERT sentiment scores | `yiyanghkust/finbert-tone` | $0 | < 1 hour inference |
| Test 2+3 | WASDE release values | USDA archive (CSV + PDF) | $0 | half day |
| Test 2+3 | WASDE consensus estimates | **See §5 — hardest problem** | $0 via proxy or Farmdoc; paid otherwise | 1 day (proxy) to indeterminate (full) |

Total Phase 0 data-acquisition budget: ≤ 3 days of setup before any test runs. If acquisition exceeds that, the constraint is being violated.

**Provisioning order is not optional.** Alpaca credentials come before §7.1 (news depth check), which gates whether Test 2+3 is buildable. See §7.0.

---

## 2. Test 1 data — prices only

### 2.1 Source

`yfinance` via pip. **10 equities + SPY only.** Commodity ETFs (DBA/CORN/WEAT/SOYB) are not pulled for Phase 0 — they are excluded from both Test 1 (per `phase0_testing.md` §1.3) and Test 2+3 (§2.3), so pulling them adds storage with no consumer.

```python
import yfinance as yf
tickers = ["NTR","MOS","CF","CTVA","FMC","ADM","BG","DE","AGCO","CNHI","SPY"]
df = yf.download(tickers, start="2014-01-01", end="2024-12-31",
                 auto_adjust=False)  # keep raw + adj close both
```

~20 lines including retry + storage. Returns daily OHLC + Adj Close per ticker.

### 2.2 Known gotchas

- **CTVA didn't exist pre-June 2019** (spun from DowDuPont). yfinance returns NaN for pre-spin dates — correct behavior. Backtest holds cash for CTVA's slot when NaN.
- **CNHI restructured January 2024** (spun Iveco). Continuation ticker handled automatically; adjusted close around the split needs an eyeball sanity check.
- **Use adjusted close for returns.** Dividends and splits matter over 10-year windows. Raw close only for intraday mechanics you're not modeling.
- **Point-in-time universe.** The 10 equities were chosen in 2026. Pretending they were chosen in 2014 is survivorship bias in disguise. Document that selection is ex-post and report sensitivity to dropping the 2 most ex-post-obvious picks.

### 2.3 Backup

Alpaca `get_bars` via `alpaca-py` returns equivalent data if you want single-provider consistency with slice 1+. Free tier sufficient for daily bars.

---

## 3. Alpaca news data

### 3.1 Source

Alpaca paper account (free) + `alpaca-py` SDK. Same credentials that slice 1 will use — zero incremental setup cost.

```python
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from datetime import datetime

client = NewsClient(api_key=..., secret_key=...)
req = NewsRequest(
    symbols=["NTR","MOS","CF","CTVA","FMC","ADM","BG","DE","AGCO","CNHI"],
    start=datetime(2021, 1, 1),
    end=datetime(2025, 12, 31),
    limit=50,  # max per page
)
for page in client.get_news(req):
    ...  # paginate via next_page_token
```

### 3.2 Coverage and limits

- **History starts ~January 2021.** This truncates Test 2+3's sample vs Test 1's 2014–2024 window. Acknowledged in phase0_testing.md §2.3.
- **Source is Benzinga.** Widely consumed, not proprietary. Coverage on major names (NTR, DE, ADM) is dense; on smaller names (AGCO, CNHI, FMC) noticeably thinner.
- **Rate limit: 200 req/min on free tier.** 4 years × 10 tickers with 50/page pagination is well under a day of walltime including backoff.
- **No article body.** Headlines + summary only. This is what FinBERT operates on anyway.

### 3.3 Known gotchas

- **`symbols` field is a list.** A single article can tag NTR *and* MOS *and* CF. Decision required upfront: count once per tagged ticker (my recommendation) vs count once total and distribute weight. Document the choice in `data/README.md`; don't silently switch between runs.
- **Ticker-level headline-count heterogeneity.** Eyeball per-ticker monthly counts before regression. If AGCO averages <5 headlines/month, sentiment aggregation on it is unstable and its row should be flagged or dropped from the test. Running this diagnostic is the first action item before committing to Test 2+3 (see §7.1).
- **Weekend/holiday news.** News exists when markets are closed. Standard fix: aggregate news from `[last_close, next_open]` onto the next trading day.
- **De-duplication.** Benzinga occasionally republishes corrected versions. Primary key on `news_id` handles this; `INSERT OR IGNORE` semantics.

### 3.4 Storage

One CSV, one row per `(news_id, tagged_ticker)`. Fields: `news_id, ticker, timestamp, headline, summary, source`. Typical size: 40k–150k rows across 10 tickers × 4 years. Fits in RAM; pandas direct.

---

## 4. FinBERT inference

### 4.1 Setup

Model: `yiyanghkust/finbert-tone` via HuggingFace `transformers`.

```python
from transformers import pipeline
clf = pipeline("sentiment-analysis",
               model="yiyanghkust/finbert-tone",
               device=0)  # RTX 5090
results = clf(headlines, batch_size=64)
```

### 4.2 Cost

- **One-time download:** ~440MB to `~/.cache/huggingface/`.
- **Inference:** ~1000 headlines/sec batched on RTX 5090. Full corpus (~150k headlines) runs in <5 minutes.

### 4.3 Output

Per headline: softmax over `{positive, negative, neutral}`. Store as three columns + argmax label in `finbert_scores.csv` keyed by `news_id`. Per-ticker daily aggregate computed on demand in the notebook.

### 4.4 Known gotchas

- **FinBERT was trained on financial text pre-2020.** Calibration on 2021+ ag-specific headlines is unverified. A null result on Test 2+3 could be FinBERT's fault, not sentiment's fault. Architecture.md already anticipates this — slice 5's Qwen zero-shot A/B addresses it later. For Phase 0, FinBERT is "good enough to falsify" not "definitive."

---

## 5. WASDE data — the hard part

Two separate acquisition problems. The release values are easy; the pre-release consensus is the actual blocker.

### 5.1 Release values (easy)

**Source A: USDA Office of the Chief Economist.**
- `usda.gov/oce/commodity-markets` hosts current and historical WASDE reports.
- CSV data tables back to ~2010 are available via direct download.
- Free, no auth.

**Source B: Cornell Mann Library.**
- `usda.library.cornell.edu` hosts every historical WASDE report as PDF + some structured data.
- Free, scrapeable.

**What to extract per release:** date, crop (corn/soy/wheat), line item (production, ending stocks, yield), value. ~144 releases 2014–2025 × 3 crops × 3 line items = ~1300 rows. Trivial to store.

**Time to ready:** half day including writing the PDF/CSV parser.

### 5.2 Release dates (easy)

USDA publishes the release calendar at `usda.gov/oce/commodity/wasde/schedule`. Scrapeable. Archive of past release dates is preserved.

### 5.3 Consensus estimates (the hard part)

This is the data required to define "surprise" = (report value − consensus).

**What exists and what it costs:**

| Source | Coverage | Cost | Viability |
|---|---|---|---|
| Reuters pre-release survey | Full history, all major crops | Reuters Connect API: paid, $$$ | ❌ Over budget |
| Bloomberg survey | Full history | Terminal only: $$$$ | ❌ Over budget |
| Dow Jones / WSJ archives | Spotty, narrative form | Scrapeable but labor-intensive | ❌ Not worth the effort |
| **Farmdoc (U. Illinois)** | **Most WASDEs from ~2012, major crops** | **Free, scrapeable** | ✅ **Primary free option** |
| Trend-residual proxy | Full history (derived) | Free, compute-only | ✅ Fallback |
| Academic datasets | Partial periods | Free if found | ⚠️ Hit-or-miss |
| AgTwit (Karen Braun et al.) | ~2018+, partial | Free, labor-intensive | ⚠️ Brittle |

**Recommended approach — two-stage:**

1. **Start with trend-residual proxy for all 144 releases.** Define surprise per (crop, line item) as:
   ```
   surprise = current_value - trailing_12_month_mean(same_line_item_same_crop)
   ```
   Normalize by trailing std. Document this is a proxy with known seasonal-pattern bias. Run Test 2+3 on this surprise definition.

2. **Conditional on result:**
   - Test **passes with proxy** → invest the effort to source real consensus (Farmdoc scrape) for a robustness check. If the result holds on real consensus, it's defensible.
   - Test **fails with proxy** → you've either falsified the thesis or used a proxy too noisy to detect signal. Spot-check 10 releases with Farmdoc consensus; if Farmdoc consensus and trend-residual agree on surprise direction, the proxy wasn't the problem and the thesis is falsified. If they disagree substantially, redo the test with Farmdoc.

This sequencing minimizes wasted effort. You only pay the Farmdoc scrape cost if the signal exists at all.

### 5.4 Farmdoc scrape notes

- `farmdocdaily.illinois.edu` — University of Illinois agricultural economics publication.
- Pre-WASDE analysis posts typically titled with the month and include a table of analyst-range estimates (low / mean / high / USDA prior).
- Coverage is consistent for corn and soybeans from ~2012; wheat coverage thinner.
- HTML structure changes periodically over 10+ years — expect per-era scrape logic.
- Estimated effort: 1–2 days of scraping + manual verification for the full historical set.

### 5.5 Storage

One CSV, one row per `(release_date, crop, line_item)`:
- `release_date, crop, line_item, value_reported, consensus_value, consensus_source, surprise_magnitude, surprise_direction`
- `consensus_source` is `"farmdoc" | "trend_proxy" | "manual"` — critical for sensitivity analysis.

---

## 6. Storage layout

Keep primitive. Phase 0 is not infrastructure.

```
phase0/
├── data/
│   ├── prices.csv              # yfinance dump
│   ├── alpaca_news.csv         # one row per (news_id, tagged_ticker)
│   ├── finbert_scores.csv      # news_id → {pos, neg, neu, label}
│   ├── wasde_releases.csv      # date, crop, line, value, consensus, source
│   └── README.md               # per-file provenance + caveats
├── notebooks/
│   ├── 00_data_loaders.py      # shared loader functions
│   ├── test1_naive_baseline.ipynb
│   └── test23_wasde_sentiment.ipynb
└── results/
    ├── test1_YYYY-MM-DD.md
    └── test23_YYYY-MM-DD.md
```

**No DuckDB at this stage.** CSV + pandas. If any file grows past 1 GB (it won't for this scope), move that file to Parquet. DuckDB-as-primary applies to hyxrestration, not to throwaway research code.

**No cross-imports with `hyx/`.** Phase 0 code does not import from `hyx/`, and `hyx/` will not import from `phase0/`. Restated from phase0_testing.md §5 because the violation is the common failure mode.

### 6.1 `data/README.md` is discipline, not documentation theater

For each CSV, one short paragraph answering:

- **Source:** where it came from (URL, API, date pulled).
- **Coverage:** date range, known gaps.
- **Caveats:** known bias, proxy decisions, ticker-level coverage issues.
- **Refresh procedure:** what command to re-run to update.

Example entry:

> **`alpaca_news.csv`** — Pulled 2026-04-23 from Alpaca news endpoint via `alpaca-py` v0.x. Coverage: 2021-01-01 to pull date, 10 ag equities. Known: headline count per month ranges from ~40 (NTR) to ~4 (AGCO) — AGCO and CNHI rows flagged in test23 notebook as underpowered. Multi-ticker articles counted once per tagged ticker. De-duped on `news_id`. Refresh: `python phase0/notebooks/00_data_loaders.py --refresh news`.

Future-you debugging test results needs this. Every time this discipline has been skipped on past projects, debugging a failed test has cost more time than the README would have taken.

---

## 7. Pre-test data diagnostics (action items before committing)

Three checks. §7.0 blocks §7.1. Together they take less than 3 hours and either confirm the tests are buildable or reveal blockers before time is sunk.

### 7.0 Alpaca paper account provisioning (blocks §7.1)

**Status: not yet provisioned.** This is the first action item in Phase 0 — nothing in §7.1 or §3 can run without it.

Steps:

1. Sign up at `alpaca.markets` for a paper trading account (free, no funding required, ~10 min).
2. Generate API key + secret under "Paper Trading → API Keys."
3. Store credentials in `~/.config/hyxrestration/alpaca.env` (not in repo). Minimum required env vars: `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`.
4. Smoke test: `alpaca-py` SDK `TradingClient.get_account()` returns 200. If this fails, resolve before proceeding — everything downstream assumes these credentials work.

Same credentials will be reused by slice 1+ per architecture.md, so this is zero-waste setup.

### 7.1 Alpaca news depth check

Pull one month of recent Alpaca news for all 10 ag equities. Per-ticker monthly headline counts:

- **≥ 20/month:** stable for sentiment aggregation.
- **5–20/month:** usable but high variance; flag in test.
- **< 5/month:** sentiment on this ticker is noise, drop or flag.

Outcome determines how many of the 10 tickers actually participate in Test 2+3's regression matrix. Test 2+3 regresses on 8 of the 10 (fertilizer NTR/MOS/CF, equipment DE/AGCO/CNHI, processors ADM/BG). If more than ~3 of those 8 fall under 5/month, Test 2+3's statistical power is worse than assumed and the test design needs revision.

### 7.2 WASDE consensus feasibility check

Pick one recent WASDE (e.g., May 2025) and attempt to source consensus estimates for corn production and ending stocks from Farmdoc. Outcome:

- **Found, clean table:** template exists for full backfill. Budget 1–2 days for historical scrape.
- **Found, messy:** usable but effort-heavier than hoped. Start with trend-proxy, backfill Farmdoc only if test passes.
- **Not found:** commit to trend-proxy as primary surprise definition; document this in §5.3's pre-registered choice.

Result of this check gets committed to the Phase 0 doc's §6 open items before test code is written.

---

## 8. Budget check

All sources listed above are free. No paid data required for Phase 0 as designed.

If Farmdoc scrape turns out infeasible *and* the trend-residual proxy produces an ambiguous result, the decision to source paid consensus data (Reuters Connect or equivalent) requires user authorization per architecture.md §12 — this would be a hard-budget revision from $0, with concrete ROI argument attached.

Default assumption until evidence otherwise: Phase 0 runs to completion on $0.

---

## References

- Test definitions: [`phase0_testing.md`](phase0_testing.md)
- Parent architecture: [`architecture.md`](architecture.md)
- Decision index: [`decisions.md`](decisions.md)
