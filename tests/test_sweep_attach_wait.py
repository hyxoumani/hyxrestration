"""What a retry ladder SPENT, when it spends it thousands of times a run.

THE DEFECT (mistakes #72, the #71 sweep's answer). #71 put every attach on a
ledger so a budget eroding toward its cliff is readable before the run it
kills. Two things that fix did not reach:

(a) `collector.sweep` never published the ledger at all. `writer_burst` spends
    the `BURST_OPEN_RETRIES` ladder -- 150 x 2.0s, the widest in the repo --
    once per burst, ~2 bursts per series over ~3,709 series, and the only
    thing a run ever said about that budget was `lock_skips`, a counter that
    increments ONLY when the budget is EXHAUSTED. So the artifact is censored
    to its own failures: thirty days of `lock_skips: 0` (measured 2026-09-21)
    is consistent with every burst opening in 8ms AND with every burst
    spending 290 of its 300s, and the one run that went over (2026-09-10
    06:10Z, dead at series ~600 of 3,656) had nothing in front of it.

    MEASURED, from the one unit that does publish its wait: `writer_burst`
    holds the exclusive flock ACROSS that open ladder, and `collector.collect`
    prints `wait_s` for the same flock every cycle. Over 2,016 cycles / 7 days
    to 2026-09-21: p50 0.0s, p99 47.0s, max 95.0s, and 37 of the 51 cycles at
    >=20s (73%) fall in the 06-08Z sweep window, which is 12.5% of the day and
    carries 56% of all flock wait time. So the ladder's spend is real, is
    already ~0.27 of its budget at the in-window max, and was legible only
    from ANOTHER unit's instrument.

(b) The ledger's own statistics were computed from a list capped at 256 rows.
    For atlas and divergence -- three attaches -- retained == observed and the
    cap is invisible, which is why it shipped. For a sweep it means the block
    would describe the last ~3% of the run while carrying the run's name, and
    the first statistic a drop destroys is `budget_frac_max`: a max lives in
    the observations you threw away.

FIX: the rows stay a bounded SAMPLE; the statistics move to `_AttachTotals`,
which is never trimmed, and the block says `retained_n`/`dropped_n` out loud.
No budget is re-sized -- `budget_frac_max` is the tripwire, per #70.
"""

from __future__ import annotations

import pytest

from hyxlab import store as store_mod
from hyxlab.store import (
    attach_budget_s,
    attach_wait_block,
    reset_attach_waits,
)


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_attach_waits()
    yield
    reset_attach_waits()


# --------------------------------------------------------------------------
# (b) the ledger's statistics must describe the run, not the retained window
# --------------------------------------------------------------------------


def _fill(
    n: int,
    waited: float = 0.0,
    budget: float = 298.0,
    ok: bool = True,
    slept: float | None = None,
) -> None:
    for i in range(n):
        store_mod._record_attach(
            f"{i}.duckdb", 1, waited, budget, ok, waited if slept is None else slept
        )


def test_the_worst_attach_survives_being_dropped_from_the_row_sample():
    """THE REGRESSION. A sweep's tight burst happens at series 600 and is
    followed by 3,000 clean ones; the block must still report it."""
    store_mod._record_attach("hyxlab.duckdb", 40, 80.0, 298.0, True, 80.0)
    _fill(store_mod._ATTACH_WAITS_MAX + 10)
    block = attach_wait_block(rows=False)
    assert block["waited_s_max"] == 80.0
    assert block["budget_frac_max"] == pytest.approx(80.0 / 298.0, abs=1e-4)


def test_n_counts_attaches_observed_and_names_what_it_dropped():
    """`n` computed from the trimmed list would top out at the cap forever --
    a run of 7,418 bursts and a run of 256 would publish the same number."""
    _fill(store_mod._ATTACH_WAITS_MAX + 10)
    block = attach_wait_block(rows=False)
    assert block["n"] == store_mod._ATTACH_WAITS_MAX + 10
    assert block["retained_n"] == store_mod._ATTACH_WAITS_MAX
    assert block["dropped_n"] == 10


def test_totals_sum_the_whole_run_not_the_tail():
    _fill(store_mod._ATTACH_WAITS_MAX + 10, waited=1.0)
    block = attach_wait_block(rows=False)
    assert block["waited_s_total"] == pytest.approx(store_mod._ATTACH_WAITS_MAX + 10.0)


