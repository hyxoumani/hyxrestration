---
PRE_REGISTERED: true
---

# Phase 0 — Edge discovery tests

Pre-slice-1 tests that must run before committing to the slice 1–9 build in [`architecture.md`](architecture.md). Purpose: determine whether the L01 edge thesis has any empirical basis *before* building infrastructure around it.

**Status of L01 as of 2026-04-21:** all three components (small under-covered universe, cross-modal LLM-specific integration, time-horizon arbitrage) soften under base-rate scrutiny. The 14-ticker large-cap ag universe is not under-covered, 1–5 day swing is not an institutional gap, and the aggregation step is deterministic so the LLM-specific claim lives only in the sentiment agent. L01 needs empirical support or replacement.

---

## 0. Philosophy

**Kill before building.** Each test is a falsification attempt with a pass criterion written in advance. If a test fails, the corresponding architectural component is deleted or demoted, not defended.

**Pre-registration is load-bearing.** Pass criteria are committed to a git commit *before* any result is inspected. Moving the goalposts after seeing results is the failure mode Phase 0 exists to prevent. If the criterion needs revision after results come in, the revised test is a new test, not the original test with a softer bar.

### 0.1 Pre-registration mechanics

A markdown file is editable notes until it is explicitly locked. The lock is a git discipline, not a technical constraint — the friction has to be visible, not impossible.

**Locking procedure.**

1. Revise this document until the thresholds in §1.4 and §2.4 reflect final intent.
2. Flip the frontmatter flag `PRE_REGISTERED: false → true` in a dedicated commit with message `phase0: lock pre-registration`. No other changes in that commit.
3. From that commit forward, any edit to §1.4, §2.4, the regression structure in §2.3, or the decision rubric in §4 is a **pre-registration violation** and must be committed with `[pre-registration-violation]` in the commit subject and a paragraph in the commit body explaining what changed and why. Violations are permitted — they are not hidden.
4. Results documents (`phase0/results/*.md`) must cite the locking commit SHA, so any post-hoc threshold drift is auditable from the result alone.

This is soft pre-registration by design. The point is to make moving goalposts expensive enough that you notice yourself doing it.

**Budget.** Test 1: one afternoon. Combined Test 2+3: two weekends. Total Phase 0 commitment: ≤ 3 weekends. If the tests keep extending past budget, that *is* the signal — planning momentum without commitment.

**No Phase 0 slippage into hyxrestration code.** Phase 0 lives in a separate directory (`phase0/`), uses pandas + scikit-learn + vanilla Python, writes to CSVs, not DuckDB. Its output is a decision (continue / pivot / kill), not reusable infrastructure. This is non-negotiable — Phase 0 code is not allowed to become "slice 0."

---

## 1. Test 1 — Naive baseline on the universe

### 1.1 Purpose

Establish the denominator. Every subsequent alpha test's magnitude is only interpretable against how much return a $0-effort active strategy already extracts from the universe. Without this, positive results from other tests are unanchored.

### 1.2 Hypothesis

Equal-weight portfolio of the top-5 trailing-6-month momentum names in the 10-equity ag universe, rebalanced monthly at 20% per name, produces OOS Sharpe ≥ SPY Sharpe + 0.2 over 2014–2024, with positive excess return in ≥ 3 of 4 regime sub-periods.

### 1.3 Design

- Data: daily adjusted OHLCV for 10 equities + SPY, 2014–2024 (yfinance, free).
- Universe: **10 ag equities only** — NTR, MOS, CF, CTVA, FMC, ADM, BG, DE, AGCO, CNH. (CNH is the post-2024 continuation ticker for CNHI; yfinance carries restated 2013→2024 adj-close under CNH only.) Commodity ETFs (DBA/CORN/WEAT/SOYB) are explicitly excluded so Test 1 remains composable with Test 2+3, which also excludes them as first-order / fully-arbitraged (see §2.3). Tickers that didn't exist in a given period (CTVA pre-June-2019 spin) are held as cash for that period — no survivorship pretending.
- Signal: trailing 126-trading-day return per ticker, computed on the month-end close.
- Portfolio: hold top-5 at **20% per name** for the next month, matching the T02 concentration cap from `decisions.md` so Test 1's result is directly interpretable under the production risk constraint. Rebalance on first trading day of the month at that day's close.
- Costs: 5 bps per side per rebalance (conservative for large-cap ag).
- Benchmark: SPY buy-and-hold over the same period, same cost model on initial buy only.
- Regimes: 2014–2016 (commodity bust), 2017–2019 (stable), 2020–2021 (pandemic + inflation), 2022–2024 (rate cycle + Ukraine).

