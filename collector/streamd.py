"""Stream daemon (B7): live WS capture from both venues into the stream
archive. This data is unrecoverable — neither venue serves historical
books or full prints — so the daemon's one job is: never lose what it saw,
and mark honestly what it missed (stream_gaps).

    python -m collector.streamd [--db data/hyxstream.duckdb]
                             [--watchlist hyxlab/watchlist.json]
                             [--smoke SECONDS]   # bounded run, then exit

Connections (each an independent task; one failing never stops others):
- kalshi-trades: exchange-wide trade firehose (~105 ev/s observed).
- kalshi-books: orderbook_delta for open markets of watchlist series;
  the open set rolls daily, so it re-resolves hourly and reconnects when
  it changes (reconnect ⇒ fresh snapshots re-seed every book).
- poly-books: market channel for watchlist polymarket_pairs tokens;
  skipped while the pair list is empty (pairs land with B3.5).

Gap discipline: a gap row is written for every reconnect, every Kalshi
seq jump, daemon downtime (mark_startup_gap), and — bounded to the first
frame actually received — every Kalshi ws sequence reset ('seq_reset':
each (re)connect starts a fresh sid/seq, and a process restart loses the
in-process last_recv, so the row starts from the last PERSISTED recv_ts;
S-188 loss class 3). Replay treats books as unknown inside gaps until
the next snapshot.

Writes go to the stream store's own DuckDB (see streamstore.py for why
not the main archive), flushed every FLUSH_SECS.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import time as _time
from datetime import UTC, datetime
from pathlib import Path

import websockets

from collector.venues import kalshi, kalshi_ws, polymarket_ws
from hyxlab.lockid import db_owner_lock_or_reason
from hyxlab.streamstore import StreamStore

FLUSH_SECS = 15.0
STATS_SECS = 300.0
TICKER_REFRESH_SECS = 3600.0
POLY_PING_SECS = 10.0
POLY_TOP_MARKETS = 50  # busiest books streamed even without verified pairs
BACKOFF_MAX = 60.0
# Reconnect a Kalshi channel that goes silent this long. Both channels
# chatter continuously (trades ~105 ev/s exchange-wide; books across
# ~500 open tickers), so real silence means a half-dead connection:
# WS pings keep the TCP session alive while the subscription is gone,
# and recv() otherwise blocks forever — the trades channel came back
# from a 07:00Z reconnect unsubscribed and archived zero trades for
# 75+ minutes with no error (2026-08-13).
DEAD_AIR_SECS = 300.0
# Space the per-series ticker-refresh calls apart (see open_tickers).
SERIES_PAUSE_S = float(os.environ.get("HYXLAB_SERIES_PAUSE_S", "0.35"))
# Retry waits when a book task's subscription set comes back EMPTY (venue
# REST down at boot); the last rung repeats until the set is non-empty.
# Subscribing empty captures nothing — and the hourly in-loop refresh may
# never fire if the venue drops the empty subscription — so neither book
# task ever subscribes with an empty set.
EMPTY_SET_RETRY_LADDER = (10, 30, 60, 120)

# Flush-stall ledger. A failed flush is already journalled, but the journal
# is the WRONG instrument for the question "how long does the backlog get":
# it records only the FAILURES, never the success that ends an episode, so a
# duration read off it is an interpolation between the first and last failure
# line; it rolls at the host's retention (7d here); and it is per-boot-session
# text nothing downstream can aggregate. Measured over the 7 days to
# 2026-09-11 -- 101 episodes, 76 of them a single 15s flush, three ~30 min,
# and TWO of those three reached SPILL_CAP (34,691 and 286 rows moved to the
# sidecar) -- so the tail this ledger exists to bound is real and recurring,
# and the cause is a long-lived READ-ONLY reader: a duckdb read-only handle
# takes a shared lock the writer cannot upgrade past (the 09-09 and 09-10
# episodes name `simulator.shadow` as the holder).
STALL_LOG = "data/stream_stalls.jsonl"
# A stall that outlives the daemon (OOM, restart, host crash) must still be
# on disk, so an episode past this age writes an interim `open` record and
# refreshes it at this cadence. Short episodes -- the 75% that are one failed
# flush -- write exactly one `closed` record and nothing else, so the quiet
# case stays quiet. 300s is well inside the ~30 min tail above and 20x
# FLUSH_SECS, i.e. it cannot fire for the common case.
STALL_HEARTBEAT_S = 300.0


def load_env(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, no quoting; existing
    environment wins (systemd EnvironmentFile= takes this same file)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _log(msg: str) -> None:
    print(f"[streamd] {datetime.now(UTC):%H:%M:%S} {msg}", flush=True)


async def _fetch_until_nonempty(fetch, channel: str, what: str):
    """Run `fetch` in a thread, retrying on EMPTY_SET_RETRY_LADDER (last
    rung repeats forever) until the result is non-empty."""
    items = await asyncio.to_thread(fetch)
    rung = 0
    while not items:
        wait = EMPTY_SET_RETRY_LADDER[min(rung, len(EMPTY_SET_RETRY_LADDER) - 1)]
        _log(f"{channel}: empty {what}; retrying in {wait}s")
        await asyncio.sleep(wait)
        items = await asyncio.to_thread(fetch)
        rung += 1
    return items


def open_tickers(series_list: list[str], pause_s: float = SERIES_PAUSE_S) -> set[str]:
    """Open market tickers for the watchlist series (REST, one call per
    series per hour).

    `pause_s` spaces the calls APART. get_markets' own `pause_s` paces
    between PAGES within one series, which does nothing for a burst of
    N single-page series calls — and the watchlist is now 31 series, so
    the burst is what matters. Measured 2026-08-02 at 23 series: the
    refresh drew 429s from /markets (KXPAYROLLS, KXU3) even though each
    series is cheap on its own. kalshi._get_with_429_retry now recovers
    from those, but recovering from a burst we chose to send is worse
    than not sending it: retries land in the same window and the whole
    refresh stretches out anyway."""
    out: set[str] = set()
    for i, s in enumerate(series_list):
        if i and pause_s:
            _time.sleep(pause_s)
        try:
            out.update(m["ticker"] for m in kalshi.get_markets(series_ticker=s, status="open"))
        except Exception as exc:  # one bad series must not sink the refresh
            _log(f"kalshi-books: ticker refresh failed for {s}: {exc}")
    return out


class FlushStalls:
    """Append-only ledger of flush-stall EPISODES (see STALL_LOG).

    One episode = the span from the first failed flush to the next successful
    one. The daemon is the only process that can see both ends, which is the
    whole reason this is written here and not derived by a reader from the
    journal.

    Two record shapes, and the difference is load-bearing:

      closed  the episode ENDED at `at`; `duration_s` is exact.
      open    the episode had lasted `duration_s` as of `at` and the daemon
              had not yet seen it end. It is a LOWER BOUND, never a claim
              that the stall is still running now -- the daemon may have been
              killed a second later, and a reader that treats `open` as
              "ongoing" would report a stall that ended weeks ago as live.

    An episode that ends normally therefore writes `closed` whether or not it
    also wrote heartbeats, and a reader keyed on `started` takes the longest
    record it has for that episode.

    Writes are best-effort: an unwritable ledger must never take down the
    daemon whose one job is not losing what it saw.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        # Resolved at WRITE time, never bound here: STALL_LOG is relative to
        # the working directory and the suite patches it (the same default-arg
        # trap `collector.collect.acquire_writer_lock` documents).
        self._path = path
        self.started: datetime | None = None
        self.fails = 0
        self.peak_pending = 0
        self.spilled = 0
        # The last failure's message, carried onto the CLOSED record too: an
        # episode that ended is the one an operator reads after the fact, and
        # "30 minutes" without "who held the lock" is half the finding.
        self.last_error = ""
        self._last_written: datetime | None = None

    def _write(self, rec: dict) -> None:
        path = Path(self._path or STALL_LOG)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as exc:
            _log(f"stall ledger unwritable ({type(exc).__name__}: {exc})")

    def _record(self, state: str, now: datetime, **extra) -> None:
        assert self.started is not None
        self._write(
            {
                "at": now.isoformat(),
                "state": state,
                "started": self.started.isoformat(),
                "duration_s": round((now - self.started).total_seconds(), 1),
                "fails": self.fails,
                "peak_pending": self.peak_pending,
                "spilled": self.spilled,
                "error": self.last_error,
                **extra,
            }
        )
        self._last_written = now

    def arm(self, now: datetime) -> None:
        """Announce that the producer is alive, from now.

        WHY A LEDGER NEEDS AN EPOCH. The reader decides an EMPTY ledger by
        asking the journal whether any flush failed; without an epoch, every
        failure the journal remembers from BEFORE this code was deployed --
        or from before the daemon was restarted onto it -- counts as evidence
        that the producer is dead. The first QA run after any deployment
        would read INERT on a perfectly healthy daemon, which is the alarm a
        producer-liveness check exists to make believable.

        Written on every start and never rotated away by the reader: a daemon
        up for 30 days must still be able to say when it armed, so this record
        is looked up OUTSIDE the reader's window.
        """
        self._write({"at": now.isoformat(), "state": "armed"})

    def observe(self, pending: int, spilled: int, exc: BaseException, now: datetime) -> None:
        """Account one failed flush against the episode, writing nothing.

        Split out of `failed` because the shutdown path needs the accounting
        WITHOUT the heartbeat: `interrupted` writes its own record at the
        same instant, and two records for one episode at the same duration
        leave the reader (which keeps the LONGEST per `started`) picking
        between them on a tie -- where the one it must not pick is the one
        without the handoff count.
        """
        if self.started is None:
            self.started = now
            self.fails = 0
            self._last_written = None
        self.fails += 1
        self.last_error = _short_err(exc)
        self.peak_pending = max(self.peak_pending, pending)
        self.spilled = max(self.spilled, spilled)

    def failed(self, pending: int, spilled: int, exc: BaseException, now: datetime) -> None:
        """One failed flush. Opens an episode if none is open."""
        self.observe(pending, spilled, exc, now)
        assert self.started is not None
        since = self._last_written or self.started
        if (now - self.started).total_seconds() >= STALL_HEARTBEAT_S and (
            now - since
        ).total_seconds() >= STALL_HEARTBEAT_S:
            self._record("open", now)

    def interrupted(self, now: datetime, handoff: int = 0) -> None:
        """Record an episode this process will not see the end of.

        Written `open` and never `closed`: the drain that would have ended
        the stall is the thing that just failed, so the stall outlived the
        daemon and `duration_s` is a lower bound. Forced regardless of
        `STALL_HEARTBEAT_S` — shutdown is the last chance this episode has
        to be written down at all, and a 20 s stall that ate a restart is
        exactly the one no heartbeat would ever have reached.

        `handoff` is the slice of `spilled` that went to the sidecar at
        SHUTDOWN rather than at SPILL_CAP. The reader needs the two apart: a
        cap spill says a stall ran an hour and the daemon is at its memory
        bound, a condition to fix; a handoff says a restart landed during a
        stall and the sidecar caught what the process would otherwise have
        dropped, which is the design working. Failing on the second would
        raise the alarm exactly when the safety net held. Carried as a count
        beside `spilled`, not as a separate state, so `spilled` stays the
        total that left memory.
        """
        if self.started is None:
            return
        self._record("open", now, interrupted=True, handoff=handoff)

    def ok(self, now: datetime) -> None:
        """A flush succeeded. Closes an open episode; a no-op otherwise."""
        if self.started is None:
            return
        self._record("closed", now)
        self.started = None
        self.peak_pending = 0
        self.spilled = 0
        self.last_error = ""


