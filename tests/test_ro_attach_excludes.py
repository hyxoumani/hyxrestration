"""EXP-1390: when does a read-only DuckDB attach start and stop excluding a writer?

WHY THIS EXISTS. Three passes of instrumentation (#91 `held_attach`, #96
`held_duck`/`held_store`, #102 `collector.qa._held_ro`) rest on one premise
that had never been measured: DuckDB's file lock is taken by the OPEN, so an
IDLE read-only connection excludes the writer exactly as hard as a busy one.
On 2026-09-29 the two arms disagreed -- `collector.qa` measured an 819.0s hold
on `hyxstream.duckdb` and `data/stream_stalls.jsonl` recorded a 592.5s episode
that began about five minutes into it -- and #92 had written down that the
victim's span is the hold ROUNDED UP, which is the opposite direction. Either
the premise was wrong or one of the clocks was.

The premise is right; the victim under-reports (see `collector.streamd`'s
flusher and `tests/test_stream_stalls.py`). This is the measurement that
settled it, kept because the premise is load-bearing and belongs to DuckDB,
not to this repo -- an upgrade can change it under us.

MEASURED FROM A SECOND PROCESS, BOTH ARMS, and it has to be: DuckDB serves a
second SAME-process attach from the instance it already has open, so an
in-process probe is green against a leak and green against no lock at all
(mistakes #97, learned one helper over). The vacuity guard is the writer
succeeding immediately before the open and immediately after the close: a
test that only sees failures passes equally against a broken writer.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hyxlab.store import duck_connect

#: The writer's cadence. Short enough that a ~2s hold resolves at both edges,
#: long enough that the probe is not a busy loop. The whole test is ~4s.
CADENCE_S = 0.25
HOLD_S = 2.0
IDLE_BEFORE_QUERY_S = 1.0

_WRITER = textwrap.dedent(
    """
    import json, sys, time
    sys.path.insert(0, {repo!r})
    import duckdb
    from hyxlab.store import duck_connect

    path, cadence, deadline = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    end = time.time() + deadline
    i = 0
    while time.time() < end:
        i += 1
        # streamd's exact pattern: open read-write, write, close.
        try:
            with duck_connect(path) as conn:
                conn.execute("insert into t values (?)", [i])
            ok = True
        except duckdb.Error:
            ok = False
        print(json.dumps({{"at": time.time(), "ok": ok}}), flush=True)
        time.sleep(cadence)
    """
)


@pytest.fixture
def probe_db(tmp_path):
    path = tmp_path / "probe.duckdb"
    with duck_connect(str(path)) as conn:
        conn.execute("create table t (i integer)")
    return path


def _writer(repo: Path, db: Path, runtime: float) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _WRITER.format(repo=str(repo)), str(db), str(CADENCE_S), str(runtime)],
        stdout=subprocess.PIPE,
        text=True,
    )


def test_a_read_only_attach_excludes_the_writer_from_the_open_until_the_close(probe_db):
    """The whole premise, in one run: OK -- open -- FAIL (no query yet) --
    query -- FAIL -- close -- OK."""
    import time

    repo = Path(__file__).resolve().parent.parent
    runtime = 1.0 + IDLE_BEFORE_QUERY_S + HOLD_S + 1.0
    proc = _writer(repo, probe_db, runtime)
    try:
        time.sleep(1.0)  # let the writer prove the file is writable first
        opened = time.time()
        conn = duck_connect(str(probe_db), read_only=True)
        # DELIBERATELY IDLE. The claim is that the OPEN excludes, so the
        # connection must not run a single query in this window.
        time.sleep(IDLE_BEFORE_QUERY_S)
        queried = time.time()
        conn.execute("select count(*) from t").fetchone()
        time.sleep(HOLD_S - IDLE_BEFORE_QUERY_S)
        conn.close()
        closed = time.time()
    finally:
        out, _ = proc.communicate(timeout=30)

    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    before = [r for r in rows if r["at"] < opened]
    idle = [r for r in rows if opened < r["at"] < queried]
    busy = [r for r in rows if queried < r["at"] < closed]
    after = [r for r in rows if r["at"] > closed]

    # Vacuity, both ends: without these the test passes against a writer that
    # never worked and against a hold that never happened.
    assert before and all(r["ok"] for r in before), rows
    assert after and all(r["ok"] for r in after), rows

    # The finding. An attach that has issued NO query already excludes.
    assert idle and not any(r["ok"] for r in idle), rows
    assert busy and not any(r["ok"] for r in busy), rows
