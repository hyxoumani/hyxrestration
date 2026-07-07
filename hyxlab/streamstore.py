"""Stream archive: WS book events, trade prints, and coverage gaps.

Lives in its OWN DuckDB file (default data/hyxstream.duckdb), separate
from the polled archive. Rationale: DuckDB is single-writer (a held write
connection blocks even read_only connects), and the 5-min collector +
daily sweep own `data/hyxlab.duckdb`. A long-lived stream daemon writing
there would deadlock the timers; giving the stream its own file makes the
daemon its sole writer. The daemon buffers in memory and flushes in
short open→write→close bursts, so the file is attachable for reads
between flushes.

Row semantics differ per venue and are recorded as-received (durability
over convenience — replay logic interprets):
- kalshi book_events: kind='snap' rows carry absolute level qty (a full
  book image re-sent on every (re)subscribe); kind='delta' rows carry a
  SIGNED qty change. side is 'yes'/'no'; prices in dollars.
- polymarket book_events: kind='snap' rows are absolute level sizes;
  kind='delta' (price_change) rows carry the NEW ABSOLUTE size at that
  price (not a signed change). side is 'bid'/'ask'; market_id is the
  CLOB token (asset) id.
- stream_gaps: closed intervals where coverage is broken (reconnects,
  seq gaps, daemon downtime). Replay must treat books as unknown inside
  a gap until the next snapshot re-seeds them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

_SCHEMA = """
CREATE TABLE IF NOT EXISTS book_events (
    venue     VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL,
    recv_ts   TIMESTAMP NOT NULL,
    src_ts    TIMESTAMP,
    sid       BIGINT,
    seq       BIGINT,
    kind      VARCHAR NOT NULL,
    side      VARCHAR NOT NULL,
    price     DOUBLE NOT NULL,
    qty       DOUBLE NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_trades (
    venue      VARCHAR NOT NULL,
    market_id  VARCHAR NOT NULL,
    recv_ts    TIMESTAMP NOT NULL,
    src_ts     TIMESTAMP,
    price      DOUBLE NOT NULL,
    qty        DOUBLE NOT NULL,
    taker_side VARCHAR,
    seq        BIGINT
);
CREATE TABLE IF NOT EXISTS stream_gaps (
    venue      VARCHAR NOT NULL,
    channel    VARCHAR NOT NULL,
    started_at TIMESTAMP NOT NULL,
    ended_at   TIMESTAMP NOT NULL,
    reason     VARCHAR
);
"""


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


@dataclass
class BookEvent:
    venue: str
    market_id: str
    recv_ts: datetime
    src_ts: datetime | None
    sid: int | None
    seq: int | None
    kind: str  # 'snap' | 'delta'
    side: str
    price: float
    qty: float


@dataclass
class StreamTrade:
    venue: str
    market_id: str
    recv_ts: datetime
    src_ts: datetime | None
    price: float
    qty: float
    taker_side: str | None
    seq: int | None


class StreamStore:
    """Buffered writer. append_*() only buffers; flush() opens a
    connection, writes everything, and closes it again."""

    def __init__(self, path: str | Path = "data/hyxstream.duckdb") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[BookEvent] = []
        self._trades: list[StreamTrade] = []
        self._gaps: list[tuple] = []
        # Create schema up front so readers see the tables immediately.
        with duckdb.connect(str(self.path)) as conn:
            conn.execute(_SCHEMA)

    # -- buffering --------------------------------------------------------

    def append_events(self, events: list[BookEvent]) -> None:
        self._events.extend(events)

    def append_trades(self, trades: list[StreamTrade]) -> None:
        self._trades.extend(trades)

    def append_gap(
        self,
        venue: str,
        channel: str,
        started_at: datetime,
        ended_at: datetime,
        reason: str,
    ) -> None:
        self._gaps.append((venue, channel, _naive_utc(started_at), _naive_utc(ended_at), reason))

    @property
    def pending(self) -> int:
        return len(self._events) + len(self._trades) + len(self._gaps)

    # -- persistence ------------------------------------------------------

    def flush(self) -> int:
        """Write all buffered rows in one transaction; returns rows written."""
        n = self.pending
        if n == 0:
            return 0
        events, self._events = self._events, []
        trades, self._trades = self._trades, []
        gaps, self._gaps = self._gaps, []
        with duckdb.connect(str(self.path)) as conn:
            conn.execute("BEGIN")
            if events:
                conn.executemany(
                    "INSERT INTO book_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            e.venue,
                            e.market_id,
                            _naive_utc(e.recv_ts),
                            _naive_utc(e.src_ts),
                            e.sid,
                            e.seq,
                            e.kind,
                            e.side,
                            e.price,
                            e.qty,
                        )
                        for e in events
                    ],
                )
            if trades:
                conn.executemany(
                    "INSERT INTO stream_trades VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (
                            t.venue,
                            t.market_id,
                            _naive_utc(t.recv_ts),
                            _naive_utc(t.src_ts),
                            t.price,
                            t.qty,
                            t.taker_side,
                            t.seq,
                        )
                        for t in trades
                    ],
                )
            if gaps:
                conn.executemany("INSERT INTO stream_gaps VALUES (?,?,?,?,?)", gaps)
            conn.execute("COMMIT")
        return n

    def mark_startup_gap(self, now: datetime | None = None) -> None:
        """Record daemon downtime: everything between the last archived
        event and this start is unknown coverage. No-op on an empty DB
        (nothing was being covered yet)."""
        now = now or datetime.now(UTC)
        with duckdb.connect(str(self.path)) as conn:
            last = conn.execute(
                "SELECT max(ts) FROM (SELECT max(recv_ts) AS ts FROM book_events"
                " UNION ALL SELECT max(recv_ts) FROM stream_trades)"
            ).fetchone()[0]
        if last is None:
            return
        self.append_gap("*", "*", last, now, "daemon_start")

    def counts(self) -> dict[str, int]:
        with duckdb.connect(str(self.path), read_only=True) as conn:
            return {
                t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("book_events", "stream_trades", "stream_gaps")
            }
