"""Where one write burst's seconds went, by statement kind -- and whether
they were worked or waited.

The poly sweep's flush has printed its hold per `Store` call since #209, and
the first reading (2026-11-18) put 52.2 of 69.2 flush minutes in ONE call:
`insert_trades`, 64-80 s for each of the six 220-300k-row batches. Replayed
the same day on a reflink copy of that night's backup slot -- the real sweep
code, a genuinely fetched 300,000-trade batch, the same 6G cgroup -- the call
takes 10.2 s (stage 1.1 s, anti-join insert 7.8 s), and a Kalshi mid-series
flush of 251,425 trades that held the archive 55.1 s in production replays
in 1.2 s. The call is three different kinds of work (a Python-side
normalisation, the bind into a temp table, the anti-join insert) and nothing
in the journal says which one production spends the extra minute in, nor
whether the process was computing or blocked (mistakes #212).

`StatementCost.attach(store)` swaps `store.conn` for a proxy that times every
statement the `Store` runs and files it under a kind; what the burst spent
outside any statement is `python`. CPU is read for the calling thread and for
the whole process, so a line can say `cpu 9.1 s` against 70 s of wall (the
burst waited) or `cpu 68 s` (it worked). It lives here, not in
`hyxlab/store.py`: both daemons import that module and an instrument for two
batch units is not worth restarting them.
"""

from __future__ import annotations

import importlib.util
import sys
import time

#: Statement kinds, in the order a line prints them. `stage` is the bind of
#: the batch into a temp table, `dedup` the anti-join insert into the archive
#: table, `close` DuckDB's checkpoint, `python` the burst's wall outside every
#: statement (row normalisation, list building).
PARTS = ("stage", "dedup", "other", "close", "python")
#: A part under this many seconds is left off the line.
MIN_PART_S = 0.05


#: Seconds one bound Python value costs when pandas cannot be imported:
#: 50.04 s for 250,000 rows x 8 columns, measured 2026-11-18 in the stable venv.
BIND_TAX_S_PER_VALUE = 25e-6
_TAX_WARNED = False


def bind_tax_warning(pandas_importable: bool | None = None) -> str | None:
    """The line a writer prints once when DuckDB's bind tax is on, or None.

    DuckDB's binding consults pandas for every value it converts from
    Python, and a FAILED import is not cached: each value walks `sys.path`
    twice. Both sweeps held the collector's archive 45-80 s per 250,000-row
    flush for that alone while every replay -- run from the dev venv, which
    has pandas -- took 1 s (mistakes #212, #213). The stable venv pins
    pandas now; this line is what says so if it goes missing again."""
    if pandas_importable is None:
        pandas_importable = (
            "pandas" in sys.modules or importlib.util.find_spec("pandas") is not None
        )
    if pandas_importable:
        return None
    return (
        "[store] WARNING: pandas is not importable -- DuckDB retries the import twice per"
        f" bound value, ~{BIND_TAX_S_PER_VALUE * 2e6:.0f}s of this thread per million values"
        " staged, inside the hold (mistakes #213; pin pandas in"
        " scripts/requirements-stable.txt)"
    )


def kind(sql: str) -> str:
    """The part one statement's wall is charged to."""
    head = " ".join(sql.split()).upper()
    if head.startswith("INSERT INTO _"):
        return "stage"
    if head.startswith("INSERT INTO") and "NOT EXISTS" in head:
        return "dedup"
    return "other"


class StatementCost:
    """Proxy for a DuckDB connection: `execute`/`executemany`/`close` timed,
    everything else passed through."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.wall: dict[str, float] = {}
        self._t0 = time.monotonic()
        self._cpu_main0 = time.thread_time()
        self._cpu_all0 = time.process_time()
        self._end: tuple[float, float, float] | None = None

    @classmethod
    def attach(cls, store) -> StatementCost:
        """Time `store`'s statements from here on. The proxy replaces
        `store.conn`, so `store.close()` closes the real connection through
        it and is itself charged (`close`)."""
        global _TAX_WARNED
        if not _TAX_WARNED:
            _TAX_WARNED = True
            tax = bind_tax_warning()
            if tax:
                print(tax, flush=True)
        cost = cls(store.conn)
        store.conn = cost
        return cost

    def _timed(self, part: str, fn, *a):
        t = time.monotonic()
        try:
            return fn(*a)
        finally:
            # Charged when the statement raises too: it held the file that long.
            self.wall[part] = self.wall.get(part, 0.0) + (time.monotonic() - t)

    def execute(self, sql, *a):
        return self._timed(kind(sql), self._conn.execute, sql, *a)

    def executemany(self, sql, *a):
        return self._timed(kind(sql), self._conn.executemany, sql, *a)

    def close(self):
        try:
            return self._timed("close", self._conn.close)
        finally:
            self.stop()

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def stop(self) -> None:
        """Freeze the clocks (idempotent): the burst ended here."""
        if self._end is None:
            self._end = (time.monotonic(), time.thread_time(), time.process_time())

    def parts(self) -> dict[str, float]:
        """Seconds per part; `python` is the wall no statement accounts for."""
        self.stop()
        total = self._end[0] - self._t0
        out = dict(self.wall)
        out["python"] = max(0.0, total - sum(self.wall.values()))
        return out

    def cpu(self) -> tuple[float, float]:
        """(calling thread, whole process) CPU seconds over the burst."""
        self.stop()
        return self._end[1] - self._cpu_main0, self._end[2] - self._cpu_all0

    def clause(self) -> str:
        """`stage 61.0, dedup 11.9, close 0.3; cpu 9.1s main thread, 24.0s
        process`, or '' when no part reaches `MIN_PART_S`. The ONE renderer:
        both sweeps' long-burst lines end with it."""
        p = self.parts()
        shown = ", ".join(f"{k} {p[k]:.1f}" for k in PARTS if p.get(k, 0.0) >= MIN_PART_S)
        if not shown:
            return ""
        main, proc = self.cpu()
        return f"{shown}; cpu {main:.1f}s main thread, {proc:.1f}s process"
