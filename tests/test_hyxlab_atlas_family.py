"""Atlas quoted tier, corrected for the search that produced it.

The tier printed zero for five readings, and a zero needs no denominator.
On 2026-09-19 it printed its first `confirmed` ever -- one of 18
simultaneous tests, at a nominal alpha of 0.0443, when a complete null is
expected to hand back 0.9 of them. These pin the reading that the tier's
headline is the best of m tests and must be judged as such, and that the
per-test fields it has always carried are unchanged beside it.
"""

import json

import pytest

from simulator.atlas import (
    ALPHA,
    QUOTED_TESTED_STATUSES,
    Z95,
    _boundary_alpha,
    _days_to_detect,
    _quoted_family,
    annotate_quoted_looks,
    wilson,
)

# The production reading this whole correction was written against:
# Economics|1h|d2, 09-19, the first `confirmed` in the archive's history.
PROD_DAYS = 92
PROD_REALIZED = 0.1539
PROD_IMPLIED = 0.2440


def _tested(name, alpha, status="not_significant", **kw):
    cat, hor, dec = name.split("|")
    b = {
        "category": cat,
        "horizon": hor,
        "decile": int(dec[1:]),
        "quoted_status": status,
        "quoted_alpha": alpha,
        "realized_day_weighted": 0.10,
        "implied_day_weighted": 0.25,
    }
    b.update(kw)
    return b


def _names(buckets):
    return [f"{b['category']}|{b['horizon']}|d{b['decile']}" for b in buckets]


# --- the p-value the boolean was hiding --------------------------------


def test_boundary_alpha_puts_implied_exactly_on_the_interval_edge(tmp_path):
    """The definition, checked against `wilson` itself rather than a
    remembered constant: at the returned alpha the implied sits on the
    edge, and a hair either side flips the verdict."""
    a = _boundary_alpha(PROD_REALIZED, PROD_IMPLIED, PROD_DAYS)
    from statistics import NormalDist

    z_star = NormalDist().inv_cdf(1 - a / 2)
    lo, hi = wilson(PROD_REALIZED * PROD_DAYS, PROD_DAYS, z=z_star)
    assert hi == pytest.approx(PROD_IMPLIED, abs=1e-6)
    # inside at a wider interval (smaller alpha), outside at a tighter one
    wide_lo, wide_hi = wilson(PROD_REALIZED * PROD_DAYS, PROD_DAYS, z=z_star * 1.01)
    assert wide_lo <= PROD_IMPLIED <= wide_hi
    tight_lo, tight_hi = wilson(PROD_REALIZED * PROD_DAYS, PROD_DAYS, z=z_star * 0.99)
    assert not tight_lo <= PROD_IMPLIED <= tight_hi


def test_the_first_confirmation_in_the_archive_clears_by_a_hair():
    """The finding, pinned at its production numbers. `confirmed` is a
    boolean over ALPHA, so this bucket and one clearing the interval by 0.2
    printed identically -- and the tier's entire history of positives is
    this one, at 0.0443 against a 0.05 bar."""
    a = _boundary_alpha(PROD_REALIZED, PROD_IMPLIED, PROD_DAYS)
    assert a == pytest.approx(0.0443, abs=5e-4)
    assert a < ALPHA  # confirmed per-test, as the shipped tier reports
    assert a > ALPHA / 18  # and not against the 18 tests that produced it


def test_boundary_alpha_is_one_when_implied_equals_realized():
    """A perfectly calibrated bucket has no interval it is outside of."""
    assert _boundary_alpha(0.25, 0.25, 90) == pytest.approx(1.0, abs=1e-6)


def test_boundary_alpha_is_none_without_days():
    assert _boundary_alpha(0.10, 0.25, 0) is None


# --- the family --------------------------------------------------------


