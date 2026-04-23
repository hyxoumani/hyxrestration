# Phase 0 post-mortem

**Written 2026-04-23, after user pushback on `docs/hyxpredict/thesis.md`.**
**Scope:** what Phase 0 actually taught, what it didn't, and what failure modes
showed up in the rushed pivot to hyxpredict. No new thesis, no architecture,
no forward-looking pillars. The recommendation at the end is one of three
options already on the table — not a blend.

---

## 1. What happened, in order

- **2026-04-13 → 2026-04-21:** hyxrestration scoping + architecture + slice 1
  scaffold + A10 decoupling of data sources from execution.
- **2026-04-22:** Phase 0 Tests 1, 3-standalone, and Test 2+3 iter 3 (FinBERT).
- **2026-04-23:** Test 2-standalone (§2.8, FinBERT daily sentiment → null),
  Qwen A/B (§2.9, both tests null on Qwen scorer too), expanded grid (§2.10,
  null on 45 + 108 cells with family-wide FDR). Pivot proposal (`docs/
  hyxpredict/thesis.md`) drafted and committed **the same day** the
  falsification locked.
- **Same day, later:** user pushback on the pivot doc.

The pivot was proposed with zero cooldown after the falsification. No separate
phase for sitting with what Phase 0 meant before proposing what comes next.

## 2. What the evidence actually supports (narrow claims only)

### Falsified in pre-registered form

**L01's cross-modal claim on ag equities.** Specifically: FinBERT- or Qwen-
scored daily sentiment on Alpaca-Benzinga headlines, aggregated via
(pos-neg)/total or two variants, multiplied by WASDE trend-residual
surprise, does not predict beta-adjusted forward equity returns at 5d/10d
horizons on the 8-ticker regression universe in the 2021-2024 window.

- Across §2 iter 3 (36 cells, FinBERT) → `wasde_only_pass` (0/36 β_interaction
  survive FDR).
- Across §2.9 (36 cells, Qwen) → `joint_fail` (0/36 survive).
- Across §2.8 (9 cells, daily sentiment-only, FinBERT) → FAIL.
- Across §2.9 (9 cells, daily sentiment-only, Qwen) → FAIL.
- Across §2.10-A (45 cells, Qwen × 3 aggregators × 5 horizons) → 0 survive.
- Across §2.10-B (108 cells, Qwen × 3 aggregators × 36 base cells) → 0 survive.

### Passing / surviving

- **WASDE surprise main effect (Test 3-standalone, §2.7):** 3/36 cells survive
  BH-FDR q=0.10, two at `p_fdr ≈ 0.097` (FDR edge), one cleanly at `p_fdr =
  0.001`. Directionally weak, mechanism-unclear on 2/3 survivors, clean on 1.
- **One narrow location:** equipment / downside / 10d / yield shows
  `β_sent p ≈ 0.0005` and `β_surp p ≈ 0.03` simultaneously across all three
  §2.10 aggregators. One cell out of 108 is within null expectation, so this
  is "interesting but doesn't rescue the broad claim."

Nothing else reached the pre-registered bar.

## 3. What the methodology validated

This is the meta-learning that's justified:

- **Pre-registration with commit-discipline works.** The `PRE_REGISTERED: false
  → true` flag, locked at 7d6ef52, forced falsification criteria to be
  committed before any result. §2.10.2's binding clause ("null outcome
  executes the pivot, no more configurations") kept us from p-hacking past
  the threshold when the top-10 diagnostic looked tempting.
- **BH-FDR across full test families corrects for multiple testing.** The
  §2.10-B 108-cell family would have produced 5-11 "significant-looking"
  cells under a true null at raw p<0.10; FDR correctly eliminated all of
  them.
- **Instrument-vs-thesis splitting matters.** The Qwen A/B (§2.9) ruled out
  FinBERT being the bottleneck before we concluded the thesis was wrong.
  Without that test, the broad claim would have remained ambiguous.
