"""BookReplayer: stream book events → honest top-of-book Snapshot stream.

The Tier-2 bridge: replays archived Kalshi WS events (orderbook
snapshots + signed deltas) into the exact displayed book at every
change, emitting a `Snapshot` whenever the top of book moves. Feeds the
same Simulator as candle snapshots, but at millisecond fidelity — which
is what makes latency-aware fills meaningful.

Honesty rules:
- A market's book is UNKNOWN until its first full snapshot, and becomes
  unknown again whenever a coverage gap touches it (reconnect, seq gap,
  daemon downtime — anything in stream_gaps). No emissions while
  unknown; the next snapshot re-seeds. A gap row that names a `shard`
  touches only the books that shard archived (the sid prefix,
  `SHARD_SID_STRIDE`); a row with no shard touches every book.
- Kalshi has ONE mirrored book: events carry resting yes/no bids; the
  asks are derived (yes_ask = 1 − best_no_bid) exactly as the venue
  displays them.
- Negative level quantities (should never happen; QA watches) clamp to
  zero rather than corrupting downstream fills.

Polymarket replay (independent token books, no seq numbers) is a later
slice — this module refuses non-Kalshi events rather than guessing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from hyxlab.models import Snapshot
from hyxlab.store import duck_connect, held_duck
from hyxlab.streamstore import SHARD_SID_STRIDE, BookEvent

_EMPTY: tuple = ()

# Gap rows that break KALSHI BOOK coverage. Foreign venues and the
# trades channel don't feed book_events; applying their gaps blanks
# every book until the next Kalshi reconnect — up to an hour of
# self-inflicted blindness per Polymarket flap. ('*' rows — daemon
# downtime, retro flush-failure marks — always apply.)
BOOK_GAPS = "venue IN ('kalshi', '*') AND channel IN ('books', '*')"


class _Book:
    __slots__ = ("levels", "pending_before", "seeded", "shard", "snap_key", "snap_ts")

    def __init__(self) -> None:
        self.levels: dict[str, dict[float, float]] = {"yes": {}, "no": {}}
        self.seeded = False
        self.snap_key: tuple | None = None  # (sid, seq) of in-progress image
        self.pending_before: tuple | None = None  # top before the image began
        self.snap_ts: datetime | None = None
        self.shard: int | None = None  # connection that archived the image


def _shard_of(sid: int | None) -> int | None:
    """The books connection a stored sid came from (`streamd` adds
    `idx * SHARD_SID_STRIDE` before archiving; shard 0 adds nothing)."""
    return None if sid is None else sid // SHARD_SID_STRIDE


class BookReplayer:
    def __init__(self) -> None:
        self._books: dict[str, _Book] = {}

    def invalidate(
        self,
        market_id: str | None = None,
        before: datetime | None = None,
        shard: int | None = None,
    ) -> None:
        """Coverage broke: forget state until the next snapshot re-seeds.

        `shard` is the books connection the gap row names: only books
        whose image that connection archived are touched. The stream runs
        ~14 shards and writes one gap row per shard's reconnect, so a
        channel-wide wipe on each left the other shards' books unknown
        until they too happened to reconnect (11-01 03:52Z: 6 h). None
        (a daemon_start row, a row written before 2026-11-02) wipes every
        book; a book whose image carried no sid is wiped either way.

        `before` is the gap's `started_at` -- the last archived event
        before the break. A book whose image arrived AFTER it was built by
        the reconnect that closed the gap and is post-gap state; wiping it
        is not conservative, it is blindness. Measured 2026-11-02 on shadow
        run 20261023T143153: the stream writes one `books` gap row PER
        SHARD (14 of them over 15:07:37-15:08:22Z at the 10-29 restart, all
        with the same `started_at`), the shadow reads them across several
        20 s polls, and every poll's copy of the gap re-wiped the books the
        previous poll's images had just re-seeded -- 16 h at 0-63
        snapshots/h against a 5k-45k norm, 28 blind hours (12.3%) over the
        run. A book with no image (`snap_ts` None) is wiped either way.
        """
        targets = [market_id] if market_id else list(self._books)
        for m in targets:
            b = self._books.get(m)
            if b is None:
                continue
            if shard is not None and b.shard is not None and b.shard != shard:
                continue  # another connection's book; its coverage held
            if before is not None and b.snap_ts is not None and b.snap_ts > before:
                continue  # re-seeded by the reconnect that closed this gap
            self._books.pop(m, None)

    def apply(self, e: BookEvent) -> Snapshot | None:
        """Apply one event; return a Snapshot iff the top of book changed
        on a seeded market.

        Snapshot images span MULTIPLE rows sharing (sid, seq): those rows
        build silently and never emit — a half-applied image is a book
        state that never existed. The caller must call finalize_snap()
        when the image's row group ends (replay_snapshots does)."""
        if e.venue != "kalshi":
            raise NotImplementedError("only kalshi book replay is implemented")
        book = self._books.setdefault(e.market_id, _Book())

        if e.kind == "snap":
            key = (e.sid, e.seq)
            if book.snap_key != key:
                # First row of a fresh full image: replace the book.
                book.pending_before = self._top(book) if book.seeded else None
                book.levels = {"yes": {}, "no": {}}
                book.snap_key = key
                book.seeded = True
            book.levels[e.side][e.price] = max(e.qty, 0.0)
            book.snap_ts = e.recv_ts
            book.shard = _shard_of(e.sid)
            return None

        if e.kind == "void":
            # A void row records a frame that archived no book level. Only
            # an EMPTY orderbook_snapshot carries book meaning, and it is a
            # full absolute image like any other snap: the ladder is now
            # empty. That is a KNOWN state, not broken coverage, so it
            # clears (and seeds) rather than invalidates — a market whose
            # book opens empty is tracked from here instead of discarding
            # every delta until the next reconnect image. Sequenced control
            # acks carry no book meaning, and legacy rows (side '', written
            # before 22c9556) cannot be attributed; both stay no-ops.
            # A single row is the whole image, so it emits immediately.
            if e.side != "orderbook_snapshot":
                return None
            before = self._finalizable_top(book) if book.seeded else None
            book.levels = {"yes": {}, "no": {}}
            book.snap_key = None
            book.pending_before = None
            book.seeded = True
            book.snap_ts = e.recv_ts
            book.shard = _shard_of(e.sid)
            after = self._top(book)
            return None if after == before else self._snapshot(e.market_id, e.recv_ts, after)

        if e.kind != "delta" or not book.seeded:
            return None  # unknown book; wait for a snapshot

        before = self._finalizable_top(book)
        book.snap_key = None
        book.pending_before = None
        side = book.levels[e.side]
        q = side.get(e.price, 0.0) + e.qty
        if q > 1e-9:
            side[e.price] = q
        else:
            side.pop(e.price, None)  # clamp: negative = removed
        after = self._top(book)
        if after != before and after is not None:
            return self._snapshot(e.market_id, e.recv_ts, after)
        return None

    def finalize_snap(self, market_id: str) -> Snapshot | None:
        """Emit the completed snapshot image (if its top differs from the
        pre-image top). Call when the image's row group ends."""
        book = self._books.get(market_id)
        if book is None or book.snap_key is None:
            return None
        before, book.pending_before = book.pending_before, None
        book.snap_key = None
        after = self._top(book)
        if after != before and after is not None:
            return self._snapshot(market_id, book.snap_ts, after)
        return None

    def depth(self, market_id: str) -> dict[str, list[tuple[float, float]]] | None:
        """Full displayed ladder for a SEEDED market: resting yes/no bids
        as (price, qty), best first. None while the book is unknown
        (pre-seed or mid-gap). Read-only view for display consumers
        (simui); fills still go through Snapshot tops only."""
        book = self._books.get(market_id)
        if book is None or not book.seeded:
            return None
        return {
            side: sorted(
                ((p, q) for p, q in book.levels[side].items() if q > 0),
                key=lambda pq: -pq[0],
            )
            for side in ("yes", "no")
        }

    def _finalizable_top(self, book: _Book) -> tuple | None:
        """Top for delta comparison: if an image is still open (caller
        skipped finalize), its pre-image top is the last EMITTED state."""
        if book.snap_key is not None:
            return book.pending_before
        return self._top(book)

    @staticmethod
    def _top(book: _Book) -> tuple | None:
        yes = book.levels["yes"]
        no = book.levels["no"]
        yb = max(yes) if yes else None
        nb = max(no) if no else None
        return (
            yb,
            yes.get(yb, 0.0) if yb is not None else 0.0,
            nb,
            no.get(nb, 0.0) if nb is not None else 0.0,
        )

    @staticmethod
    def _snapshot(market_id: str, ts: datetime, top: tuple) -> Snapshot:
        yes_bid, yes_bid_size, no_bid, no_bid_size = top
        # Mirrored single book: buying YES lifts the best NO bid and
        # vice versa, so ask price/size are the opposite side's bid.
        return Snapshot(
            venue="kalshi",
            market_id=market_id,
            ts=ts,
            yes_bid=yes_bid,
            yes_ask=None if no_bid is None else round(1.0 - no_bid, 4),
            no_bid=no_bid,
            no_ask=None if yes_bid is None else round(1.0 - yes_bid, 4),
            yes_bid_size=yes_bid_size,
            yes_ask_size=no_bid_size,
            no_bid_size=no_bid_size,
            no_ask_size=yes_bid_size,
        )


