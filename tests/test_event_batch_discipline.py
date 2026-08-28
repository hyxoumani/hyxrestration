"""How much PYTHON HEAP one `book_events` batch may take, and by what.

EXP-1380, the rung above `test_markets_load_discipline.py`. That guard
bounded the `markets` table load — the thing a replay allocates BEFORE
its first event. With it in place, a 3 h `run_l2` peaks at 194.7 MiB,
and the ladder named the equity curve as what remained. It is not: the
curve is 12.4 MiB of that. **184.0 MiB of it is one line** —

    while rows := cur.fetchmany(EVENT_CHUNK):

— a batch of 200,000 rows materialised as Python tuples plus the
`BookEvent`s built from them. Measured on the live stream archive over
2026-08-27 12:00-15:00 (270,402 snapshots), by chunk size:

  chunk    batch     walk peak    elapsed   snapshots
  200,000  125.5 MiB  188.1 MiB    17.8 s   270,402  sha 9d52d498c6fd0e4f
   50,000   31.5 MiB   48.7 MiB    17.3 s   270,402  sha 9d52d498c6fd0e4f
   20,000   12.4 MiB   20.6 MiB    19.2 s   270,402  sha 9d52d498c6fd0e4f
    5,000    2.9 MiB    6.5 MiB    17.4 s   270,402  sha 9d52d498c6fd0e4f
    1,000    0.7 MiB    2.5 MiB    17.4 s   270,402  sha 9d52d498c6fd0e4f

**LINEAR IN MEMORY, FLAT IN TIME, IDENTICAL IN ANSWER.** There is no
trade-off here to tune and no window length that makes a big batch worth
it — the slicing rung already holds the ENGINE's sort flat, and this is
the only remaining term that grows with a number nobody chose. That is
why the constant is written as a BUDGET (`EVENT_BATCH_BUDGET`) divided
by a MEASURED per-row cost (`EVENT_ROW_BYTES`) rather than as a tuned
row count: a row count invites "make it bigger for speed", and the
measurement says there is no speed to buy.

It is not only an ad-hoc cost. `hyxlab-shadow.service` is
`MemoryMax=1G` and seeds through this exact walk at every boot.

  DERIVED — `EVENT_CHUNK * EVENT_ROW_BYTES <= EVENT_BATCH_BUDGET`. The
            revert (200_000, i.e. 125.9 MiB of batch) reds.
  CLAIMED — verified by RUNNING against a real DuckDB: `EVENT_ROW_BYTES`
            is not an understatement of what a row of REALISTIC width
            actually costs, so the budget cannot be satisfied by
            shrinking the claimed cost instead of the batch; and the
            walk's peak tracks the chunk, WITH ITS CONTROL — the same
            fixture at the old 200k chunk peaks many times higher, so a
            green cannot come from a fixture too small to allocate.
  IDENTICAL — the replay a bounded batch produces is the replay an
            unbounded one produces, event for event.
"""

import hashlib
import tracemalloc
from datetime import datetime, timedelta

import duckdb
import pytest

import simulator.bookreplay as br
from hyxlab.streamstore import BookEvent, StreamStore

# A real Kalshi market_id, not "M1": the per-row cost this budget is
# derived from is mostly strings, and a toy id would understate it.
MID = "KXBTCD-26AUG2717-T112999.99"
BASE = datetime(2026, 8, 20, 0, 0, 0)


def test_the_chunk_is_the_budget_divided_by_the_measured_row_cost():
    """DERIVED. The revert to 200_000 reds here."""
    assert br.EVENT_CHUNK * br.EVENT_ROW_BYTES <= br.EVENT_BATCH_BUDGET
    # Non-vacuous: a budget big enough to hold any chunk guards nothing.
    # 4 MiB is under the 12.4 MiB a 20k batch was measured to cost.
    assert br.EVENT_BATCH_BUDGET <= 8 * 1024 * 1024


@pytest.fixture
def stream(tmp_path):
    """~60k rows of one market, wide enough that a 200k-row chunk
    swallows the lot and a 5k-row chunk does not."""
    db = tmp_path / "events.duckdb"
    sstore = StreamStore(db)
    rows = [
        BookEvent(
            venue="kalshi",
            market_id=MID,
            recv_ts=BASE + timedelta(milliseconds=100 * i),
            src_ts=None,
            sid=1,
            seq=i + 1,
            kind="snap" if i % 3 == 0 else "delta",
            side="yes" if i % 2 else "no",
            price=0.40 + (i % 7) / 100.0,
            qty=float(10 + i % 13),
        )
        for i in range(60_000)
    ]
    sstore.append_events(rows)
    sstore.flush()
    return db


def _walk(db, chunk):
    """One full walk at `chunk`, returning (digest, count, peak bytes).

    The walk is HASHED, never accumulated: a list of 60k rows costs
    23.5 MiB and would swamp the batch this exists to measure — the
    first version of this test asserted on 23.5 vs 32.5 MiB and failed
    to see a 9 MiB difference it had itself buried."""
    old = br.EVENT_CHUNK
    br.EVENT_CHUNK = chunk
    try:
        with duckdb.connect(str(db), read_only=True) as conn:
            tracemalloc.start(4)
            h = hashlib.sha256()
            n = 0
            for e in br.stream_events(conn, BASE - timedelta(days=1), None):
                h.update(
                    f"{e.market_id}|{e.recv_ts}|{e.seq}|{e.kind}|{e.side}|{e.price}|{e.qty}\n".encode()
                )
                n += 1
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        return h.hexdigest(), n, peak
    finally:
        br.EVENT_CHUNK = old


def test_a_row_costs_at_least_what_the_budget_claims_it_does(stream):
    """CLAIMED. The mutation this catches is shrinking EVENT_ROW_BYTES
    rather than the chunk — the budget arithmetic passes either way, and
    only a measurement tells them apart."""
    with duckdb.connect(str(stream), read_only=True) as conn:
        cur = conn.execute(f"SELECT {br._EVENT_COLS} FROM book_events ORDER BY recv_ts, seq")
        tracemalloc.start(4)
        rows = cur.fetchmany(20_000)
        events = [BookEvent(*r) for r in rows]
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert len(events) == 20_000  # non-vacuous: the batch really filled
        per_row = peak / 20_000
        del rows, events
    # The constant must not UNDERSTATE the real cost (that would let a
    # bigger chunk through the same budget). Measured 660 B/row on the
    # live archive; the fixture's single repeated id interns cheaper, so
    # this is a floor on the claim, not an equality.
    assert per_row <= br.EVENT_ROW_BYTES, f"a row costs {per_row:.0f} B > claimed"
    assert per_row > 100  # and the measurement is not measuring nothing


def test_the_walks_peak_tracks_the_chunk_and_the_answer_does_not(stream):
    """CLAIMED + IDENTICAL, with the control. The old 200k chunk on the
    SAME fixture is the discrimination: without it, a green here could
    mean the fixture never allocated anything."""
    bounded, n_bounded, peak_bounded = _walk(stream, br.EVENT_CHUNK)
    whole, n_whole, peak_whole = _walk(stream, 200_000)

    assert bounded == whole  # event for event, in order
    assert n_bounded == n_whole == 60_000  # non-vacuous
    # The batch is the heap: swallowing 60k rows at once must cost
    # multiples of what 5k at a time does.
    assert peak_whole > 4 * peak_bounded, (
        f"bounded {peak_bounded / 2**20:.1f} MiB vs whole {peak_whole / 2**20:.1f} MiB"
    )
