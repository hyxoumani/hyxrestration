"""The tradepass instance lock is held for the WHOLE run (mistakes #107).

`trades_backfill.main` took `instance_lock_or_reason("trades_backfill")`
into `lock`, then opened `data/writer.lock` for its schema burst with
`with open(LOCK_FILE, "a") as lock:` -- rebinding the name. The instance
lock's file object lost its last reference, CPython closed it, and the
flock was released before the first fetch: the guard
`test_instance_lock_discipline` verifies (takes a lock, exits 75 when
refused) was satisfied by a lock held for a few milliseconds.

Probed from a SUBPROCESS, mid-fetch, both arms: a flock in this process
on a second descriptor would also conflict, but the rule (ops.md, #90)
is to prove a hold from outside the process that holds it.
"""

import subprocess
import sys
from datetime import datetime

import collector.trades_backfill as tb
from hyxlab.lockid import instance_lock_path
from hyxlab.store import Store

PROBE = (
    "import fcntl, sys\n"
    "f = open(sys.argv[1], 'a')\n"
    "try:\n"
    "    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "except OSError:\n"
    "    print('REFUSED')\n"
    "else:\n"
    "    print('ACQUIRED')\n"
)


def _probe(path: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", PROBE, path], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_instance_lock_is_held_across_every_fetch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "t.duckdb")
    store = Store(db)
    try:
        for i in range(3):
            store.conn.execute(
                "INSERT INTO markets (venue, market_id, series, close_time, result)"
                " VALUES ('kalshi', ?, 'KXT', ?, 'yes')",
                [f"KXT-{i}", datetime(2026, 7, 1 + i)],
            )
    finally:
        store.close()
    monkeypatch.setattr(tb, "LOCK_FILE", str(tmp_path / "data" / "writer.lock"))
    path = instance_lock_path("trades_backfill")

    seen: list[str] = []

    def get_trades(ticker, session=None, **kwargs):
        seen.append(_probe(path))
        return [], False

    monkeypatch.setattr(tb.kalshi, "get_trades", get_trades)
    monkeypatch.setattr(tb.time, "sleep", lambda s: None)
    monkeypatch.setattr("sys.argv", ["tradepass", "--db", db, "--rps", "1000"])
    tb.main()

    assert seen == ["REFUSED"] * 3, (
        f"a second tradepass could take the instance lock mid-run: {seen}"
    )
    # Vacuity guard, the other arm: the probe CAN acquire, so REFUSED above
    # is the run's hold and not a probe that never succeeds.
    assert _probe(path) == "ACQUIRED"
