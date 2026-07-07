"""Stream daemon (B7): live WS capture from both venues into the stream
archive. This data is unrecoverable — neither venue serves historical
books or full prints — so the daemon's one job is: never lose what it saw,
and mark honestly what it missed (stream_gaps).

    python -m hyxlab.streamd [--db data/hyxstream.duckdb]
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
seq jump, and daemon downtime (mark_startup_gap). Replay treats books as
unknown inside gaps until the next snapshot.

Writes go to the stream store's own DuckDB (see streamstore.py for why
not the main archive), flushed every FLUSH_SECS.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import websockets

from hyxlab.streamstore import StreamStore
from hyxlab.venues import kalshi, kalshi_ws, polymarket_ws

FLUSH_SECS = 15.0
STATS_SECS = 300.0
TICKER_REFRESH_SECS = 3600.0
POLY_PING_SECS = 10.0
BACKOFF_MAX = 60.0


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


def open_tickers(series_list: list[str]) -> set[str]:
    """Open market tickers for the watchlist series (REST, paced fine:
    one call per series per hour)."""
    out: set[str] = set()
    for s in series_list:
        try:
            out.update(m["ticker"] for m in kalshi.get_markets(series_ticker=s, status="open"))
        except Exception as exc:  # one bad series must not sink the refresh
            _log(f"kalshi-books: ticker refresh failed for {s}: {exc}")
    return out


class Daemon:
    def __init__(self, store: StreamStore, watchlist: dict) -> None:
        self.store = store
        self.watchlist = watchlist
        self.key_id = os.environ.get("KALSHI_API_KEY_ID", "")
        pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        self.pem = Path(pem_path).read_bytes() if pem_path and Path(pem_path).exists() else b""
        self.stats: dict[str, int] = {}

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
        tickers = await asyncio.to_thread(open_tickers, series)
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
                    next_refresh = asyncio.get_event_loop().time() + TICKER_REFRESH_SECS
                    while True:
                        timeout = (
                            max(1.0, next_refresh - asyncio.get_event_loop().time())
                            if refresh
                            else None
                        )
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except TimeoutError:
                            next_refresh = asyncio.get_event_loop().time() + TICKER_REFRESH_SECS
                            if await refresh():
                                break  # reconnect with the new ticker set
                            continue
                        recv_ts = datetime.now(UTC)
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

    async def poly_books(self) -> None:
        pairs = self.watchlist.get("polymarket_pairs", [])
        assets = [tok for pair in pairs for tok in pair[1:3]]
        if not assets:
            _log("poly-books: no polymarket_pairs in watchlist; task idle (pairs land with B3.5)")
            return
        backoff, last_recv, first = 1.0, None, True
        while True:
            try:
                async with websockets.connect(polymarket_ws.WS_URL, max_size=2**23) as ws:
                    await ws.send(polymarket_ws.subscribe_market(assets))
                    if not first:
                        self._gap("polymarket", "market", last_recv, "reconnect")
                    first, backoff = False, 1.0
                    _log(f"poly-books: connected ({len(assets)} tokens)")
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=POLY_PING_SECS)
                        except TimeoutError:
                            await ws.send("PING")  # idle keepalive
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
                _log(f"flush FAILED ({type(exc).__name__}: {exc}); buffer held for retry")
                continue
            now = asyncio.get_event_loop().time()
            if now - last_stats >= STATS_SECS:
                _log(f"stats {self.stats} (flushed {n} this round)")
                last_stats = now

    async def run(self, duration: float | None = None) -> None:
        self.store.mark_startup_gap()
        tasks = [
            asyncio.create_task(self.kalshi_trades(), name="kalshi-trades"),
            asyncio.create_task(self.kalshi_books(), name="kalshi-books"),
            asyncio.create_task(self.poly_books(), name="poly-books"),
            asyncio.create_task(self.flusher(), name="flusher"),
        ]
        try:
            if duration:
                await asyncio.sleep(duration)
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.store.flush()  # final drain — never lose buffered events
            _log(f"shutdown; stats {self.stats}")


def main() -> None:
    ap = argparse.ArgumentParser(description="hyxlab live stream daemon")
    ap.add_argument("--db", default="data/hyxstream.duckdb")
    ap.add_argument("--watchlist", default=None)
    ap.add_argument("--smoke", type=float, default=None, help="run N seconds, then exit")
    args = ap.parse_args()

    load_env()
    from hyxlab.collect import DEFAULT_WATCHLIST, load_watchlist

    watchlist = load_watchlist(args.watchlist or str(DEFAULT_WATCHLIST))
    store = StreamStore(args.db)
    daemon = Daemon(store, watchlist)
    _log(f"starting (db={args.db}, smoke={args.smoke})")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(daemon.run(duration=args.smoke))
    print(f"[streamd] final counts: {store.counts()}", flush=True)


if __name__ == "__main__":
    main()
