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
from datetime import datetime

import duckdb

from hyxlab.models import Snapshot
from hyxlab.streamstore import BookEvent

_EMPTY: tuple = ()


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


def replay_snapshots(
    events: Iterable[BookEvent],
    gaps: Iterable[tuple[datetime, datetime]] = _EMPTY,
) -> Iterator[Snapshot]:
    """Events (recv_ts order) + gap intervals → top-of-book Snapshots.

    Any gap whose start falls before an event invalidates ALL book state
    at that point (conservative: gap rows aren't per-market). Snapshot
    images emit once complete — when their (market, sid, seq) row group
    ends — never row-by-row."""
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


def load_stream_snapshots(
    db_path: str = "data/hyxstream.duckdb",
    market_ids: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Snapshot]:
    """Read archived book events (read-only) and replay to Snapshots."""
    conn = duckdb.connect(db_path, read_only=True)
    try:
        where, params = ["venue = 'kalshi'"], []
        if market_ids:
            where.append(f"market_id IN ({','.join('?' * len(market_ids))})")
            params.extend(market_ids)
        if start:
            where.append("recv_ts >= ?")
            params.append(start.replace(tzinfo=None) if start.tzinfo else start)
        if end:
            where.append("recv_ts < ?")
            params.append(end.replace(tzinfo=None) if end.tzinfo else end)
        rows = conn.execute(
            "SELECT venue, market_id, recv_ts, src_ts, sid, seq, kind, side, price, qty"
            f" FROM book_events WHERE {' AND '.join(where)} ORDER BY recv_ts, seq",
            params,
        ).fetchall()
        gaps = conn.execute(
            "SELECT started_at, ended_at FROM stream_gaps ORDER BY started_at"
        ).fetchall()
    finally:
        conn.close()
    events = (BookEvent(*r) for r in rows)
    return list(replay_snapshots(events, gaps=gaps))
