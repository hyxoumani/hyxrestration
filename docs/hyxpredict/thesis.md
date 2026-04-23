# hyxpredict — project thesis

**Status:** thesis only. No falsification criteria, no pre-registered tests, no
architecture decisions yet. Everything below the thesis is "questions we will
need to answer," not "answers we have."

**Originating session:** 2026-04-23.
**Predecessor:** `docs/architecture.md` (hyxrestration, ag-equities). L01 was
falsified in its pre-registered form by Phase 0 `phase0/results/` — specifically
the cross-modal sentiment × WASDE interaction produced no signal on either
FinBERT or Qwen 2.5 7B scoring, on 2021-2024 ag-equity data, across the
§2.10 expanded grid.

## Why pivot

Phase 0 eliminated hyxrestration's specific thesis but left two of L01's three
pillars structurally intact in a different venue. The cross-modal integration
claim has independent theoretical and empirical support in **prediction-market
forecasting** (Metaculus aggregate research, LLM ensemble forecasting papers
2024-2026, Polymarket retail-mispricing literature) that it never had in the
ag-equity context we tested.

hyxpredict is the attempt to re-home the same core claim — LLM cross-modal
forecasting generates edge on under-covered universes — onto a venue where:

1. **The participant mix is actually under-covered.** Kalshi (CFTC-regulated)
   and Polymarket (offshore) are structurally closed to most institutional
   capital for size or regulatory reasons. Retail, speculators, and partisans
   dominate pricing. This matches L01's "under-covered universe" claim in a
   way that 14 large-cap ag equities never did.

2. **The output modality matches LLM native capability.** Market questions are
   natural-language-structured yes/no reasoning tasks with specific resolution
   criteria. LLMs are trained on exactly this shape. Regressing return-on-
   sentiment was always a coerced fit; predicting
   `P(Fed cuts 50bp at March meeting | FOMC statement + data + analyst
   forecasts)` is what LLMs do.

3. **Feedback is deterministic.** Every market resolves with a known outcome.
   Calibration curves, Brier scores, and forecast-vs-market comparisons are
   measurable within days-to-weeks, not months of backtest.

## Thesis (single sentence)

> **LLM cross-modal forecasting can generate edge over market-implied
> probabilities on under-covered prediction-market categories, and the same
> forecaster can be applied to equity event-capture for a second independent
> trading venue.**

## Load-bearing pillars

Three components, same structure as L01, evaluated on a different substrate:

### Pillar 1 — Cross-modal integration on under-covered universe

Prediction markets reward integrating across news + official statements +
historical base rates + expert forecasts + macroeconomic data + polling +
domain-specific signals. Institutional forecasting desks are expensive; retail
forecasters typically use one or two information sources. An LLM that
synthesizes across sources plausibly outperforms both on well-chosen
categories.

**Must be true for the thesis to hold:** at least one well-chosen category of
prediction markets shows systematic divergence between market-implied
probability and a properly-calibrated cross-modal LLM forecast, large enough
to overcome transaction costs.

### Pillar 2 — Encoded domain knowledge that compounds

Same mechanism as L01: accumulated forecasting expertise — which prompts work
on which question types, which sources are reliable in which contexts, which
historical precedents apply — becomes a moat when systematically encoded into
the forecaster rather than lost between predictions. Year-3 forecaster >
year-1 forecaster.

**Must be true:** forecaster quality (Brier score, calibration) improves
materially over time as accumulated experience is encoded, rather than
plateauing at whatever a zero-shot base model delivers.

### Pillar 3 — Time-horizon and feedback-loop fit

Most prediction markets resolve in days-to-months, matching the swing cadence
hyxrestration was designed for. Unlike equity returns (noisy labels, months of
backtest to reach statistical significance), prediction-market resolutions are
clean labels available per-market. The feedback loop is fundamentally tighter
for LLM self-improvement than the equity-returns loop.

**Must be true:** the feedback-loop tightness actually translates to faster
learning / adaptation rather than being obscured by other noise sources.

## Transfer corollary (additional, not load-bearing)

A forecaster validated on prediction markets plausibly has upstream skill
that applies to equity-event trading — specifically the weak-but-real WASDE
surprise main effect from hyxrestration Test 3-standalone. If the forecaster
can predict "P(corn production > N bu)" on Kalshi better than the market,
the same prediction has direct value for sizing equity positions on
ADM/DE/NTR/MOS around WASDE releases.

This gives a **second independent validation venue** for the same upstream
forecaster, and a **second monetization surface** if it works.