- **Diagnostic honesty.** Writing out the §2.10-B top-10 table even though
  nothing survived FDR was the right move; it informed the post-hoc decision
  (narrow Phase 1 hypothesis vs. broad pivot) without using the diagnostic
  as verdict-level evidence.

These are real learnings. They're about *process*, not about sentiment or
LLMs or equities.

## 4. What Phase 0 did NOT touch

Scope discipline requires being explicit:

- **Pillar 2 of L01 ("encoded domain knowledge compounds").** Never tested
  in hyxrestration. No Phase 0 horizon could test it — it's a years-long
  empirical claim. Was carried on faith.
- **Sophisticated sentiment pipelines.** Source-trust weighting, event
  deduplication, temporal decay, headline+summary integration, topic
  filtering — none exist in the tested pipeline. User correctly identified
  this gap after the fact.
- **Non-linear interaction structures.** The β_interaction regression
  assumes a multiplicative sentiment × surprise term. LLM-Meta-style
  qualitative reasoning (pass/veto/adjust) is a different functional form
  that wasn't tested.
- **Fine-tuned models.** Only zero-shot Qwen. Fine-tuning on ag-labeled
  data wasn't attempted (label-availability problem remains unresolved).
- **Universes beyond 8 ag equities.** No cross-sectional expansion.
- **Cross-asset / commodity-direct / options surfaces.** Only cash equities.
- **Prediction markets.** Zero Phase 0 evidence one way or the other.

## 5. What extrapolation from Phase 0 is and isn't justified

**Is justified:**
- "Pre-registration + kill criteria worked on this project." Process claim.
- "On the specific pre-registered test, the null was clean enough to act
  on." Narrow claim.
- "My pattern-matching instinct to scaffold a new thesis on the falsified
  thesis's shape is a failure mode worth catching." Self-knowledge claim.

**Is NOT justified:**
- "Sentiment can't predict equity returns" — we tested a prototype pipeline.
- "Cross-modal LLM forecasting doesn't work" — we tested one instantiation.
- "Three-pillar-plus-corollary is the right structure for edge-discovery
  theses" — L01 was n=1 and failed, not a pattern worth replicating.
- "Prediction markets are the natural successor venue" — zero Phase 0
  evidence for or against.

Single-falsification generalizations are the main failure mode to avoid
post-hoc.

## 6. Failure modes in the hyxpredict pivot draft (named and owned)

I wrote `docs/hyxpredict/thesis.md` in hours, same day as the falsification.
Specific things that went wrong:

1. **Scaffold re-use without re-derivation.** Copied L01's three-pillar
   structure to a new substrate instead of asking what was *actually*
   re-derivable from Phase 0 evidence.
2. **Copied Pillar 2 unchanged.** "Domain knowledge compounds" was never
   validated in L01 and I propagated it to the successor without question.
3. **Invented a corollary with a logical hole.** "Forecaster trained on
   prediction markets transfers to equity event-capture" resolves to "either
   we beat prediction markets (and should trade them directly, no transfer
   needed) or we don't (and nothing transfers)." There's no coherent
   middle. It read like structural synergy but was two independent claims
   dressed up.
4. **Outdated "under-covered" framing.** Kalshi (Susquehanna MM) and
   Polymarket (Jane Street flow) are not 2022's retail-dominated venues.
   Specific categories might still be under-covered; venues broadly are not.
5. **Unsourced literature claim.** Gestured at "LLM ensemble forecasting
   papers 2024-2026" without citing specifics. Halawi, Schoenegger, and
   related work show LLMs *approaching* but not cleanly beating crowd
   accuracy. The literature is mixed; I implied support.
6. **No cooldown between falsification and successor.** Zero time for
   Phase 0's result to settle before proposing the next direction.

**Why?** Honest read:
- Sunk-cost orientation. Multi-weekend build invested; pivot framing kept
  the investment feeling useful.
