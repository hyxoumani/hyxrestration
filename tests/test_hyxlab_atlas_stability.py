"""Atlas tier stability: a tier's survivor count only means something if
membership is stable across readings, and a reading is a distinct DATA
state — not a report file."""

import json

from simulator.atlas import tier_stability

TIERS = ("flagged", "flagged_robust", "flagged_day_robust", "flagged_day_weighted")


def _bucket(cat, hor, dec, tiers=()):
    b = {"category": cat, "horizon": hor, "decile": dec, "n": 300}
    for t in TIERS:
        b[t] = t in tiers
    return b


def _report(fingerprint, buckets, tiers=TIERS):
    """A report carrying only `tiers` — reports predating a tier have no key."""
    rep = {"data_fingerprint": fingerprint, "buckets": buckets}
    for t in tiers:
        rep[t] = [b for b in buckets if b[t]]
    return rep


def _write(out_dir, name, report):
    (out_dir / f"{name}.json").write_text(json.dumps(report))


def test_persistence_and_reentry_are_reported_per_bucket(tmp_path):
    """The load-bearing case: a bucket that leaves the tier and comes back
    must be marked `reentered`, and its persistence must be the FRACTION of
    readings it held — not a bare in/out of the latest one.

    A steady bucket and an oscillating bucket are indistinguishable in a
    survivor COUNT; this is the field that separates them.
    """
    steady = ("Economics", "1h", 2)
    osc = ("Financials", "6h", 7)
    reports = [
        # steady in every reading; osc in / out / in
        (1, [steady, osc]),
        (2, [steady]),
        (3, [steady, osc]),
    ]
    for i, (fp, members) in enumerate(reports):
        buckets = [
            _bucket(*steady, tiers=("flagged_day_robust",) if steady in members else ()),
            _bucket(*osc, tiers=("flagged_day_robust",) if osc in members else ()),
        ]
        _write(tmp_path, f"2026080{i + 1}T140000", _report({"settled_markets": fp}, buckets))

    current = _report(
        {"settled_markets": 4},  # a new data state, not a re-run of reading 3
        [
            _bucket(*steady, tiers=("flagged_day_robust",)),
            _bucket(*osc, tiers=("flagged_day_robust",)),
        ],
    )
    st = tier_stability(tmp_path, current)["flagged_day_robust"]

    assert st["readings"] == 3
    per = {tuple(b["bucket"]): b for b in st["buckets"]}
    assert per[steady]["persistence"] == 1.0
    assert per[steady]["reentered"] is False
    # 2 of 3 readings, and it LEFT in between — the number and the flag
    assert per[osc]["persistence"] == round(2 / 3, 4)
    assert per[osc]["reentered"] is True
    assert st["oscillators"] == [list(osc)]


def test_a_rerun_on_identical_data_is_not_a_second_reading(tmp_path):
    """Two reports with the same `data_fingerprint` are one measurement.
    Counting files makes a re-run contribute a guaranteed-zero churn step
    and biases every stability estimate toward stable.
    """
    b_in = [_bucket("Financials", "6h", 7, tiers=("flagged_day_robust",))]
    b_out = [_bucket("Financials", "6h", 7)]
    _write(tmp_path, "20260801T140000", _report({"settled_markets": 1}, b_in))
    # same data, re-run twice -> still ONE reading
    _write(tmp_path, "20260801T141000", _report({"settled_markets": 1}, b_in))
    _write(tmp_path, "20260801T142000", _report({"settled_markets": 1}, b_in))
    _write(tmp_path, "20260802T140000", _report({"settled_markets": 2}, b_out))

    current = _report({"settled_markets": 3}, b_in)
    st = tier_stability(tmp_path, current)["flagged_day_robust"]

    # 4 files, 2 distinct data states
    assert st["reports_read"] == 4
    assert st["readings"] == 2
    # in 1 of 2 real readings, not 3 of 4
    per = {tuple(b["bucket"]): b for b in st["buckets"]}
    assert per[("Financials", "6h", 7)]["persistence"] == 0.5
    assert per[("Financials", "6h", 7)]["reentered"] is True


def test_dedup_keeps_the_richest_report_for_a_data_state(tmp_path):
    """A re-run on identical data is exactly how a NEW tier first appears:
    ship the tier, re-run the report. Keeping the FIRST report per data
    state silently discards the only reading that carries the tier.
    """
    buckets = [_bucket("Economics", "1h", 2, tiers=("flagged", "flagged_day_robust"))]
    # earlier file: same data, tier did not exist yet
    _write(
        tmp_path,
        "20260801T140000",
        _report({"settled_markets": 1}, buckets, tiers=("flagged",)),
    )
    # later file: same data, re-run after the tier shipped
    _write(tmp_path, "20260801T141000", _report({"settled_markets": 1}, buckets))

    current = _report({"settled_markets": 2}, buckets)
    st = tier_stability(tmp_path, current)["flagged_day_robust"]

    assert st["readings"] == 1
    per = {tuple(b["bucket"]): b for b in st["buckets"]}
    assert per[("Economics", "1h", 2)]["persistence"] == 1.0


