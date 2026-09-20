"""A field published as `median` must BE the median.

`sorted(xs)[len(xs) // 2]` is the upper straddler of an even population,
not its median. It is always >= the true median, so a report field built
that way is biased in ONE direction, and for the two live sites it was
found at that direction was the flattering one:

  * `atlas._quoted_verdict.gap_retained_median` -- the share of the
    pooled implied-minus-realized gap that survives on two-sided books,
    i.e. the headline diagnostic for how much of the edge is real.
    Measured 2026-09-20 over the nine archived atlas readings: SEVEN had
    an even `gap_retained_measurable`, and every one of the seven
    published a number above the true median, by +0.0096 to +0.0349
    (worst: 20260829T021631, 0.4508 published against a true 0.4159 --
    8.4% relative).
  * `divergence.compare.price_delta_median` -- the signed fill-model
    calibration haircut. Six of thirteen archived runs have an even
    `matched`; all thirteen read exactly 0.0, so nothing archived moves,
    but this is the field that would carry a real haircut's sign.
  * `shadow_diurnal` bound 14's `span_hours.median`, in a file that
    already owned a correct `_median` and used it at its other two
    median sites.

This is mistakes #64's class -- a number that only appears in a report
artifact and in prose, outside every threshold the module guards -- with
#65's distinction one axis over: `iterate._median_slot` faces a list of
CANDIDATES and must refuse to name a member of an even family, whereas
these three are lists of QUANTITIES, where the even case has a median
and the honest answer is to compute it.

The AST guard is what stops a fourth site: the defect is two characters
wide and reads as correct.
"""

import ast
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).parent.parent
PACKAGES = ("simulator", "collector", "hyxlab", "strategies")

# `iterate._median_slot` is the member-naming case: it indexes
# `ordered[n // 2]` only to identify the two straddlers it then REFUSES
# to choose between (`even_family_has_no_median_member`), and it never
# publishes a field called `median` holding one of them. Named, not
# pattern-matched, so a new exemption has to be argued for here.
MEMBER_NAMING_EXEMPT = {"simulator/iterate.py"}


def _straddler_subscripts(tree: ast.AST) -> list[ast.Subscript]:
    """Subscripts of the form `x[<anything> // 2]`."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        idx = node.slice
        if (
            isinstance(idx, ast.BinOp)
            and isinstance(idx.op, ast.FloorDiv)
            and isinstance(idx.right, ast.Constant)
            and idx.right.value == 2
        ):
            out.append(node)
    return out


def _median_keyed_values(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """(key, value) for every dict entry whose key name mentions `median`."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and "median" in key.value
            ):
                out.append((key.value, value))
    return out


def test_no_median_field_is_an_upper_straddler():
    offenders = []
    for pkg in PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            if rel in MEMBER_NAMING_EXEMPT:
                continue
            tree = ast.parse(path.read_text())
            straddlers = {id(n) for n in _straddler_subscripts(tree)}
            for key, value in _median_keyed_values(tree):
                for sub in ast.walk(value):
                    if id(sub) in straddlers:
                        offenders.append(f"{rel}:{sub.lineno} field {key!r}")
    assert not offenders, (
        "a field published as a median is computed as the upper straddler of"
        f" an even population: {offenders}. Use `statistics.median` (or"
        " `shadow_diurnal._median`) for a list of quantities; for a list of"
        " CANDIDATES, refuse to name one, as `iterate._median_slot` does."
    )


def test_atlas_gap_retained_median_is_the_median():
    from simulator.atlas import _quoted_verdict

    def bucket(retained, i):
        return {
            "category": "C",
            "horizon": "1h",
            "decile": i,
            "flagged_day_weighted": True,
            "quoted_status": "confirmed",
            "quoted_powered": True,
            "quoted_gap_retained": retained,
        }

    # even population: 0.40 and 0.50 straddle; the upper straddler is 0.50
    out = _quoted_verdict([bucket(v, i) for i, v in enumerate([0.10, 0.40, 0.50, 0.90])])
    assert out["gap_retained_measurable"] == 4
    assert out["gap_retained_median"] == 0.45
    assert out["gap_retained_min"] == 0.1
    assert out["gap_retained_max"] == 0.9

    # odd population is unchanged, so readings stay comparable
    odd = _quoted_verdict([bucket(v, i) for i, v in enumerate([0.10, 0.40, 0.90])])
    assert odd["gap_retained_median"] == 0.4