~80 lines of pandas. No ML, no text processing.

### 1.4 Pass criterion (pre-registered)

Both must hold:

1. Full-period annualized Sharpe ≥ SPY Sharpe + 0.2.
2. Positive excess CAGR vs SPY in ≥ 3 of 4 regime sub-periods.

### 1.5 Outcome interpretation

| Outcome | Implication for hyxrestration |
|---|---|
| **Strong pass** (Sharpe + 0.3 or more, 4/4 regimes) | Passive ag momentum is the edge. A monthly rebalance rule captures most of what hyxrestration is trying to capture. Architecture is over-engineered — seriously consider shipping a ~200-line monthly rebalancer as the actual "system" and reframing the complex build as a learning project only. |
| **Marginal pass** | Ag universe has capturable structure; LLM system needs to deliver edge *above* what momentum already gives. Quantified target for what slices 1–9 must add. |
| **Fail** | No inherent structural tilt in this universe. All alpha must come from active security selection. Raises the bar for everything else and is a strong signal the universe is wrong. |

### 1.6 Deliverable

`phase0/notebooks/test1_naive_baseline.ipynb` — one notebook, reproducible from raw download. Results table + equity curve + regime breakdown committed to `phase0/results/test1_YYYY-MM-DD.md`, with the pre-registration locking commit SHA cited in the results header.

---

## 2. Test 2+3 — WASDE-conditional sentiment (combined)

### 2.1 Purpose

Directly test the L01 thesis as originally written: does multi-source information integration (WASDE economic surprise + FinBERT news sentiment) predict forward returns on downstream ag equities at swing horizons? This is the only test that falsifies L01 rather than testing peripheral components.

The combined design replaces the originally separate Test 2 (sentiment alone) and Test 3 (WASDE alone). Combining is methodologically stronger — event windows concentrate signal, and the conditional structure matches the actual claimed mechanism (institutional silos between commodity and equity desks).

### 2.2 Hypothesis

FinBERT sentiment on news about ticker T in the `[D-1, D+2]` window around a WASDE release date D **interacts with** WASDE surprise to predict market-beta-adjusted returns on T over `[D, D+5]` and `[D, D+10]` horizons. The load-bearing claim is the cross-modal interaction — pure sentiment without a surprise event is slice-1-era general news flow, not an L01-specific edge. Formal test: at least three (ticker-category, direction, window, report-line) combinations show `β_interaction` t-stat > 2 after FDR correction over the 36 primary tests.

### 2.3 Design

**WASDE data:**

- 144 monthly releases 2014–2025 (usda.gov archive, free).
- Surprise defined as: report value minus pre-release consensus. Consensus source: Reuters / Bloomberg survey archives where free; fallback proxy is trailing 12-month trend residual. Document the source per release; flag proxy-derived surprises separately in analysis.
- Three crops: corn, soybeans, wheat. Three report lines per crop: production, ending stocks, yield.
- Bucket surprises per (crop, line) into terciles: upside / neutral / downside.

**Sentiment data:**

- Alpaca news history 2021–2025 for the 10 ag equities only (commodity ETFs not pulled — see data_sources §2.1). Alpaca news starts ~2021, truncating the combined sample relative to Test 1.
- FinBERT `yiyanghkust/finbert-tone` on every headline.
- Per-ticker daily aggregate: `(pos_count - neg_count) / total_count`, weighted by headline count.
- Compute average daily sentiment over `[D-1, D+2]` for each (release D, ticker T).

**Joint test structure:**

- Downstream ticker categories:
  - Fertilizer: NTR, MOS, CF
  - Equipment: DE, AGCO, CNH
  - Processors: ADM, BG
  - (Commodity ETFs CORN/WEAT/SOYB/DBA excluded — first-order effect, fully arbitraged, diagnostic only.)
- Forward returns: market-beta-adjusted `[D, D+5]` and `[D, D+10]` closes.
- Regression per (category, surprise direction, window, report line):
  ```
  forward_return ~ α + β_surprise · |surprise_magnitude|
                    + β_sentiment · sentiment_score
                    + β_interaction · (surprise · sentiment)
                    + ε
  ```
- **Primary hypothesis:** `β_interaction`. This is the coefficient that operationalizes L01's cross-modal claim — sentiment matters *conditional on* a surprise event. `β_sentiment` is reported descriptively (point estimate, t-stat, unadjusted p) but is **not** in the FDR family and does not count toward the pass criterion.
- Multiple testing: 3 categories × 2 directions (upside/downside; neutral excluded) × 2 windows × 3 report lines = **36 primary tests**, one `β_interaction` per regression. Apply Benjamini-Hochberg FDR correction at q = 0.10 across these 36.

