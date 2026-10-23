"""Which Kalshi markets the stream daemon holds an `orderbook_delta` book for,
and on which connection -- the bookkeeping half of the 2026-10-20 coverage
expansion, kept free of sockets so it can be tested as plain state.

Until then the book set was re-resolved from REST once an hour and any
change cost a RECONNECT of the one books socket (~111 per 7 days), each
re-seeding ~900 books and writing a gap row that excused every book on the
channel. Two things changed:

- The set is edited IN PLACE with `update_subscription` (add/delete markets
  on the live sid), so a change costs one command and no gap.
- The set follows the exchange-wide `market_lifecycle_v2` channel: a market
  in a book series is added the moment it opens (15-minute series list a new
  market every 15 min, so an hourly REST refresh missed most of each market's
  life) and dropped when it is determined or settled. A REST reconciliation
  every RECONCILE_SECS stays as the backstop for anything the lifecycle
  channel missed (its own reconnects, a frame type we misread).

The books channel is SHARDED: Kalshi limits markets per subscription (error
26; the number is not published), so each connection carries at most
`shard_max` markets and a new connection opens when every shard is full. A
code-26 rejection lowers `shard_max` to what the rejecting shard already
holds and re-homes the rejected markets -- the cap is learned, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from collector.venues.kalshi_ws import series_of

#: Default most markets one books connection subscribes. A placeholder until
#: the real per-subscription cap is measured; `kalshi_book_shard_max` in the
#: watchlist overrides it.
SHARD_MAX = 500

#: How long a market dropped on determination stays barred from re-entry by
#: a REST reconciliation. REST can list a market `open` for a while after
#: its determination frame; without the bar the backstop would re-add every
#: market the lifecycle channel had just removed.
CLOSED_BAR_SECS = 6 * 3600.0

#: Lifecycle event types that put a market's book on (if its series is
#: watched and it is open) and take it off.
OPEN_EVENTS = frozenset({"created", "activated"})
CLOSE_EVENTS = frozenset({"determined", "settled"})


@dataclass(eq=False)
class Shard:
    """One books connection. `assigned` is what it SHOULD carry; `subscribed`
    is what the venue has been told (the subscribe at connect plus every
    update sent since). The daemon sends the difference whenever the two
    disagree and the connection's sid is known."""

    idx: int
    assigned: set[str] = field(default_factory=set)
    subscribed: set[str] = field(default_factory=set)
    sid: int | None = None
    # Was this shard part of the boot set? Only those bound their first
    # seq_reset row by the channel's last PERSISTED frame: a shard opened
    # mid-run carries markets nothing was covering before it.
    at_boot: bool = False
    # Live-connection state, owned by the daemon's loop for this shard.
    ws: Any = field(default=None, repr=False, compare=False)
    pending: dict[int, tuple[str, set[str]]] = field(default_factory=dict, repr=False)
    awaiting: dict[str, float] = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        return f"books-{self.idx}"


