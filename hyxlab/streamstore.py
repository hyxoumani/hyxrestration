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

import json
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


# -- sidecar (spill) serialization: one JSON line per row, tagged by table.
# ISO timestamps round-trip tz-awareness exactly, so rows drained from the
# sidecar store the ORIGINAL event timestamps, never a drain-time restamp.


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _from_iso(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


def _enc_event(e: BookEvent) -> str:
    row = [
        e.venue,
        e.market_id,
        _iso(e.recv_ts),
        _iso(e.src_ts),
        e.sid,
        e.seq,
        e.kind,
        e.side,
        e.price,
        e.qty,
    ]
    return json.dumps({"t": "e", "r": row}) + "\n"


def _enc_trade(t: StreamTrade) -> str:
    row = [
        t.venue,
        t.market_id,
        _iso(t.recv_ts),
        _iso(t.src_ts),
        t.price,
        t.qty,
        t.taker_side,
        t.seq,
    ]
    return json.dumps({"t": "t", "r": row}) + "\n"


def _enc_gap(g: tuple) -> str:
    return json.dumps({"t": "g", "r": [g[0], g[1], _iso(g[2]), _iso(g[3]), g[4]]}) + "\n"


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
        # Rows moved to the sidecar by _spill_overflow since the last good
        # flush — observability only (`pending` plateaus at SPILL_CAP during
        # a wedge). Approximate across restarts: starts at 0 even if a
        # crashed daemon left a sidecar on disk (the file itself survives
        # and is drained on the first good flush regardless).
        self.spilled = 0
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

    # ~30 min of firehose at the observed ~105 ev/s. Exceeding this
    # means a reader has wedged the file far beyond a flush burst —
    # the flusher escalates its log so the journal shows it clearly
    # before memory pressure ever could (review M3).
    PENDING_ALARM = 200_000

    # 2x the alarm (~1 h of firehose). Past this, a failed flush moves
    # the OLDEST pending rows to a JSONL sidecar next to the DB, so a
    # multi-hour reader wedge (poly sweep runs ~7 h) bounds daemon
    # memory instead of growing without limit. The sidecar is drained
    # ahead of the in-memory buffer on the next good flush — recv order
    # preserved — and survives a daemon restart.
    SPILL_CAP = 400_000

    @property
    def pending(self) -> int:
        return len(self._events) + len(self._trades) + len(self._gaps)

    @property
    def _spill_path(self) -> Path:
        return self.path.parent / (self.path.name + ".spill.jsonl")

    # -- persistence ------------------------------------------------------

    def flush(self) -> int:
        """Write all buffered rows in one transaction; returns rows written.

        On any failure (e.g. a reader briefly holds the file lock) the
        batch is restored to the buffer front — recv order preserved —
        so the next flush retries it. Losing it would leave a silent,
        unmarked hole in the archive. If a wedge holds past SPILL_CAP
        pending rows, the oldest rows spill to the sidecar; it is
        written FIRST here (older than anything in memory), then
        removed only after the transaction commits — a crash between
        commit and unlink re-drains it (duplicates over holes)."""
        n = self.pending
        if n == 0 and not self._spill_path.exists():
            return 0
        events, self._events = self._events, []
        trades, self._trades = self._trades, []
        gaps, self._gaps = self._gaps, []
        try:
            with duckdb.connect(str(self.path)) as conn:
                # Parse the sidecar only once the write lock is held: in
                # a wedge it can hold hours of rows, and a flush that is
                # about to fail on connect must not pay to load it.
                s_events, s_trades, s_gaps = self._read_spill()
                self._insert(conn, s_events + events, s_trades + trades, s_gaps + gaps)
                n += len(s_events) + len(s_trades) + len(s_gaps)
        except BaseException:
            self._events[:0] = events
            self._trades[:0] = trades
            self._gaps[:0] = gaps
            self._spill_overflow()
            raise
        self._spill_path.unlink(missing_ok=True)
        self.spilled = 0
        return n

    def _spill_overflow(self) -> None:
        """Move the oldest pending rows to the sidecar until the buffer
        is back at SPILL_CAP. Append-only, oldest-first from each buffer
        front, so the sidecar always holds rows older than anything
        still in memory. Sidecar write lands BEFORE the buffers are
        trimmed — a failed disk write must not drop rows (mistakes #12:
        recovery claims get tested, not assumed)."""
        over = self.pending - self.SPILL_CAP
        if over <= 0:
            return
        lines: list[str] = []
        takes: list[tuple[list, int]] = []
        for buf, enc in (
            (self._events, _enc_event),
            (self._trades, _enc_trade),
            (self._gaps, _enc_gap),
        ):
            take = min(over, len(buf))
            lines.extend(enc(row) for row in buf[:take])
            takes.append((buf, take))
            over -= take
        with self._spill_path.open("a", encoding="utf-8") as f:
            f.writelines(lines)
        for buf, take in takes:
            del buf[:take]
        self.spilled += len(lines)

    def _read_spill(self) -> tuple[list[BookEvent], list[StreamTrade], list[tuple]]:
        events: list[BookEvent] = []
        trades: list[StreamTrade] = []
        gaps: list[tuple] = []
        if not self._spill_path.exists():
            return events, trades, gaps
        with self._spill_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                r = rec["r"]
                if rec["t"] == "e":
                    events.append(
                        BookEvent(
                            r[0],
                            r[1],
                            _from_iso(r[2]),
                            _from_iso(r[3]),
                            r[4],
                            r[5],
                            r[6],
                            r[7],
                            r[8],
                            r[9],
                        )
                    )
                elif rec["t"] == "t":
                    trades.append(
                        StreamTrade(
                            r[0], r[1], _from_iso(r[2]), _from_iso(r[3]), r[4], r[5], r[6], r[7]
                        )
                    )
                else:
                    gaps.append((r[0], r[1], _from_iso(r[2]), _from_iso(r[3]), r[4]))
        return events, trades, gaps

    def _insert(
        self,
        conn: duckdb.DuckDBPyConnection,
        events: list[BookEvent],
        trades: list[StreamTrade],
        gaps: list[tuple],
    ) -> None:
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