# One `fetchmany` batch is materialised Python: a tuple of 10 objects
# per row plus the `BookEvent` built from it. MEASURED on the live
# stream archive (EXP-1380), a batch costs **660 bytes/row** — 125.5
# MiB at 200k, 31.5 at 50k, 12.4 at 20k, 2.9 at 5k, linear across two
# orders of magnitude — and the batch is the WHOLE non-trivial heap of a
# replay: a 3 h `run_l2` peaked at 188.1 MiB traced with 184.0 of it on
# the `fetchmany` line, against 12.4 MiB for the full equity curve.
#
# So the chunk is not a memory/speed trade-off to tune; the same 3 h
# window replays in 17.4 s at 5k and 17.8 s at 200k, and every chunk
# size from 1k to 200k yields the IDENTICAL 270,402 snapshots
# (sha 9d52d498c6fd0e4f). It is a memory budget with no bill attached,
# so it is written as one: the batch may cost EVENT_BATCH_BUDGET, and
# the row count follows from what a row costs. This walk runs inside
# `hyxlab-shadow` (MemoryMax=1G), which seeds through it at boot.
EVENT_ROW_BYTES = 660  # measured; see tests/test_event_batch_discipline.py
EVENT_BATCH_BUDGET = 4 * 1024 * 1024
EVENT_CHUNK = 5_000  # <= EVENT_BATCH_BUDGET // EVENT_ROW_BYTES (6357)
EVENT_SLICE_HOURS = 6.0

