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
  unknown; the next snapshot re-seeds.
- Kalshi has ONE mirrored book: events carry resting yes/no bids; the
  asks are derived (yes_ask = 1 − best_no_bid) exactly as the venue
  displays them.
- Negative level quantities (should never happen; QA watches) clamp to
  zero rather than corrupting downstream fills.

Polymarket replay (independent token books, no seq numbers) is a later
slice — this module refuses non-Kalshi events rather than guessing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta

from hyxlab.models import Snapshot
from hyxlab.streamstore import BookEvent

_EMPTY: tuple = ()

# Gap rows that break KALSHI BOOK coverage. Foreign venues and the
# trades channel don't feed book_events; applying their gaps blanks
# every book until the next Kalshi reconnect — up to an hour of
# self-inflicted blindness per Polymarket flap. ('*' rows — daemon
# downtime, retro flush-failure marks — always apply.)
BOOK_GAPS = "venue IN ('kalshi', '*') AND channel IN ('books', '*')"


class _Book:
    __slots__ = ("levels", "pending_before", "seeded", "snap_key", "snap_ts")

    def __init__(self) -> None:
        self.levels: dict[str, dict[float, float]] = {"yes": {}, "no": {}}
        self.seeded = False
        self.snap_key: tuple | None = None  # (sid, seq) of in-progress image
        self.pending_before: tuple | None = None  # top before the image began
        self.snap_ts: datetime | None = None


class BookReplayer:
    def __init__(self) -> None:
        self._books: dict[str, _Book] = {}

    def invalidate(self, market_id: str | None = None) -> None:
        """Coverage broke: forget state until the next snapshot re-seeds."""
        targets = [market_id] if market_id else list(self._books)
        for m in targets:
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


EVENT_CHUNK = 200_000
EVENT_SLICE_HOURS = 6.0

_EVENT_COLS = "venue, market_id, recv_ts, src_ts, sid, seq, kind, side, price, qty"


def stream_events(
    conn,
    lo: datetime,
    hi: datetime | None,
    *,
    prefix: str | None = None,
    slice_hours: float = EVENT_SLICE_HOURS,
) -> Iterator[BookEvent]:
    """Kalshi book events in (lo, hi] in replay order, chunked and sliced.

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
    """
    where = "venue='kalshi' AND recv_ts > ?" + (" AND recv_ts <= ?" if hi else "")
    params: list = [lo, hi] if hi else [lo]
    if prefix:
        where += " AND market_id LIKE ?"
        params.append(prefix + "%")
    # Resolve the real extent first: `lo` may be datetime.min (no prior
    # coverage break) and `hi` may be open, either of which would make a
    # naive slice walk iterate over empty millennia.
    first, last = conn.execute(
        f"SELECT min(recv_ts), max(recv_ts) FROM book_events WHERE {where}", params
    ).fetchone()
    if first is None:
        return
    step = timedelta(hours=slice_hours)
    # Start strictly below `first` so the first slice's `> lo` keeps it.
    start = first - timedelta(microseconds=1)
    tail = [prefix + "%"] if prefix else []
    sql = (
        f"SELECT {_EVENT_COLS} FROM book_events"
        " WHERE venue='kalshi' AND recv_ts > ? AND recv_ts <= ?"
        + (" AND market_id LIKE ?" if prefix else "")
        + " ORDER BY recv_ts, seq"
    )
    while start < last:
        stop = min(start + step, last)
        cur = conn.execute(sql, [start, stop, *tail])
        while rows := cur.fetchmany(EVENT_CHUNK):
            for r in rows:
                yield BookEvent(*r)
        start = stop


def replay_snapshots(
    events: Iterable[BookEvent],
    gaps: Iterable[tuple[datetime, datetime]] = _EMPTY,
    replayer: BookReplayer | None = None,
) -> Iterator[Snapshot]:
    """Events (recv_ts order) + gap intervals → top-of-book Snapshots.

    Any gap whose start falls before an event invalidates ALL book state
    at that point (conservative: gap rows aren't per-market). Snapshot
    images emit once complete — when their (market, sid, seq) row group
    ends — never row-by-row.

    Pass a persistent `replayer` to carry book state across successive
    batches (the shadow harness tails the stream archive in polls; a WS
    frame's rows are flushed atomically, so image groups never span
    batches and the end-of-batch finalize is safe)."""
    if replayer is None:
        replayer = BookReplayer()
    gap_starts = sorted(g[0] for g in gaps)
    gi = 0
    open_group: tuple | None = None  # (market_id, sid, seq) of open image
    for e in events:
        while gi < len(gap_starts) and gap_starts[gi] <= e.recv_ts:
            # A completed image is a real pre-gap book state: emit it
            # before coverage is declared broken.
            if open_group is not None:
                snap = replayer.finalize_snap(open_group[0])
                if snap is not None:
                    yield snap
                open_group = None
            replayer.invalidate()
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

