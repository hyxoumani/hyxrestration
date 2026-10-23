"""Raw Kalshi WS frames, kept verbatim, off the archive's write path.

The stream daemon parses every frame into rows and, until 2026-10-20,
discarded the frame. That made the parser the archive's only reader of the
wire: a field it did not name (a new `ticker` key, a lifecycle metadata blob)
or a frame type it parsed to a void row was gone the moment it arrived, and
neither venue serves the stream again. #9 and #82 were both a parser reading
a key the wire did not carry; in both, a raw copy would have turned "weeks of
silently thin data" into "re-parse the files".

    <root>/<YYYY-MM-DD>/<HH>-<channel>.jsonl.<ext>

one file per UTC hour per channel (`trades`, `books`, `ticker`, `lifecycle`),
each line `<recv_ts ISO>\\t<connection label>\\t<frame text>`. The label
names the socket (`books-3`), because books shards each number their
subscription from sid 1 and the frame alone cannot say which one it came
from. Compression is zstd (Python 3.14's stdlib `compression.zstd`; the
`zstandard` package is in neither venv) and gzip otherwise; both formats
are multi-frame, so re-opening an hour's file in append mode after a restart
leaves one valid file. A crash can leave a truncated final frame: readers
should read up to the first decode error, and everything before it is whole.

Never on the event loop's critical path: `put` only appends to a bounded
in-memory queue and returns; one daemon thread compresses and writes. When
the queue is over its byte budget the frame is DROPPED and counted -- a raw
copy that back-pressured the socket would cost the parsed archive the very
frames it exists to duplicate, and the parsed rows are still the primary
record. Drops and write errors are published through `stats()` into the
daemon's stats line; they are never silent and never raised.
"""

from __future__ import annotations

import collections
import gzip
import re
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

#: Queue budget in BYTES of frame text. Frames are ~150-600 B; at the
#: coverage expansion's projected ~2-6k frames/s busy-hour rate this is
#: ~10-60 s of wire, far more than the writer needs (zstd level 3 runs at
#: hundreds of MB/s against an input of ~1-3 MB/s), so it fills only if the
#: DISK stalls.
QUEUE_BYTES = 64 * 1024 * 1024
#: How often open files are flushed to a decodable boundary. Bounds what a
#: SIGKILL or power loss can take from the tail of each open file.
FLUSH_SECS = 5.0
#: Days of raw frames kept on disk. MEASURED 2026-10-23 (10-minute staging
#: run of the coverage expansion): ~58 MB per 10 min compressed, ~8 GB/day,
#: so 14 days is ~120 GB. The parsed archive is the primary record; this is
#: the window in which a parser defect can still be repaired from the wire.
RETAIN_DAYS = 14
#: How often the writer thread prunes day directories past RETAIN_DAYS.
PRUNE_SECS = 3600.0


def _codec() -> tuple[str, Any]:
    """(extension, opener(path) -> binary writable file in APPEND mode).

    Python 3.14's stdlib `compression.zstd` (the stable venv runs 3.14.5 and
    pins no compression package -- `scripts/requirements-stable.txt`), gzip
    on an interpreter without it."""
    try:
        from compression import zstd

        def open_stdlib_zstd(path: Path) -> BinaryIO:
            return zstd.ZstdFile(path, "a", level=3)

        return "zst", open_stdlib_zstd
    except ImportError:

        def open_gzip(path: Path) -> BinaryIO:
            return gzip.open(path, "ab", compresslevel=6)

        return "gz", open_gzip


def _flush_block(fh: Any) -> None:
    """Flush a compressor to a boundary a reader can decode up to."""
    try:
        from compression import zstd

        if isinstance(fh, zstd.ZstdFile):
            fh.flush(zstd.ZstdFile.FLUSH_BLOCK)
            return
    except ImportError:
        pass
    fh.flush()  # GzipFile: Z_SYNC_FLUSH


