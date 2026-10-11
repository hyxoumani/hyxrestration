"""`collector.poly_sweep._open`: the DuckDB thread pin, and the lock it must
not leak while applying it.

The pin is what makes `hyxlab-poly-sweep`'s MemoryMax a bound at all (see the
unit file): a trades flush's scan threads hold buffers OUTSIDE `memory_limit`,
so at the default 24 threads a 2G cap was oom-killed on one real flush, and at
4 the same flush fit under 1G. The release is proved from OUTSIDE the process
(mistakes #90): DuckDB serves a same-process re-attach from the instance it
already holds, so an in-process probe is green against a leak.
"""

from __future__ import annotations

import subprocess
import sys

import duckdb
import pytest

from collector import poly_sweep
from hyxlab.store import duck_connect

WRITER = "import duckdb,sys; duckdb.connect(sys.argv[1]).close()"


def _writable_from_outside(db) -> bool:
    proc = subprocess.run([sys.executable, "-c", WRITER, str(db)], capture_output=True, timeout=120)
    return proc.returncode == 0


def _db(tmp_path):
    p = tmp_path / "a.duckdb"
    duck_connect(str(p)).close()
    return p


def test_the_pin_lands_on_the_connection_the_sweep_writes_through(tmp_path):
    store = poly_sweep._open(str(_db(tmp_path)))
    try:
        got = store.conn.execute("SELECT current_setting('threads')").fetchone()[0]
    finally:
        store.close()
    assert int(got) == poly_sweep.DUCK_THREADS == 4


def test_an_open_store_is_refused_from_outside(tmp_path):
    """Vacuity guard: without it the release arm passes against a helper
    that never locked."""
    p = _db(tmp_path)
    store = poly_sweep._open(str(p))
    try:
        assert _writable_from_outside(p) is False
    finally:
        store.close()
    assert _writable_from_outside(p) is True


def test_a_failed_pin_releases_the_writer_lock(tmp_path, monkeypatch):
    """The open has taken the file before the SET runs. `excinfo` holds the
    traceback, as the sweep's own `except` around `_flush` does -- without
    it CPython's refcount closes the leaked connection and this is green
    against the leak (rules/ops.md)."""
    p = _db(tmp_path)
    monkeypatch.setattr(poly_sweep, "DUCK_THREADS", "not_a_number")
    with pytest.raises(duckdb.Error) as excinfo:
        poly_sweep._open(str(p))
    assert excinfo.value is not None
    assert _writable_from_outside(p) is True
