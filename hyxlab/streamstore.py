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
  CLOB token (asset) id. kind='void' rows record a frame that archived
  no level, with `side` = the frame's event_type (same device, and same
  rationale, as kalshi's).
  **2026-07-08 -> 2026-09-24 HOLDS NO POLYMARKET DELTAS AT ALL.** The
  parser read the venue's documented list key, not the one on the wire,
  so 242,828,734 poly book rows in that span are ALL reconnect/refresh
  `book` seeds -- roughly 35 per token per day, a sampled image and not
  a stream. Measured: only 13.6% of poly trades in the last 14 days have
  a book frame within 2s of them (median 26s, p90 23 min). Nothing
  downstream consumed it (`simulator.capabilities` already declares
  polymarket INDEPENDENT_NO_BOOK), so no verdict rests on it -- but any
  analysis reaching back past 09-24 must treat poly L2 as absent, not
  thin. See mistakes #82.
- stream_gaps: closed intervals where coverage is broken (reconnects,
  seq gaps, daemon downtime). Replay must treat books as unknown inside
  a gap until the next snapshot re-seeds them.
- kalshi book `sid` is the WIRE sid PLUS `shard * SHARD_SID_STRIDE` since the
  2026-10-20 coverage expansion: the books channel is sharded across several
  connections, and Kalshi numbers each connection's subscriptions from 1, so
  without the offset two shards' (sid, seq) streams interleave into one and
  QA's seq-hole check (partitioned by sid) reads every row as a hole. Shard 0
  keeps the raw wire sid, so every row written before the expansion and every
  row from the first shard after it read exactly as they always did. Gap rows
  from every shard still say channel='books': an outage on one shard marks the
  whole channel blind, which over-excuses (books unknown where they were in
  fact known) and never under-excuses.
- stream_ticker (since 2026-10-20): the exchange-wide `ticker` channel -- top
  of book, last price, volume and open interest per market, one row per
  frame, prices in dollars. KXMVE* combos are excluded at the daemon unless
  the watchlist opts in. The channel carries no `seq` (documented: type, sid,
  msg), so its only detectable gaps are reconnects and dead air.
