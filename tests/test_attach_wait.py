"""The attach budget's own margin, measured in the code that spends it.

THE DEFECT (mistakes #70's sweep, second instance). Every retry budget in
this repo is justified by a comment citing a wait-time distribution measured
OUTSIDE the code: `connect_retry`'s docstring ("calibrated for a brief
reader"), `divergence.STREAM_ATTACH` ("FOUR OF FIVE attempts died on a clean
IOException after 30s ... the lock samples free 87% of the time"),
`atlas.ARCHIVE_ATTACH` ("24 reader attaches gave p50 0.0s, p90 7.6s, max
22.6s"). Not one of those numbers is emitted by any artifact, and the retry
ladder's outcome is BINARY -- it returns a connection or it raises. An attach
that returned on its first attempt and one that returned on its nineteenth of
twenty are indistinguishable in every report downstream, so a budget eroding
toward its cliff is invisible until the run it kills.

MEASURED 2026-09-21, both ends. Common case, all three live DBs while
`hyxlab-poly-sweep` was 10h into a run: p50 11ms, max 14ms -- the ledger is
almost always boring, which is exactly why a threshold sized by its tail
needs the tail written down. Refused case, a read-only attach against a held
writer: p50 0.03ms, so the elapsed time of an exhausted ladder IS its sleep
total and `budget_frac` is a true share, not an approximation.

Deliberately NOT a re-sizing of any budget: re-sizing off a freshly observed
number is how the `LIVE_GRACE_S` prose got wrong, and archived readings stay
comparable. `budget_frac` is the tripwire instead.
"""

from __future__ import annotations

import time

import duckdb
import pytest

from hyxlab import store as store_mod
from hyxlab.store import (
    attach_budget_s,
    attach_wait_block,
    attach_waits,
    connect_retry,
    open_retry,
    reset_attach_waits,
)


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_attach_waits()
    yield
    reset_attach_waits()


def _always_locked(monkeypatch, sleeps):
    monkeypatch.setattr(time, "sleep", sleeps.append)
    monkeypatch.setattr(
        store_mod.duckdb, "connect", lambda *a, **k: (_ for _ in ()).throw(duckdb.Error("locked"))
    )


def test_budget_arithmetic_is_one_definition_not_a_retyped_literal():
    """`atlas.ATTACH_BUDGET_S` was the hand-computed sum of a ladder defined
    four lines above it. The two drift apart the moment any field changes."""
    from simulator import atlas
    from simulator.divergence import STREAM_ATTACH

    assert attach_budget_s(15, 2.0) == 28.0  # the flat default's real total
    assert attach_budget_s(1, 2.0) == 0.0  # one attempt sleeps zero times
    assert attach_budget_s(**atlas.ARCHIVE_ATTACH) == atlas.ATTACH_BUDGET_S
    assert 210 < atlas.ATTACH_BUDGET_S < 220  # still the "~3.6 min" it claims
    assert 600 < attach_budget_s(**STREAM_ATTACH) < 660  # and the "~10.5 min"


def test_budget_is_the_sum_of_the_sleeps_the_ladder_actually_performs(monkeypatch):
    """The denominator has to be the ladder's own behaviour, or `budget_frac`
    is a share of a number nothing spends."""
    from simulator.divergence import STREAM_ATTACH

    sleeps: list[float] = []
    _always_locked(monkeypatch, sleeps)
    with pytest.raises(duckdb.Error):
        connect_retry("nope.duckdb", **STREAM_ATTACH)
    assert sum(sleeps) == pytest.approx(attach_budget_s(**STREAM_ATTACH))


def test_a_successful_attach_is_recorded_too(tmp_path):
    """The whole point: the ledger is not an error log. A run that succeeded
    after spending most of its budget is the reading that warns."""
    db = tmp_path / "ok.duckdb"
    connect_retry(db, read_only=False).close()
    (w,) = attach_waits()
    assert (w.db, w.attempts, w.ok) == ("ok.duckdb", 1, True)
    assert w.waited_s >= 0.0
    # A real share of a real budget: creating the file costs milliseconds
    # against a 28s ladder, so the field is a small NUMBER -- not absent,
    # not rounded away.
    assert 0.0 <= w.budget_frac < 0.01


def test_the_exhausted_attach_is_recorded_before_the_raise(monkeypatch):
    """The observation the ledger most needs is the one on the path that
    unwinds every frame that could have recorded it."""
    sleeps: list[float] = []
    _always_locked(monkeypatch, sleeps)
    with pytest.raises(duckdb.Error):
        connect_retry("nope.duckdb", retries=3, delay=1.0)
    (w,) = attach_waits()
    assert (w.attempts, w.ok, w.budget_s) == (3, False, 2.0)
    assert attach_wait_block()["exhausted_n"] == 1


def test_open_retry_is_on_the_same_ledger(tmp_path):
    """Two helpers, one question. A report that attaches through both must
    not publish half its attaches."""
    open_retry(tmp_path / "s.duckdb").close()
    (w,) = attach_waits()
    assert (w.db, w.ok) == ("s.duckdb", True)
    assert w.budget_s == attach_budget_s(30, 2.0)


def test_budget_frac_is_none_when_there_is_no_budget_to_spend(tmp_path):
    """A one-attempt ladder sleeps zero times. Publishing 0.0 there would
    read as "measured, and it was fine" -- the #70 failure exactly."""
    connect_retry(tmp_path / "one.duckdb", read_only=False, retries=1).close()
    (w,) = attach_waits()
    assert w.budget_s == 0.0
    assert w.budget_frac is None
    assert attach_wait_block()["attaches"][0]["budget_frac"] is None
    assert attach_wait_block()["budget_frac_max"] is None


