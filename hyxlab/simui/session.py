"""ReplaySession: one archived event group replayed through the real sim.

The session owns three things and keeps them in lockstep behind a single
time cursor:
- a persistent BookReplayer (full-depth ladders for display),
- a Simulator with the latency model (all fills — user clicks and
  strategy decisions alike — go through sim.step()),
- in-memory slices of book_events / stream_trades / stream_gaps for the
  chosen event, loaded once from the stream archive (read-only).

Honesty rules (see docs/plans/simui/plan.md):
- User orders enter via ManualTrader, a Strategy whose queue is drained
  by the next sim.step() — the decision-time quote is never fillable.
- MarketInfo.result is blanked: captured markets may have settled since,
  and showing the result would leak the answer to the human trading the
  replay. Sessions end at end-of-data, positions marked to last mid.
- seek() re-seeds book state from history WITHOUT stepping the sim
  (shadow's anchor logic) and restarts the portfolio flat — a portfolio
  cannot be carried backwards in time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import duckdb

from hyxlab.bookreplay import BookReplayer, replay_snapshots
from hyxlab.capabilities import LIVE_VENUE_CAPS
from hyxlab.models import Cancel, MarketInfo, Order
from hyxlab.sim import Simulator
from hyxlab.store import Store
from hyxlab.strategy import Context, Strategy
from hyxlab.streamstore import BookEvent, StreamTrade

STREAM_DB = "data/hyxstream.duckdb"
ARCHIVE_DB = "data/hyxlab.duckdb"
DEPTH_LEVELS = 12  # ladder rows per side sent to the UI
_EPS = timedelta(microseconds=1)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


class ManualTrader(Strategy):
    """The human's account. submit() only queues; the next sim.step()
    drains the queue, so manual orders ride the same latency + fill path
    as any strategy's. requires is empty — a human clicking a ladder has
    no structural trigger to vacuously satisfy."""

    name = "you"
    requires: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self._queue: list[Order | Cancel] = []

    def submit(self, cmd: Order | Cancel) -> None:
        self._queue.append(cmd)

    def on_snapshot(self, snap, ctx) -> list:
        cmds, self._queue = self._queue, []
        return cmds


# -- catalog ----------------------------------------------------------------


def _connect_ro(path: str, attempts: int = 5, wait: float = 2.0):
    """The stream daemon / collector may hold the write lock for a burst;
    retry briefly instead of failing the UI."""
    last: duckdb.Error | None = None
    for _ in range(attempts):
        try:
            return duckdb.connect(path, read_only=True)
        except duckdb.Error as e:  # pragma: no cover - timing dependent
            last = e
            time.sleep(wait)
    raise last


def _event_ticker(market_id: str) -> str:
    return market_id.rsplit("-", 1)[0]


def _try_load_markets(archive_db: str) -> dict[tuple[str, str], MarketInfo]:
    """Market metadata (titles, close times, strikes). The archive may be
    writer-locked for minutes; the UI degrades to bare tickers."""
    try:
        store = Store(archive_db, read_only=True)
    except duckdb.Error:
        return {}
    try:
        return store.markets()
    finally:
        store.close()


def list_events(stream_db: str = STREAM_DB, archive_db: str = ARCHIVE_DB) -> list[dict]:
    """Replayable Kalshi event groups in the stream archive, most book
    activity first."""
    with _connect_ro(stream_db) as conn:
        rows = conn.execute(
            "SELECT market_id, count(*), min(recv_ts), max(recv_ts)"
            " FROM book_events WHERE venue = 'kalshi' GROUP BY market_id"
        ).fetchall()
    infos = _try_load_markets(archive_db)
    events: dict[str, dict] = {}
    for market_id, n, t0, t1 in rows:
        ev = events.setdefault(
            _event_ticker(market_id),
            {"event": _event_ticker(market_id), "markets": [], "n_events": 0},
        )
        info = infos.get(("kalshi", market_id))
        ev["markets"].append(
            {
                "market_id": market_id,
                "title": info.title if info else "",
                "floor_strike": info.floor_strike if info else None,
                "n_events": n,
                "t0": _iso(t0),
                "t1": _iso(t1),
            }
        )
        ev["n_events"] += n
    for ev in events.values():
        ev["markets"].sort(key=lambda m: (m["floor_strike"] or 0, m["market_id"]))
        ev["t0"] = min(m["t0"] for m in ev["markets"])
        ev["t1"] = max(m["t1"] for m in ev["markets"])
        ev["title"] = next((m["title"] for m in ev["markets"] if m["title"]), "")
    return sorted(events.values(), key=lambda e: -e["n_events"])


# -- session ----------------------------------------------------------------


def _sanitize(info: MarketInfo) -> MarketInfo:
    return replace(info, result="") if info.result else info


class ReplaySession:
    def __init__(
        self,
        market_ids: list[str],
        events: list[BookEvent],
        trades: list[StreamTrade],
        gaps: list[tuple[datetime, datetime]],
        markets: dict[tuple[str, str], MarketInfo],
        strategies_factory: Callable[[], list[Strategy]] | None = None,
        latency: float = 2.0,
        start_cash: float = 1000.0,
        archive_db: str = ARCHIVE_DB,
        meta_loaded: bool = True,
    ) -> None:
        self.market_ids = market_ids
        self.events = events  # sorted by (recv_ts, seq)
        self.trades = trades  # sorted by recv_ts
        self.gaps = sorted(gaps)
        self.markets = {k: _sanitize(v) for k, v in markets.items()}
        self.archive_db = archive_db
        self.meta_loaded = meta_loaded
        self.strategies_factory = strategies_factory or (lambda: [])
        self.latency = latency
        self.start_cash = start_cash
        self.t_min = events[0].recv_ts if events else None
        ends = [
            t
            for t in (
                events[-1].recv_ts if events else None,
                trades[-1].recv_ts if trades else None,
            )
            if t is not None
        ]
        self.t_max = max(ends) if ends else None
        self.cursor: datetime | None = None
        self._reset(self.t_min)

    def _reset(self, start: datetime | None) -> None:
        """Fresh sim + replayer; seed book state from events before
        `start` (state derivation only — the sim never sees them)."""
        self.manual = ManualTrader()
        self.sim = Simulator(
            self.markets,
            [self.manual, *self.strategies_factory()],
            data_capabilities={"kalshi": LIVE_VENUE_CAPS["kalshi"]},
            latency=self.latency,
        )
        self.replayer = BookReplayer()
        self._ei = self._ti = 0
        self._pending_gaps = list(self.gaps)
        self._n_fills_sent = 0
        self.cursor = start
        if start is None or self.t_min is None or start <= self.t_min:
            self.cursor = self.t_min
            return
        seed_end = self._bisect_events(start)
        seed_gaps = [g for g in self.gaps if g[0] < start]
        for _ in replay_snapshots(self.events[:seed_end], gaps=seed_gaps, replayer=self.replayer):
            pass
        self._ei = seed_end
        self._ti = next(
            (i for i, t in enumerate(self.trades) if t.recv_ts >= start),
            len(self.trades),
        )
        # A gap already applied during seeding was one whose start an event
        # at/after it flowed through; anything later stays pending.
        last_seed_ts = self.events[seed_end - 1].recv_ts if seed_end else None
        self._pending_gaps = [g for g in self.gaps if last_seed_ts is None or g[0] > last_seed_ts]

    def _bisect_events(self, ts: datetime) -> int:
        lo, hi = 0, len(self.events)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.events[mid].recv_ts < ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def seek(self, ts: datetime) -> None:
        """Jump the cursor. Books re-seed from history; the portfolio and
        all strategy state restart FLAT at `ts` (no time travel)."""
        self._reset(_naive(ts))

    def ensure_metadata(self) -> bool:
        """Retry market metadata if the archive was writer-locked at load
        (shadow's pattern: sweeps/backfills hold the lock for minutes).
        Titles, strikes, and close-time expiry arrive when it frees up.
        Returns True the one time metadata lands."""
        if self.meta_loaded:
            return False
        infos = _try_load_markets(self.archive_db)
        if not infos:
            return False
        self.markets = {
            ("kalshi", m): _sanitize(infos.get(("kalshi", m), MarketInfo("kalshi", m)))
            for m in self.market_ids
        }
        self.sim.markets = self.markets
        self.sim.ctx._markets = self.markets  # Context holds its own ref
        self.meta_loaded = True
        return True

    # -- trading ----------------------------------------------------------

    def place_order(
        self,
        market_id: str,
        side: str,
        qty: float,
        limit_price: float | None = None,
        action: str = "open",
        tif: str = "GTC",
    ) -> None:
        if market_id not in self.market_ids:
            raise ValueError(f"market {market_id} not in session")
        if side not in ("yes", "no"):
            raise ValueError(f"bad side {side!r}")
        if action not in ("open", "close"):
            raise ValueError(f"bad action {action!r}")
        if tif not in ("GTC", "IOC"):
            raise ValueError(f"bad tif {tif!r}")
        if not qty > 0:
            raise ValueError("qty must be > 0")
        if limit_price is not None and not 0 < limit_price < 1:
            raise ValueError("limit_price must be in (0, 1)")
        self.manual.submit(Order("kalshi", market_id, side, qty, limit_price, action, tif))

    def cancel_order(self, order_id: int) -> None:
        self.manual.submit(Cancel(order_id))

    # -- clock ------------------------------------------------------------

    def advance(self, to: datetime) -> dict:
        """Feed everything in (cursor, to] through the sim; return a UI
        frame. Gap intervals invalidate books exactly as in backtests;
        a gap with no event after it yet stays pending for later batches."""
        to = min(_naive(to), self.t_max) if self.t_max else _naive(to)
        if self.cursor is not None and to < self.cursor:
            to = self.cursor
        j = self._bisect_events(to + _EPS)
        batch = self.events[self._ei : j]
        for snap in replay_snapshots(batch, gaps=self._due_gaps(to, batch), replayer=self.replayer):
            self.sim.step(snap)
        self._ei = j
        k = self._ti
        while k < len(self.trades) and self.trades[k].recv_ts <= to:
            k += 1
        new_trades = self.trades[self._ti : k]
        self._ti = k
        self.cursor = to
        return self.frame(new_trades)

    def _due_gaps(self, to: datetime, batch: list[BookEvent]) -> list:
        """Gaps whose start is <= `to` and not yet consumed by a batch.
        replay_snapshots only applies a gap when an event at/after its
        start flows through, so gaps stay pending until that happens."""
        due = [g for g in self._pending_gaps if g[0] <= to]
        if batch:
            last_ts = batch[-1].recv_ts
            self._pending_gaps = [
                g for g in self._pending_gaps if not (g[0] <= to and g[0] <= last_ts)
            ]
        return due

    # -- frame assembly ----------------------------------------------------

    def describe(self) -> dict:
        """Static session metadata for the UI (sent once at create)."""
        return {
            "market_ids": self.market_ids,
            "markets": {
                m: {
                    "title": info.title,
                    "floor_strike": info.floor_strike,
                    "cap_strike": info.cap_strike,
                    "strike_type": info.strike_type,
                    "close_time": _iso(info.close_time),
                }
                for (_v, m), info in self.markets.items()
            },
            "t_min": _iso(self.t_min),
            "t_max": _iso(self.t_max),
            "latency": self.latency,
            "start_cash": self.start_cash,
            "accounts": [s.name for s in self.sim.strategies],
        }

    def frame(self, new_trades: list[StreamTrade] | None = None) -> dict:
        books, tops = {}, {}
        for mid in self.market_ids:
            depth = self.replayer.depth(mid)
            if depth is not None:
                books[mid] = {
                    "yes": depth["yes"][:DEPTH_LEVELS],
                    "no": depth["no"][:DEPTH_LEVELS],
                }
            snap = self.sim.ctx.last("kalshi", mid)
            if snap is not None:
                tops[mid] = {
                    "yes_bid": snap.yes_bid,
                    "yes_ask": snap.yes_ask,
                    "mid": snap.mid(),
                }
        fills = self.sim.result.fills
        new_fills = fills[self._n_fills_sent :]
        self._n_fills_sent = len(fills)
        return {
            "cursor": _iso(self.cursor),
            "t_min": _iso(self.t_min),
            "t_max": _iso(self.t_max),
            # Display honesty: the ladder shown during a coverage gap is
            # the last pre-gap state; the UI greys it out. Fills are safe
            # regardless (no events → no snapshots → no executions).
            "in_gap": any(
                g0 <= self.cursor < g1 for g0, g1 in self.gaps if self.cursor is not None
            ),
            "books": books,
            "tops": tops,
            "trades": [
                {
                    "market_id": t.market_id,
                    "ts": _iso(t.recv_ts),
                    "price": t.price,
                    "qty": t.qty,
                    "taker_side": t.taker_side,
                }
                for t in (new_trades or [])
            ],
            "fills": [
                {
                    "account": f.strategy,
                    "market_id": f.market_id,
                    "side": f.side,
                    "qty": f.qty,
                    "price": f.price,
                    "fee": f.fee,
                    "maker": f.maker,
                    "ts": _iso(f.ts),
                }
                for f in new_fills
            ],
            "accounts": self._accounts(),
        }

    def _accounts(self) -> dict:
        """Per-account profile from the sim's component ledger. The sim
        pools cash globally; each account's cash is start_cash plus its
        own ledger flows (proceeds − purchases − fees + payouts)."""
        ctx: Context = self.sim.ctx
        names = [s.name for s in self.sim.strategies]
        out = {
            n: {
                "cash": self.start_cash,
                "positions": [],
                "open_orders": [],
                "in_flight": 0,
                "equity": self.start_cash,
            }
            for n in names
        }
        for (strat, _v, _m), led in self.sim._by_market.items():
            acct = out.get(strat)
            if acct is not None:
                acct["cash"] += led["payout"] - led["cost"] - led["fees"]
        for (strat, venue, mid, side), qty in ctx._positions.items():
            if qty <= 1e-9 or strat not in out:
                continue
            mark = self.sim._mark(venue, mid, side)
            out[strat]["positions"].append(
                {"market_id": mid, "side": side, "qty": qty, "mark": mark}
            )
        for n, acct in out.items():
            acct["equity"] = acct["cash"] + sum(p["qty"] * p["mark"] for p in acct["positions"])
            acct["open_orders"] = [
                {
                    "order_id": oid,
                    "market_id": o.market_id,
                    "side": o.side,
                    "qty": o.qty,
                    "limit_price": o.limit_price,
                    "action": o.action,
                }
                for oid, o in ctx.open_orders(n)
            ]
        for pend in self.sim._pending.values():
            for _ts, strat, _o in pend:
                if strat in out:
                    out[strat]["in_flight"] += 1
        for cmd in self.manual._queue:
            if isinstance(cmd, Order):
                out[self.manual.name]["in_flight"] += 1
        return out


def load_session(
    event: str,
    stream_db: str = STREAM_DB,
    archive_db: str = ARCHIVE_DB,
    strategies_factory: Callable[[], list[Strategy]] | None = None,
    latency: float = 2.0,
    start_cash: float = 1000.0,
) -> ReplaySession:
    """Load one event group's archive slice into a fresh session."""
    with _connect_ro(stream_db) as conn:
        ev_rows = conn.execute(
            "SELECT venue, market_id, recv_ts, src_ts, sid, seq, kind, side,"
            " price, qty FROM book_events WHERE venue = 'kalshi'"
            " AND market_id LIKE ? ORDER BY recv_ts, seq",
            [event + "-%"],
        ).fetchall()
        tr_rows = conn.execute(
            "SELECT venue, market_id, recv_ts, src_ts, price, qty, taker_side,"
            " seq FROM stream_trades WHERE venue = 'kalshi'"
            " AND market_id LIKE ? ORDER BY recv_ts",
            [event + "-%"],
        ).fetchall()
        gaps = conn.execute(
            "SELECT started_at, ended_at FROM stream_gaps ORDER BY started_at"
        ).fetchall()
    events = [BookEvent(*r) for r in ev_rows]
    if not events:
        raise ValueError(f"no archived book events for event {event!r}")
    market_ids = sorted({e.market_id for e in events})
    infos = _try_load_markets(archive_db)
    markets = {("kalshi", m): infos.get(("kalshi", m), MarketInfo("kalshi", m)) for m in market_ids}
    return ReplaySession(
        market_ids,
        events,
        [StreamTrade(*r) for r in tr_rows],
        [(g[0], g[1]) for g in gaps],
        markets,
        strategies_factory=strategies_factory,
        latency=latency,
        start_cash=start_cash,
        archive_db=archive_db,
        meta_loaded=bool(infos),
    )