- Sponsor-mode memory says the user wants me to drive — which I
  over-applied as "drive toward continued building" when "drive toward
  writing the falsification up as the finished artifact" was a valid
  interpretation I didn't consider.
- Pattern-recognition bias. I had a thesis template ready; the path of
  least resistance was to fill it in.

## 7. Time allocation context (added from user)

User is job-searching. This changes the opportunity cost calculation for
any new multi-weekend research commitment:

- A multi-weekend hyxpredict build competes with interview prep.
- hyxrestration as it stands is already a strong portfolio artifact if
  written up honestly — a falsification-driven kill with rigorous methodology
  is a better interview signal than a hasty pivot.
- The methodology itself (pre-reg + FDR + sensitivity + bound pivot) is
  publishable at a higher level than the object-level trading claim. A
  technical blog post or write-up could extract that value without more
  build work.

I didn't weigh this when proposing hyxpredict. It changes the calculus
significantly.

## 8. Honest recommendation (one direction, not a blend)

**Option 2 from the three the user offered: defer hyxpredict until the job
search wraps; spend the interim turning hyxrestration into a clean
artifact.**

Reasoning:

- The evidence supports it. Phase 0 is actually already complete. Tests,
  diagnostics, commits, verdicts — all there. Nothing empirical is blocking
  a write-up.
- The opportunity cost of the alternatives is real. Option 1 alone would
  leave the project in limbo. Option 3 (tighten hyxpredict) requires venue
  data, category selection, literature review, and a redesigned falsification
  framework before it could be locked — that's a weekend of work minimum,
  probably two, during the job-search window.
- The artifact value is already there. "Ran a pre-registered falsification
  test on a trading thesis, accepted the null, wrote it up honestly" is a
  stronger portfolio piece than "started a second project that isn't done
  yet."
- The methodology extracts without additional research. Pre-reg discipline,
  BH-FDR, sensitivity-check commitment clauses, honest diagnostics even on
  nulls — these transfer to non-trading problem domains and are teachable.
- Deferring hyxpredict doesn't kill it. If after the job search the thesis
  still seems compelling (which requires more scrutiny than it has gotten
  so far), it can be rewritten properly with the time it deserves.

This is not the blended "do post-mortem + tighten hyxpredict + continue
building" path. It's: close out Phase 0 as a complete artifact, stop new
research commitments until job-search pressure lifts, decide later whether
any successor project is worth starting fresh.

## 9. Explicitly deferred (not answered here)

- Whether hyxpredict, in some form, survives a rigorous second look after
  the job search wraps.
- Whether the equipment/downside/10d/yield narrow hypothesis is worth
  testing on fresh 2025+ data at some later point.
- Whether a sophisticated sentiment pipeline could rescue any of L01 in
  its original ag-equity form.
- Whether the `hyx/` code stays in this repo, gets archived, or gets
  restarted fresh when a successor project is ready.
- Whether `docs/hyxpredict/thesis.md` should be revised, retracted, or
  left as-is alongside this post-mortem for the historical record.

These are all real questions. None of them should be answered in the same
session as the falsification.

## 10. What the next concrete action would be (optional, if the recommendation stands)

Not part of this post-mortem's conclusion, offered only so the direction has
a shape:

A hyxrestration write-up artifact — could be a README-style summary in
this repo, or a standalone technical blog post, or both. Centered on:
- The original L01 thesis, stated honestly with its pillars
- Phase 0's design (pre-registration + kill criteria)
- The specific tests, their results, the diagnostics
- The Qwen A/B and §2.10 expansion as proper-sensitivity-discipline examples
- The honest conclusion (null, accepted, pivot deferred)
- The methodological lessons separate from the object-level trading claim

No build work. No new data collection. No new hypotheses. Purely extraction
of what already exists into a polished artifact.

That's a weekend of writing, not a multi-weekend research commitment. It
finishes the loop Phase 0 opened.
