"""The sweep summary's rank slots must not name a variant that ties.

mistakes #64 bound 15 one report over: `simulator.iterate.family_report` is
the designated pre-reg quoting path ("the number a pre-reg verdict may
quote"), and its `best` was `sorted(srs, key=srs.get)[-1]` -- the best of
`n_trials` candidates with no tie check. `sharpe` maps EVERY degenerate
variant onto the same 0.0, so the tie is manufactured by the family, not
stumbled into; and the tie-break is load-bearing, because `deflated_sharpe`
reads the chosen member's own length and moments.
"""

import math

import pytest

from simulator.iterate import deflated_sharpe, family_report, moments, sharpe

# Four variants that never traded or never moved: sharpe() -> exactly 0.0 each.
NOTHING_TRADED = {
    "thr_10": [],
    "thr_20": [0.0],
    "thr_30": [0.0, 0.0, 0.0],
    "thr_40": [1.0, 1.0, 1.0],
}
REAL = [0.1, -0.05, 0.2, 0.0]
OTHER = [0.01, 0.0, -0.01, 0.0]


def test_all_degenerate_family_names_no_best_and_declines_dsr():
    assert {k: sharpe(v) for k, v in NOTHING_TRADED.items()} == dict.fromkeys(NOTHING_TRADED, 0.0)
    rep = family_report(NOTHING_TRADED)
    best = rep["best"]
    assert best["variant"] is None
    assert best["tied_n"] == 4 and best["of_n_trials"] == 4
    assert best["tied_variants"] == ["thr_10", "thr_20", "thr_30", "thr_40"]
    assert best["sr"] == 0.0 and best["reason"] == "tied"
    assert rep["deflated_sharpe_of_best"] is None
    assert "tied across 4 of 4" in rep["deflated_sharpe_declined"]
    assert rep["degenerate_variants"] == ["thr_10", "thr_20", "thr_30", "thr_40"]


def test_tie_break_was_load_bearing_not_cosmetic():
    """The refused pick spanned crash-to-0.5, so the span must be reported."""
    span = family_report(NOTHING_TRADED)["tied_best_dsr_span"]
    assert span["n"] == 4 and span["min"] == 0.0 and span["max"] == 0.5


def test_insertion_order_cannot_change_the_report():
    forward = family_report(NOTHING_TRADED)
    reverse = family_report(dict(reversed(list(NOTHING_TRADED.items()))))
    assert forward == reverse


def test_empty_returns_no_longer_raise_through_moments():
    assert moments([]) == (0.0, 3.0)
    d = deflated_sharpe([], n_trials=4, sr_var=0.0)
    assert d.dsr == 0.0 and d.n_returns == 0


def test_even_family_has_no_median_member():
    rep = family_report({"a": REAL, "b": OTHER})
    assert rep["best"]["variant"] == "a"
    med = rep["median"]
    assert med["variant"] is None and med["sr"] is None
    assert med["reason"] == "even_family_has_no_median_member"
    assert med["tied_variants"] == ["a", "b"]
    # the old code returned ordered[n // 2], i.e. the best variant again
    assert rep["best"]["variant"] in med["tied_variants"]


def test_duplicate_variants_tie_and_still_count_as_trials():
    rep = family_report({"w_5": REAL, "w_6": list(REAL), "w_7": OTHER})
    assert rep["n_trials"] == 3, "a look is a look; dropping it shrinks the divisor"
    assert rep["best"]["variant"] is None
    assert rep["best"]["tied_variants"] == ["w_5", "w_6"]
    assert rep["best"]["sr"] == pytest.approx(sharpe(REAL))
    assert rep["worst"] == {
        "variant": "w_7",
        "sr": pytest.approx(sharpe(OTHER)),
        "tied_variants": None,
        "tied_n": 1,
        "of_n_trials": 3,
        "reason": None,
    }
    # identical members: the refused pick would not have moved the number,
    # and saying so is the point of reporting the span rather than hiding it.
    span = rep["tied_best_dsr_span"]
    assert span["min"] == pytest.approx(span["max"])


def test_untied_family_is_unchanged_so_readings_stay_comparable():
    fam = {"a": [0.3, -0.1, 0.05, 0.02, 0.11], "b": REAL, "c": OTHER}
    rep = family_report(fam)
    srs = {k: sharpe(v) for k, v in fam.items()}
    assert len({round(v, 12) for v in srs.values()}) == 3
    ordered = sorted(srs, key=lambda k: srs[k])
    assert rep["best"]["variant"] == ordered[-1]
    assert rep["median"]["variant"] == ordered[1]
    assert rep["worst"]["variant"] == ordered[0]
    assert rep["deflated_sharpe_declined"] is None
    assert rep["deflated_sharpe_of_best"]["n_trials"] == 3
    assert not math.isnan(rep["deflated_sharpe_of_best"]["dsr"])


def test_odd_family_median_reports_its_own_tie():
    rep = family_report({"a": [], "b": [0.0, 0.0], "c": REAL})
    assert rep["median"]["variant"] is None
    assert rep["median"]["tied_variants"] == ["a", "b"]
    assert rep["median"]["reason"] == "tied"
    assert rep["best"]["variant"] == "c"
    assert rep["deflated_sharpe_of_best"] is not None


def test_single_variant_family_names_it():
    rep = family_report({"only": REAL})
    for slot in ("best", "median", "worst"):
        assert rep[slot]["variant"] == "only"
        assert rep[slot]["tied_n"] == 1 and rep[slot]["of_n_trials"] == 1
    assert rep["deflated_sharpe_of_best"]["n_trials"] == 1