def test_the_block_reports_the_worst_attach_not_the_last(tmp_path):
    """`budget_frac_max` is the margin. Pooling three attaches on three
    different budgets and publishing the final one would hide the tight
    one -- and the divergence report makes exactly three."""
    store_mod._record_attach("a.duckdb", 1, 0.5, 10.0, True)
    store_mod._record_attach("b.duckdb", 9, 90.0, 100.0, True)
    store_mod._record_attach("c.duckdb", 1, 0.1, 638.7, True)
    block = attach_wait_block()
    assert block["n"] == 3
    assert block["waited_s_max"] == 90.0
    assert block["waited_s_total"] == pytest.approx(90.6)
    assert block["budget_frac_max"] == 0.9
    assert block["exhausted_n"] == 0
    assert [a["db"] for a in block["attaches"]] == ["a.duckdb", "b.duckdb", "c.duckdb"]


def test_no_attaches_is_none_rather_than_an_empty_block():
    """An empty block reads as "attached instantly"; absence of a measurement
    must not render as a good measurement."""
    assert attach_wait_block() is None


def test_the_ledger_is_bounded_and_keeps_the_most_recent():
    """A daemon attaches for as long as it lives."""
    for i in range(store_mod._ATTACH_WAITS_MAX + 10):
        store_mod._record_attach(f"{i}.duckdb", 1, 0.0, 1.0, True)
    obs = attach_waits()
    assert len(obs) == store_mod._ATTACH_WAITS_MAX
    assert obs[-1].db == f"{store_mod._ATTACH_WAITS_MAX + 9}.duckdb"


def test_reset_scopes_the_block_to_one_run(tmp_path):
    store_mod._record_attach("stale.duckdb", 1, 0.0, 1.0, True)
    reset_attach_waits()
    connect_retry(tmp_path / "fresh.duckdb", read_only=False).close()
    assert [w.db for w in attach_waits()] == ["fresh.duckdb"]


def test_atlas_failure_line_prints_the_measured_wait_not_the_constant(monkeypatch, capsys):
    """THE REGRESSION. The busy-archive line read `waited {ATTACH_BUDGET_S}s`
    -- a constant formatted as though it were an observation, so it asserted
    214s whatever the run had spent."""
    from simulator import atlas

    sleeps: list[float] = []
    _always_locked(monkeypatch, sleeps)
    monkeypatch.setattr(atlas, "lock_holder", lambda exc: "hyxlab-poly-sweep (pid 42)")
    monkeypatch.setattr("sys.argv", ["atlas", "--db", "nope.duckdb"])
    with pytest.raises(SystemExit) as exc:
        atlas.main()
    msg = str(exc.value)
    # The real elapsed is ~0s here (sleep is stubbed) and the budget is 214s.
    # A line that cannot tell those apart is the defect.
    assert "waited 0s of a 214s budget" in msg
    assert "hyxlab-poly-sweep" in msg


def test_atlas_publishes_the_block_and_prints_the_margin(monkeypatch, tmp_path, capsys):
    """The margin beside the reading it paid for, and in the artifact beside
    the numbers it bought."""
    import json

    from simulator import atlas

    empty = {
        "buckets": [],
        "flagged": [],
        "flagged_robust": [],
        "flag_verdict": {
            "tested": 0,
            "buckets": 0,
            "counts": {"silent": 0, "flagged": 0, "not_significant": 0},
            "flagged_share_of_tested": None,
        },
        "quoted_verdict": {
            "day_weighted_survivors": 0,
            "tested_powered": 0,
            "unpowered": 0,
            "gap_retained_measurable": 0,
            "counts": {
                "confirmed": 0,
                "not_significant": 0,
                "refuted_sign": 0,
                "silent": 0,
            },
        },
    }
    monkeypatch.setattr(atlas, "build_atlas", lambda conn: dict(empty))
    monkeypatch.setattr(atlas, "tier_stability", lambda *a: None)
    monkeypatch.setattr(
        atlas, "verdict_stability", lambda *a: {"quoted_verdict": {"delta_vs_prior": None}}
    )
    monkeypatch.setattr(atlas, "annotate_quoted_looks", lambda *a: None)
    monkeypatch.setattr(atlas, "TIERS", ())

    db = tmp_path / "a.duckdb"
    connect_retry(db, read_only=False).close()
    reset_attach_waits()
    monkeypatch.setattr("sys.argv", ["atlas", "--db", str(db), "--out", str(tmp_path / "rep")])
    atlas.main()

    out = capsys.readouterr().out
    assert "[atlas] attach: 1 waited" in out
    assert "of its budget" in out

    (written,) = (tmp_path / "rep").glob("*.json")
    block = json.loads(written.read_text())["attach_wait"]
    assert block["n"] == 1
    assert block["attaches"][0]["db"] == "a.duckdb"
    assert block["attaches"][0]["budget_s"] == round(atlas.ATTACH_BUDGET_S, 1)
    assert block["exhausted_n"] == 0
    assert block["budget_frac_max"] is not None


def test_divergence_report_carries_the_block():
    """Three attaches on three different budgets reach that report -- and one
    of them uses the brief-reader DEFAULT against a daemon-held file."""
    import inspect

    from simulator import divergence

    src = inspect.getsource(divergence.main)
    assert '"attach_wait": attach_wait_block()' in src
    assert "reset_attach_waits()" in src
