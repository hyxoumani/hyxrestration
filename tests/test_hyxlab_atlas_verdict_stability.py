"""Atlas verdict stability: a partition read across readings.

`quoted_verdict` has printed `confirmed: 0` at every reading since it
shipped. That sentence is identical whether the untested share is receding
or growing, which is #32's defect moved onto the time axis -- so the
trajectory is a field, with the population beside every count.
"""

import ast
import json
from pathlib import Path

from simulator.atlas import (
    FLAG_STATUSES,
    QUOTED_STATUSES,
    VERDICT_POPULATION,
    verdict_stability,
)


def _report(fp, *, survivors, silent, confirmed=0, refuted=0, at="2026-08-01 00:00:00"):
    """A report whose quoted tier holds `survivors` buckets in known statuses."""
    counts = {
        "confirmed": confirmed,
        "refuted_sign": refuted,
        "silent": silent,
        "not_significant": survivors - silent - confirmed - refuted,
    }
    return {
        "generated_at": at,
        "data_fingerprint": {"settled_markets": fp},
        "flag_verdict": {
            "buckets": 10,
            "counts": dict.fromkeys(FLAG_STATUSES, 0) | {"silent": 10},
            "tested": 0,
        },
        "quoted_verdict": {"day_weighted_survivors": survivors, "counts": counts},
    }


def _write(out_dir, name, report):
    (out_dir / f"{name}.json").write_text(json.dumps(report))


def test_a_prior_that_predates_the_field_is_absent_not_a_zero(tmp_path):
    """The load-bearing case. A report shipped before `quoted_verdict` existed
    has NO opinion on its counts; plotting it as `confirmed: 0` would
    manufacture the very run of zeros the field was written to break up."""
    old = _report(1, survivors=0, silent=0)
    del old["quoted_verdict"]
    _write(tmp_path, "20260801T140000", old)
    _write(tmp_path, "20260802T140000", _report(2, survivors=6, silent=5))

    vs = verdict_stability(tmp_path, _report(3, survivors=8, silent=6))["quoted_verdict"]

    assert vs["readings"] == 2  # the carrying prior and the current one
    assert vs["absent_in_priors"] == 1
    assert [p["population"] for p in vs["trajectory"]] == [6, 8]
    assert all(p["counts"] for p in vs["trajectory"])


def test_a_prior_with_a_different_status_set_is_also_absent(tmp_path):
    """`refuted_sign` was added with the partition. A report carrying only the
    older statuses does not partition the same population, and reading its
    counts as if it did silently attributes a status nobody measured."""
    stale = _report(1, survivors=6, silent=5)
    del stale["quoted_verdict"]["counts"]["refuted_sign"]
    _write(tmp_path, "20260801T140000", stale)

    vs = verdict_stability(tmp_path, _report(2, survivors=8, silent=6))["quoted_verdict"]

    assert vs["absent_in_priors"] == 1
    assert vs["readings"] == 1
    assert vs["delta_vs_prior"] is None  # nothing to compare against


def test_counts_are_published_against_the_population_that_moved_under_them(tmp_path):
    """Counts alone cannot say whether a tier changed. Here every status count
    is IDENTICAL across the two readings while the population grew by 4 -- the
    delta must show that, or the reading is #35 on a time axis."""
    _write(tmp_path, "20260801T140000", _report(1, survivors=6, silent=5))

    vs = verdict_stability(tmp_path, _report(2, survivors=10, silent=5))["quoted_verdict"]

    d = vs["delta_vs_prior"]
    assert d["counts"]["silent"] == 0
    assert d["counts"]["confirmed"] == 0
    assert d["population"] == 4
    assert d["tested"] == 4
    # ...and the share moves even though the silent COUNT did not
    assert vs["tested_share_first"] == round(1 / 6, 4)
    assert vs["tested_share_latest"] == round(5 / 10, 4)


def test_tested_excludes_the_silent_and_the_shares_use_the_population(tmp_path):
    """`tested` is the honest denominator: survivors minus the untested."""
    vs = verdict_stability(tmp_path, _report(1, survivors=8, silent=6, refuted=1))["quoted_verdict"]

    point = vs["trajectory"][-1]
    assert point["tested"] == 2
    assert point["tested_share"] == 0.25
    assert point["shares"]["silent"] == 0.75
    assert point["counts"] == {
        "confirmed": 0,
        "not_significant": 1,
        "refuted_sign": 1,
        "silent": 6,
    }
    assert sum(point["counts"].values()) == point["population"]


def test_a_rerun_on_the_same_data_does_not_compare_against_itself(tmp_path):
    """Re-running minutes after shipping a field is how the archive got its
    duplicate pairs. The current data state must be excluded, or the delta
    reads a guaranteed zero and the trajectory gains a free reading."""
    _write(tmp_path, "20260801T140000", _report(1, survivors=6, silent=5))
    current = _report(2, survivors=10, silent=5)
    _write(tmp_path, "20260802T140000", current)  # already on disk, same fingerprint

    vs = verdict_stability(tmp_path, current)["quoted_verdict"]

    assert vs["readings"] == 2
    assert vs["delta_vs_prior"]["population"] == 4


def test_every_published_verdict_is_registered_for_tracking(tmp_path):
    """Sweep by ROLE and pin the answer (mistakes #37): a fourth `*_verdict`
    added to the report must be registered here, or it is a red suite rather
    than a field nobody thought to track."""
    tree = ast.parse(Path("simulator/atlas.py").read_text())
    published = {
        k.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for k in node.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value.endswith("_verdict")
    }
    assert published == set(VERDICT_POPULATION)
    assert VERDICT_POPULATION["quoted_verdict"] == ("day_weighted_survivors", QUOTED_STATUSES)
