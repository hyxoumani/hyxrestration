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
