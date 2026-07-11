"""B5 iteration machinery: inverse-normal vs table values, DSR
deflation behavior, embargo correctness in purged folds."""

import math
import random
from datetime import date, timedelta

import pytest

from simulator.iterate import (
    deflated_sharpe,
    expected_max_sr,
    inv_norm,
    moments,
    norm_cdf,
    purged_folds,
    sharpe,
)


def test_inv_norm_against_standard_table():
    for p, z in [(0.90, 1.28155), (0.95, 1.64485), (0.975, 1.95996), (0.99, 2.32635)]:
        assert inv_norm(p) == pytest.approx(z, abs=1e-4)
        assert inv_norm(1 - p) == pytest.approx(-z, abs=1e-4)
        assert norm_cdf(z) == pytest.approx(p, abs=1e-4)  # round-trip


def test_moments_of_normal_ish_sample():
    rng = random.Random(1)
    xs = [rng.gauss(0, 1) for _ in range(20000)]
    skew, kurt = moments(xs)
    assert abs(skew) < 0.1 and abs(kurt - 3.0) < 0.2


def test_psr_special_case_hand_computed():
    """With skew=0, kurt=3 and one trial (SR0=0), DSR reduces to
    Φ(SR·sqrt(T-1)/sqrt(1+SR²/2)) — check against an erf-computed value."""
    rng = random.Random(2)
    xs = [rng.gauss(0.02, 1.0) for _ in range(5000)]
    res = deflated_sharpe(xs, n_trials=1)
    sr, (skew, kurt) = sharpe(xs), moments(xs)
    z = sr * math.sqrt(len(xs) - 1) / math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr**2)
    assert res.sr0 == 0.0
    assert res.dsr == pytest.approx(0.5 * (1 + math.erf(z / math.sqrt(2))), abs=1e-12)


def test_more_trials_deflate_harder():
    rng = random.Random(3)
    xs = [rng.gauss(0.03, 1.0) for _ in range(2000)]
    d1 = deflated_sharpe(xs, n_trials=1).dsr
    d10 = deflated_sharpe(xs, n_trials=10).dsr
    d1000 = deflated_sharpe(xs, n_trials=1000).dsr
    assert d1 > d10 > d1000


def test_expected_max_sr_grows_with_family_and_variance():
    assert expected_max_sr(1, 1.0) == 0.0
    assert expected_max_sr(100, 1.0) > expected_max_sr(10, 1.0) > 0
    assert expected_max_sr(10, 4.0) == pytest.approx(2 * expected_max_sr(10, 1.0))


def test_negative_skew_lowers_dsr():
    rng = random.Random(4)
    base = [rng.gauss(0.05, 1.0) for _ in range(3000)]
    # graft a crash tail: same-ish SR, heavy negative skew
    skewed = base[:-30] + [-6.0] * 30
    shift = (sum(base) / len(base)) - (sum(skewed) / len(skewed))
    skewed = [x + shift for x in skewed]  # re-center to equal mean
    s_base, s_skew = deflated_sharpe(base, 5), deflated_sharpe(skewed, 5)
    assert s_skew.skew < -0.5
    if abs(s_base.sr - s_skew.sr) < 0.02:  # comparable SRs
        assert s_skew.dsr < s_base.dsr


def test_purged_folds_embargo_excludes_both_sides():
    closes = {f"M{i:02d}": date(2026, 1, 1) + timedelta(days=i) for i in range(20)}
    folds = purged_folds(closes, n_folds=4, embargo_days=3)
    assert len(folds) == 3
    for train, test in folds:
        test_start = min(closes[m] for m in test)
        for m in train:
            # strictly before test span minus embargo
            assert closes[m] < test_start - timedelta(days=3)
        # embargoed markets appear in NEITHER set
        belt = [m for m in closes if test_start - timedelta(days=3) <= closes[m] < test_start]
        for m in belt:
            assert m not in train and m not in test

    # walk-forward: no train market closes after any test market
    train2, test2 = folds[1]
    assert max(closes[m] for m in train2) < min(closes[m] for m in test2)


def test_purged_folds_all_markets_partitioned_without_overlap():
    closes = {f"M{i:02d}": date(2026, 1, 1) + timedelta(days=i) for i in range(21)}
    folds = purged_folds(closes, n_folds=3, embargo_days=0)
    for train, test in folds:
        assert not set(train) & set(test)
    # with zero embargo, every earlier market trains for the last fold
    train_last, test_last = folds[-1]
    assert len(train_last) + len(test_last) == 21


def test_family_report_deflates_best_by_family_size():
    from simulator.iterate import family_report

    rng = random.Random(9)
    family = {f"v{i}": [rng.gauss(0.0, 1.0) for _ in range(1500)] for i in range(20)}
    rep = family_report(family)
    assert rep["n_trials"] == 20
    srs = sorted(v for v in (sharpe(r) for r in family.values()))
    assert rep["best"]["sr"] == pytest.approx(srs[-1])
    assert rep["worst"]["sr"] == pytest.approx(srs[0])
    d = rep["deflated_sharpe_of_best"]
    assert d["n_trials"] == 20 and d["sr0"] > 0
    # the best of 20 zero-skill variants must not look significant
    assert d["dsr"] < 0.95