_EVENT_COLS = "venue, market_id, recv_ts, src_ts, sid, seq, kind, side, price, qty"


#: `book_events` rows whose market belongs to one of a bound list of Kalshi
#: series (one `?`, a VARCHAR list). Exact on the series field, so
#: `KXHIGHT...` never matches a `KXHIGH...` universe the way a LIKE prefix
#: would.
SERIES_PREDICATE = "list_contains(?, split_part(market_id, '-', 1))"


def stream_events(
    conn,
    lo: datetime,
    hi: datetime | None,
    *,
    prefix: str | None = None,
    series: Sequence[str] | None = None,
    slice_hours: float = EVENT_SLICE_HOURS,
    lo_inclusive: bool = False,
) -> Iterator[BookEvent]:
    """Kalshi book events in (lo, hi] in replay order, chunked and sliced.

    `lo_inclusive` makes the low bound `>= lo` instead. SEED callers want
    it and reports do not: a seed's `lo` is a gap's `ended_at`, and
    `streamd` stamps a `seq_reset` gap's `ended_at` with the recv_ts of
    the FIRST post-reset frame — on the books channel that frame is the
    reconnect's full orderbook image. Excluding it drops every row of the
    image (they share one recv_ts), leaving the book unseeded until the
    NEXT connect, which on Kalshi can be hours. Implemented by stepping
    `lo` back one microsecond, DuckDB's TIMESTAMP resolution, so the
    predicate stays the pushable plain range the memory note below is
    about; `datetime.min` (the no-prior-break sentinel) has no predecessor
    and is already inclusive of everything, so it is left alone.

    THE ONE walk over `book_events`. It lived twice — in
    `simulator.divergence` and `simulator.run_l2` — and that duplication
    is exactly why the memory fix below reached only one of them; keep it
    here and call it, do not re-inline it.

    The window is walked in `slice_hours` slices, each with its OWN
    `ORDER BY recv_ts, seq`, rather than as one cursor over the whole
    span. The yielded order is IDENTICAL either way — slice bounds are
    half-open on `recv_ts` (`> lo AND <= hi`), so every row sharing a
    `recv_ts` lands in exactly one slice and no tie can straddle a
    boundary — but the peak memory is not. DuckDB materialises a sort, so
    one cursor over a long window costs memory LINEAR IN WINDOW LENGTH:
    measured 3.14 GB for a 2-day window (18.5M rows), and the divergence
    report's 10.5-day default target peaked at 14.3 GB. Slicing holds the
    sort's contribution flat at one slice regardless of window length, at
    no wall-clock cost (13.6s sliced vs 12.6s single over 18.5M rows).

    `series` restricts the walk to markets of those Kalshi series (the
    ticker's first dash field), IN the SQL, so rows of other series are
    never sorted, fetched or built. A caller with a fixed universe passes
    it: since the 2026-10-20 coverage expansion `book_events` carries books
    for ~45 more series (~290M rows/day measured at staging), and a walk
    sized "since the last gap" over all of them is a different order of
    memory. `None` walks every series, as before.
    """
    sql, bounds, tail = _plan_walk(conn, lo, hi, prefix, series, slice_hours, lo_inclusive)
    # Own cursor: a caller's `conn.execute` between yields would replace
    # the pending result and end the walk early, silently (see
    # `queuescore.iter_market_rows`, measured 2026-10-30).
    for start, stop in bounds:
        cur = conn.cursor()
        try:
            cur.execute(sql, [start, stop, *tail])
            while rows := cur.fetchmany(EVENT_CHUNK):
                for r in rows:
                    yield BookEvent(*r)
        finally:
            cur.close()