def test_the_production_family_confirms_nothing():
    """18 tests, smallest alpha 0.0443, Holm threshold 0.05/18 = 0.00278.
    The tier's first-ever confirmation does not survive the search that
    produced it -- so family-wise the atlas has still never confirmed a
    quoted bucket."""
    buckets = [
        _tested("Economics|1h|d2", 0.044291, status="confirmed"),
        *[_tested(f"Financials|1h|d{i}", 0.10 + i / 100) for i in range(13)],
        *[_tested(f"Commodities|6h|d{i}", None, status="refuted_sign") for i in range(4)],
    ]
    fam = _quoted_family(buckets)
    assert fam["tests"] == 18
    assert fam["alpha_family"] == pytest.approx(0.05 / 18, abs=1e-6)
    assert fam["expected_false_confirmations"] == 0.9
    assert fam["min_alpha"] == pytest.approx(0.044291)
    assert fam["confirmed_family"] == []
    assert buckets[0]["quoted_status"] == "confirmed"
    assert buckets[0]["quoted_confirmed_family"] is False


def test_a_bucket_below_the_family_threshold_survives():
    """The correction is not a blanket rejection: the same family with a
    bucket at 0.001 confirms it, so an empty `confirmed_family` is a
    measurement rather than a constant."""
    buckets = [
        _tested("Economics|1h|d2", 0.001, status="confirmed"),
        *[_tested(f"Financials|1h|d{i}", 0.10 + i / 100) for i in range(17)],
    ]
    fam = _quoted_family(buckets)
    assert fam["confirmed_family"] == ["Economics|1h|d2"]
    assert buckets[0]["quoted_confirmed_family"] is True


def test_holm_steps_down_and_stops_at_the_first_failure():
    """Step-DOWN, not a flat Bonferroni: with m = 4 the thresholds are
    0.0125, 0.0167, 0.025, 0.05. The second bucket clears its own step and
    is rejected; the third fails and the fourth is NOT rejected even though
    its alpha would clear the final 0.05 step."""
    buckets = [
        _tested("A|1h|d1", 0.010, status="confirmed"),
        _tested("A|1h|d2", 0.016, status="confirmed"),
        _tested("A|1h|d3", 0.030, status="confirmed"),
        _tested("A|1h|d4", 0.040, status="confirmed"),
    ]
    fam = _quoted_family(buckets)
    assert fam["confirmed_family"] == ["A|1h|d1", "A|1h|d2"]
    assert [b["quoted_confirmed_family"] for b in buckets] == [True, True, False, False]


def test_refuted_sign_counts_a_look_and_can_never_be_confirmed():
    """It consumed one of the family's looks -- on different data that same
    test would have reached the interval -- so excluding it would shrink the
    divisor using the outcome. It enters at alpha 1.0 and so can neither be
    rejected nor shorten the ladder above it."""
    with_refuted = [
        _tested("A|1h|d1", 0.004, status="confirmed"),
        *[_tested(f"B|1h|d{i}", None, status="refuted_sign") for i in range(11)],
    ]
    fam = _quoted_family(with_refuted)
    assert fam["tests"] == 12
    assert fam["alpha_family"] == pytest.approx(0.05 / 12, abs=1e-6)
    # 0.004 > 0.05/12 = 0.00417 is false, so it survives -- and every
    # refuted bucket is False rather than rejected on a missing alpha.
    assert fam["confirmed_family"] == ["A|1h|d1"]
    assert all(b["quoted_confirmed_family"] is False for b in with_refuted[1:])
    # drop the refuted looks and the SAME evidence reads as a smaller family
    alone = [_tested("A|1h|d1", 0.004, status="confirmed")]
    assert _quoted_family(alone)["tests"] == 1


def test_silent_buckets_are_not_in_the_family_and_carry_no_family_verdict():
    """`silent` is the test not running. It cannot have been a look."""
    buckets = [
        _tested("A|1h|d1", 0.001, status="confirmed"),
        _tested("B|1h|d1", None, status="silent"),
        _tested("C|1h|d1", None, status="not_applicable"),
    ]
    fam = _quoted_family(buckets)
    assert fam["tests"] == 1
    assert buckets[1]["quoted_confirmed_family"] is None
    assert buckets[2]["quoted_confirmed_family"] is None
    assert buckets[1]["quoted_days_to_detect_family"] is None