def test_a_tier_denominator_counts_only_readings_that_carried_the_tier(tmp_path):
    """`flagged_day_weighted` shipped after 20 atlas runs. Scoring its
    survivors against every archived report would read 3/23 and print a
    stable tier as maximally unstable.
    """
    b = [_bucket("Economics", "1h", 2, tiers=("flagged", "flagged_day_weighted"))]
    for i in range(4):  # old reports: only the base tier exists
        _write(
            tmp_path,
            f"2026072{i}T140000",
            _report({"settled_markets": i}, b, tiers=("flagged",)),
        )
    for i in range(2):  # new reports carry the strict tier
        _write(tmp_path, f"2026073{i}T140000", _report({"settled_markets": 10 + i}, b))

    current = _report({"settled_markets": 99}, b)
    out = tier_stability(tmp_path, current)

    assert out["flagged"]["readings"] == 6
    assert out["flagged_day_weighted"]["readings"] == 2
    per = {tuple(x["bucket"]): x for x in out["flagged_day_weighted"]["buckets"]}
    assert per[("Economics", "1h", 2)]["persistence"] == 1.0


def test_a_bucket_absent_from_the_data_is_not_counted_as_unstable(tmp_path):
    """Discrimination control. A bucket only becomes eligible once it has
    enough settled markets to appear in `buckets` at all. Counting the
    readings before it existed as "not in the tier" makes every genuinely
    new survivor read as churn — the opposite of the measurement.
    """
    old = [_bucket("Economics", "1h", 2, tiers=("flagged_day_robust",))]
    new_b = _bucket("Climate and Weather", "1h", 1, tiers=("flagged_day_robust",))
    for i in range(3):  # three readings where the new bucket does not exist
        _write(tmp_path, f"2026073{i}T140000", _report({"settled_markets": i}, old))
    _write(tmp_path, "20260801T140000", _report({"settled_markets": 9}, old + [new_b]))

    current = _report({"settled_markets": 99}, old + [new_b])
    st = tier_stability(tmp_path, current)["flagged_day_robust"]

    assert st["readings"] == 4
    per = {tuple(b["bucket"]): b for b in st["buckets"]}
    # eligible in 1 reading, in the tier for that 1 -> 1.0, not 0.25
    assert per[("Climate and Weather", "1h", 1)]["eligible_readings"] == 1
    assert per[("Climate and Weather", "1h", 1)]["persistence"] == 1.0
    assert per[("Climate and Weather", "1h", 1)]["reentered"] is False
    # the long-lived bucket is unaffected
    assert per[("Economics", "1h", 2)]["eligible_readings"] == 4
    assert per[("Economics", "1h", 2)]["persistence"] == 1.0


def test_churn_is_measured_against_the_last_distinct_data_state(tmp_path):
    """Tier-level churn: the symmetric difference against the previous real
    reading, so a headline count change can be read against how much the
    membership moved to produce it.
    """
    a = _bucket("Economics", "1h", 2, tiers=("flagged_day_robust",))
    b = _bucket("Financials", "6h", 7, tiers=("flagged_day_robust",))
    c = _bucket("Financials", "1h", 5, tiers=("flagged_day_robust",))
    _write(tmp_path, "20260731T140000", _report({"settled_markets": 1}, [a, b]))
    _write(tmp_path, "20260731T141000", _report({"settled_markets": 1}, [a, b]))

    current = _report({"settled_markets": 2}, [a, c])
    st = tier_stability(tmp_path, current)["flagged_day_robust"]

    # size is flat at 2 -- and the membership changed by two buckets
    assert st["size"] == 2
    assert st["prior_size"] == 2
    assert st["churn_vs_prior"] == 2
    assert st["gained"] == [["Financials", "1h", 5]]
    assert st["lost"] == [["Financials", "6h", 7]]


def test_a_rerun_does_not_compare_against_itself(tmp_path):
    """Found by validating in the real pipeline. Re-running atlas after
    shipping a tier is exactly what produced the archive's three duplicate
    pairs, and on a re-run the already-written report shares the current
    run's `data_fingerprint`. Left in the priors it is a self-comparison:
    churn reads 0 and every survivor gains a free reading.
    """
    a = _bucket("Economics", "1h", 2, tiers=("flagged_day_robust",))
    b = _bucket("Financials", "6h", 7, tiers=("flagged_day_robust",))
    b_out = _bucket("Financials", "6h", 7)  # eligible, but not in the tier
    _write(tmp_path, "20260731T140000", _report({"settled_markets": 1}, [a, b_out]))
    # this run already wrote its own report before stability was computed
    _write(tmp_path, "20260801T140000", _report({"settled_markets": 2}, [a, b]))

    current = _report({"settled_markets": 2}, [a, b])
    st = tier_stability(tmp_path, current)["flagged_day_robust"]

    assert st["readings"] == 1  # the 07-31 reading only
    assert st["churn_vs_prior"] == 1
    assert st["gained"] == [["Financials", "6h", 7]]
    per = {tuple(x["bucket"]): x for x in st["buckets"]}
    assert per[("Financials", "6h", 7)]["persistence"] == 0.0


def test_no_priors_reads_null_rather_than_stable(tmp_path):
    """First-ever run. Persistence of 1.0 against zero priors would print
    "maximally stable" for a tier nothing has ever confirmed.
    """
    b = [_bucket("Economics", "1h", 2, tiers=("flagged_day_robust",))]
    st = tier_stability(tmp_path, _report({"settled_markets": 1}, b))["flagged_day_robust"]

    assert st["readings"] == 0
    assert st["churn_vs_prior"] is None
    assert st["prior_size"] is None
    assert all(x["persistence"] is None for x in st["buckets"])