### 2.4 Pass criterion (pre-registered)

All three must hold:

1. After BH-FDR correction at q = 0.10 over the 36 primaries, ≥ 3 combinations show `β_interaction` t-stat > 2 with correct sign (see criterion 2).
2. Surviving combinations are directionally consistent — same (category, window, report-line) does not produce opposite-signed surviving `β_interaction` for upside vs downside surprises on the same underlying mechanism.
3. Economic magnitude: at 1σ sentiment conditional on a surprise event, the implied forward return computed from `β_interaction` > 30 bps (cost-overcomable).

Partial pass (1–2 surviving combinations, or magnitude below 30 bps) is explicitly *not* a pass — treated as underpowered evidence, not validation. A strong `β_sentiment` result with weak `β_interaction` is likewise not a pass under L01; see §2.5 "Sentiment-only pass" for the follow-up path.

### 2.5 Outcome interpretation

| Outcome | Implication |
|---|---|
| **Joint pass** | L01 validated in a specific, mechanistic, testable form. Architecture collapses around WASDE-response capture: simpler universe, simpler agent roster, much stronger portfolio story. Rewrite architecture.md §1.4 around the concrete mechanism, not the general claim. |
| **Sentiment-only pass** (WASDE surprise not predictive, sentiment is) | Unexpected. Suggests sentiment effect is general, not event-driven. Raises the question of whether news *about* WASDE vs news about the ticker generally is doing the work. Warrants a Test 2 followup isolating non-WASDE-window sentiment. |
| **WASDE-only pass** (surprise predictive, sentiment adds nothing) | Demote sentiment agent to context-only, not a weighted Quant Meta input. Architecture simplifies: no need for Qwen sentiment alongside FinBERT, no Sentiment LoRA stretch (slice 5b deleted). |
| **Joint fail** | L01 falsified. Architecture's central claim has no empirical basis on this universe. Pivot decision required — see §4. |

### 2.6 Deliverable

`phase0/notebooks/test23_wasde_sentiment.ipynb` + `phase0/results/test23_YYYY-MM-DD.md` with full regression tables, FDR-corrected p-values for the 36 `β_interaction` primaries, surviving combinations with economic magnitudes, descriptive `β_sentiment` output (not FDR-counted), pre-registration locking commit SHA in the results header, and a plain-English verdict paragraph.

---

## 2.7 Interim Test 3-standalone

**Added 2026-04-22.** Not part of the original §2 pre-registration. Added as an interim
falsification step while Test 2+3's sentiment arm is blocked on news-source
availability (Alpaca signup errored on the user's attempt; Tiingo free plan does not
include News API, and paid Tiingo caps news history at 3 months per its published
pricing — unsuitable for a multi-year regression).

### 2.7.1 Purpose

Tests ONE component of L01 — whether WASDE surprises alone predict ag-equity returns
at swing horizons — without the sentiment arm. Reuses WASDE data already in hand
(`phase0/data/wasde_releases.csv`, 132 releases × 3 crops × 3 line items,
2014-01-10 → 2024-12-10) and Test 1's price data. Zero external-data dependencies,
zero dollars, runnable immediately.

### 2.7.2 What this does NOT test

**The L01 cross-modal claim.** L01 is fundamentally about integration across information
sources, not any single source in isolation. Test 3-standalone can tell us whether
WASDE surprises contribute anything, not whether the LLM-specific cross-modal layer
adds value. A full L01 falsification still requires the combined Test 2+3 once a news
source unblocks.

### 2.7.3 Hypothesis

Public USDA WASDE surprises on (production, ending_stocks, yield) of
(corn, soybeans, wheat) predict market-beta-adjusted forward returns on downstream ag
equity categories (fertilizer, equipment, processors) at 5-day and 10-day swing horizons.

### 2.7.4 Design

**Data:** same WASDE and price data as §2.3. Sentiment arm is omitted entirely.

**Surprise:** stage-1 trend-residual proxy (phase0_data_sources.md §5.3), unchanged.

**Regression per (category, direction, horizon, line_item):**

```
excess_return[D, D+h] ~ α + β_surprise · |surprise_magnitude| + ε
```

HC3 robust standard errors. `excess_return` is the same beta-adjusted construction as
§2.3 — 252-day strictly-trailing OLS for beta, non-trading-day releases rolled to the
next trading day.