def test_atlas_archived_readings_recompute_to_the_true_median():
    """The correction is re-derivable from every reading already taken.

    No new look is spent: `quoted_gap_retained` is published per bucket,
    so the corrected series for the whole archive is arithmetic on data
    the report already read. Any reading whose buckets are on file must
    now agree with `statistics.median` of them.
    """
    from simulator.atlas import _quoted_verdict

    reports = sorted((ROOT / "reports" / "atlas").glob("*.json"))
    if not reports:  # a fresh checkout has no archive
        return
    checked = 0
    for path in reports:
        doc = json.loads(path.read_text())
        buckets = doc.get("buckets")
        # `flagged_day_weighted` postdates the earliest readings; those
        # predate the field this block scores and are not re-derivable.
        need = (
            "flagged_day_weighted",
            "quoted_status",
            "quoted_gap_retained",
            "quoted_powered",
        )
        if not buckets or any(k not in b for b in buckets for k in need):
            continue
        out = _quoted_verdict(buckets)
        if not out["gap_retained_measurable"]:
            continue
        expected = [
            b["quoted_gap_retained"]
            for b in buckets
            if b["flagged_day_weighted"] and b.get("quoted_gap_retained") is not None
        ]
        assert out["gap_retained_median"] == round(statistics.median(expected), 4), path.name
        checked += 1
    assert checked, "atlas reports on disk but none carried scorable buckets"


def test_divergence_price_delta_median_is_the_median():
    """Four matched fills whose deltas are symmetric about zero.

    The upper straddler is +0.01 -- a fill model that reads as paying a
    cent too much on every trade -- while the median is 0.0.
    """
    from datetime import datetime, timedelta

    from simulator.divergence import compare

    class _RF:
        def __init__(self, qty, ts, price, market_id, side="yes"):
            self.market_id, self.side = market_id, side
            self.qty, self.price, self.fee, self.maker = qty, price, 0.0, False
            self.ts = ts

    t = datetime(2026, 9, 20, 0, 0, 0)
    # per-(market, side) buckets keep each pair from stealing another's
    # counterpart; the deltas are replay - shadow.
    shadow, replay, deltas = [], [], [-0.02, -0.01, 0.01, 0.02]
    for i, d in enumerate(deltas):
        ts = t + timedelta(minutes=10 * i)
        shadow.append((f"M{i}", "yes", 5.0, 0.50, 0.0, False, ts))
        replay.append(_RF(5.0, ts, round(0.50 + d, 4), f"M{i}"))

    rep = compare(shadow, replay)
    assert rep["matched"] == 4
    assert rep["price_delta_median"] == 0.0
    assert rep["price_delta_mean"] == 0.0

    # odd populations are unchanged, so archived readings stay comparable
    rep3 = compare(shadow[:3], replay[:3])
    assert rep3["matched"] == 3
    assert rep3["price_delta_median"] == -0.01  # rounded like price_delta_mean


def test_shadow_diurnal_span_median_is_the_median():
    from simulator.shadow_diurnal import _lifetime_census

    def run(rid, span):
        return {
            "run_id": rid,
            "lifetime": {
                "span_hours": span,
                "succession": "immediate",
                "successor_gap_s": 1.0,
            },
            "diurnal_level": {"settlement_absence": "no_balanced_panel"},
        }

    # even: straddlers 2.0 and 3.0, upper straddler 3.0, median 2.5
    out = _lifetime_census([run(f"r{i}", s) for i, s in enumerate([1.0, 2.0, 3.0, 10.0])])
    sp = out["no_balanced_panel"]["span_hours"]
    assert (sp["min"], sp["median"], sp["max"]) == (1.0, 2.5, 10.0)

    odd = _lifetime_census([run(f"r{i}", s) for i, s in enumerate([1.0, 2.0, 10.0])])
    assert odd["no_balanced_panel"]["span_hours"]["median"] == 2.0
