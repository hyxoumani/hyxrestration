"""`collector.poly_sweep._flush` charges its burst to the attach ledger.

The 15-17h Polymarket sweep writes the 5-minute collector's file in one
flocked burst per batch through the `_open` wrapper (open + thread pin),
and nothing summed those bursts: the done line was JSON totals. Now each
burst's body runs under `charge_hold`, and `main` prints the run's total
through the one renderer after `done:` (the #96/#102 shape, third timer
unit in October after `signals` and `tradepass`).
"""

import collector.poly_sweep as ps
from hyxlab.store import (
    Store,
    attach_wait_block,
    attach_wait_line,
    attach_waits,
    reset_attach_waits,
)


def _batch() -> dict:
    return {
        "infos": [],
        "stats": [],
        "prices": [],
        "trades": [],
        "tail_stops": [],
        "n_prices": 0,
        "n_trades": 0,
        "n_tail_stops": 0,
    }


def test_a_flush_burst_is_a_measured_hold_on_the_collectors_file(tmp_path, monkeypatch):
    db = str(tmp_path / "hyxlab.duckdb")
    Store(db).close()
    monkeypatch.setattr(ps, "LOCK_FILE", str(tmp_path / "writer.lock"))
    reset_attach_waits()

    ps._flush(db, _batch())
    ps._flush(db, _batch())

    rows = attach_waits()
    assert len(rows) == 2 and all(r.held_s is not None for r in rows), rows
    line = attach_wait_line(attach_wait_block(db="hyxlab.duckdb"), prefix="[poly]")
    assert "on hyxlab.duckdb 2 attaches" in line and "held 2 for" in line, line
    assert "0 unmeasured" in line, line


# 2026-10-23: the done line's `flush 157.6 min` had the attach ledger
# explaining ~112 min; the other ~45 min was in front of the attach, on
# the flock every writer of the collector's file queues on, which no
# instrument timed. `_flush` now charges that wait to the batch and the
# done line publishes it as `flush_lock_min`.
def test_the_flock_wait_in_front_of_the_attach_is_timed(tmp_path, monkeypatch):
    import fcntl
    import subprocess
    import sys
    import time

    db = str(tmp_path / "hyxlab.duckdb")
    Store(db).close()
    lock_path = tmp_path / "writer.lock"
    monkeypatch.setattr(ps, "LOCK_FILE", str(lock_path))
    reset_attach_waits()

    free = _batch()
    ps._flush(db, free)
    assert 0.0 <= free["lock_wait_s"] < 0.2, free["lock_wait_s"]

    # Hold the flock from ANOTHER process (flock is per open file, and a
    # same-process second open would succeed) for 0.6s, then flush.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX);"
            " print('held', flush=True); time.sleep(0.6)",
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        t0 = time.monotonic()
        queued = _batch()
        ps._flush(db, queued)
        waited = time.monotonic() - t0
    finally:
        holder.wait(timeout=10)
    assert queued["lock_wait_s"] >= 0.3, (queued["lock_wait_s"], waited)
    assert queued["lock_wait_s"] <= waited + 1e-3
    # The lock is released after the burst either way.
    with open(lock_path, "a") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)


# 2026-11-17: the run's holds rose 3,487 s -> 5,647 s for the same flushes
# and rows, and the done line could not say which statement (mistakes #209).


def _seeded(tmp_path, monkeypatch):
    db = str(tmp_path / "hyxlab.duckdb")
    Store(db).close()
    monkeypatch.setattr(ps, "LOCK_FILE", str(tmp_path / "writer.lock"))
    return db


def test_a_flush_charges_each_statement_it_ran_and_only_those(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    db = _seeded(tmp_path, monkeypatch)
    batch = _batch()
    batch["prices"] = [("tok", "m1", "Yes", datetime(2026, 11, 17, tzinfo=UTC), 0.5)]
    ps._flush(db, batch)
    assert set(batch["step_s"]) == {"open", "prices", "close"}
    assert all(v >= 0.0 for v in batch["step_s"].values())
    assert batch["n_prices"] == 1 and batch["prices"] == []


def test_step_seconds_sum_across_flushes(tmp_path, monkeypatch):
    db = _seeded(tmp_path, monkeypatch)
    batch = _batch()
    ps._flush(db, batch)
    first = dict(batch["step_s"])
    ps._flush(db, batch)
    assert batch["step_s"]["close"] > first["close"] and batch["step_s"]["open"] > first["open"]


def test_a_statement_that_raises_is_charged_and_the_long_line_still_prints(
    tmp_path, monkeypatch, capsys
):
    import time as _time

    import pytest

    db = _seeded(tmp_path, monkeypatch)
    monkeypatch.setattr(ps, "LONG_FLUSH_S", 0.05)

    def slow_boom(self, rows):
        _time.sleep(0.1)
        raise RuntimeError("disk full")

    monkeypatch.setattr(Store, "insert_trades", slow_boom)
    batch = _batch()
    batch["trades"] = [("polymarket", "m1", "t1", None, 0.5, 1.0, "yes", False)]
    with pytest.raises(RuntimeError):
        ps._flush(db, batch)
    assert batch["step_s"]["trades"] >= 0.1
    assert batch["trades"], "a failed flush keeps its batch for the retry"
    out = capsys.readouterr().out
    assert "[poly] long flush: held 0." in out and "trades 0.1" in out
    assert "for 0 prices + 1 trades staged" in out


def test_the_long_flush_line_splits_the_hold_and_leaves_open_out_of_it():
    spent = {"open": 5.0, "prices": 4.04, "trades": 80.2, "close": 9.86, "stats": 0.01}
    assert ps.long_flush_line(spent, 2.0, 4012, 5210) == (
        "[poly] long flush: held 94.1s after 2.0s queued"
        " (open 5.0, prices 4.0, trades 80.2, close 9.9) for 4012 prices + 5210 trades staged"
    )


def test_a_routine_flush_prints_no_line():
    assert ps.LONG_FLUSH_S >= 45.0, "the routine hold is 17-32 s; keep the line to the tail"
    assert ps.long_flush_line({"open": 500.0, "trades": 31.0, "close": 3.0}, 0.0, 1, 1) is None
