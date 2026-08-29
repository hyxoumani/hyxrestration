"""Maker bracket direction verdict, read ACROSS readings.

`direction_verdict` partitions four readings inside ONE run. Across runs it
prints the same sentence -- "0 significant, N powered" -- whether the bracket
is gaining independent units on econ or has swapped to a weather market set
that never had the power to reject anything. That is the atlas quoted-tier
defect at a second site (mistakes #33's escalation rule), so the trajectory is
a field, with the units and the power ceilings beside every count.
"""

import ast
import json
from pathlib import Path

from simulator.queuescore import (
    DIRECTION_STATUSES,
    VERDICT_FIELDS,
    direction_stability,
)


def _report(
    tag,
    *,
    markets,
    underlyings,
    powered=4,
    significant=0,
    composition=None,
    at="2026-08-01 00:00:00",
):
    """A bracket report whose four direction readings are all `not_significant`
    unless `significant` says otherwise, over a known unit count."""
    counts = dict.fromkeys(DIRECTION_STATUSES, 0)
    counts["significant_over"] = significant
    counts["underpowered"] = 4 - powered
    counts["not_significant"] = powered - significant
    return {
        "generated_at": at,
        "orders": 100,
        "window_hours": 336.0,
        "market_composition": composition or {"KXCPI": 100},
        "concentration": {
            "markets": markets,
            "underlyings": underlyings,
            "market_min_sign_p": 0.0625,
            "underlying_min_sign_p": 0.125,
        },
        "concentration_strict": {
            "markets": markets,
            "underlyings": underlyings,
            "market_min_sign_p": 0.125,
            "underlying_min_sign_p": 0.25,
        },
        "direction_verdict": {
            "counts": counts,
            "powered": powered,
            "significant": significant,
        },
        # the reading's data identity: which virtual orders it scored
        "orders_detail": [
            {"market_id": f"KXCPI-{tag}-T0", "placed": f"2026-08-0{i} 00:00:00", "price": 0.5}
            for i in range(1, 4)
        ],
    }


def _write(out_dir, name, report):
    (out_dir / f"{name}.json").write_text(json.dumps(report))


def test_a_prior_that_predates_the_field_is_absent_not_a_zero(tmp_path):
    """The load-bearing case. `direction_verdict` shipped 34 runs into the
    archive; plotting those priors as `powered: 0, significant: 0` would read
    as 33 consecutive readings that tested a fill-model direction and found
    none -- an absent measurement drawn as a measured null."""
    old = _report("a", markets=8, underlyings=4)
    del old["direction_verdict"]
    _write(tmp_path, "20260801T140000", old)
    _write(tmp_path, "20260802T140000", _report("b", markets=8, underlyings=5))

    ds = direction_stability(tmp_path, _report("c", markets=20, underlyings=9))["direction_verdict"]

    assert ds["readings"] == 2  # the carrying prior and the current one
    assert ds["absent_in_priors"] == 1
    assert [p["units"]["underlyings"] for p in ds["trajectory"]] == [5, 9]


def test_a_prior_with_a_different_status_set_is_also_absent(tmp_path):
    """The counts must PARTITION the four readings against the same status
    enum. A report missing a status does not, and reading it as if it did
    attributes a reading nobody classified."""
    stale = _report("a", markets=8, underlyings=5)
    del stale["direction_verdict"]["counts"]["no_direction"]
    _write(tmp_path, "20260801T140000", stale)

    ds = direction_stability(tmp_path, _report("b", markets=8, underlyings=5))["direction_verdict"]

    assert ds["absent_in_priors"] == 1
    assert ds["readings"] == 1
    assert ds["delta_vs_prior"] is None  # nothing to compare against


def test_identical_counts_over_a_moved_unit_count_are_not_the_same_reading(tmp_path):
    """Every status count is IDENTICAL across the two readings while the
    independent units doubled. The delta has to show that, or "0 significant"
    reads as a null that firmed up when it was a different bracket entirely
    (mistakes #35)."""
    _write(tmp_path, "20260801T140000", _report("a", markets=8, underlyings=5))

    ds = direction_stability(tmp_path, _report("b", markets=20, underlyings=10))[
        "direction_verdict"
    ]

    d = ds["delta_vs_prior"]
    assert d["counts"]["significant_over"] == 0
    assert d["counts"]["not_significant"] == 0
    assert d["powered"] == 0
    assert d["markets"] == 12
    assert d["underlyings"] == 5