def _plan_walk(
    conn,
    lo: datetime,
    hi: datetime | None,
    prefix: str | None,
    series: Sequence[str] | None,
    slice_hours: float,
    lo_inclusive: bool,
) -> tuple[str, list[tuple[datetime, datetime]], list]:
    """The walk's slice SQL and its slice bounds, resolved once.

    Split out of `stream_events` so `export_events` can walk the SAME
    slices rather than re-deriving them. THE ONE walk is a claim about
    the bounds as much as the SQL — a second copy of this arithmetic is
    how the seed boundary drifted the first time — so the two entry
    points share it and neither owns it.
    """
    if lo_inclusive and lo != datetime.min:
        lo -= timedelta(microseconds=1)
    where = "venue='kalshi' AND recv_ts > ?" + (" AND recv_ts <= ?" if hi else "")
    params: list = [lo, hi] if hi else [lo]
    if prefix:
        where += " AND market_id LIKE ?"
        params.append(prefix + "%")
    if series is not None:
        where += f" AND {SERIES_PREDICATE}"
        params.append(sorted(series))
    # Resolve the real extent first: `lo` may be datetime.min (no prior
    # coverage break) and `hi` may be open, either of which would make a
    # naive slice walk iterate over empty millennia.
    first, last = conn.execute(
        f"SELECT min(recv_ts), max(recv_ts) FROM book_events WHERE {where}", params
    ).fetchone()
    sql = (
        f"SELECT {_EVENT_COLS} FROM book_events"
        " WHERE venue='kalshi' AND recv_ts > ? AND recv_ts <= ?"
        + (" AND market_id LIKE ?" if prefix else "")
        + (f" AND {SERIES_PREDICATE}" if series is not None else "")
        + " ORDER BY recv_ts, seq"
    )
    tail = ([prefix + "%"] if prefix else []) + ([sorted(series)] if series is not None else [])
    if first is None:
        return sql, [], tail
    step = timedelta(hours=slice_hours)
    # Start strictly below `first` so the first slice's `> lo` keeps it.
    start = first - timedelta(microseconds=1)
    bounds = []
    while start < last:
        stop = min(start + step, last)
        bounds.append((start, stop))
        start = stop
    return sql, bounds, tail


# ---------------------------------------------------------------------------
# Copy-out: the walk, materialised, so the archive is not held while it runs.
#
# `stream_events` streams with a live cursor, and a streaming cursor cannot
# release the file it is reading. `simulator.divergence` replays a shadow
# run's whole window through one such cursor, so it holds `hyxstream.duckdb`
# for as long as the SIMULATION takes -- measured 2026-09-25: 45m45s, during
# which `collector.streamd` could not flush, held a peak 400,154 rows and
# moved 387,856 of them to the torn-append sidecar. ~97% of that hold is
# simulation with an IDLE connection; the DB work is a rounding error inside
# it.
#
# So the window is copied out FIRST and replayed from the copy. Measured
# 2026-09-26 against `data/backups/hyxstream.Fri.duckdb` over divergence's
# real 12.3-day window (117.2M rows, floor 09-12 01:40Z -> 09-24 08:38Z):
#
#   sliced copy-out   50 slices, 6.2 s, 0.89 GB on disk, 2.60 GB peak RSS
#   parquet read-back 117,208,791 rows, 58.7 s
#
# 6.2 s against 2,745 s is the whole point -- a 440x shorter hold -- and the
# read-back is FASTER than the archive walk it replaces (the same rows out of
# `book_events` measure ~86 s). The slices are the walk's own slices, so the
# memory profile of the copy is the memory profile of the walk: flat in window
# length, not linear (see the note in `stream_events`).
#
# Cost is 0.89 GB of scratch per replay, written into this process's private
# `<db>.tmp/pid-<pid>` directory (`hyxlab.scratch`) -- owner-locked, dropped at
# clean exit, and reaped by the next process to run if this one is SIGKILLed.
# It is NOT a cache: a replay that reads a stale export would be reading a
# window the archive no longer has.
# ---------------------------------------------------------------------------

