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
