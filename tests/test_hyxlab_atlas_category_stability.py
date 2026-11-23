"""Atlas category stability: the venue relabels series, and a bucket's category
is the series' CURRENT one.

Measured 2026-11-23 on the daily backup slots: 33 series went `Companies` ->
`Financials` in six days and the weekly report lost 20 `Companies` buckets
(28 -> 45 -> 14 settled markets) with nothing in it saying why. Settled
markets only accumulate, so a category that shrank is the whole proof.
"""

import json
from pathlib import Path

from simulator.atlas import CATEGORY_STATUSES, category_stability


def _report(at: str, cats: dict[str, int], buckets: list[tuple[str, str, int]] = ()) -> dict:
    return {
        "generated_at": at,
        "data_fingerprint": {"settled_markets": sum(cats.values()), "settled_by_category": cats},
        "buckets": [{"category": c, "horizon": h, "decile": d} for c, h, d in buckets],
    }


def _write(tmp_path: Path, name: str, rep: dict) -> None:
    (tmp_path / f"{name}.json").write_text(json.dumps(rep))


def test_no_prior_is_its_own_status_not_stable(tmp_path: Path) -> None:
    out = category_stability(tmp_path, _report("2026-11-23 00:00:00", {"Crypto": 5}))
    assert out["status"] == "no_prior" and out["prior_generated_at"] is None
    assert out["status"] in CATEGORY_STATUSES


def test_a_category_that_shrank_is_a_relabel_with_the_buckets_it_took(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a",
        _report(
            "2026-11-16 02:48:04",
            {"Companies": 45, "Financials": 409118},
            [("Companies", "1h", 3), ("Companies", "72h", 1), ("Financials", "1h", 5)],
        ),
    )
    now = _report(
        "2026-11-23 17:40:54",
        {"Companies": 14, "Financials": 443071},
        [("Companies", "72h", 1), ("Financials", "1h", 5)],
    )
    out = category_stability(tmp_path, now)
    assert out["status"] == "relabelled"
    assert out["prior_generated_at"] == "2026-11-16 02:48:04"
    assert out["shrunk"] == {"Companies": {"prior": 45, "now": 14, "delta": -31}}
    assert out["settled_markets_left"] == 31
    assert out["buckets_vanished_n"] == 1
    assert out["buckets_vanished"] == [["Companies", "1h", 3]]


def test_growth_everywhere_is_stable_and_a_vanished_category_counts_as_zero(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path, "a", _report("2026-11-09 00:00:00", {"Companies": 28, "Crypto": 10, "Exotics": 7})
    )
    grown = category_stability(
        tmp_path, _report("2026-11-16 00:00:00", {"Companies": 45, "Crypto": 12, "Exotics": 7})
    )
    assert grown["status"] == "stable" and grown["shrunk"] == {}
    assert grown["settled_markets_left"] == 0
    gone = category_stability(
        tmp_path, _report("2026-11-16 00:00:00", {"Crypto": 12, "Exotics": 7})
    )
    assert gone["shrunk"] == {"Companies": {"prior": 28, "now": 0, "delta": -28}}


def test_the_prior_is_the_newest_other_data_state_never_this_run(tmp_path: Path) -> None:
    _write(tmp_path, "a", _report("2026-11-09 00:00:00", {"Companies": 99}))
    _write(tmp_path, "b", _report("2026-11-16 00:00:00", {"Companies": 45}))
    now = _report("2026-11-23 00:00:00", {"Companies": 50})
    _write(tmp_path, "c", now)  # a re-run finds its own file on disk
    out = category_stability(tmp_path, now)
    assert out["prior_generated_at"] == "2026-11-16 00:00:00" and out["status"] == "stable"


def test_a_prior_without_the_fingerprint_field_is_absent_not_zero(tmp_path: Path) -> None:
    _write(tmp_path, "a", {"generated_at": "2026-07-01 00:00:00", "data_fingerprint": {"x": 1}})
    out = category_stability(tmp_path, _report("2026-11-23 00:00:00", {"Crypto": 5}))
    assert out["status"] == "no_prior"