def test_family_verdict_nests_inside_the_per_test_tier():
    """The invariant every tier in this module holds: the stricter reading
    is a SUBSET. A bucket the family confirms must be one the per-test tier
    confirmed, at every family size."""
    for m in (1, 3, 18):
        buckets = [_tested(f"A|1h|d{i}", 0.05 / (m + 1), status="confirmed") for i in range(m)]
        _quoted_family(buckets)
        for b in buckets:
            if b["quoted_confirmed_family"]:
                assert b["quoted_alpha"] < ALPHA
                assert b["quoted_status"] == "confirmed"


def test_empty_family_does_not_divide_by_zero():
    buckets = [_tested("A|1h|d1", None, status="silent")]
    fam = _quoted_family(buckets)
    assert fam["tests"] == 0
    assert fam["alpha_family"] is None
    assert fam["expected_false_confirmations"] is None
    assert fam["confirmed_family"] == []


# --- what confirming honestly would cost -------------------------------


def test_family_cost_is_the_same_gap_at_the_stricter_z():
    """`quoted_days_to_detect_family` answers the forward question the
    finding raises -- what would it take -- in the same unit and at the same
    fixed effect size, so the two numbers are comparable. It is strictly
    larger, because the family's z is."""
    buckets = [
        _tested("Economics|1h|d2", 0.044291, status="confirmed"),
        *[_tested(f"F|1h|d{i}", 0.20) for i in range(17)],
    ]
    _quoted_family(buckets)
    b = buckets[0]
    per_test = _days_to_detect(b["realized_day_weighted"], b["implied_day_weighted"])
    assert b["quoted_days_to_detect_family"] > per_test
    assert _days_to_detect(0.10, 0.25, z=Z95) == per_test


# --- the looks, reported and not spent ---------------------------------


def _rep(fingerprint, statuses):
    return {
        "data_fingerprint": fingerprint,
        "buckets": [
            {"category": "Economics", "horizon": "1h", "decile": d, "quoted_status": s}
            for d, s in statuses.items()
        ],
    }


def test_looks_count_distinct_data_states_excluding_the_current_run(tmp_path):
    """The exposure the family correction does NOT bound: the same bucket
    tested again every few days on a sample that only grows. Counted on
    DATA states -- a re-run on identical data is not a second look -- and
    the current run is the subject, never one of its own priors."""
    (tmp_path / "a.json").write_text(json.dumps(_rep({"m": 1}, {2: "confirmed"})))
    (tmp_path / "b.json").write_text(json.dumps(_rep({"m": 2}, {2: "not_significant"})))
    # same data state as b: a re-run, not a look
    (tmp_path / "c.json").write_text(json.dumps(_rep({"m": 2}, {2: "not_significant"})))
    # the current run's own state, already on disk
    (tmp_path / "d.json").write_text(json.dumps(_rep({"m": 3}, {2: "confirmed"})))

    current = _rep({"m": 3}, {2: "confirmed"})
    annotate_quoted_looks(tmp_path, current)
    assert current["buckets"][0]["quoted_looks"] == 2


def test_a_silent_prior_is_not_a_look(tmp_path):
    """A reading in which the test did not run cannot have spent an alpha.
    Same rule as the family's membership, so the two denominators agree."""
    (tmp_path / "a.json").write_text(json.dumps(_rep({"m": 1}, {2: "silent"})))
    (tmp_path / "b.json").write_text(json.dumps(_rep({"m": 2}, {2: "not_applicable"})))
    (tmp_path / "c.json").write_text(json.dumps(_rep({"m": 3}, {2: "refuted_sign"})))
    current = _rep({"m": 9}, {2: "confirmed"})
    annotate_quoted_looks(tmp_path, current)
    assert current["buckets"][0]["quoted_looks"] == 1
    assert set(QUOTED_TESTED_STATUSES) == {"confirmed", "not_significant", "refuted_sign"}


def test_a_prior_predating_the_field_contributes_nothing(tmp_path):
    """Not a zero -- the report simply cannot say whether a test ran."""
    old = {
        "data_fingerprint": {"m": 1},
        "buckets": [{"category": "Economics", "horizon": "1h", "decile": 2}],
    }
    (tmp_path / "a.json").write_text(json.dumps(old))
    current = _rep({"m": 2}, {2: "confirmed"})
    annotate_quoted_looks(tmp_path, current)
    assert current["buckets"][0]["quoted_looks"] == 0