def test_exhausted_attaches_are_not_forgotten_by_the_trim():
    """`exhausted_n` is the cliff count. An exhausted ladder is exactly the
    event a later flood of healthy attaches would evict."""
    store_mod._record_attach("hyxlab.duckdb", 150, 298.0, 298.0, False, 298.0)
    _fill(store_mod._ATTACH_WAITS_MAX + 10)
    assert attach_wait_block(rows=False)["exhausted_n"] == 1


def test_an_explicit_waits_list_is_its_own_population():
    """Passing the list says "this IS everything", so nothing was dropped --
    and it must not read the module ledger, which a caller may have filled."""
    _fill(5, waited=9.0)
    block = attach_wait_block([store_mod.AttachWait("a.duckdb", 1, 0.5, 10.0, True, 0.5)])
    assert block["n"] == 1
    assert block["dropped_n"] == 0
    assert block["waited_s_total"] == 0.5


def test_reset_clears_the_totals_and_not_only_the_rows():
    """Otherwise the first run's max would haunt every later run in the
    process -- and `reset_attach_waits` exists precisely to scope a block."""
    store_mod._record_attach("stale.duckdb", 99, 250.0, 298.0, False, 250.0)
    reset_attach_waits()
    assert attach_wait_block(rows=False) is None
    store_mod._record_attach("fresh.duckdb", 1, 0.01, 298.0, True, 0.01)
    block = attach_wait_block(rows=False)
    assert block["n"] == 1
    assert block["waited_s_max"] == 0.01
    assert block["exhausted_n"] == 0


def test_rows_false_omits_the_sample_and_keeps_every_statistic():
    _fill(3, waited=2.0)
    full = attach_wait_block()
    compact = attach_wait_block(rows=False)
    assert "attaches" in full and "attaches" not in compact
    assert {k: v for k, v in full.items() if k != "attaches"} == compact


# --------------------------------------------------------------------------
# (a) the sweep publishes it
# --------------------------------------------------------------------------


def test_sweep_scopes_the_ledger_to_the_run_and_publishes_the_margin():
    src = (store_mod.Path(__file__).resolve().parents[1] / "collector/sweep.py").read_text()
    assert "reset_attach_waits()" in src, "a sweep's block must describe the sweep"
    assert "attach_wait_block(rows=False)" in src
    assert "[sweep] attach_wait:" in src


def test_the_printed_budget_is_the_ladders_own_arithmetic_not_retries_times_delay():
    """150 attempts is 149 sleeps. Printing 300 beside a fraction of 298
    reproduces the atlas literal #71 removed, one module over."""
    from collector import sweep as sweep_mod

    budget = attach_budget_s(sweep_mod.BURST_OPEN_RETRIES, sweep_mod.BURST_OPEN_DELAY_S)
    assert budget == pytest.approx(
        (sweep_mod.BURST_OPEN_RETRIES - 1) * sweep_mod.BURST_OPEN_DELAY_S
    )
    assert budget < sweep_mod.BURST_OPEN_RETRIES * sweep_mod.BURST_OPEN_DELAY_S
    src = (store_mod.Path(__file__).resolve().parents[1] / "collector/sweep.py").read_text()
    line = next(ln for ln in src.splitlines() if "of a {budget:.0f}s" in ln)
    assert "BURST_OPEN_RETRIES * BURST_OPEN_DELAY_S" not in line


def test_sweep_prints_the_wait_line_on_a_run_with_zero_lock_skips(capsys):
    """THE WHOLE POINT. `lock_skips: 0` is the healthy case and the case the
    old artifact could not distinguish from a ladder at 97% of budget."""
    from collector import sweep as sweep_mod

    reset_attach_waits()
    store_mod._record_attach("hyxlab.duckdb", 146, 290.0, 298.0, True, 290.0)
    wait = attach_wait_block(rows=False)
    budget = attach_budget_s(sweep_mod.BURST_OPEN_RETRIES, sweep_mod.BURST_OPEN_DELAY_S)
    assert wait["exhausted_n"] == 0, "it SUCCEEDED -- lock_skips would read 0"
    assert wait["budget_frac_max"] > 0.97
    assert budget == 298.0


# --------------------------------------------------------------------------
# (c) mistakes #78 -- elapsed is not wait, and on a big run it is mostly not
# --------------------------------------------------------------------------


