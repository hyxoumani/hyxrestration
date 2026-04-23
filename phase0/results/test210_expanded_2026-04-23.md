# Phase 0 §2.10 — expanded grid results

**Run date:** 2026-04-23
**Current commit SHA:** `a0ca35d98c307a402bc3c8eb542c975a5359c5f2`
**Pre-registration:** locked in the commit that introduced §2.10 and this file.
**Scorer:** Qwen 2.5 7B Instruct zero-shot (§2.9).
**FDR:** Benjamini-Hochberg at q=0.1, family-wide.
**Economic-magnitude threshold:** |bps at 1σ| ≥ 50 (elevated).

## Joint outcome

- §2.10-A Test 2-standalone expanded: **FAIL** (0/45 surviving)
- §2.10-B Test 2+3 combined expanded: **FAIL** (0/108 surviving)

### §2.10.2 commitment binds

Both expanded families null under Qwen × 3 aggregators × expanded horizons. Per the §2.10.2 pre-registered commitment: **the §2.5 architectural pivot (or Options B / C) executes immediately.** No further Test 2 / Test 2+3 sensitivity configurations will be added without a [pre-registration-violation] commit.

## §2.10-A — Test 2-standalone expanded (n=45 cells)

- Criterion 1 (≥1 surviving BH-FDR): ❌ (0 surviving)
- Criterion 2 (directional consistency): ✅
- Criterion 3 (≥1 economic ≥50 bps): ❌ (0 economic)

### §2.10-A full table (45 cells)

| aggregator | category | horizon | n | β | t | p_raw | p_fdr | bps @ 1σ |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| A1_mean_label | fertilizer | 1d | 682 | +0.00046 | +0.33 | 0.739 | 0.948 | +2.9 |
| A1_mean_label | fertilizer | 3d | 682 | +0.00346 | +1.44 | 0.151 | 0.703 | +21.7 |
| A1_mean_label | fertilizer | 5d | 682 | +0.00174 | +0.52 | 0.602 | 0.948 | +10.9 |
| A1_mean_label | fertilizer | 10d | 680 | +0.00018 | +0.04 | 0.970 | 0.970 | +1.1 |
| A1_mean_label | fertilizer | 20d | 675 | +0.01229 | +1.75 | 0.081 | 0.588 | +76.9 |
| A1_mean_label | equipment | 1d | 614 | +0.00023 | +0.17 | 0.864 | 0.948 | +1.2 |
| A1_mean_label | equipment | 3d | 613 | +0.00076 | +0.34 | 0.731 | 0.948 | +4.0 |
| A1_mean_label | equipment | 5d | 612 | -0.00089 | -0.33 | 0.743 | 0.948 | -4.7 |
| A1_mean_label | equipment | 10d | 609 | -0.00171 | -0.42 | 0.672 | 0.948 | -9.1 |
| A1_mean_label | equipment | 20d | 602 | -0.00889 | -1.78 | 0.075 | 0.588 | -47.5 |
| A1_mean_label | processors | 1d | 426 | -0.00235 | -1.62 | 0.105 | 0.588 | -13.0 |
| A1_mean_label | processors | 3d | 426 | -0.00221 | -0.84 | 0.401 | 0.948 | -12.2 |
| A1_mean_label | processors | 5d | 426 | -0.00403 | -1.28 | 0.199 | 0.720 | -22.2 |
| A1_mean_label | processors | 10d | 424 | -0.00169 | -0.35 | 0.728 | 0.948 | -9.3 |
| A1_mean_label | processors | 20d | 418 | +0.00427 | +0.56 | 0.575 | 0.948 | +23.3 |
| A2_conf_weighted | fertilizer | 1d | 682 | +0.00074 | +0.53 | 0.597 | 0.948 | +4.5 |
| A2_conf_weighted | fertilizer | 3d | 682 | +0.00401 | +1.65 | 0.100 | 0.588 | +24.7 |
| A2_conf_weighted | fertilizer | 5d | 682 | +0.00254 | +0.76 | 0.450 | 0.948 | +15.7 |
| A2_conf_weighted | fertilizer | 10d | 680 | +0.00104 | +0.22 | 0.825 | 0.948 | +6.4 |
| A2_conf_weighted | fertilizer | 20d | 675 | +0.01359 | +1.88 | 0.060 | 0.588 | +83.6 |
| A2_conf_weighted | equipment | 1d | 614 | +0.00021 | +0.16 | 0.876 | 0.948 | +1.1 |
| A2_conf_weighted | equipment | 3d | 613 | +0.00068 | +0.30 | 0.762 | 0.948 | +3.6 |
| A2_conf_weighted | equipment | 5d | 612 | -0.00103 | -0.38 | 0.705 | 0.948 | -5.4 |
| A2_conf_weighted | equipment | 10d | 609 | -0.00195 | -0.46 | 0.642 | 0.948 | -10.3 |
| A2_conf_weighted | equipment | 20d | 602 | -0.00874 | -1.69 | 0.092 | 0.588 | -46.2 |
| A2_conf_weighted | processors | 1d | 426 | -0.00246 | -1.67 | 0.095 | 0.588 | -13.4 |
| A2_conf_weighted | processors | 3d | 426 | -0.00223 | -0.84 | 0.403 | 0.948 | -12.1 |
| A2_conf_weighted | processors | 5d | 426 | -0.00406 | -1.28 | 0.202 | 0.720 | -22.1 |
| A2_conf_weighted | processors | 10d | 424 | -0.00188 | -0.39 | 0.699 | 0.948 | -10.3 |
| A2_conf_weighted | processors | 20d | 418 | +0.00396 | +0.53 | 0.599 | 0.948 | +21.4 |
| A3_volume_normalized | fertilizer | 1d | 682 | +0.00005 | +0.06 | 0.952 | 0.970 | +0.5 |
| A3_volume_normalized | fertilizer | 3d | 682 | +0.00198 | +1.26 | 0.209 | 0.720 | +19.7 |
| A3_volume_normalized | fertilizer | 5d | 682 | +0.00106 | +0.47 | 0.635 | 0.948 | +10.6 |
| A3_volume_normalized | fertilizer | 10d | 680 | +0.00053 | +0.17 | 0.866 | 0.948 | +5.3 |
| A3_volume_normalized | fertilizer | 20d | 675 | +0.00822 | +1.74 | 0.081 | 0.588 | +81.7 |
| A3_volume_normalized | equipment | 1d | 614 | +0.00017 | +0.22 | 0.827 | 0.948 | +1.6 |
| A3_volume_normalized | equipment | 3d | 613 | +0.00097 | +0.76 | 0.447 | 0.948 | +9.1 |
| A3_volume_normalized | equipment | 5d | 612 | +0.00044 | +0.29 | 0.771 | 0.948 | +4.1 |
| A3_volume_normalized | equipment | 10d | 609 | -0.00034 | -0.14 | 0.885 | 0.948 | -3.2 |
| A3_volume_normalized | equipment | 20d | 602 | -0.00378 | -1.22 | 0.224 | 0.720 | -35.4 |
| A3_volume_normalized | processors | 1d | 426 | -0.00130 | -1.42 | 0.156 | 0.703 | -12.1 |
| A3_volume_normalized | processors | 3d | 426 | -0.00057 | -0.34 | 0.732 | 0.948 | -5.3 |
| A3_volume_normalized | processors | 5d | 426 | -0.00186 | -0.96 | 0.336 | 0.948 | -17.4 |
| A3_volume_normalized | processors | 10d | 424 | -0.00020 | -0.07 | 0.945 | 0.970 | -1.9 |
| A3_volume_normalized | processors | 20d | 418 | +0.00363 | +0.84 | 0.399 | 0.948 | +33.6 |