**Regression matrix:** 3 categories × 2 directions (upside/downside; neutral excluded)
× 2 horizons × 3 line_items = **36 primaries**. Matches the combined-test FDR family size
so the pass bar is comparable.

**Multiple testing:** Benjamini-Hochberg FDR correction at q = 0.10 on the 36
`β_surprise` p-values.

### 2.7.5 Pass criterion (pre-registered)

All three must hold:

1. After BH-FDR correction at q = 0.10, ≥ 3 combinations show `β_surprise` t-stat > 2.
2. **Directional consistency.** For any (category, horizon, line_item) triple where
   both upside AND downside directions produce surviving `β_surprise`, the two β signs
   must be **opposite** on the `|surprise_magnitude|` regressor. (Upside β and
   downside β both positive on absolute magnitudes would mean "more surprise of any
   kind → returns move the same way" — a volatility response, not a directional one.)
3. **Economic magnitude.** At 1σ of `|surprise|`, implied excess return from
   `β_surprise` ≥ 30 bps (cost-overcomable at Test 1's 5 bps/side).

**Partial pass** (1–2 surviving combinations, or magnitude below 30 bps, or
sign-inconsistent) is explicitly **not** a pass — treated as underpowered evidence,
not validation. Mirrors §2.4's rigor.

### 2.7.6 Outcome interpretation

| Outcome | Implication |
|---|---|
| **Pass** | WASDE surprises aren't fully arbitraged at swing horizons. A tradable single-modal signal exists. Does NOT validate L01 (cross-modal). Either pivot to a simpler WASDE-event capture system (making hyxrestration's LLM layer a secondary question) or still run Test 2+3 when a news source unblocks, to test whether sentiment adds marginal value above the main effect. |
| **Fail** | WASDE surprises are fully arbitraged at swing horizons. Weakens one L01 pillar but does NOT falsify the cross-modal claim — the interaction could still exist. L01 verdict still requires Test 2+3 with news. Combined with Test 1's fail, moves us toward the §4 "Test 1 fail + Test 2+3 fail → kill trading-P&L goal" branch. |
| **Partial** | Do not promote. Treat as fail. Redesign or abandon, per §4's bold "partial pass is the highest-risk outcome because it's the easiest to rationalize" principle. |

### 2.7.7 Deliverable

`phase0/test3_wasde_standalone.py` + `phase0/results/test3_YYYY-MM-DD.md` with the
full 36-row regression table, surviving-FDR subset, economic-magnitude flags,
pre-registration locking commit SHA cited in the results header, and a plain-English
verdict paragraph.

### 2.7.8 Pre-registration lock

This subsection's thresholds (§2.7.5), regression structure (§2.7.4), and decision
rubric (§2.7.6) are locked by the commit that introduces §2.7 and
`phase0/test3_wasde_standalone.py` together. Subsequent edits to those subsections
require the `[pre-registration-violation]` commit-subject marker per §0.1, with a
paragraph in the commit body explaining what changed and why.

The locking commit SHA is cited in the results file header at run time.

---

## 2.8 Interim Test 2-standalone (daily sentiment-alone)

**Added 2026-04-22** after Test 2+3 iter 3 produced a `WASDE-only pass` verdict (0/36
β_interaction survive FDR, 0/36 β_sentiment survive at raw p<0.05, β_surprise
significant in multiple cells). That result leaves one question unanswered: does
FinBERT-on-ag-news sentiment contain *any* predictive signal for these tickers at
swing horizons, or is the sentiment arm contributing no information at all?

If sentiment is **predictive** on its own → the §2.5 "WASDE-only pass" interpretation
stands cleanly: FinBERT captures real signal, the cross-modal interaction specifically
doesn't multiply that signal with WASDE surprises. L01's cross-modal claim is
falsified, §2.5's prescription (demote sentiment, simplify architecture) is defensible.

If sentiment is **null** on its own → FinBERT-on-ag-news may be the bottleneck rather
than the thesis. Qwen zero-shot re-scoring (slice 5's planned A/B, pulled forward)
becomes a load-bearing sensitivity check before locking the pivot.

The combined Test 2+3 cannot distinguish these interpretations on its own. §2.8 does.

### 2.8.1 Purpose

Confirm or falsify the premise that FinBERT-scored sentiment on Alpaca-Benzinga
ag-ticker headlines contains any predictive information on forward returns at
1d/5d/10d horizons, independent of WASDE event conditioning.

### 2.8.2 What this does NOT test

Same disclaimer as §2.7: this is a component diagnostic, not a full L01 test. L01's
load-bearing claim is cross-modal integration, not sentiment alone. Sentiment-alone
signal would be consistent with a pure news-flow thesis that many institutional
equity desks have priced, not an L01-specific edge.

### 2.8.3 Hypothesis

Daily FinBERT-aggregated sentiment on ag-ticker headlines 2021-2024 predicts
market-beta-adjusted forward returns on the pooled ticker-category panel at 1d / 5d
/ 10d horizons.

### 2.8.4 Design

**Data:** reuses the corpus fetched for Test 2+3 iter 3.
- `phase0/data/alpaca_news.csv` — 3,744 Alpaca-Benzinga articles, 2021-2024
- `phase0/data/finbert_scores.csv` — FinBERT scores on all 3,744 articles
- `phase0/data/prices.csv` — cached yfinance Adj Close for the universe + SPY

No new data fetching, no new model inference.

**Daily sentiment:** per (ticker, trading day t),
`sentiment[t] = (pos_count − neg_count) / total_count` over headlines tagged to
the ticker on day t. Days without news have undefined sentiment and are excluded.

**Forward return:** `excess[t, t+h] = r[ticker, t, t+h] − β[t] · r[SPY, t, t+h]`,
with β from 252-day strictly-trailing OLS of ticker daily returns on SPY returns
(same construction as §2.3).

**Regression per (category, horizon):** pool the category's tickers, regress
```
excess_return[t, t+h] ~ α + β · sentiment[t] + ε
```

Standard errors: **Newey-West HAC with bandwidth = h**, correcting for overlap in
the forward-return windows at h ≥ 2 (consecutive daily observations at h=5 share
4 days of return).

**Regression matrix:**
- Categories: fertilizer (NTR, MOS, CF), equipment (DE, AGCO; CNH excluded per §7.1),
  processors (ADM, BG).
- Horizons: 1, 5, 10 trading days.
- 3 categories × 3 horizons = **9 primaries**.

**Multiple testing:** Benjamini-Hochberg FDR at q = 0.10 on the 9 p-values.

### 2.8.5 Pass criterion (pre-registered)

All three must hold:

1. After BH-FDR correction at q = 0.10, **≥ 1 of 9** regressions shows β t-stat > 2.
2. **Directional consistency.** Within a category, the signs of surviving β
   across horizons must agree. If β at 1d is positive and β at 5d is negative
   (both surviving), the sentiment effect isn't monotone across horizons —
   consistent with noise, not signal.
3. **Economic magnitude.** At 1σ of sentiment, implied excess return from β
   ≥ 30 bps on at least one surviving cell.

### 2.8.6 Outcome interpretation

| Outcome | Implication for the §2.5 interpretation |
|---|---|
| **Pass** | FinBERT captures real ag-ticker signal at daily swing horizons. The Test 2+3 `WASDE-only pass` finding is robust: the interaction specifically is zero, not the sentiment arm broadly. §2.5's architectural prescription (demote sentiment agent, delete slice 5b) stands. Qwen re-scoring becomes a lower-priority second-order sensitivity, not a pivot-blocker. |
| **Fail** | FinBERT-on-ag-news sentiment shows no standalone predictive signal either. FinBERT is the live candidate for "bottleneck model, not bottleneck thesis." Qwen zero-shot re-scoring becomes load-bearing before §2.5's pivot is executed. The cross-modal claim cannot be cleanly falsified on FinBERT data alone. |
| **Partial** | 1-2 survivors with inconsistent signs or below economic-magnitude threshold. Treat as fail for decision-making per the partial-pass principle (§2.4). |

### 2.8.7 Deliverable

`phase0/test2_sentiment_standalone.py` + `phase0/results/test2_YYYY-MM-DD.md` with
the full 9-row regression table, FDR-corrected p-values, surviving cells, economic
magnitudes, pre-registration locking commit SHA in the header, and verdict.

### 2.8.8 Pre-registration lock

This subsection's thresholds (§2.8.5), regression structure (§2.8.4), and decision
rubric (§2.8.6) are locked by the commit that introduces §2.8 and
`phase0/test2_sentiment_standalone.py` together. Subsequent edits to those
subsections require the `[pre-registration-violation]` commit-subject marker per
§0.1.

---

## 2.9 Interim Qwen sentiment A/B

**Added 2026-04-23** after Test 2-standalone (§2.8) produced a FAIL verdict —
FinBERT captures no standalone predictive signal on the 2021-2024 ag corpus.
Per §2.8.6, this makes Qwen zero-shot re-scoring load-bearing before executing
§2.5's architectural pivot: we need to rule out FinBERT being the measurement
bottleneck rather than the thesis being wrong.

### 2.9.1 Purpose

Swap the sentiment scorer (FinBERT → Qwen 2.5 7B Instruct, zero-shot) and re-run
the same pre-registered tests that already ran on FinBERT data: Test 2-standalone
(§2.8) and Test 2+3 combined (§2). All other pipeline components are identical —
same news corpus, same prices, same WASDE surprises, same regressions, same FDR.

### 2.9.2 What this does NOT test

Not a fine-tune. Not a prompt sweep. A single pre-registered zero-shot prompt with
greedy decoding. The whole point is a clean single-variable A/B against FinBERT.

### 2.9.3 Model and prompt (pre-registered)

**Model:** `Qwen/Qwen2.5-7B-Instruct` from HuggingFace, loaded in fp16 on GPU.

**Prompt** (applied via Qwen's chat template with `add_generation_prompt=True`):

```
System: You are a financial sentiment classifier. Classify news headlines
about publicly traded agricultural companies as positive, negative, or
neutral, based on their likely short-term impact on the company's stock
price.

User: Headline: "{headline}"

Respond with exactly one letter: P (positive), N (negative), or Z (neutral).
```

**Decoding:** greedy (`do_sample=False`), max_new_tokens=1.

**Label extraction:** at the first generated-token position, extract logits for
the single-token IDs of "P", "N", "Z". Softmax over those three logits to obtain
(pos, neg, neu) probabilities. Argmax = predicted label.

**Output schema:** same as `phase0/data/finbert_scores.csv` — `news_id, label,
score, pos, neg, neu, scored_at` — so downstream code is scorer-agnostic.

### 2.9.4 Tests re-run

Both tests executed with the Qwen-derived scores, same pre-registered pass
criteria:

- **Test 2-standalone (§2.8)** — 9 regressions on daily sentiment × forward
  returns, Newey-West HAC SEs, BH-FDR at q=0.10. Pass criteria: §2.8.5.
- **Test 2+3 combined (§2)** — 36 regressions on surprise × sentiment ×
  interaction, HC3 SEs, BH-FDR at q=0.10 on β_interaction. Pass criteria: §2.4.

Same ticker universe (CNH excluded per §7.1), same 2021-2024 window, same
corpus of 3,744 articles.

### 2.9.5 Outcome interpretation (combined)

Four outcomes from the two tests (Pass/Fail for each):

| Qwen Test 2-standalone | Qwen Test 2+3 | Interpretation |
|---|---|---|
| Pass | Pass | **Full L01 validation.** Qwen captures ag-news sentiment with a cross-modal interaction that survives FDR. Stop the §2.5 pivot; build as originally designed with Qwen sentiment from slice 1. |
| Pass | Fail | **§2.5 pivot is robust.** Sentiment arm works (Qwen captures signal), but the cross-modal interaction specifically is zero. Demote sentiment agent to context-only per §2.5. FinBERT was a weaker instrument but the cross-modal falsification stands. |
| Fail | Pass | **Surprising.** Cross-modal interaction picks up signal neither arm does alone — a classic cross-modal effect that Test 2-standalone can't see. Treat as L01 validation with a specific mechanism and investigate further before locking architecture. |
| Fail | Fail | **Thesis robustly falsified.** Both the stronger and the weaker instruments show no signal. §2.5 pivot proceeds with maximum confidence. Sentiment arm is definitively not contributing on this universe. |

The (Fail, Fail) outcome is the cleanest kill. The (Pass, Pass) outcome is the
cleanest save. The cross-configuration outcomes require architectural discussion
before locking.

### 2.9.6 Deliverable

`phase0/sentiment_qwen.py` (scorer) + `phase0/test29_qwen_ab.py` (driver), and
two result files:
- `phase0/results/test2_qwen_YYYY-MM-DD.md` — Test 2-standalone on Qwen scores
- `phase0/results/test23_qwen_YYYY-MM-DD.md` — Test 2+3 combined on Qwen scores

Both cite this commit's SHA in their header as the pre-registration lock.

### 2.9.7 Pre-registration lock

Model choice (§2.9.3), prompt template (§2.9.3), decoding parameters (§2.9.3),
label-extraction rule (§2.9.3), and outcome interpretation (§2.9.5) are locked
by the commit that introduces §2.9 and `phase0/sentiment_qwen.py` +
`phase0/test29_qwen_ab.py`. Subsequent edits require the
`[pre-registration-violation]` marker per §0.1.

---

## 2.10 Interim expanded grid — horizons × aggregators

**Added 2026-04-23** after §2.9 produced the (Fail, Fail) Qwen A/B outcome.
Final sensitivity pass before the architectural pivot: does the null result
hold across a broader set of horizons and sentiment-aggregation schemas, or
does a different configuration reveal buried signal that the pre-registered
3-horizon / single-aggregator design missed?

### 2.10.1 Purpose

Confirm (or falsify) the robustness of the null result across **all plausible
single-axis variations** of the prior tests. Expanded on two axes:

- **Horizons:** add 3d (intraday-reaction window) and 20d (monthly drift) to
  the existing 1d / 5d / 10d set. Total = 5 horizons for Test 2-standalone.
- **Aggregators:** three daily-sentiment aggregation schemas, specified in
  §2.10.3 below.

Run on Qwen 2.5 7B scores (the stronger instrument from §2.9). FinBERT
expansion deliberately omitted — that adds FDR load without new information,
since §2.9 already established instrument-level null agreement.

### 2.10.2 Pre-registered commitment (load-bearing)

**This is the final sensitivity test.** The architectural decision is binding
on the outcome:

- **If any cell survives family-wide BH-FDR at q=0.10 AND economic magnitude
  ≥ 50 bps at 1σ AND signs are directionally consistent across the category's
  surviving cells:** we treat it as evidence worth further investigation and
  discuss architectural implications before pivoting. Legitimate partial-pass
  outcome worthy of follow-up.
- **If no cell survives the joint bar:** we execute §2.5's pivot (or Options
  B / C from the post-iter-3 decision set) immediately. **No further test
  configurations will be added.** Any further expansion of Test 2 / Test 2+3
  after §2.10 requires a `[pre-registration-violation]` commit per §0.1, with
  explicit justification visible in history.

This commitment is the distinguishing feature between a principled robustness
test and garden-of-forking-paths post-hoc search. Without the commitment, this
test has no scientific value and would inflate our false-positive rate.

### 2.10.3 Aggregators (pre-registered)

Three daily-ticker sentiment aggregation schemas. All operate on the same
`scores` DataFrame (`news_id, label, score, pos, neg, neu`) joined to news
on news_id. Daily aggregation is per (ticker, trading date):

**A1 — `mean_label` (current default, §2.8 baseline):**
```
sentiment = (pos_count − neg_count) / total_count
```
where counts are over headlines with `label == "positive" / "negative"`
(argmax over the three probabilities).

**A2 — `conf_weighted`:**
```
sentiment = mean(P_positive − P_negative)
```
averaged across that day's headlines. Uses the softmax probabilities directly,
not argmax labels. Picks up subtleties where one model is confident-positive
and another is barely-positive — treats them differently.

**A3 — `volume_normalized`:**
```
sentiment = (pos_count − neg_count) / log(1 + total_count)
```
Same numerator as A1, but log-dampened denominator. Rationale: on heavy-news
days (e.g., earnings), total count spikes and the A1 denominator can produce
unstable sentiment magnitudes. A3 softens this.

### 2.10.4 Tests and dimensions

**§2.10-A — Test 2-standalone expanded:**
- Categories: 3 (fertilizer, equipment, processors)
- Horizons: 5 (1d, 3d, 5d, 10d, 20d)
- Aggregators: 3 (A1, A2, A3)
- **Total: 45 cells**

Regression per (category, horizon, aggregator):
```
excess_return[t, t+h] ~ α + β · sentiment[t] + ε
```
Newey-West HAC SEs with maxlags=h (as in §2.8.4).

**§2.10-B — Test 2+3 combined expanded:**
- Cells: 36 base × 3 aggregators = **108 cells**
- Horizons kept at §2.3's 5d / 10d (not expanded on both axes simultaneously —
  keeps FDR load tractable while still swapping aggregators)
- Regression unchanged from §2.3 (HC3 SEs, surprise × sentiment interaction
  is still the load-bearing coefficient)

### 2.10.5 Pass criteria (pre-registered, elevated)

Both expanded tests share the same elevated bar, deliberately stricter than
§2.4/§2.8.5 because we're explicitly in multiple-testing territory:

1. After BH-FDR correction at q=0.10 **across the full test family** (not
   per-subset), ≥ 1 cell surviving. For §2.10-A, FDR across all 45; for
   §2.10-B, FDR across all 108.
2. **Economic magnitude ≥ 50 bps at 1σ** (was 30 in §2.4/§2.8.5). Elevated
   because a genuine signal robust across configurations should have stronger
   economic footprint; one-cell survivors right at the 30 bps edge under a
   larger family are more likely noise artifacts.
3. **Directional consistency.** Within a category, the signs of surviving β
   across horizons/aggregators must agree. Sign-flipping across
   configurations within the same category is the classical garden-of-forking-
   paths artifact.

Partial pass (1-2 surviving but below magnitude, or sign-inconsistent) is
explicitly NOT a pass per §0.1's pre-registration spirit.

### 2.10.6 Deliverables

- `phase0/aggregators.py` — the three aggregator functions, §2.10.3.
- `phase0/test210_expanded_grid.py` — driver that runs both §2.10-A and
  §2.10-B, writes two results files.
- `phase0/results/test210_expanded_2026-04-XX.md` — unified report with
  both expanded panels, surviving cells, full 45+108 tables, verdict.

### 2.10.7 Pre-registration lock

Aggregator specifications (§2.10.3), test dimensions (§2.10.4), pass criteria
(§2.10.5), and the commitment clause (§2.10.2) are locked by the commit that
introduces §2.10 + the implementation files. The `[pre-registration-violation]`
marker is required for any subsequent edit to §2.10.2-5.

---

## 3. Test 4 — Drought Monitor (deferred)

Originally scoped as a fourth test. Deferred because:

1. The mechanism (retail-speed propagation of drought severity into ag equities) is weaker than Test 2+3's institutional-silo mechanism.
2. If Test 2+3 passes, drought can be added as an additional signal within the validated framework without a standalone falsification test.
3. If Test 2+3 fails, running Test 4 is unlikely to produce a different outcome given overlapping mechanism weaknesses (both bet on public-data propagation delays into liquid equities).

Reconsidered only if Test 2+3 produces the "WASDE-only" partial-pass outcome above, which would reopen the question of whether other environmental data has the same asymmetry.

---

## 4. Decision rubric after Phase 0

After both tests complete, one of four paths is chosen — committed to before any slice 1 code is written.

| Scenario | Path |
|---|---|
| Test 1 strong pass + Test 2+3 joint pass | **Build hyxrestration around WASDE mechanism.** Rewrite L01 concretely, narrow universe and agent roster to what the tests validated, proceed to slices 1–9 with a tightened scope. |
| Test 1 marginal + Test 2+3 joint pass | **Build hyxrestration as designed, with documented quantified target.** Architecture stands; know the alpha budget to beat. |
| Test 1 strong pass + Test 2+3 fail | **Pivot: ship the simple momentum rebalancer** as the actual trading system. Build hyxrestration as a research workbench (Thesis C) on top, *not* as an autonomous trader. |
| Test 1 fail + Test 2+3 fail | **Kill the trading-P&L goal.** Reframe project as pure portfolio piece / learning vehicle. Write up the negative result honestly — this is a stronger interview story than handwaved positive claims. |
| Test 1 fail + Test 2+3 pass | **Rare but possible.** Alpha lives entirely in event-driven capture, not passive exposure. Narrow hyxrestration to event-window trading only. |
| Anything + Test 2+3 partial pass | **Do not promote partial-pass to pass.** Extend or redesign the test, or treat as fail. Partial pass is the highest-risk outcome because it's the easiest to rationalize. |

---

## 5. What Phase 0 does *not* do

- Does not test Qwen zero-shot sentiment vs FinBERT. That's slice 5's A/B. Phase 0 uses FinBERT as the sentiment proxy because it's fast and good enough for a falsification test.
- Does not test LLM Meta (qualitative review). That layer is only worth testing if the signals it would review are themselves validated.
- Does not test DPO / Meta LoRA. Conditional stretch goals stay stretch goals.
- Does not test the risk module, execution, or portfolio construction. Those are engineering surfaces, not alpha surfaces.
- Does not become infrastructure. Phase 0 code is one-shot research code. Nothing in `phase0/` imports anything from `hyx/`, and nothing in `hyx/` ever imports from `phase0/`.

---

## 6. Open items

- **Consensus data source for WASDE surprises.** Reuters/Bloomberg archives may require paid access for full history. Budget check against $0 hard budget (architecture.md §12). If paid sourcing required, frame the decision to user with concrete ROI argument before committing.
- **Multiple-testing correction choice.** Benjamini-Hochberg at q=0.10 is the default; Bonferroni at α=0.05 is stricter. Pre-register one, don't switch.
- **Proxy surprise definition.** If consensus data unavailable, trailing-trend residual is a proxy with known bias. Flag proxy-derived releases and run a sensitivity analysis excluding them.

---

## References

- Parent architecture: [`architecture.md`](architecture.md)
- Original L01 rationale: `agent-memory/orchestrator/project_scoping.md`
- Decision index: [`decisions.md`](decisions.md) — new decision IDs P01–P0x for Phase 0 outcomes to be added post-run.