class BookCoverage:
    def __init__(self, series: set[str], shard_max: int = SHARD_MAX) -> None:
        self.series = set(series)
        self.shard_max = max(1, int(shard_max))
        self.shards: list[Shard] = []
        self._next_idx = 0
        # Lifecycle-announced markets waiting for their open_ts.
        self.pending_open: dict[str, datetime] = {}
        # Determined/settled markets, barred from REST re-entry until mono.
        self.closed_until: dict[str, float] = {}
        self.adds: set[str] = set()
        self.removes: set[str] = set()

    # -- what is held ------------------------------------------------------

    @property
    def held(self) -> set[str]:
        return set().union(*(s.assigned for s in self.shards)) if self.shards else set()

    def watches(self, ticker: str) -> bool:
        return series_of(ticker) in self.series

    # -- inputs ------------------------------------------------------------

    def on_lifecycle(
        self, ticker: str, event_type: str | None, open_ts: datetime | None, mono: float
    ) -> bool:
        """Feed one market-lifecycle event. Only queues work (`take()` hands
        it to the daemon); True when something was queued."""
        if not ticker or not self.watches(ticker):
            return False
        if event_type in CLOSE_EVENTS:
            self.pending_open.pop(ticker, None)
            self.closed_until[ticker] = mono + CLOSED_BAR_SECS
            self.adds.discard(ticker)
            self.removes.add(ticker)
            return True
        if event_type in OPEN_EVENTS and ticker not in self.closed_until:
            if event_type == "created" and open_ts is not None and open_ts > datetime.now(UTC):
                self.pending_open[ticker] = open_ts
                return True
            self.pending_open.pop(ticker, None)
            self.removes.discard(ticker)
            self.adds.add(ticker)
            return True
        return False

    def due_opens(self, now: datetime | None = None) -> None:
        """Move lifecycle-announced markets whose open time has come into
        the add queue."""
        now = now or datetime.now(UTC)
        for t, ts in list(self.pending_open.items()):
            if ts <= now:
                del self.pending_open[t]
                self.removes.discard(t)
                self.adds.add(t)

    def reconcile(self, rest_open: set[str], mono: float) -> None:
        """Queue the difference between what REST lists open and what is
        held. A market the lifecycle channel closed stays out while barred;
        a market REST no longer lists goes, unless a lifecycle add for it is
        still queued (it opened after the REST page was read)."""
        for t, until in list(self.closed_until.items()):
            if until <= mono:
                del self.closed_until[t]
        want = {t for t in rest_open if t not in self.closed_until}
        held = self.held
        self.adds |= want - held
        self.removes |= held - want - self.adds

    def take(self) -> tuple[set[str], set[str]]:
        adds, removes = self.adds - self.removes, set(self.removes)
        self.adds, self.removes = set(), set()
        return adds, removes

    # -- placement ---------------------------------------------------------

    def new_shard(self, at_boot: bool = False) -> Shard:
        s = Shard(self._next_idx, at_boot=at_boot)
        self._next_idx += 1
        self.shards.append(s)
        return s

    def apply(self, adds: set[str], removes: set[str]) -> tuple[set[int], list[Shard], list[Shard]]:
        """Edit the shards' `assigned` sets. Returns (indices of shards whose
        assignment changed, shards newly created, shards left EMPTY and
        removed from the list -- the daemon closes those connections rather
        than leave a subscription with no markets on it)."""
        touched: set[int] = set()
        for s in self.shards:
            gone = s.assigned & removes
            if gone:
                s.assigned -= gone
                touched.add(s.idx)
        created: list[Shard] = []
        held = self.held
        for t in sorted(adds - held):
            home = next((s for s in self.shards if len(s.assigned) < self.shard_max), None)
            if home is None:
                home = self.new_shard()
                created.append(home)
            home.assigned.add(t)
            touched.add(home.idx)
        emptied = [s for s in self.shards if not s.assigned and s not in created]
        self.shards = [s for s in self.shards if s.assigned or s in created]
        return touched, created, emptied

    def boot(self, tickers: set[str]) -> list[Shard]:
        """The initial placement: every shard is a boot shard."""
        out = []
        for t in sorted(tickers):
            if not out or len(out[-1].assigned) >= self.shard_max:
                out.append(self.new_shard(at_boot=True))
            out[-1].assigned.add(t)
        return out

    def over_limit(self, shard: Shard, rejected: set[str]) -> None:
        """The venue refused `rejected` on `shard` (code 26). Learn the cap
        from what the shard already carries and queue the rejects to be
        placed elsewhere."""
        shard.assigned -= rejected
        shard.subscribed -= rejected
        self.shard_max = max(1, min(self.shard_max, len(shard.assigned)))
        self.adds |= rejected

    def split(self, shard: Shard) -> None:
        """The venue refused the shard's whole SUBSCRIBE (code 26): keep the
        first half, halve the cap, queue the rest for placement."""
        keep = sorted(shard.assigned)[: max(1, len(shard.assigned) // 2)]
        rest = shard.assigned - set(keep)
        shard.assigned = set(keep)
        shard.subscribed = set()
        self.shard_max = max(1, min(self.shard_max, len(keep)))
        self.adds |= rest

    def find(self, idx: int) -> Shard | None:
        return next((s for s in self.shards if s.idx == idx), None)