- stream_lifecycle (since 2026-10-20): every SEQUENCED frame of the
  exchange-wide `market_lifecycle_v2` channel -- market created / activated /
  deactivated / close_date_updated / determined / settled / metadata, plus
  `event_lifecycle` and `event_fee_update` -- with the parsed fields AND the
  frame's raw JSON, so a field this schema does not name is still on disk.
  `market_id` is the market ticker, or the EVENT ticker for event-level types.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from operator import attrgetter
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
CREATE TABLE IF NOT EXISTS stream_ticker (
    venue                VARCHAR NOT NULL,
    market_id            VARCHAR NOT NULL,
    recv_ts              TIMESTAMP NOT NULL,
    src_ts               TIMESTAMP,
    sid                  BIGINT,
    seq                  BIGINT,
    price                DOUBLE,
    yes_bid              DOUBLE,
    yes_ask              DOUBLE,
    yes_bid_size         DOUBLE,
    yes_ask_size         DOUBLE,
    last_trade_size      DOUBLE,
    volume               DOUBLE,
    open_interest        DOUBLE,
    dollar_volume        BIGINT,
    dollar_open_interest BIGINT
);
CREATE TABLE IF NOT EXISTS stream_lifecycle (
    venue            VARCHAR NOT NULL,
    market_id        VARCHAR NOT NULL,
    recv_ts          TIMESTAMP NOT NULL,
    src_ts           TIMESTAMP,
    sid              BIGINT,
    seq              BIGINT,
    msg_type         VARCHAR NOT NULL,
    event_type       VARCHAR,
    event_ticker     VARCHAR,
    series_ticker    VARCHAR,
    open_ts          TIMESTAMP,
    close_ts         TIMESTAMP,
    determination_ts TIMESTAMP,
    settled_ts       TIMESTAMP,
    result           VARCHAR,
    settlement_value DOUBLE,
    is_deactivated   BOOLEAN,
    raw              VARCHAR NOT NULL
);
"""

#: Added to a kalshi books shard's wire sid before it is stored (see the
#: module docstring). A connection opens ONE subscription, so its wire sids
#: stay single-digit; the stride only has to clear that by a wide margin.
SHARD_SID_STRIDE = 1_000_000

# Column order and SQL type of every table, as `_insert` writes it (the CSV
# it stages is read back with exactly these types -- see `_insert_frame`).
_COLS = {
    "book_events": (
        ("venue", "VARCHAR"),
        ("market_id", "VARCHAR"),
        ("recv_ts", "TIMESTAMP"),
        ("src_ts", "TIMESTAMP"),
        ("sid", "BIGINT"),
        ("seq", "BIGINT"),
        ("kind", "VARCHAR"),
        ("side", "VARCHAR"),
        ("price", "DOUBLE"),
        ("qty", "DOUBLE"),
    ),
    "stream_trades": (
        ("venue", "VARCHAR"),
        ("market_id", "VARCHAR"),
        ("recv_ts", "TIMESTAMP"),
        ("src_ts", "TIMESTAMP"),
        ("price", "DOUBLE"),
        ("qty", "DOUBLE"),
        ("taker_side", "VARCHAR"),
        ("seq", "BIGINT"),
    ),
    "stream_gaps": (
        ("venue", "VARCHAR"),
        ("channel", "VARCHAR"),
        ("started_at", "TIMESTAMP"),
        ("ended_at", "TIMESTAMP"),
        ("reason", "VARCHAR"),
    ),
    "stream_ticker": (
        ("venue", "VARCHAR"),
        ("market_id", "VARCHAR"),
        ("recv_ts", "TIMESTAMP"),
        ("src_ts", "TIMESTAMP"),
        ("sid", "BIGINT"),
        ("seq", "BIGINT"),
        ("price", "DOUBLE"),
        ("yes_bid", "DOUBLE"),
        ("yes_ask", "DOUBLE"),
        ("yes_bid_size", "DOUBLE"),
        ("yes_ask_size", "DOUBLE"),
        ("last_trade_size", "DOUBLE"),
        ("volume", "DOUBLE"),
        ("open_interest", "DOUBLE"),
        ("dollar_volume", "BIGINT"),
        ("dollar_open_interest", "BIGINT"),
    ),
    "stream_lifecycle": (
        ("venue", "VARCHAR"),
        ("market_id", "VARCHAR"),
        ("recv_ts", "TIMESTAMP"),
        ("src_ts", "TIMESTAMP"),
        ("sid", "BIGINT"),
        ("seq", "BIGINT"),
        ("msg_type", "VARCHAR"),
        ("event_type", "VARCHAR"),
        ("event_ticker", "VARCHAR"),
        ("series_ticker", "VARCHAR"),
        ("open_ts", "TIMESTAMP"),
        ("close_ts", "TIMESTAMP"),
        ("determination_ts", "TIMESTAMP"),
        ("settled_ts", "TIMESTAMP"),
        ("result", "VARCHAR"),
        ("settlement_value", "DOUBLE"),
        ("is_deactivated", "BOOLEAN"),
        ("raw", "VARCHAR"),
    ),
}

#: Every table the archive holds, in the order `counts()` reports them.
TABLES = tuple(_COLS)


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


def _row(obj) -> tuple:
    """A row dataclass as a flat tuple in field order (its `_COLS` order).
    `dataclasses.astuple` deep-copies every value; this is one C call."""
    getter = _GETTERS.get(type(obj))
    if getter is None:
        getter = _GETTERS[type(obj)] = attrgetter(*obj.__dataclass_fields__)
    return getter(obj)


_GETTERS: dict[type, attrgetter] = {}


def _ts_cols(table: str) -> frozenset[int]:
    return frozenset(i for i, (_, dt) in enumerate(_COLS[table]) if dt == "TIMESTAMP")


def _enc_generic(tag: str, table: str, row) -> str:
    """The two 2026-10-20 tables, one encoder: every timestamp column to ISO
    (tz-awareness round-trips, as for the original three), everything else
    as-is."""
    ts = _ts_cols(table)
    vals = [_iso(v) if i in ts else v for i, v in enumerate(_row(row))]
    return json.dumps({"t": tag, "r": vals}) + "\n"


def _dec_generic(cls, table: str, r: list):
    ts = _ts_cols(table)
    if len(r) != len(_COLS[table]):
        raise ValueError(f"{table} sidecar row has {len(r)} fields")
    return cls(*[_from_iso(v) if i in ts else v for i, v in enumerate(r)])


def _insert_frame(
    conn: duckdb.DuckDBPyConnection, table: str, rows: list[tuple], tmpdir: str | Path
) -> None:
    """Insert `rows` (tuples in `_COLS[table]` order, timestamps already naive
    UTC) through a staged CSV file and one `read_csv` scan. A no-op for an
    empty batch.

    Why a file: the stable deployment's venv carries duckdb and nothing that
    DuckDB can scan in memory (no pandas, numpy, pyarrow or fsspec --
    `scripts/requirements-stable.txt`), and `executemany` runs one prepared
    INSERT per row. The CSV is lossless for every type here: QUOTE_NOTNULL
    quotes every non-None value, so an unquoted empty field is NULL and a
    quoted one is the empty string (`allow_quoted_nulls=false`); str() of a
    naive datetime is DuckDB's timestamp text and of a float its repr; quotes
    and newlines inside VARCHARs (the lifecycle `raw` frame) are escaped by
    the csv module. `tests/test_stream_coverage.py` pins the round trip
    against `executemany` as an oracle, NULLs and empty strings included."""
    if not rows:
        return
    cols = _COLS[table]
    fd, path = tempfile.mkstemp(prefix=f".{table}-", suffix=".csv", dir=str(tmpdir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            csv.writer(fh, quoting=csv.QUOTE_NOTNULL, lineterminator="\n").writerows(rows)
        names = ", ".join(n for n, _ in cols)
        conn.execute(
            f"INSERT INTO {table} ({names}) SELECT {names} FROM read_csv(?, header=false,"
            " columns=?, auto_detect=false, quote='\"', escape='\"', nullstr='',"
            " allow_quoted_nulls=false)",
            [path, dict(cols)],
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _enc_ticker(t: StreamTicker) -> str:
    return _enc_generic("k", "stream_ticker", t)


def _enc_lifecycle(lc: StreamLifecycle) -> str:
    return _enc_generic("l", "stream_lifecycle", lc)


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


@dataclass
class StreamTicker:
    """One `ticker` frame. Fields follow `_COLS['stream_ticker']` exactly
    (the sidecar codec and `_insert` both rely on that order)."""

    venue: str
    market_id: str
    recv_ts: datetime
    src_ts: datetime | None
    sid: int | None
    seq: int | None
    price: float | None
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_size: float | None
    yes_ask_size: float | None
    last_trade_size: float | None
    volume: float | None
    open_interest: float | None
    dollar_volume: int | None
    dollar_open_interest: int | None


@dataclass
class StreamLifecycle:
    """One sequenced `market_lifecycle_v2`-channel frame. Field order is
    `_COLS['stream_lifecycle']`."""

    venue: str
    market_id: str
    recv_ts: datetime
    src_ts: datetime | None
    sid: int | None
    seq: int | None
    msg_type: str
    event_type: str | None
    event_ticker: str | None
    series_ticker: str | None
    open_ts: datetime | None
    close_ts: datetime | None
    determination_ts: datetime | None
    settled_ts: datetime | None
    result: str | None
    settlement_value: float | None
    is_deactivated: bool | None
    raw: str


@dataclass(frozen=True)
class FlushOutcome:
    """What one `StreamStore.flush_outcome` did.

    `rows` is what it wrote. `touched` is whether it OPENED THE ARCHIVE,
    which is the only thing that makes a non-raising flush evidence that
    the file was writable -- see `flush_outcome`.
    """

    rows: int
    touched: bool


#: How far back `announced_opens` reads `stream_lifecycle` for `created`
#: frames. The 15-minute series are created ~24-30h ahead (measured
#: 2026-10-27 20:01Z for opens through 10-29 02:15Z); 7 days covers any
#: series announced a week out without scanning the whole table.
ANNOUNCED_LOOKBACK_S = 7 * 86400.0


class StreamStore:
    """Buffered writer. append_*() only buffers; flush() opens a
    connection, writes everything, and closes it again."""

    def __init__(self, path: str | Path = "data/hyxstream.duckdb") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[BookEvent] = []
        self._trades: list[StreamTrade] = []
        self._gaps: list[tuple] = []
        self._tickers: list[StreamTicker] = []
        self._lifecycles: list[StreamLifecycle] = []
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

    def append_tickers(self, tickers: list[StreamTicker]) -> None:
        self._tickers.extend(tickers)

    def append_lifecycles(self, rows: list[StreamLifecycle]) -> None:
        self._lifecycles.extend(rows)

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
    #:
    #: RE-MEASURED 2026-10-23 for the coverage expansion: a 10-minute staging
    #: run of the branch (Fri 11:52Z, scratch DB) archived ~3,400 book rows/s
    #: (per-minute range ~1,400-5,000; the 15-minute series roll at :00/:15
    #: moves it most) and ~650 ticker rows/s; trades stay ~136/s and
    #: lifecycle is ~7/s. Sum ~4,200/s -- 17x the 250 above, which is kept
    #: in this history, not in the constant. One 10-minute window: re-measure
    #: from a day of production once promoted.
    BUFFER_ROWS_PER_S = 4_200

    # ~4 min of firehose at the measured BUFFER_ROWS_PER_S. Exceeding this
    # means a reader has wedged the file far beyond a flush burst — the
    # flusher escalates its log so the journal shows it clearly (review M3).
    # Until 2026-10-23 this was 200,000 rows = ~13 min at 250/s, and memory
    # was the loose bound. It no longer is: a buffered row is MEASURED
    # (2026-10-23, tracemalloc, one market string per row as on the wire) at
    # ~425 B/BookEvent and ~613 B/StreamTicker, ~460 B at the expansion's
    # mix, so minutes of buffer now cost real fractions of the 4G unit cap.
    # The two constants are therefore sized by MEMORY and their minutes
    # follow; at the old 13/27 min they would have been ~1.5/3.1 GB.
    PENDING_ALARM = 1_000_000

    # 2x the alarm: ~8 min of firehose at BUFFER_ROWS_PER_S, ~0.9 GB
    # resident -- under a quarter of the unit's 4G cap, leaving the rest to
    # DuckDB's flush (hyxlab.memcap), the raw-frame queue and page cache.
    # (It was 400,000 = ~27 min at the pre-expansion 250/s; it once claimed
    # "~1 h" off the trade-channel rate, which let `streamd._final_drain`
    # carry the false "a sub-hour stall never reaches SPILL_CAP".) A stall
    # past ~8 min now reaches the sidecar, which is lossless and drains at
    # FLUSH_ROWS_PER_S. Past this, a failed
    # flush moves the OLDEST pending rows to a JSONL sidecar next to the DB,
    # so a multi-hour reader wedge (poly sweep runs ~7 h) bounds daemon
    # memory instead of growing without limit. The sidecar is drained
    # ahead of the in-memory buffer on the next good flush — recv order
    # preserved — and survives a daemon restart.
    SPILL_CAP = 2_000_000

    #: Rows/s one `flush()` sustains end to end — sidecar parse, the insert
    #: into every table, commit. This is the rate a SHUTDOWN drain is
    #: racing; it is not the rate rows ARRIVE at (`BUFFER_ROWS_PER_S`), and
    #: the two are not interchangeable.
    #:
    #: HISTORY: 7,100 until 2026-10-23 -- MEASURED 2026-09-18 on the live
    #: box over the `executemany` path, linear within 0.7% from 50k to 1M
    #: rows. The coverage expansion replaced that path with a staged-CSV
    #: `read_csv` (`_insert_frame`); MEASURED 2026-10-23 on a temp database
    #: under the STABLE venv, observed channel mix plus ticker rows: buffer
    #: flushes 313k-396k rows/s (50k / 200k / 1M rows), and the slowest path,
    #: a 400k-row SIDECAR drain (JSON parse dominates), 189,584 rows/s.
    #: Set to ~half that slowest reading, not to it: the benchmark file was
    #: empty and uncontended, the live one is tens of GB under a memory cap
    #: shared with page cache. Re-measure on the box once promoted.
    #:
    #: Its consumer is `collector.streamd.DRAIN_BUDGET_S`: systemd stops
    #: the daemon with SIGTERM and SIGKILLs it `TimeoutStopSec` later, so
    #: the final drain has a row budget, and a flush that cannot finish
    #: inside it must not be STARTED — a SIGKILL lands mid-transaction,
    #: after `flush()` has already moved the buffers into locals, where no
    #: `except BaseException` will restore them and no gap row marks the
    #: hole.
    FLUSH_ROWS_PER_S = 100_000

    #: Bytes one sidecar JSONL record occupies. MEASURED 2026-09-18:
    #: 167 B/event, 150 B/trade, ~160 weighted by the observed channel mix
    #: (49.3 MB for 400,000 rows, 247.4 MB for 2,000,000). Lets
    #: `drain_rows_estimate` size the sidecar from a `stat()` instead of
    #: the parse the drain is deciding whether it can afford.
    SPILL_BYTES_PER_ROW = 160

    #: Most sidecar rows one drain transaction parses and inserts. The drain
    #: costs ~750 B/row resident (MEASURED 2026-10-07 on the box, parse +
    #: `executemany`: 279 MB peak at 200k rows, 730 MB at 800k, 116 MB
    #: base), so a drain that took the whole sidecar at once needed ~1.6 GB
    #: for the 1.69M rows one 3750s QA hold spilled, under a 2G cap -- and
    #: since the sidecar survives a restart by design, every boot re-ran the
    #: same OOM: 189 kills in 1h53m (mistakes #112). At this size a chunk is
    #: ~75 MB whatever the wedge was, and the checkpoint below lets a killed
    #: drain resume instead of starting over.
    DRAIN_CHUNK_ROWS = 100_000

    @property
    def pending(self) -> int:
        return (
            len(self._events)
            + len(self._trades)
            + len(self._gaps)
            + len(self._tickers)
            + len(self._lifecycles)
        )

    def _buffers(self) -> tuple[tuple[list, Callable[[object], str]], ...]:
        """Every buffer with its sidecar encoder, OLDEST-TABLE-FIRST in the
        fixed order the sidecar has always been written in (events, trades,
        gaps), the two 2026-10-20 tables after them."""
        return (
            (self._events, _enc_event),
            (self._trades, _enc_trade),
            (self._gaps, _enc_gap),
            (self._tickers, _enc_ticker),
            (self._lifecycles, _enc_lifecycle),
        )

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
                left = self._spill_path.stat().st_size - self._spill_done()
                n += max(0, left) // self.SPILL_BYTES_PER_ROW
            return n

    @property
    def _spill_path(self) -> Path:
        return self.path.parent / (self.path.name + ".spill.jsonl")

    @property
    def _spill_done_path(self) -> Path:
        """Byte offset of the sidecar's last COMMITTED drain chunk.

        Meaningful only beside the sidecar it was written for: it is removed
        BEFORE the sidecar (a crash between the two re-drains the whole file
        -- duplicates, never holes), and a spill that starts a fresh sidecar
        removes any orphan first, so an old offset can never skip new rows.
        """
        return self.path.parent / (self._spill_path.name + ".done")

    def _spill_done(self) -> int:
        try:
            return int(self._spill_done_path.read_text())
        except (OSError, ValueError):
            return 0

    def _mark_spill_done(self, offset: int) -> None:
        tmp = self._spill_done_path.with_name(self._spill_done_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(str(offset))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._spill_done_path)

    def _drain_spill(self, conn: duckdb.DuckDBPyConnection) -> int:
        """Insert the sidecar oldest-first, one committed chunk at a time,
        checkpointing after each; returns rows drained. A failure part-way
        leaves the committed chunks committed and the rest for next time."""
        offset, drained = self._spill_done(), 0
        while True:
            events, trades, gaps, tickers, lifecycles, end = self._read_spill(
                offset, self.DRAIN_CHUNK_ROWS
            )
            if end == offset:
                return drained
            self._insert(conn, events, trades, gaps, tickers, lifecycles)
            drained += len(events) + len(trades) + len(gaps) + len(tickers) + len(lifecycles)
            offset = end
            self._mark_spill_done(offset)

    # -- persistence ------------------------------------------------------

    def flush(self) -> int:
        """Rows written by `flush_outcome`; see there. Kept because most
        callers want the count and nothing else."""
        return self.flush_outcome().rows

    def flush_outcome(self) -> FlushOutcome:
        """Write all buffered rows in one transaction; returns the outcome.

        On any failure (e.g. a reader briefly holds the file lock) the
        batch is restored to the buffer front — recv order preserved —
        so the next flush retries it. Losing it would leave a silent,
        unmarked hole in the archive. If a wedge holds past SPILL_CAP
        pending rows, the oldest rows spill to the sidecar; it is
        written FIRST here (older than anything in memory), then
        removed only after the transaction commits — a crash between
        commit and unlink re-drains it (duplicates over holes).

        `touched` IS THE POINT OF THIS RETURN TYPE, AND `rows` CANNOT
        STAND IN FOR IT. There is nothing to write far more often than
        one would guess, and this method then returns WITHOUT OPENING
        THE FILE — so a caller reading "did not raise" as "the archive
        is writable" is reading a claim about the buffer. `streamd`'s
        stall ledger did exactly that and closed an open stall episode
        on an empty tick, mid-hold, with the lock still held by someone
        else (EXP-1390, mistakes #103). A flush that touched nothing is
        evidence about nothing. `rows == 0` does NOT imply it: a drain
        of an empty sidecar opens the file and writes no rows.
        """
        with self._flush_lock:
            n = self.pending
            if n == 0 and not self._spill_path.exists():
                return FlushOutcome(rows=0, touched=False)
            events, self._events = self._events, []
            trades, self._trades = self._trades, []
            gaps, self._gaps = self._gaps, []
            tickers, self._tickers = self._tickers, []
            lifecycles, self._lifecycles = self._lifecycles, []
            try:
                with duck_connect(str(self.path)) as conn:
                    # Parse the sidecar only once the write lock is held: in
                    # a wedge it can hold hours of rows, and a flush that is
                    # about to fail on connect must not pay to load it. It
                    # drains in its own chunked transactions, ahead of the
                    # buffer -- recv order kept, memory bounded by a chunk.
                    n += self._drain_spill(conn)
                    self._insert(conn, events, trades, gaps, tickers, lifecycles)
            except BaseException:
                self._events[:0] = events
                self._trades[:0] = trades
                self._gaps[:0] = gaps
                self._tickers[:0] = tickers
                self._lifecycles[:0] = lifecycles
                self._spill_overflow()
                raise
            self._spill_done_path.unlink(missing_ok=True)
            self._spill_path.unlink(missing_ok=True)
            self.spilled = 0
            return FlushOutcome(rows=n, touched=True)

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

    # -- shutdown snapshot: a sidecar no in-flight flush knows about ---------
    # The drain's REFUSED branch (streamd.DRAIN_LOCK_WAIT_S): a periodic
    # flush still holds `_flush_lock`, so `spill_all` would append to the
    # sidecar that flush unlinks post-commit (mistakes #62, measured). Until
    # 2026-10-14 the drain therefore declined and the buffer died with the
    # process: measured that day, a poweroff landed inside an outside hold
    # and 143,520 rows were "left in memory" -- gone. A snapshot to a FRESH
    # file closes that: the in-flight flush never learns the file's name, so
    # it can neither parse it twice nor unlink it, and the next boot adopts
    # it into the sidecar before any flush runs. The buffers are NOT trimmed
    # (the flush's failure path restores and overflow-spills them, and a
    # trim would race that), so the worst case is a duplicate, never a hole.

    @property
    def _snapshot_glob(self) -> str:
        return self.path.name + ".spill.snap-*.jsonl"

    def _snapshot_path(self, now: datetime | None = None) -> Path:
        ts = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%f")
        return self.path.parent / f"{self.path.name}.spill.snap-{ts}-{os.getpid()}.jsonl"

    def spill_snapshot(self, now: datetime | None = None) -> tuple[int, Path | None]:
        """Copy EVERY pending row to a fresh, uniquely named sidecar WITHOUT
        taking the writer lock or trimming the buffers; returns rows written
        and the file, or (0, None) when there was nothing to write.

        Deliberately lock-free: it exists for the one moment the lock is
        held by a flush this process cannot wait for (SIGKILL is
        `TimeoutStopSec` away). Each buffer is copied in ONE list operation,
        which under the GIL is atomic against the flush worker's restore
        (`buf[:0] = batch`) and overflow trim (`del buf[:take]`), so the copy
        is a consistent instant of the buffer -- either side of the restore,
        never half of it. All-or-nothing on disk: a partial file is removed
        rather than left for the next boot to parse a torn tail from.
        """
        copies = [(list(buf), enc) for buf, enc in self._buffers()]
        n = sum(len(rows) for rows, _ in copies)
        if n == 0:
            return 0, None
        path = self._snapshot_path(now)
        try:
            with path.open("x", encoding="utf-8") as f:
                for rows, enc in copies:
                    f.writelines(enc(r) for r in rows)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self.spilled += n
        return n, path

    def adopt_snapshots(self) -> int:
        """Fold every shutdown snapshot into the sidecar, oldest first;
        returns rows adopted. Call at BOOT, before the first flush: the
        snapshot's rows are newer than anything the sidecar held at the
        shutdown that wrote it, so they are appended, and the `.done`
        offset (a prefix of the existing file) stays valid. With no sidecar
        on disk the snapshot BECOMES it, and a stale `.done` is removed
        first so an orphaned offset cannot skip the adopted rows."""
        with self._flush_lock:
            adopted = 0
            for snap in sorted(self.path.parent.glob(self._snapshot_glob)):
                data = snap.read_bytes()
                rows = sum(1 for line in data.splitlines() if line.strip())
                if self._spill_path.exists() and self._spill_path.stat().st_size > 0:
                    with self._spill_path.open("ab") as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                    snap.unlink()
                else:
                    self._spill_done_path.unlink(missing_ok=True)
                    os.replace(snap, self._spill_path)
                adopted += rows
            return adopted

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
        for buf, enc in self._buffers():
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
        if before == 0:
            # A fresh sidecar: an orphaned checkpoint would skip its rows.
            self._spill_done_path.unlink(missing_ok=True)
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

    def _read_spill(
        self, start: int = 0, limit: int | None = None
    ) -> tuple[
        list[BookEvent],
        list[StreamTrade],
        list[tuple],
        list[StreamTicker],
        list[StreamLifecycle],
        int,
    ]:
        """Decode up to `limit` sidecar records from byte `start`, skipping
        records it cannot parse; returns them and the byte offset reached.

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
        tickers: list[StreamTicker] = []
        lifecycles: list[StreamLifecycle] = []
        if not self._spill_path.exists():
            return events, trades, gaps, tickers, lifecycles, start
        offset, taken = start, 0
        with self._spill_path.open("rb") as f:
            f.seek(start)
            for raw in f:
                if limit is not None and taken >= limit:
                    break
                offset += len(raw)
                taken += 1
                line = raw.decode("utf-8", errors="replace")
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
                    elif rec["t"] == "k":
                        tickers.append(_dec_generic(StreamTicker, "stream_ticker", r))
                    elif rec["t"] == "l":
                        lifecycles.append(_dec_generic(StreamLifecycle, "stream_lifecycle", r))
                    else:
                        gaps.append((r[0], r[1], _from_iso(r[2]), _from_iso(r[3]), r[4]))
                except (ValueError, KeyError, TypeError, IndexError):
                    self.spill_corrupt += 1
                    continue
        return events, trades, gaps, tickers, lifecycles, offset

    def _insert(
        self,
        conn: duckdb.DuckDBPyConnection,
        events: list[BookEvent],
        trades: list[StreamTrade],
        gaps: list[tuple],
        tickers: list[StreamTicker] = (),  # type: ignore[assignment]
        lifecycles: list[StreamLifecycle] = (),  # type: ignore[assignment]
    ) -> None:
        """All five tables in ONE transaction, each as one staged-CSV scan.

        Until 2026-10-20 this was `executemany`, which DuckDB runs as one
        prepared INSERT per row: 7,100 rows/s end to end on this box
        (`FLUSH_ROWS_PER_S` before the change). The coverage expansion
        multiplies the arrival rate several-fold, and at that path a
        15 s flush of a busy minute would have needed most of the 15 s.
        `_insert_frame` loads the same rows in one vectorised `read_csv` --
        measured on a temp database 2026-10-23, see `FLUSH_ROWS_PER_S`."""
        conn.execute("BEGIN")
        tmp = self.path.parent
        _insert_frame(
            conn,
            "book_events",
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
            tmp,
        )
        _insert_frame(
            conn,
            "stream_trades",
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
            tmp,
        )
        _insert_frame(conn, "stream_gaps", gaps, tmp)
        for table, rows in (("stream_ticker", tickers), ("stream_lifecycle", lifecycles)):
            ts = _ts_cols(table)
            _insert_frame(
                conn,
                table,
                [
                    tuple(_naive_utc(v) if i in ts else v for i, v in enumerate(_row(r)))
                    for r in rows
                ],
                tmp,
            )
        conn.execute("COMMIT")

    def last_recv_ts(self, venue: str, channel: str) -> datetime | None:
        """Last persisted recv_ts for one venue/channel (the trades
        firehose lands in stream_trades; every book channel in
        book_events). The daemon uses it to bound a `seq_reset` gap when
        its in-process `last_recv` is gone (fresh process): the
        venue='*' daemon_start row starts at the max across BOTH tables,
        so the channel that died earlier keeps an uncovered tail without
        this (S-188 loss class 3). Returns naive UTC (as stored). The
        2026-10-20 channels read their own tables: `ticker` from
        stream_ticker, `lifecycle` from stream_lifecycle -- reading
        book_events for them would bound their seq_reset row by the BOOK
        channel's last frame, a different stream entirely."""
        table = {
            "trades": "stream_trades",
            "ticker": "stream_ticker",
            "lifecycle": "stream_lifecycle",
        }.get(channel, "book_events")
        with duck_connect(str(self.path)) as conn:
            return conn.execute(
                f"SELECT max(recv_ts) FROM {table} WHERE venue = ?", [venue]
            ).fetchone()[0]

    def announced_opens(
        self, now: datetime | None = None, lookback_s: float = ANNOUNCED_LOOKBACK_S
    ) -> dict[str, datetime]:
        """`{market_id: open_ts}` (aware UTC) for every market the lifecycle
        channel announced (`created` with an `open_ts`) inside `lookback_s`
        that no later frame determined, settled or deactivated -- whether
        or not it has opened yet. The daemon seeds `BookCoverage.pending_open`
        from this at boot: that queue lived only in memory, and the 15-minute
        series are created in one daily batch 24-30h ahead, so a restart
        forgot every market of the next day and each reached the book set
        only through the reconcile walk, ~4m45s after opening (measured
        2026-10-29: 7 of 8 KXBTC15M markets, 18-28% of each one's prints
        with no book). Empty when the archive does not exist yet.

        The attach is the owner's read-WRITE configuration, like `flush`:
        DuckDB serves every same-process attach from one instance and
        refuses a second configuration while the first is open, and the
        flusher thread may be mid-write when the book set boots. The first
        production boot (2026-10-29 14:59:39Z) asked read-only and got
        `Can't open a connection to same database file with a different
        configuration` -- the backstop logged it and the fix was inert."""
        if not self.path.exists():
            return {}
        now = now or datetime.now(UTC)
        since = (now - timedelta(seconds=lookback_s)).replace(tzinfo=None)
        with duck_connect(str(self.path)) as conn:
            rows = conn.execute(
                "WITH c AS (SELECT market_id, max(open_ts) AS open_ts FROM stream_lifecycle"
                " WHERE msg_type = 'market_lifecycle_v2' AND event_type = 'created'"
                " AND open_ts IS NOT NULL AND recv_ts > ? GROUP BY market_id)"
                " SELECT c.market_id, c.open_ts FROM c WHERE NOT EXISTS ("
                "   SELECT 1 FROM stream_lifecycle x WHERE x.market_id = c.market_id"
                "   AND x.recv_ts > ? AND (x.event_type IN ('determined', 'settled')"
                "        OR x.is_deactivated IS TRUE))",
                [since, since],
            ).fetchall()
        return {m: ts.replace(tzinfo=UTC) for m, ts in rows}

    def mark_startup_gap(self, now: datetime | None = None) -> None:
        """Record daemon downtime: everything between the last archived
        event and this start is unknown coverage. No-op on an empty DB
        (nothing was being covered yet)."""
        now = now or datetime.now(UTC)
        with duck_connect(str(self.path)) as conn:
            last = conn.execute(
                "SELECT max(ts) FROM (SELECT max(recv_ts) AS ts FROM book_events"
                " UNION ALL SELECT max(recv_ts) FROM stream_trades"
                " UNION ALL SELECT max(recv_ts) FROM stream_ticker"
                " UNION ALL SELECT max(recv_ts) FROM stream_lifecycle)"
            ).fetchone()[0]
        if last is None:
            return
        self.append_gap("*", "*", last, now, "daemon_start")

    def counts(self) -> dict[str, int]:
        with duck_connect(str(self.path), read_only=True) as conn:
            return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