**Important:** the corollary is not required for the thesis to hold. If the
forecaster works on prediction markets but transfer to equity-event capture
fails, the thesis still stands on its primary venue. If transfer works, it's
a bonus.

## What this is NOT claiming

- Not: "LLMs are good at predicting *any* prediction market." Specific
  categories will work, others won't. Scope selection is itself a research
  question.
- Not: "Prediction markets are broadly inefficient." They are probably
  efficient in aggregate on liquid, well-studied events. Edge claim applies
  only to under-covered categories (to be identified).
- Not: "Any off-the-shelf LLM beats the market." Validation is required. The
  thesis is that *a carefully-designed forecaster* can beat market-implied
  probabilities, not that any LLM does.
- Not: "Same architecture as hyxrestration." This is a fresh build. The
  original 9-slice, Meta-split, LLM-stack design was load-bearing on the
  falsified L01 claim. hyxpredict's architecture will be re-derived from the
  prediction-market substrate, not inherited.
- Not: "Brier-improvement is automatically P&L." Edge in probability estimates
  only becomes dollars if (improvement × position size) exceeds (execution
  costs + resolution risk). That is its own empirical question.

## Open questions (deferred to subsequent scoping, not answered now)

All of these need empirical data from the venues before we can answer:

- **Which specific categories are under-covered enough to be worth targeting?**
  Ag-commodity markets (our existing domain expertise), macro-economic markets
  (FOMC, CPI, unemployment), weather events, SEC/regulatory outcomes, some
  others. Priority ordering cannot be assigned without data.
- **Kalshi vs Polymarket — which is the better primary venue?** Kalshi is
  US-legal but newer and thinner; Polymarket is deeper but offshore and
  trickier. Likely answer is "use both," but how they're used differs.
- **What does the forecaster actually look like?** Zero-shot LLM, fine-tuned
  LLM, multi-agent ensemble (à la Metaculus), retrieval-augmented with a
  curated-context store, something else. Architectural choice deferred.
- **What is the transfer-to-equity mechanism?** Does the forecaster emit
  distributions over numeric outcomes (useful for sizing equity bets) or just
  binaries? What's the right equity-response model conditional on
  forecaster output?
- **What are meaningful Brier-improvement thresholds?** Calibration curves,
  tail behavior, and cost-adjusted edges need scoping against actual venue
  execution costs (Kalshi has 0-5c spreads typically, Polymarket varies).
- **Paper-to-real-money transition criteria.** Hyxrestration had "3+ months
  paper + T2 gate"; prediction markets have different risk characteristics
  (resolution risk, binary payoff variance) that require a different rule.
- **Capital sizing on binary-payoff bets.** Kelly on binary outcomes is
  sharper than on continuous; fractional-Kelly defaults will need
  re-derivation.
- **Falsification framework for hyxpredict's Phase 0.** What specific
  hypotheses get pre-registered before any build work starts.

## Kill criteria (qualitative, not pre-registered)

Placeholder — these are directional intents until Phase 0 formalizes them:

- **Category discovery fails.** If no prediction-market category shows even
  preliminary evidence of forecaster-over-market improvement in initial
  scoping, the thesis is falsified before any build.
- **Venue access breaks.** Regulatory or execution changes that make both
  Kalshi and Polymarket unavailable at research scale kill the project.
- **Execution costs overwhelm edge.** If in any category where edge appears
  to exist, round-trip costs eat the Brier-improvement translated to dollars,
  kill that category (and if no category survives, kill the thesis).
- **Transfer corollary fails** (conditional kill of corollary only). If the
  forecaster validates on prediction markets but doesn't improve equity-event
  capture, we drop the transfer claim but keep the primary-venue thesis
  alive.

## Budget (placeholder, not locked)

Hyxrestration's Phase 0 was ≤3 weekends. A reasonable starting budget for
hyxpredict's equivalent scoping + falsification phase is similar — 3-4
weekends to decide continue/pivot/kill before any slice-1-style build work.
Actual pre-registered budget gets locked when the Phase 0 doc is written.

## References

- `docs/architecture.md` — hyxrestration's original (L01) architecture, now a
  Phase 0 artifact.
- `docs/phase0_testing.md` + `phase0/results/*.md` — the falsification record
  that motivated this pivot.
- This conversation's transfer-learning insight: resolved prediction markets
  are cleanly labeled supervised data, and the same forecaster plausibly
  generalizes upstream for equity event-capture.

---

Next document to write (after discussion): `docs/hyxpredict/phase0_predict.md`
— the falsification framework. That document needs venue-level data review
(Kalshi category inventory, Polymarket historical depth, etc.) before it can
be meaningfully pre-registered, so it is deliberately not being drafted here.