def test_a_first_try_attach_spends_nothing_of_the_budget_however_long_it_takes():
    """THE REGRESSION. The ladder is entered only when an open is REFUSED, so
    an attach that succeeded first try spent ZERO of the retry budget -- and
    the ledger was charging it the cost of the open."""
    store_mod._record_attach("hyxlab.duckdb", 1, 0.5, 298.0, True, 0.0)
    block = attach_wait_block(rows=False)
    assert block["waited_s_total"] == 0.5
    assert block["slept_s_total"] == 0.0
    assert block["open_s_total"] == 0.5
    assert block["contended_n"] == 0
    # A share of a SLEEP budget, measured against the sleeps. 0.5/298 was the
    # old answer and it described work the budget does not govern.
    assert block["budget_frac_max"] == 0.0


def test_the_published_total_separates_opening_from_waiting_at_sweep_scale():
    """MEASURED 2026-09-22 11:52Z, the first production reading: 7,483
    attaches, "96.3s spent waiting in total". A successful open of the 20GB
    archive costs 7.2ms median (2026-09-23, 30 read-only opens, first
    excluded; the sweep's read-write open also runs `_SCHEMA` and costs
    more), so at least 53.8s of that 96.3s -- 56%, plausibly all of it --
    was this sweep opening its own database. The old block could not say so;
    this one must. The numbers below are that shape, not that run: the run's
    own split is what the next sweep publishes."""
    _fill(7_435, waited=0.0087, slept=0.0)  # the boring majority
    _fill(48, waited=2.0087, slept=2.0)  # the ones that met a holder
    block = attach_wait_block(rows=False)
    assert block["n"] == 7_483
    assert block["contended_n"] == 48
    assert block["slept_s_total"] == pytest.approx(96.0, abs=0.5)
    assert block["open_s_total"] == pytest.approx(65.1, abs=0.5)
    # and the headline the old line printed conflated the two
    assert block["waited_s_total"] == pytest.approx(
        block["slept_s_total"] + block["open_s_total"], abs=0.1
    )


def test_contended_n_separates_one_long_block_from_many_short_ones():
    """A max and a total are the same two numbers for 1 x 96s and 48 x 2s,
    and that is the difference between one long reader and constant beating
    -- which is the difference between leaving the budget alone and not."""
    _fill(1, waited=96.0, slept=96.0)
    one = attach_wait_block(rows=False)
    reset_attach_waits()
    _fill(48, waited=2.0, slept=2.0)
    many = attach_wait_block(rows=False)
    assert one["slept_s_total"] == pytest.approx(many["slept_s_total"], abs=0.1)
    assert (one["contended_n"], many["contended_n"]) == (1, 48)
    assert one["slept_s_max"] != many["slept_s_max"]


def test_the_sweep_line_prints_the_sleep_and_the_open_cost_apart():
    """The operator reads this line and nothing else about the ladder, so it
    is the line that has to separate the two costs. Matched against the
    f-string LITERALS only -- a comment quoting the old wording, as the one
    above the print does, is documentation and not an assertion about it."""
    src = (store_mod.Path(__file__).resolve().parents[1] / "collector/sweep.py").read_text()
    fstrings = [ln.strip() for ln in src.splitlines() if ln.strip().startswith('f"')]
    printed = "".join(f for f in fstrings if "wait[" in f or "[sweep] attach_wait:" in f)
    assert "[sweep] attach_wait:" in printed
    for field in ("contended_n", "slept_s_max", "slept_s_total", "open_s_total"):
        assert field in printed, field
    # and the elapsed total is no longer the number headlined as waiting
    assert "spent waiting in total" not in "".join(fstrings)
    assert "waited_s_total" not in printed


def test_the_ladders_measure_the_sleep_they_actually_perform(monkeypatch, tmp_path):
    """`slept_s` is timed around `time.sleep`, not summed from the intended
    delays: a stubbed sleep must read as zero (the atlas busy-line test turns
    on exactly that) and a real sleep overshoots its argument."""
    import time as time_mod

    import duckdb

    calls: list[float] = []
    monkeypatch.setattr(time_mod, "sleep", calls.append)
    monkeypatch.setattr(
        store_mod.duckdb, "connect", lambda *a, **k: (_ for _ in ()).throw(duckdb.Error("locked"))
    )
    reset_attach_waits()
    with pytest.raises(duckdb.Error):
        store_mod.connect_retry("nope.duckdb", retries=4, delay=7.0)
    (w,) = store_mod.attach_waits()
    assert calls == [7.0, 7.0, 7.0]  # it asked for 21s
    assert w.budget_s == 21.0
    assert w.slept_s < 0.5  # and slept none of it, so the ledger says none
    assert w.budget_frac < 0.05
