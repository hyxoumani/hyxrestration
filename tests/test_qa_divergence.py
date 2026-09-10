"""The calibration report, given a reader.

`simulator.divergence` decides whether Tier-3 shadow and Tier-2 replay
are still one semantics -- the number under every backtest verdict. It
wrote that number to a JSON file nothing read, and mistake #46 is what
that cost: the report answered a three-week-old question, printed its
subject on line one, and the line scrolled past. These tests pin the
reader, and they pin the one property that separates it from the checks
retired as mistakes #29 and #45 -- it cannot pin itself to a past it
can no longer repair.
"""

import json
from datetime import datetime, timedelta

import duckdb
import pytest

import collector.qa as qa
from hyxlab.shadowruns import latest_complete_run, run_completed_at
from simulator.shadow import ShadowLedger

NOW = datetime(2026, 9, 10, 12, 0, 0)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Own section record per test — the deployment's real
    reports/qa/sections.json decides whether production QA alarms."""
    monkeypatch.setattr(qa, "STATE", tmp_path / "sections.json")
    qa._skipped.clear()
    qa._failures.clear()
    yield
    qa._skipped.clear()
    qa._failures.clear()


def _ledger(tmp_path, runs):
    """(run_id, started_at, n_fills) -> a shadow ledger holding just those.

    Schema from `ShadowLedger`, never a hand-copy: the selection reads
    real columns, so a schema change must reach these tests.
    """
    db = tmp_path / f"led{len(list(tmp_path.iterdir()))}" / "hyxshadow.duckdb"
    ShadowLedger(db)
    with duckdb.connect(str(db)) as conn:
        for run_id, started_at, n_fills in runs:
            conn.execute(
                "INSERT INTO shadow_runs VALUES (?,?,2.0,'probe',?)",
                [run_id, started_at, started_at],
            )
            conn.execute(
                "INSERT INTO shadow_fills SELECT ?,'probe','kalshi','M1','yes',1,0.5,0,false,"
                " ? + to_seconds(CAST(i AS BIGINT)) FROM range(?) t(i)",
                [run_id, started_at, n_fills],
            )
    return db


def _report(**over):
    """A clean report, shaped like the real 20260829T191841 one."""
    r = {
        "run_id": "b",
        "generated_at": "2026-09-09 20:50:04",
        "match_rate_vs_shadow": 1.0,
        "match_rate_vs_replay": 1.0,
        "price_delta_abs_mean": 0.0,
        "shadow_fills": 38143,
        "replay_fills": 38143,
        "unmatched_shadow_by_cause": {"unexplained": 0},
        "unmatched_replay_by_cause": {"unexplained": 0},
    }
    r.update(over)
    return r


def _run(tmp_path, runs, reports, now=NOW):
    """Run the section over a synthetic ledger + report dir; return failures."""
    db = _ledger(tmp_path, runs)
    out = db.parent / "reports"
    out.mkdir()
    for run_id, body in reports.items():
        (out / f"{run_id}.json").write_text(body if isinstance(body, str) else json.dumps(body))
    qa._failures.clear()
    qa.qa_divergence(now=now, path=str(db), reports=out)
    failed = set(qa._failures)
    qa._failures.clear()
    return failed


#: A finished run ("b") measured, plus a live successor the report must
#: never select. `b` finished when `live` started.
TWO_RUNS = [
    ("a", datetime(2026, 8, 10), 54007),
    ("b", datetime(2026, 8, 29), 38143),
    ("live", datetime(2026, 9, 7), 900),
]


def test_a_measured_newest_run_passes(tmp_path):
    assert _run(tmp_path, TWO_RUNS, {"b": _report()}) == set()


def test_an_unmeasured_run_inside_the_budget_does_not_alarm_yet(tmp_path):
    # `b` finished when `live` started, 2026-09-07. The daily timer has not
    # had 36h to produce the measurement, so complaining would alarm on the
    # normal state of a report that takes 30 minutes to compute.
    now = datetime(2026, 9, 7) + timedelta(hours=35)
    assert _run(tmp_path, TWO_RUNS, {"a": _report(run_id="a")}, now=now) == set()


def test_a_run_unmeasured_past_the_budget_fails(tmp_path):
    now = datetime(2026, 9, 7) + timedelta(hours=37)
    assert _run(tmp_path, TWO_RUNS, {"a": _report(run_id="a")}, now=now) == {
        qa.DIVERGENCE_FRESH_CHECK
    }


def test_the_mistake_46_shape_fails(tmp_path):
    # The live record on 2026-09-09: the only report on file was for the run
    # that ended 08-20, while eight later runs went unmeasured. Green before
    # this check existed; the whole point is that it is not green now.
    assert qa.DIVERGENCE_FRESH_CHECK in _run(tmp_path, TWO_RUNS, {"a": _report(run_id="a")})


def test_a_report_for_a_LATER_run_does_not_excuse_the_subject(tmp_path):
    # Reading "the newest report" instead of "the subject's report" would
    # let a stale-but-newer artefact (e.g. a hand `--run` on the live run)
    # stand in for the measurement actually owed.
    assert qa.DIVERGENCE_FRESH_CHECK in _run(tmp_path, TWO_RUNS, {"live": _report(run_id="live")})


def test_an_unparseable_report_reads_as_absent_rather_than_crashing_qa(tmp_path):
    # A truncated write must degrade to "not measured" — repairable by
    # re-running — and must not take the rest of the daily run down with it.
    assert _run(tmp_path, TWO_RUNS, {"b": "{ truncated"}) == {qa.DIVERGENCE_FRESH_CHECK}


def test_a_low_match_rate_fails_in_either_direction(tmp_path):
    # The pre-fix 2026-07-09 regime, which this floor clears by 45x.
    assert "shadow-vs-replay fill match rate" in _run(
        tmp_path, TWO_RUNS, {"b": _report(match_rate_vs_shadow=0.6943)}
    )
    assert "shadow-vs-replay fill match rate" in _run(
        tmp_path, TWO_RUNS, {"b": _report(match_rate_vs_replay=0.9337)}
    )


def test_the_worst_benign_window_on_record_still_passes(tmp_path):
    # 20260803T142853: 0.9954/0.9927, all of it reclassified seed-settling.
    # A floor that fired here would be a floor nobody could leave green.
    assert (
        _run(
            tmp_path,
            TWO_RUNS,
            {"b": _report(match_rate_vs_shadow=0.9954, match_rate_vs_replay=0.9927)},
        )
        == set()
    )


def test_a_systematic_price_disagreement_fails(tmp_path):
    assert "shadow-vs-replay fill price agreement" in _run(
        tmp_path, TWO_RUNS, {"b": _report(price_delta_abs_mean=0.01)}
    )


def test_float_noise_in_the_price_delta_does_not_fail(tmp_path):
    # 7e-6 is the largest the record has ever held, broken era included.
    assert _run(tmp_path, TWO_RUNS, {"b": _report(price_delta_abs_mean=7e-06)}) == set()


def test_a_missing_rate_is_a_failure_not_a_pass(tmp_path):
    # `None >= floor` raises in Python; a report missing the field must
    # fail loudly rather than being compared into a free pass.
    assert "shadow-vs-replay fill match rate" in _run(
        tmp_path, TWO_RUNS, {"b": _report(match_rate_vs_shadow=None)}
    )
    assert "shadow-vs-replay fill price agreement" in _run(
        tmp_path, TWO_RUNS, {"b": _report(price_delta_abs_mean=None)}
    )


def test_no_measurable_run_is_watched_not_passed_and_not_failed(tmp_path, capsys):
    # A fresh deployment (one live run) must not alarm — and must not bank a
    # free pass either (mistakes #28).
    assert _run(tmp_path, [("only_live", datetime(2026, 9, 7), 500)], {}) == set()
    assert "WATCH " + qa.DIVERGENCE_FRESH_CHECK in capsys.readouterr().out


def test_the_subject_advances_past_a_run_that_can_no_longer_be_measured(tmp_path):
    """The property mistakes #29 and #45 lacked: repairability.

    A run whose stream window aged out of the archive can never be
    measured. If it stayed the subject, this check would be red forever
    and would mask everything else in the daily run — exactly the mask
    the 2026-09-09 disk check turned out to be. It does not: the subject
    is the NEWEST finished run, so the next daemon restart hands the
    check a run it CAN go green on.
    """
    lost = [("unmeasurable", datetime(2026, 6, 1), 10), ("live", datetime(2026, 9, 7), 5)]
    assert qa.DIVERGENCE_FRESH_CHECK in _run(tmp_path, lost, {})
    # The daemon restarts; the new run finishes and is measured.
    restarted = lost + [("fresh", datetime(2026, 9, 9), 100), ("live2", datetime(2026, 9, 10), 3)]
    assert _run(tmp_path, restarted, {"fresh": _report(run_id="fresh")}) == set()


def test_completion_is_the_successors_start_not_the_last_equity_row(tmp_path):
    """The clock starts when the run stopped being LIVE.

    Read off the same fact `latest_complete_run` reads completion from,
    so the report and its reader cannot disagree about when a
    measurement came due. A daemon killed mid-flush wrote its last row
    earlier than that; a daemon that stays down owes nothing at all.
    """
    db = _ledger(tmp_path, TWO_RUNS)
    with duckdb.connect(str(db), read_only=True) as conn:
        assert latest_complete_run(conn) == "b"
        assert run_completed_at(conn, "b") == datetime(2026, 9, 7)
        assert run_completed_at(conn, "a") == datetime(2026, 8, 29)
        assert run_completed_at(conn, "live") is None