#: One parquet file per walk slice, named in walk order. Zstd because the
#: bill is disk on the archive's own volume and the rows compress 8x.
_EXPORT_GLOB = "*.parquet"


def export_events(
    conn,
    lo: datetime,
    hi: datetime | None,
    dest: str | Path,
    *,
    prefix: str | None = None,
    series: Sequence[str] | None = None,
    slice_hours: float = EVENT_SLICE_HOURS,
    lo_inclusive: bool = False,
) -> Path:
    """Write `stream_events(conn, lo, hi, ...)` to `dest` as parquet slices.

    Same arguments, same slices, same order -- this is the walk written
    down instead of yielded. Returns `dest`, which `stream_exported`
    reads back as the identical `BookEvent` stream.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    sql, bounds, tail = _plan_walk(conn, lo, hi, prefix, series, slice_hours, lo_inclusive)
    for i, (start, stop) in enumerate(bounds):
        out = str(dest / f"{i:04d}.parquet").replace("'", "''")
        conn.execute(
            f"COPY ({sql}) TO '{out}' (FORMAT parquet, COMPRESSION zstd)",
            [start, stop, *tail],
        )
    return dest


def stream_exported(dest: str | Path) -> Iterator[BookEvent]:
    """Read an `export_events` directory back as the same event stream.

    The slices are already sorted and named in walk order, so the
    read-back is a concatenation -- no ORDER BY, and therefore no sort to
    hold. Chunked on `EVENT_CHUNK` for the same reason the live walk is.
    """
    paths = sorted(Path(dest).glob(_EXPORT_GLOB))
    if not paths:
        return
    conn = duck_connect(str(Path(dest) / "reader.duckdb"))
    try:
        for p in paths:
            cur = conn.cursor()
            try:
                cur.execute(f"SELECT {_EVENT_COLS} FROM read_parquet(?)", [str(p)])
                while rows := cur.fetchmany(EVENT_CHUNK):
                    for r in rows:
                        yield BookEvent(*r)
            finally:
                cur.close()
    finally:
        conn.close()


BOOK_COLS = _EVENT_COLS
TRADE_COLS = "recv_ts, price, qty, taker_side, market_id, venue"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def copy_out(conn, markets: list[str], since: datetime, scratch: Path) -> None:
    """Copy the selected markets' window rows out of the archive, UNSORTED,
    so the hold on `hyxstream.duckdb` is two filtered scans and nothing else.

    Until 2026-11-13 the market-ordered `ORDER BY` of `queuescore.iter_market_rows` ran
    on the archive connection, inside the hold. A sort has to finish before
    its first row comes back, and DuckDB's `memory_limit` does not bound it
    (the #142 class): on 2026-11-13 the 336h econ bracket -- 3.32M selected
    book rows, up from 2.45M on 10-29 -- was killed by the kernel at the
    transient unit's 4G `MemoryMax` 91s into that statement, before one
    market had been yielded, having held the archive 142s for no report.

    Measured the same evening, same unit, same window (three live runs):
    the copy-out alone is not the fix -- with the pool at its cgroup-derived
    2.0 GiB the hold ends at anon 3,957 MB of the 4,096, and 114 MB the
    moment the archive connection closes. So the peak is the SCAN's, not the
    sort's or the replay's, and it is `memory_limit` plus ~2.0 GB the limit
    does not count. `SET threads = 4` made it worse (killed again). Lowering
    the pool to `queuescore.ARCHIVE_POOL` (1 GiB; its `main` sets it) moved the peak down
    by exactly the difference, to 3,022 MB, with the hold unchanged at
    122-123s (it was 122-139s at 2 GiB): the pool was caching blocks a
    single pass never re-reads. The hold itself is the window's volume --
    ~35-45s in `queuescore.select_markets`, the rest reading ten columns for the copy.

    The rows are inlined as literals because `COPY` takes no bound
    parameters; market ids come from `select_markets`, i.e. from the archive.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    conn.execute("SET preserve_insertion_order = false")
    ids = ",".join(_lit(m) for m in markets) or "NULL"
    where = f"market_id IN ({ids}) AND venue='kalshi' AND recv_ts > TIMESTAMP {_lit(str(since))}"
    for table, cols in (("book_events", BOOK_COLS), ("stream_trades", TRADE_COLS)):
        out = _lit(str(scratch / f"{table}.parquet"))
        conn.execute(f"COPY (SELECT {cols} FROM {table} WHERE {where}) TO {out} (FORMAT parquet)")


@contextmanager
def scratch_rows(scratch: Path):
    """A private connection whose `book_events` / `stream_trades` are the
    rows `copy_out` left in `scratch` -- the two names `queuescore.iter_market_rows`
    reads, so the streamed sort is the same statement on a different file.
    The scratch database is this process's own (created here, deleted by
    the caller with the directory), so it takes nobody's lock."""
    with held_duck(scratch / "scratch.duckdb") as conn:
        for table in ("book_events", "stream_trades"):
            src = _lit(str(scratch / f"{table}.parquet"))
            conn.execute(f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM read_parquet({src})")
        yield conn


def replay_snapshots(
    events: Iterable[BookEvent],
    gaps: Iterable[tuple] = _EMPTY,
    replayer: BookReplayer | None = None,
) -> Iterator[Snapshot]:
    """Events (recv_ts order) + gap intervals → top-of-book Snapshots.

    A gap is `(started_at, ended_at)` or `(started_at, ended_at, shard)`.
    Any gap whose start falls before an event invalidates the book state
    built before that start -- every book for a two-tuple or a None
    shard, only that connection's books otherwise (gap rows are not
    per-market; see `BookReplayer.invalidate`, also for why an image
    newer than the gap's start survives it). Snapshot
    images emit once complete — when their (market, sid, seq) row group
    ends — never row-by-row.

    A gap that starts after the LAST event is applied when the events run
    out, so how a stream is cut into batches cannot decide whether a gap
    is honoured.

    Pass a persistent `replayer` to carry book state across successive
    batches (the shadow harness tails the stream archive in polls; a WS
    frame's rows are flushed atomically, so image groups never span
    batches and the end-of-batch finalize is safe)."""
    if replayer is None:
        replayer = BookReplayer()
    gap_starts = sorted(((g[0], g[2] if len(g) > 2 else None) for g in gaps), key=lambda g: g[0])
    gi = 0
    open_group: tuple | None = None  # (market_id, sid, seq) of open image
    for e in events:
        while gi < len(gap_starts) and gap_starts[gi][0] <= e.recv_ts:
            # A completed image is a real pre-gap book state: emit it
            # before coverage is declared broken.
            if open_group is not None:
                snap = replayer.finalize_snap(open_group[0])
                if snap is not None:
                    yield snap
                open_group = None
            replayer.invalidate(before=gap_starts[gi][0], shard=gap_starts[gi][1])
            gi += 1
        group = (e.market_id, e.sid, e.seq) if e.kind == "snap" else None
        if open_group is not None and group != open_group:
            snap = replayer.finalize_snap(open_group[0])
            if snap is not None:
                yield snap
        open_group = group
        snap = replayer.apply(e)
        if snap is not None:
            yield snap
    if open_group is not None:
        snap = replayer.finalize_snap(open_group[0])
        if snap is not None:
            yield snap
    # Gaps no event reached. With a one-shot replayer this changes nothing
    # (no event follows to observe the state). With a PERSISTENT one it is
    # the difference between a gap and no gap: the shadow's poll advances
    # its gap cursor past every row it read, so a gap row that lands a
    # poll ahead of the next event (an empty batch, or one whose events
    # all predate the gap's start) was read once, applied never, and the
    # next poll's delta replayed onto the pre-gap book (mistakes #186).
    # `invalidate(before=start)` spares any image newer than the start, so
    # applying it here equals applying it ahead of the next batch's first
    # event, and a caller that re-offers the gap (simui) wipes nothing twice.
    while gi < len(gap_starts):
        replayer.invalidate(before=gap_starts[gi][0], shard=gap_starts[gi][1])
        gi += 1