class RawFrameWriter:
    def __init__(
        self,
        root: str | Path,
        *,
        queue_bytes: int = QUEUE_BYTES,
        flush_secs: float = FLUSH_SECS,
        codec: tuple[str, Any] | None = None,
        retain_days: int | None = RETAIN_DAYS,
    ) -> None:
        self.root = Path(root)
        self.ext, self._open = codec or _codec()
        self.queue_bytes = queue_bytes
        self.flush_secs = flush_secs
        self.retain_days = retain_days  # None keeps everything
        self.pruned = 0  # day directories removed
        self._q: collections.deque[tuple[str, str, datetime, str]] = collections.deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._queued = 0  # bytes of frame text currently queued
        self._thread: threading.Thread | None = None
        # (channel) -> (hour key, handle). One open hour per channel.
        self._files: dict[str, tuple[str, Any]] = {}
        self.written = 0
        self.dropped = 0
        self.errors = 0
        self.last_error = ""

    # -- producer side (event loop) ------------------------------------------

    def put(self, channel: str, conn: str, recv_ts: datetime, raw: str | bytes) -> bool:
        """Queue one frame; False (and counted) when the budget is full."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        n = len(raw)
        with self._lock:
            if self._queued + n > self.queue_bytes:
                self.dropped += 1
                return False
            self._queued += n
            self._q.append((channel, conn, recv_ts, raw))
        self._wake.set()
        return True

    def stats(self) -> dict[str, int]:
        return {
            "raw_written": self.written,
            "raw_dropped": self.dropped,
            "raw_errors": self.errors,
            "raw_queued_bytes": self._queued,
            "raw_pruned_days": self.pruned,
        }

    # -- retention -----------------------------------------------------------

    _DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    def prune(self, now: datetime | None = None) -> list[str]:
        """Remove `<root>/<YYYY-MM-DD>` directories dated more than
        `retain_days` before `now` (UTC). Anything else under the root is
        left alone. Returns the names removed; errors are counted, never
        raised -- this runs on the writer thread."""
        if self.retain_days is None or not self.root.is_dir():
            return []
        cutoff = ((now or datetime.now(UTC)) - timedelta(days=self.retain_days)).strftime(
            "%Y-%m-%d"
        )
        gone = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and self._DAY.match(d.name) and d.name < cutoff:
                try:
                    shutil.rmtree(d)
                except OSError as exc:
                    self.errors += 1
                    self.last_error = f"prune: {type(exc).__name__}: {exc}"[:200]
                    continue
                gone.append(d.name)
        self.pruned += len(gone)
        return gone

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="rawframes", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 10.0) -> bool:
        """Drain what is queued, close every file; True if it finished in
        `timeout`. Bounded, because it runs on the daemon's stop path, which
        systemd SIGKILLs `TimeoutStopSec` after SIGTERM."""
        self._stop.set()
        self._wake.set()
        if self._thread is None:
            self._drain()
            self._close_all()
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # -- writer thread -------------------------------------------------------

    def _run(self) -> None:
        last_flush = time.monotonic()
        self.prune()
        last_prune = last_flush
        while True:
            self._wake.wait(self.flush_secs)
            self._wake.clear()
            self._drain()
            now = time.monotonic()
            if now - last_flush >= self.flush_secs:
                self._flush_all()
                last_flush = now
            if now - last_prune >= PRUNE_SECS:
                self.prune()
                last_prune = now
            if self._stop.is_set():
                self._drain()
                self._close_all()
                return

    def _take(self, limit: int = 10_000) -> list[tuple[str, str, datetime, str]]:
        out = []
        with self._lock:
            while self._q and len(out) < limit:
                item = self._q.popleft()
                self._queued -= len(item[3])
                out.append(item)
        return out

    def _drain(self) -> None:
        while batch := self._take():
            lines: dict[tuple[str, str, str], list[bytes]] = {}
            for channel, conn, recv_ts, raw in batch:
                day, hour = recv_ts.strftime("%Y-%m-%d"), recv_ts.strftime("%H")
                text = raw.replace("\n", " ") if "\n" in raw else raw
                lines.setdefault((channel, day, hour), []).append(
                    f"{recv_ts.isoformat()}\t{conn}\t{text}\n".encode()
                )
            for (channel, day, hour), chunk in lines.items():
                try:
                    fh = self._handle(channel, day, hour)
                    fh.write(b"".join(chunk))
                    self.written += len(chunk)
                except Exception as exc:  # noqa: BLE001 -- best-effort copy, see module doc
                    self.errors += len(chunk)
                    self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                    self._drop_handle(channel)

    def _handle(self, channel: str, day: str, hour: str) -> Any:
        key = f"{day}/{hour}"
        cur = self._files.get(channel)
        if cur is not None and cur[0] == key:
            return cur[1]
        if cur is not None:
            self._drop_handle(channel)
        d = self.root / day
        d.mkdir(parents=True, exist_ok=True)
        fh = self._open(d / f"{hour}-{channel}.jsonl.{self.ext}")
        self._files[channel] = (key, fh)
        return fh

    def _drop_handle(self, channel: str) -> None:
        cur = self._files.pop(channel, None)
        if cur is not None:
            try:
                cur[1].close()
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"close: {type(exc).__name__}: {exc}"[:200]

    def _flush_all(self) -> None:
        for channel, (_, fh) in list(self._files.items()):
            try:
                _flush_block(fh)
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"flush: {type(exc).__name__}: {exc}"[:200]
                self._drop_handle(channel)

    def _close_all(self) -> None:
        for channel in list(self._files):
            self._drop_handle(channel)