def _short_err(exc: BaseException) -> str:
    """First line of the exception, bounded. DuckDB's lock error is a
    paragraph with a URL in it; the ledger needs the class and the holder,
    not the manual."""
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {first}"[:200]


class Daemon:
    def __init__(self, store: StreamStore, watchlist: dict) -> None:
        self.store = store
        self.watchlist = watchlist
        self.key_id = os.environ.get("KALSHI_API_KEY_ID", "")
        pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        self.pem = Path(pem_path).read_bytes() if pem_path and Path(pem_path).exists() else b""
        self.stats: dict[str, int] = {}
        self._spill_corrupt_seen = 0
        self.stalls = FlushStalls()

    def _count(self, key: str, n: int) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def _gap(self, venue: str, channel: str, since: datetime | None, reason: str) -> None:
        now = datetime.now(UTC)
        self.store.append_gap(venue, channel, since or now, now, reason)
        _log(f"{venue}/{channel} GAP ({reason})")

    def _clock_check(self, venue: str, channel: str, recv_ts: datetime, last: datetime | None):
        """recv_ts moving backwards = the system clock stepped (e.g. NTP
        kicking in on a skewed box). Not lost coverage, but timestamps
        around the step are non-monotonic — record it so replay knows."""
        if last is not None and recv_ts < last:
            self._gap(
                venue, channel, recv_ts, f"clock_step_{(recv_ts - last).total_seconds():.1f}s"
            )

    # -- connection loops --------------------------------------------------

    async def kalshi_trades(self) -> None:
        await self._kalshi_loop("trades", lambda: kalshi_ws.subscribe_trades(), None)

    async def kalshi_books(self) -> None:
        series = self.watchlist.get("kalshi_series", [])
        if not series:
            _log("kalshi-books: no series in watchlist; task idle")
            return
        # An empty INITIAL set (REST down at boot) would leave books dark
        # until the hourly refresh — keep retrying until tickers exist.
        tickers = await _fetch_until_nonempty(
            lambda: open_tickers(series), "kalshi-books", "initial ticker set"
        )
        _log(f"kalshi-books: {len(tickers)} open tickers across {len(series)} series")

        async def refresh() -> bool:
            nonlocal tickers
            new = await asyncio.to_thread(open_tickers, series)
            if new and new != tickers:
                _log(f"kalshi-books: open set changed {len(tickers)} -> {len(new)}; reconnecting")
                tickers = new
                return True
            return False

        await self._kalshi_loop(
            "books", lambda: kalshi_ws.subscribe_books(sorted(tickers)), refresh
        )

    async def _kalshi_loop(self, channel: str, make_subscribe, refresh) -> None:
        """Shared connect/read/reconnect loop for the two Kalshi channels."""
        if not (self.key_id and self.pem):
            _log(f"kalshi-{channel}: missing KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH; idle")
            return
        backoff, last_recv, first = 1.0, None, True
        # Bound for the first connection's seq_reset row: in-process
        # `last_recv` does not survive a restart (S-188 class 3), so read
        # the channel's last PERSISTED recv_ts once, up front — never in
        # the frame path, where a transient reader lock would cost the
        # frame in hand and force a reconnect. Best-effort: a failed read
        # only shortens the startup row, and the '*' daemon_start row
        # still marks the downtime coarsely.
        try:
            resume_since = await asyncio.to_thread(self.store.last_recv_ts, "kalshi", channel)
        except Exception as exc:
            _log(f"kalshi-{channel}: last_recv_ts unavailable ({type(exc).__name__}: {exc})")
            resume_since = None
        while True:
            try:
                headers = kalshi_ws.auth_headers(self.key_id, self.pem)
                async with websockets.connect(
                    kalshi_ws.WS_URL, additional_headers=headers, max_size=2**23
                ) as ws:
                    await ws.send(make_subscribe())
                    if not first:
                        self._gap("kalshi", channel, last_recv, "reconnect")
                    first, backoff = False, 1.0
                    _log(f"kalshi-{channel}: connected")
                    tracker = kalshi_ws.SeqTracker()
                    seeded = False  # first frame of THIS connection not yet seen
                    mono = asyncio.get_event_loop().time
                    next_refresh = mono() + TICKER_REFRESH_SECS
                    last_frame = mono()
                    while True:
                        deadline = last_frame + DEAD_AIR_SECS
                        if refresh:
                            deadline = min(deadline, next_refresh)
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=max(1.0, deadline - mono())
                            )
                        except TimeoutError:
                            if refresh and mono() >= next_refresh:
                                next_refresh = mono() + TICKER_REFRESH_SECS
                                if await refresh():
                                    break  # reconnect with the new ticker set
                            if mono() - last_frame > DEAD_AIR_SECS:
                                self._gap("kalshi", channel, last_recv, "dead_air")
                                raise ConnectionError(
                                    f"no frames for {DEAD_AIR_SECS:.0f}s (dead air)"
                                ) from None
                            continue
                        last_frame = mono()
                        recv_ts = datetime.now(UTC)
                        if not seeded:
                            # Every (re)connect resets the ws sequence, so
                            # anything between the last frame we archived and
                            # this connection's FIRST frame is unrecoverable.
                            # The reconnect row above ends at connect time and
                            # a process restart loses `last_recv` entirely
                            # (S-188 class 3) — bound the hole honestly:
                            # [last in-process recv, or last PERSISTED recv on
                            # a fresh process, -> first post-reset frame].
                            seeded = True
                            since = last_recv or resume_since
                            if since is not None:
                                self.store.append_gap(
                                    "kalshi", channel, since, recv_ts, "seq_reset"
                                )
                                _log(f"kalshi/{channel} GAP (seq_reset)")
                        self._clock_check("kalshi", channel, recv_ts, last_recv)
                        frame = json.loads(raw)
                        # Continuity check on the raw frame (sid/seq are
                        # frame-level); a jump means missed messages, so
                        # log the gap and reconnect to re-seed books.
                        if tracker.observe(frame.get("sid"), frame.get("seq")):
                            self._gap("kalshi", channel, last_recv, "seq_gap")
                            raise ConnectionError("seq gap; reconnecting to re-seed")
                        events, trades = kalshi_ws.parse_message(frame, recv_ts)
                        if events:
                            self.store.append_events(events)
                            self._count(f"kalshi_{channel}_events", len(events))
                        if trades:
                            self.store.append_trades(trades)
                            self._count("kalshi_trades", len(trades))
                        last_recv = recv_ts
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log(f"kalshi-{channel}: {type(exc).__name__}: {exc}; retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    def _poly_token_set(self) -> list[str]:
        """Watchlist pair tokens + top-volume markets' tokens (data-first:
        stream the busiest books even before any pair is hand-verified)."""
        pairs = self.watchlist.get("polymarket_pairs", [])
        assets = {tok for pair in pairs for tok in pair[1:3]}
        try:
            from collector.venues import polymarket as poly

            top = poly.iter_markets_by_volume(
                0.0, max_pages=1, want_top_n=True
            )  # one page, vol-desc: truncation IS the request here
            for m in top[:POLY_TOP_MARKETS]:
                tp = poly.token_pair(m)
                if tp:
                    assets.update(tp)
        except Exception as exc:
            _log(f"poly-books: top-volume refresh failed: {exc}")
        return sorted(assets)

    async def poly_books(self) -> None:
        # An empty initial set (watchlist empty AND Gamma unreachable at
        # boot) must not idle the task forever — retry until tokens exist.
        assets = await _fetch_until_nonempty(
            self._poly_token_set, "poly-books", "token set (watchlist empty, Gamma unreachable)"
        )
        _log(f"poly-books: {len(assets)} tokens (top {POLY_TOP_MARKETS} by volume + pairs)")
        backoff, last_recv, first = 1.0, None, True
        while True:
            try:
                async with websockets.connect(polymarket_ws.WS_URL, max_size=2**23) as ws:
                    await ws.send(polymarket_ws.subscribe_market(assets))
                    if not first:
                        self._gap("polymarket", "market", last_recv, "reconnect")
                    first, backoff = False, 1.0
                    _log(f"poly-books: connected ({len(assets)} tokens)")
                    next_refresh = asyncio.get_event_loop().time() + TICKER_REFRESH_SECS
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=POLY_PING_SECS)
                        except TimeoutError:
                            await ws.send("PING")  # idle keepalive
                            if asyncio.get_event_loop().time() >= next_refresh:
                                next_refresh = asyncio.get_event_loop().time() + TICKER_REFRESH_SECS
                                new = await asyncio.to_thread(self._poly_token_set)
                                if new and new != assets:
                                    _log(
                                        f"poly-books: token set {len(assets)} -> {len(new)};"
                                        " reconnecting"
                                    )
                                    assets = new
                                    break  # reconnect re-seeds via fresh books
                            continue
                        recv_ts = datetime.now(UTC)
                        self._clock_check("polymarket", "market", recv_ts, last_recv)
                        events, trades = polymarket_ws.parse_message(raw, recv_ts)
                        if events:
                            self.store.append_events(events)
                            self._count("poly_events", len(events))
                        if trades:
                            self.store.append_trades(trades)
                            self._count("poly_trades", len(trades))
                        last_recv = recv_ts
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log(f"poly-books: {type(exc).__name__}: {exc}; retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    # -- persistence -------------------------------------------------------

    async def flusher(self) -> None:
        last_stats = asyncio.get_event_loop().time()
        while True:
            await asyncio.sleep(FLUSH_SECS)
            try:
                n = await asyncio.to_thread(self.store.flush)
            except Exception as exc:
                # pending size makes a wedged-reader buildup visible in
                # the journal long before it could OOM the daemon. Past
                # SPILL_CAP pending plateaus, so show sidecar growth too.
                n_pending = self.store.pending
                n_spilled = self.store.spilled
                sev = "CRITICAL " if n_pending > self.store.PENDING_ALARM else ""
                spill = f" (+{n_spilled} spilled to sidecar)" if n_spilled else ""
                _log(
                    f"{sev}flush FAILED ({type(exc).__name__}: {exc});"
                    f" {n_pending} rows held for retry{spill}"
                )
                # The journal line above says a flush failed; the ledger says
                # how long the episode ran and how big it got. Written from
                # here because this is the only scope that sees BOTH ends.
                self.stalls.failed(n_pending, n_spilled, exc, datetime.now(UTC))
                continue
            self.stalls.ok(datetime.now(UTC))
            # A drain that skipped sidecar records is a real archive hole
            # (torn append from a host crash). The store no longer stalls on
            # it, so the journal is the only place it can surface (EXP-936).
            if self.store.spill_corrupt != self._spill_corrupt_seen:
                lost = self.store.spill_corrupt - self._spill_corrupt_seen
                self._spill_corrupt_seen = self.store.spill_corrupt
                _log(
                    f"CRITICAL sidecar drain skipped {lost} unreadable row(s)"
                    f" — archive hole (total {self.store.spill_corrupt})"
                )
            now = asyncio.get_event_loop().time()
            if now - last_stats >= STATS_SECS:
                _log(f"stats {self.stats} (flushed {n} this round)")
                last_stats = now

    def _install_stop(self) -> asyncio.Task | None:
        """Make SIGTERM reach the shutdown drain instead of killing the
        process on the spot.

        systemd stops this unit with SIGTERM, and Python's default
        disposition for SIGTERM is the OS one: terminate immediately, no
        `finally`, no drain. MEASURED 2026-09-12 on the live box — 4
        `starting (db=` lines in 14 days of `hyxlab-stream` journal and
        ZERO `shutdown; stats` lines. The final drain that exists so the
        daemon never loses what it saw had not run in production once; it
        was reachable only from Ctrl-C and from `--smoke`. Every
        `systemctl restart` (promote.sh does one whenever streamd changes)
        dropped the buffer instead — a flush interval normally, up to
        SPILL_CAP rows if a stall was in progress, which is the restart a
        wedged archive makes most likely.

        Returns None where the loop cannot take signal handlers (Windows,
        a non-main thread, an embedded loop in the suite): the daemon then
        behaves exactly as before rather than refusing to start.
        """
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        self._stop_signals = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            self._stop_signals.append(sig)
        if not self._stop_signals:
            return None
        return asyncio.create_task(stop.wait(), name="stop")

    def _release_stop(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in getattr(self, "_stop_signals", []):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)
        self._stop_signals = []

    def _final_drain(self) -> None:
        """The last write of this process's life, and the one that must not
        raise.

        Two archive holes live here, and both only open on the path where
        the archive is unreachable — which at shutdown is the LIKELY path,
        because a wedged archive is the usual reason a restart is happening.
        (a) `store.flush()` raising means every buffered row dies with the
        process; the sidecar exists exactly for that, so spill EVERYTHING
        (not just the overflow above SPILL_CAP, which a sub-hour stall never
        reaches) and let the next boot drain it. (b) The open stall episode
        would never be written: `ok()` is unreachable when the drain that
        would have ended the stall is the thing that failed, so the episode
        the restart interrupted is recorded as `open` — a lower bound.

        Nothing here propagates: this runs inside `run()`'s `finally`, where
        a raise would mask the exception that ended the daemon and skip the
        shutdown line.
        """
        now = datetime.now(UTC)
        try:
            self.store.flush()
        except Exception as exc:
            held = self.store.pending
            try:
                moved = self.store.spill_all()
            except Exception as spill_exc:
                moved = 0
                _log(
                    f"CRITICAL final drain failed ({_short_err(exc)}) and the sidecar "
                    f"refused it ({_short_err(spill_exc)}) — {held} row(s) lost"
                )
            else:
                _log(
                    f"final drain FAILED ({_short_err(exc)}); {moved} row(s) moved to the "
                    f"sidecar for the next boot to drain"
                )
            self.stalls.observe(held, self.store.spilled, exc, now)
            self.stalls.interrupted(now, handoff=moved)
        else:
            self.stalls.ok(now)

    async def run(self, duration: float | None = None) -> None:
        self.store.mark_startup_gap()
        self.stalls.arm(datetime.now(UTC))
        tasks = [
            asyncio.create_task(self.kalshi_trades(), name="kalshi-trades"),
            asyncio.create_task(self.kalshi_books(), name="kalshi-books"),
            asyncio.create_task(self.poly_books(), name="poly-books"),
            asyncio.create_task(self.flusher(), name="flusher"),
        ]
        stop = self._install_stop()
        running = asyncio.gather(*tasks)
        try:
            if duration:
                await asyncio.sleep(duration)
            elif stop is None:
                await running
            else:
                done, _ = await asyncio.wait({running, stop}, return_when=asyncio.FIRST_COMPLETED)
                # A task that died still has to surface with its traceback;
                # only the signal arm is a normal end.
                if running in done:
                    running.result()
        finally:
            self._release_stop()
            if stop is not None:
                stop.cancel()
            running.cancel()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # `running` gathers the same tasks, so its outcome is already
            # accounted for -- but an unretrieved cancelled gather prints a
            # bare traceback over the shutdown line at interpreter exit, and
            # the shutdown line is the one an operator reads. Consumed, never
            # awaited: this is a shutdown path and must not be able to block.
            if running.done():
                with contextlib.suppress(asyncio.CancelledError):
                    running.exception()
            self._final_drain()
            _log(f"shutdown; stats {self.stats}")


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab live stream daemon")
    ap.add_argument("--db", default="data/hyxstream.duckdb")
    ap.add_argument("--watchlist", default=None)
    ap.add_argument("--smoke", type=float, default=None, help="run N seconds, then exit")
    args = ap.parse_args()

    # Own the stream archive before opening it. DuckDB's file lock is held
    # only for the duration of each flush, so between flushes it excludes
    # nothing: two daemons on one file interleave duplicate book_events and
    # stream_trades into tables that have no key and never dedupe, and a
    # duplicated DELTA corrupts every book replay downstream of it. The
    # data is unrecoverable — neither venue serves historical books — so
    # the second copy must never start.
    lock, why = db_owner_lock_or_reason(args.db)
    if lock is None:
        print(f"[streamd] {why}; not starting", flush=True)
        raise SystemExit(75)

    load_env()
    from hyxlab.watchlist import DEFAULT_WATCHLIST, load_watchlist

    watchlist = load_watchlist(args.watchlist or str(DEFAULT_WATCHLIST))
    store = StreamStore(args.db)
    daemon = Daemon(store, watchlist)
    _log(f"starting (db={args.db}, smoke={args.smoke})")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(daemon.run(duration=args.smoke))
    print(f"[streamd] final counts: {store.counts()}", flush=True)


if __name__ == "__main__":
    main()
