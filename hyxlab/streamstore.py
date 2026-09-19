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

import contextlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from hyxlab.store import duck_connect

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
    kind: str  # 'snap' | 'delta' | 'void' (kalshi: frame archived no level)
    side: str  # 'yes' | 'no'; for kind='void' the frame's `type` instead
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
        # Sidecar records that could not be decoded on drain — i.e. actual
        # archive holes. Cumulative for the daemon's lifetime and NOT reset
        # with `spilled`: a lost row does not become un-lost once the file
        # drains, and the operator needs to see it after the fact.
        self.spill_corrupt = 0
        # One writer at a time, across THREADS -- not a DuckDB lock, which
        # excludes nothing here because both racers are this same process.
        # `streamd.flusher()` runs `flush()` on an `asyncio.to_thread`
        # worker, and cancelling a task awaiting `to_thread` returns in
        # 0.00s while that worker keeps running (measured 2026-09-18; the
        # thread is joined only at interpreter exit). So the shutdown
        # drain's write overlaps a periodic flush's write, on one buffer and
        # one sidecar, and three silent holes open: both flushes parse the
        # SAME sidecar and insert it twice (400 rows in, 800 archived --
        # measured); a `spill_all` lands in the sidecar that the in-flight
        # flush then unlinks post-commit, deleting rows it never read (60
        # rows, gone from archive, file and buffer alike -- measured); and
        # the three-statement buffer swap splits one recv-ordered batch
        # across two transactions. Re-entrant because the drain holds it
        # across its decision and then calls `flush`/`spill_all` under it;
        # see `exclusive` and `streamd.DRAIN_LOCK_WAIT_S`.
        self._flush_lock = threading.RLock()
        # Create schema up front so readers see the tables immediately.
        with duck_connect(str(self.path)) as conn:
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

    #: Rows per second the BUFFER fills at. Not a channel rate: `pending`
    #: counts events + trades + gaps, so every sizing below must use the
    #: SUM over the subscribed channels. Sizing these two constants off
    #: "~105 ev/s" -- the kalshi-trades figure quoted in `streamd`,
    #: `kalshi_ws` and venues.md -- understated them by 2.4x, because the
    #: book channel fills the same buffer and was never added in.
    #:
    #: MEASURED 2026-09-18, three independent instruments agreeing:
    #:   1. `hyxstream.duckdb` hourly counts, 174h to 09-18: book_events
    #:      median 96.1/s, stream_trades median 136.0/s, COMBINED median
    #:      249.0/s (p90 320.1, busiest hour 433.4, quietest 13.3).
    #:   2. The stall ledger's one long episode (09-13 01:20Z): 145,176
    #:      rows held over 599.7s = 242/s.
    #:   3. The pre-ledger journal tail: the 09-09/09-10 episodes reached
    #:      SPILL_CAP at 1756s and 1801s, i.e. 400_000/1756 = 228/s.
    #: Trades outnumber book events here, so the channel most likely to be
    #: quoted alone is also the one that is not the majority of the buffer.
    BUFFER_ROWS_PER_S = 250

    # ~13 min of firehose at the measured BUFFER_ROWS_PER_S (~8 min in the
    # busiest observed hour) -- NOT the ~30 min this once claimed off the
    # trade-channel rate. Exceeding this means a reader has wedged the file
    # far beyond a flush burst — the flusher escalates its log so the
    # journal shows it clearly before memory pressure ever could (review
    # M3). Memory is the loose bound of the two: a buffered row costs ~256 B
    # resident (measured 2026-09-18, tracemalloc: 264 B/BookEvent,
    # 248 B/StreamTrade), so even a buffer pinned at SPILL_CAP is ~102 MB
    # against the unit's 2G cgroup cap. The rows leaving memory, not the
    # memory itself, is what the cap below is protecting against.
    PENDING_ALARM = 200_000

    # 2x the alarm: ~27 min of firehose at BUFFER_ROWS_PER_S, and ~15 min in
    # the busiest observed hour. It was documented as "~1 h" while the real
    # rate made it half an hour, which is why `streamd._final_drain` could
    # carry the flatly false "a sub-hour stall never reaches SPILL_CAP" --
    # refuted, in the same commit, by the measurement in `streamd.STALL_LOG`
    # recording two ~30 min episodes that DID reach it. Past this, a failed
    # flush moves the OLDEST pending rows to a JSONL sidecar next to the DB,
    # so a multi-hour reader wedge (poly sweep runs ~7 h) bounds daemon
    # memory instead of growing without limit. The sidecar is drained
    # ahead of the in-memory buffer on the next good flush — recv order
    # preserved — and survives a daemon restart.
    SPILL_CAP = 400_000

    #: Rows/s one `flush()` sustains end to end — sidecar parse,
    #: `executemany` into all three tables, commit. This is the rate a
    #: SHUTDOWN drain is racing, and it is 28x SLOWER than the rate rows
    #: ARRIVE at (`BUFFER_ROWS_PER_S`); the two are not interchangeable
    #: and nothing before 2026-09-18 had measured this one at all.
    #:
    #: MEASURED 2026-09-18 on the live box, four buffer sizes at the
    #: observed channel mix, linear within 0.7%: 50k in 7.05s (7,090/s),
    #: 200k in 28.04s (7,132/s), 400k in 56.46s (7,085/s), 1M in 140.66s
    #: (7,110/s). Rounded DOWN to the slowest reading.
    #:
    #: Its consumer is `collector.streamd.DRAIN_BUDGET_S`: systemd stops
    #: the daemon with SIGTERM and SIGKILLs it `TimeoutStopSec` later, so
    #: the final drain has a row budget, and a flush that cannot finish
    #: inside it must not be STARTED — a SIGKILL lands mid-transaction,
    #: after `flush()` has already moved the buffers into locals, where no
    #: `except BaseException` will restore them and no gap row marks the
    #: hole.
    FLUSH_ROWS_PER_S = 7_100

    #: Bytes one sidecar JSONL record occupies. MEASURED 2026-09-18:
    #: 167 B/event, 150 B/trade, ~160 weighted by the observed channel mix
    #: (49.3 MB for 400,000 rows, 247.4 MB for 2,000,000). Lets
    #: `drain_rows_estimate` size the sidecar from a `stat()` instead of
    #: the parse the drain is deciding whether it can afford.
    SPILL_BYTES_PER_ROW = 160

    @property
    def pending(self) -> int:
        return len(self._events) + len(self._trades) + len(self._gaps)

    @contextlib.contextmanager
    def exclusive(self, timeout: float):
        """Hold the store's writer lock for a decision AND the write it
        leads to; yields whether it was acquired within `timeout`.

        For the shutdown drain, which cannot use a plain `with`: it reads
        `drain_rows_estimate()`, chooses flush or spill, and then performs
        it, and all three have to see the same buffer. It also cannot wait
        forever -- SIGKILL arrives `TimeoutStopSec` after SIGTERM whether or
        not the flusher's worker thread has finished -- so the refusal is
        reported to the caller rather than raised: a `False` here means the
        only safe action left is none, and saying so in the journal beats
        racing. Never blocks the FLUSHER, which takes the lock plainly; a
        drain that loses this wait is a drain whose rows a still-running
        flush is in many cases already writing.
        """
        got = self._flush_lock.acquire(timeout=timeout)
        try:
            yield got
        finally:
            if got:
                self._flush_lock.release()

    def drain_rows_estimate(self) -> int:
        """Rows the NEXT `flush()` would write: the buffer plus whatever
        the sidecar still holds.

        The sidecar is sized from its BYTE length over
        `SPILL_BYTES_PER_ROW`, never by parsing it. The one caller is the
        shutdown drain deciding whether it can afford the parse, so an
        estimate that pays for it first answers nothing; and the estimate
        only has to be good enough to separate "a few thousand rows" from
        "a multi-hour wedge", which are four orders of magnitude apart.
        An unreadable sidecar counts as zero: the drain then attempts the
        flush, which is the pre-existing behaviour for a sidecar it
        cannot stat.
        """
        with self._flush_lock:
            n = self.pending
            with contextlib.suppress(OSError):
                n += self._spill_path.stat().st_size // self.SPILL_BYTES_PER_ROW
            return n

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
        with self._flush_lock:
            n = self.pending
            if n == 0 and not self._spill_path.exists():
                return 0
            events, self._events = self._events, []
            trades, self._trades = self._trades, []
            gaps, self._gaps = self._gaps, []
            try:
                with duck_connect(str(self.path)) as conn:
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

    def spill_all(self) -> int:
        """Move EVERY pending row to the sidecar; returns rows moved.

        The overflow path below keeps SPILL_CAP rows in memory because it is
        protecting a daemon that will flush again in FLUSH_SECS. At shutdown
        neither is true: there is no next flush, so a row left in the buffer
        is a row lost, and the cap is 0. Called only when the final drain
        itself failed — the sidecar is the one place left that can hold the
        tape until the next boot drains it.
        """
        return self._spill_overflow(cap=0)

    def _spill_overflow(self, cap: int | None = None) -> int:
        """Move the oldest pending rows to the sidecar until the buffer
        is back at `cap` (SPILL_CAP by default); returns rows moved.
        Append-only, oldest-first from each buffer front, so the sidecar
        always holds rows older than anything still in memory. Sidecar
        write lands BEFORE the buffers are trimmed — a failed disk write
        must not drop rows (mistakes #12: recovery claims get tested, not
        assumed)."""
        with self._flush_lock:
            return self._spill_locked(cap)

    def _spill_locked(self, cap: int | None) -> int:
        over = self.pending - (self.SPILL_CAP if cap is None else cap)
        if over <= 0:
            return 0
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
        # Append all-or-nothing. A partial append (ENOSPC part-way through a
        # multi-hour wedge — the sidecar runs ~160 B/row (measured
        # 2026-09-18: 167 B/event, 150 B/trade, weighted by the observed
        # channel mix), so a 7 h poly-sweep wedge is 6.3M rows and ~1.0 GB,
        # not the ~430 MB this said while it used the trade-channel rate for
        # a buffer that also holds books; disk-full is a live possibility)
        # would leave a
        # torn record mid-file that the drain can only skip: a hole. Rewinding
        # to the last record boundary keeps that loss at zero, since the
        # buffers below are trimmed only once the bytes are down (EXP-936).
        before = self._spill_path.stat().st_size if self._spill_path.exists() else 0
        try:
            with self._spill_path.open("a", encoding="utf-8") as f:
                f.writelines(lines)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                with self._spill_path.open("r+b") as f:
                    f.truncate(before)
            except OSError:
                pass
            raise
        for buf, take in takes:
            del buf[:take]
        self.spilled += len(lines)
        return len(lines)

    def _read_spill(self) -> tuple[list[BookEvent], list[StreamTrade], list[tuple]]:
        """Decode the sidecar, skipping records it cannot parse.

        Skipping loses that row — the one thing this module otherwise refuses
        to do — but the alternative measured in EXP-936 is far worse: raising
        here aborts every future flush, so ONE torn trailing record (host
        crash mid-append, or a pre-fix ENOSPC) permanently stops ALL
        archiving and funnels live ingest into the same poisoned file. Bounded
        hole beats unbounded stall; `spill_corrupt` counts it so the hole is
        never silent."""
        events: list[BookEvent] = []
        trades: list[StreamTrade] = []
        gaps: list[tuple] = []
        if not self._spill_path.exists():
            return events, trades, gaps
        with self._spill_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
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
                except (ValueError, KeyError, TypeError, IndexError):
                    self.spill_corrupt += 1
                    continue
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

    def last_recv_ts(self, venue: str, channel: str) -> datetime | None:
        """Last persisted recv_ts for one venue/channel (the trades
        firehose lands in stream_trades; every book channel in
        book_events). The daemon uses it to bound a `seq_reset` gap when
        its in-process `last_recv` is gone (fresh process): the
        venue='*' daemon_start row starts at the max across BOTH tables,
        so the channel that died earlier keeps an uncovered tail without
        this (S-188 loss class 3). Returns naive UTC (as stored)."""
        table = "stream_trades" if channel == "trades" else "book_events"
        with duck_connect(str(self.path)) as conn:
            return conn.execute(
                f"SELECT max(recv_ts) FROM {table} WHERE venue = ?", [venue]
            ).fetchone()[0]

    def mark_startup_gap(self, now: datetime | None = None) -> None:
        """Record daemon downtime: everything between the last archived
        event and this start is unknown coverage. No-op on an empty DB
        (nothing was being covered yet)."""
        now = now or datetime.now(UTC)
        with duck_connect(str(self.path)) as conn:
            last = conn.execute(
                "SELECT max(ts) FROM (SELECT max(recv_ts) AS ts FROM book_events"
                " UNION ALL SELECT max(recv_ts) FROM stream_trades)"
            ).fetchone()[0]
        if last is None:
            return
        self.append_gap("*", "*", last, now, "daemon_start")

    def counts(self) -> dict[str, int]:
        with duck_connect(str(self.path), read_only=True) as conn:
            return {
                t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("book_events", "stream_trades", "stream_gaps")
            }