## §2.10-B — Test 2+3 combined expanded (n=108 cells)

- Criterion 1 (≥1 surviving BH-FDR): ❌ (0 surviving)
- Criterion 2 (directional consistency): ✅
- Criterion 3 (≥1 economic ≥50 bps): ❌ (0 economic)

### §2.10-B top-10 cells by raw p_interaction (diagnostic)

| aggregator | category | direction | horizon | line_item | β_int | p_raw | p_fdr | bps @ 1σ |
|---|---|---|---:|---|---:|---:|---:|---:|
| A3_volume_normalized | equipment | downside | 10d | yield | +0.0038 | 0.015 | 0.537 | +150.5 |
| A3_volume_normalized | fertilizer | downside | 10d | production | +0.0121 | 0.019 | 0.537 | +109.0 |
| A1_mean_label | fertilizer | downside | 10d | production | +0.0175 | 0.026 | 0.537 | +100.6 |
| A2_conf_weighted | equipment | downside | 10d | yield | +0.0059 | 0.032 | 0.537 | +142.6 |
| A1_mean_label | equipment | upside | 10d | production | -0.0206 | 0.038 | 0.537 | -140.6 |
| A1_mean_label | equipment | downside | 10d | yield | +0.0058 | 0.041 | 0.537 | +140.9 |
| A2_conf_weighted | equipment | upside | 10d | production | -0.0200 | 0.046 | 0.537 | -136.1 |
| A3_volume_normalized | equipment | upside | 10d | production | -0.0135 | 0.050 | 0.537 | -148.3 |
| A3_volume_normalized | equipment | upside | 5d | production | -0.0066 | 0.052 | 0.537 | -72.3 |
| A2_conf_weighted | fertilizer | downside | 10d | production | +0.0158 | 0.054 | 0.537 | +90.0 |

## Caveats

- **Single scorer (Qwen).** §2.10.1 deliberately omits FinBERT — the §2.9 Qwen A/B already established instrument-level agreement on the null.
- **CNH excluded via §7.1** (same as §2.9).
- **Family-wide FDR.** BH correction is applied to each family (§2.10-A: 45 cells; §2.10-B: 108 cells) separately, not across both jointly. Each family tests a different hypothesis (main-effect sentiment vs cross-modal interaction) so joint correction would mis-represent the test structure.
- **Economic bar elevated to 50 bps** from §2.4/§2.8.5's 30 bps, per §2.10.5. Deliberately stricter in multiple-testing territory.