def test_powered_moves_are_visible_even_when_significance_never_does(tmp_path):
    """A run that could not have rejected anything and a run that tested and
    found nothing print the same `significant: 0`. `powered` is the field that
    tells them apart, so its trajectory is published first-to-latest."""
    _write(tmp_path, "20260801T140000", _report("a", markets=4, underlyings=3, powered=0))

    ds = direction_stability(tmp_path, _report("b", markets=20, underlyings=9))["direction_verdict"]

    assert ds["powered_first"] == 0
    assert ds["powered_latest"] == 4
    assert ds["delta_vs_prior"]["powered"] == 4
    assert ds["delta_vs_prior"]["counts"]["underpowered"] == -4


def test_a_weather_reading_is_not_spliced_into_an_econ_trajectory(tmp_path):
    """The bracket's market set is chosen by `--series`, so consecutive reports
    can measure two different populations. Splicing them manufactures movement
    out of a command-line flag, exactly the comparability rule
    `independence_vs_prior` already applies."""
    _write(
        tmp_path,
        "20260801T140000",
        _report("a", markets=8, underlyings=3, powered=0, composition={"KXHIGHNY": 100}),
    )
    _write(tmp_path, "20260802T140000", _report("b", markets=8, underlyings=5))

    ds = direction_stability(tmp_path, _report("c", markets=20, underlyings=9))["direction_verdict"]

    assert ds["incomparable_composition"] == 1
    assert ds["reports_read"] == 2
    assert ds["readings"] == 2
    assert ds["powered_first"] == 4  # the econ prior, not the weather one


def test_a_rerun_on_the_same_orders_does_not_compare_against_itself(tmp_path):
    """Re-running minutes after shipping a field is how the archive got its
    08-03 pair. The current run's own orders must be excluded, or the delta
    reads a guaranteed zero and the trajectory gains a free reading."""
    _write(tmp_path, "20260801T140000", _report("a", markets=8, underlyings=5))
    current = _report("b", markets=20, underlyings=9)
    _write(tmp_path, "20260802T140000", current)  # already on disk, same orders

    ds = direction_stability(tmp_path, current)["direction_verdict"]

    assert ds["readings"] == 2
    assert ds["delta_vs_prior"]["underlyings"] == 4


def test_a_silent_unit_count_makes_the_delta_absent_not_zero(tmp_path):
    """`concentration` predates the unit fields on the oldest reports. A
    missing markets count is not a count of zero, and differencing it would
    print a fabricated -8."""
    prior = _report("a", markets=8, underlyings=5)
    del prior["concentration"]["markets"]
    _write(tmp_path, "20260801T140000", prior)

    ds = direction_stability(tmp_path, _report("b", markets=20, underlyings=9))["direction_verdict"]

    assert ds["delta_vs_prior"]["markets"] is None
    assert ds["delta_vs_prior"]["underlyings"] == 4


def test_the_power_ceiling_is_carried_per_reading_not_per_run(tmp_path):
    """`min_sign_p` is 2^-decisive and `decisive` differs between the floor and
    the ceiling -- the two bounds lean different numbers of units. One number
    per run would report the wrong ceiling for two of the four readings."""
    ds = direction_stability(tmp_path, _report("a", markets=8, underlyings=5))["direction_verdict"]

    point = ds["trajectory"][-1]
    assert set(point["min_sign_p"]) == {
        "market_pess",
        "market_opt",
        "underlying_pess",
        "underlying_opt",
    }
    assert point["min_sign_p"]["underlying_pess"] != point["min_sign_p"]["underlying_opt"]
    assert sum(point["counts"].values()) == 4


def test_every_published_verdict_is_registered_for_tracking(tmp_path):
    """Sweep by ROLE and pin the answer (mistakes #37): a second `*_verdict`
    added to this report must be registered for tracking, or it is a red suite
    rather than a field nobody thought to compare across readings."""
    tree = ast.parse(Path("simulator/queuescore.py").read_text())
    published = {
        k.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for k in node.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value.endswith("_verdict")
    }
    assert published == set(VERDICT_FIELDS)
    assert VERDICT_FIELDS["direction_verdict"] == DIRECTION_STATUSES


def test_two_reports_over_the_same_orders_are_one_reading(tmp_path):
    """The archive holds a re-run pair minutes apart (08-03). Counting report
    FILES gives that pair a guaranteed-identical verdict step -- the same
    orders must yield the same verdict -- and biases the trajectory toward
    stable, the unit-of-counting class `new_share_vs_all` already handles."""
    _write(tmp_path, "20260801T140000", _report("a", markets=8, underlyings=5))
    _write(tmp_path, "20260801T142200", _report("a", markets=8, underlyings=5))

    ds = direction_stability(tmp_path, _report("b", markets=20, underlyings=9))["direction_verdict"]

    assert ds["reports_read"] == 2
    assert ds["comparable_readings"] == 1  # one data state, read twice
    assert ds["readings"] == 2  # that state, plus the current run
