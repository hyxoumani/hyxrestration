"""The shutdown path, run as a real process and stopped with a real SIGTERM.

Everything that tests this path today builds the daemon with
`Daemon.__new__`, a stub store and stub channel coroutines, and in the one
test that sends a signal (`test_stream_stalls.py`) `_final_drain` itself is
replaced by a lambda. Those tests prove the pieces. They cannot prove the
assembly, because every one of them runs INSIDE the pytest process, where
the signal is delivered to a loop pytest owns, the `finally` runs with the
suite's interpreter state, and nothing ever exits.

That matters more here than it usually would. Three separate fixes to this
exact path are queued behind a deferred restart of `hyxlab-stream` --
`streamstore.py` (2026-09-18 02:15Z), the `_final_drain` budget refusal
(09-18 21:00Z) and the `exclusive` lock (09-19 02:45Z) -- and the panel
guard means they all land together, in production, on a path whose failure
mode is silent data loss and which has logged `shutdown; stats` ZERO times
(mistakes #54). The production rehearsal is restart-gated. This one is not.

So: spawn `Daemon.run()` in a real subprocess against a real `StreamStore`
on a real DuckDB file, with rows sitting in the buffer and nothing having
flushed them yet, send it SIGTERM the way systemd does, and assert the
three things an operator reads afterwards --

  * the process exits 0, with no traceback over the shutdown line;
  * `shutdown; stats` is in its output (the marker whose absence WAS the
    whole proof in mistakes #54);
  * every buffered row is in the archive. FLUSH_SECS is 15s and the signal
    lands ~1s in, so the periodic flush has not run: the drain is the only
    thing that could have saved them, which is exactly the restart that
    used to drop the buffer.

The channel coroutines are the only stubs, because they are the only part
that needs a venue. `_install_stop`, the `finally`, `_final_drain`, the
`exclusive` lock, `flusher`, `StreamStore` and the archive are all real.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from collector import streamd

REPO = Path(__file__).resolve().parent.parent

#: Rows the driver buffers before it signals readiness. Small enough that
#: the drain is instant at the measured 7,100 rows/s, large enough that a
#: partial write is visible as one.
ROWS = 500

# The daemon under test, as `collector.streamd` will actually run it. Only
# the three venue loops are replaced; keeping the real `flusher` is the
# point of the exercise, since the lock this path now takes exists solely
# because the drain and the flusher share one store.
DRIVER = """
import asyncio, sys
from datetime import UTC, datetime
from collector import streamd
from hyxlab.streamstore import BookEvent, StreamStore

db, n = sys.argv[1], int(sys.argv[2])


async def idle():
    await asyncio.Event().wait()


store = StreamStore(db)
daemon = streamd.Daemon(store, {})
now = datetime.now(UTC)
store.append_events(
    [
        BookEvent("kalshi", "M%d" % i, now, None, None, i, "delta", "yes", 0.5, 1.0)
        for i in range(n)
    ]
)
for name in ("kalshi_trades", "kalshi_books", "poly_books"):
    setattr(daemon, name, idle)
print("READY %d" % store.pending, flush=True)
asyncio.run(daemon.run())
print("FINAL %s" % store.counts(), flush=True)
"""


def _archived(db: Path) -> int:
    from hyxlab.store import connect_retry

    conn = connect_retry(str(db), read_only=True)
    try:
        return conn.execute("SELECT count(*) FROM book_events").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.skipif(
    not hasattr(signal, "SIGTERM") or sys.platform.startswith("win"),
    reason="POSIX signal delivery",
)
def test_sigterm_to_a_real_streamd_process_drains_the_buffer_and_logs_the_shutdown(tmp_path):
    db = tmp_path / "hyxstream.duckdb"
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "PYTHONUNBUFFERED": "1",
        # Daemon() reads these; an absent key is the intended no-auth path,
        # but be explicit so a developer's live .env cannot reach a test.
        "KALSHI_API_KEY_ID": "",
        "KALSHI_PRIVATE_KEY_PATH": "",
    }
    # cwd is the tmp tree so `FlushStalls`' relative `data/stream_stalls.jsonl`
    # lands there and not in the repo's real ledger.
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", DRIVER, str(db), str(ROWS)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        ready = proc.stdout.readline()
        assert ready.startswith(f"READY {ROWS}"), f"driver never buffered: {ready!r}"
        # Let the loop reach `asyncio.wait` and install its handlers. Far
        # short of FLUSH_SECS (15s), so the periodic flush has not fired and
        # the rows are still only in memory.
        time.sleep(1.0)
        assert _archived(db) == 0, "the periodic flush beat the signal; test proves nothing"

        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=streamd.DRAIN_BUDGET_S + 60)
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a hung drain
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, f"SIGTERM did not end cleanly ({proc.returncode}):\n{out}"
    assert "shutdown; stats" in out, (
        f"the shutdown line mistakes #54 never saw is still absent:\n{out}"
    )
    assert "Traceback" not in out, f"a traceback printed over the shutdown line:\n{out}"
    assert "CRITICAL" not in out, f"the drain reported a hole:\n{out}"
    assert _archived(db) == ROWS, f"the drain lost rows:\n{out}"
