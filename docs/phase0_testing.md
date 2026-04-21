---
PRE_REGISTERED: false
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
- Universe: **10 ag equities only** — NTR, MOS, CF, CTVA, FMC, ADM, BG, DE, AGCO, CNHI. Commodity ETFs (DBA/CORN/WEAT/SOYB) are explicitly excluded so Test 1 remains composable with Test 2+3, which also excludes them as first-order / fully-arbitraged (see §2.3). Tickers that didn't exist in a given period (CTVA pre-June-2019 spin) are held as cash for that period — no survivorship pretending.
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
  - Equipment: DE, AGCO, CNHI
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
