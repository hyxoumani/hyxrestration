"""Polymarket CLOB WebSocket protocol: subscribe payload + parsing.

Re-probed against the LIVE WIRE 2026-09-24 (the 2026-07-06 note this
replaces was taken from the venue's docs, and two of its three claims
about the delta frame were wrong — mistakes #82):
- URL wss://ws-subscriptions-clob.polymarket.com/ws/market, no auth;
  send {"type": "market", "assets_ids": [...]} once after connect.
- Server replies with one full `book` per token, then `price_change`
  frames and occasional `last_trade_price` prints. Frames may be a
  single object or a JSON array of objects. ~5 connections/IP.
- A `price_change` frame carries `market` + `timestamp` and a
  **`price_changes`** list; each ENTRY carries its own `asset_id`,
  `price`, `size`, `side` (BUY/SELL) and the server's own `best_bid`/
  `best_ask`. There is no frame-level `asset_id`. The old note said the
  list was `changes` and the asset was on the frame; against that shape
  every delta parsed to nothing, so 2026-07-08 -> 09-24 archived
  242,828,734 polymarket book rows and NOT ONE delta.
- `size` is the NEW ABSOLUTE size at that price level, not a signed
  change (0 = level removed) — the one claim of the old note that
  survived, and it is now MEASURED. Scoring the two readings against the
  `best_bid`/`best_ask` the server states on every entry does NOT
  discriminate (2026-09-24: 1730/1730 for both) — a top-of-book witness
  only sees which prices are non-empty, and both readings agree on that
  until a level empties. The discriminating test replays 150s of live
  deltas from a seed book, then opens a SECOND connection for a fresh
  server `book` and compares level sizes: 2,068 deltas (622 of them
  `size: 0`) over 96 assets, **absolute-replace reproduces 944/948
  touched levels (99.58%), signed-add 138/948 (14.56%)**. A witness that
  cannot separate two readings is not evidence for either.
- The reply to the daemon's idle `PING` is the bare text `PONG`, not
  JSON. `parse_message` names it, because letting `json.loads` raise
  through the read loop tore the connection down 802 times in 24 days —
  the largest poly-books fault class on record by 9x.
- No sequence numbers: coverage gaps are only detectable as disconnects,
  so the daemon logs a gap on every reconnect and the fresh `book`
  re-seeds state.

market_id in emitted rows is the CLOB token (asset) id; the YES/NO pair
mapping lives in the watchlist (`polymarket_pairs`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from hyxlab.streamstore import BookEvent, StreamTrade

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
VENUE = "polymarket"

#: Bare-text frames the server sends that are not JSON and carry no data.
#: Only the reply to our own keepalive is on this list, and it is named
#: rather than pattern-matched: any OTHER non-JSON payload must still raise
#: out of the daemon's read loop, where it is loud.
KEEPALIVE_REPLIES = frozenset({"PONG"})


def subscribe_market(asset_ids: list[str]) -> str:
    return json.dumps({"type": "market", "assets_ids": sorted(asset_ids)})


def _src_ts(obj: dict[str, Any]) -> datetime | None:
    v = obj.get("timestamp")
    if not v:
        return None
    return datetime.fromtimestamp(int(v) / 1000.0, tz=UTC)  # epoch millis


def _void(obj: dict[str, Any], recv_ts: datetime) -> tuple[list[BookEvent], list[StreamTrade]]:
    """A frame that archived no book row and no trade, recorded as one row.

    Kalshi has had this device since 2026-07-30 and its rationale applies
    here verbatim: "a new frame type silently swallowed would otherwise
    just thin the book capture". Polymarket had no equivalent, and that is
    precisely why a delta frame whose list key had been misnamed since the
    first day of capture stayed invisible for 78 days — no row, no counter,
    no log, and a green unit test written against the same wrong shape.

    `side` carries the frame's `event_type`, so the row is attributable: a
    renamed frame type reads as an UNKNOWN void in QA rather than as a
    plausible-looking count. `price_change` is deliberately NOT benign
    downstream — a delta frame that archives nothing is a parse failure
    until proven otherwise, which is the reading that was missing.

    market_id falls back to the frame's `market` (the condition id) when
    there is no `asset_id`: a delta frame has none, and a void row with an
    empty market_id cannot be traced back to what produced it.
    """
    return [
        BookEvent(
            venue=VENUE,
            market_id=str(obj.get("asset_id") or obj.get("market") or ""),
            recv_ts=recv_ts,
            src_ts=_src_ts(obj),
            sid=None,
            seq=None,
            kind="void",
            side=str(obj.get("event_type") or "?"),
            price=0.0,
            qty=0.0,
        )
    ], []


def _parse_one(obj: dict[str, Any], recv_ts: datetime) -> tuple[list[BookEvent], list[StreamTrade]]:
    typ = obj.get("event_type")
    asset = obj.get("asset_id", "")

    if typ == "book":
        events = []
        for side_key, side in (("bids", "bid"), ("asks", "ask")):
            for level in obj.get(side_key) or []:
                events.append(
                    BookEvent(
                        venue=VENUE,
                        market_id=asset,
                        recv_ts=recv_ts,
                        src_ts=_src_ts(obj),
                        sid=None,
                        seq=None,
                        kind="snap",
                        side=side,
                        price=float(level["price"]),
                        qty=float(level["size"]),
                    )
                )
        return (events, []) if events else _void(obj, recv_ts)

    if typ == "price_change":
        events = []
        # `price_changes`, and the asset is PER ENTRY — one frame spans both
        # legs of a market. The frame-level `asset_id` is the fallback only
        # so a single-asset frame, if the venue ever sends one, still lands.
        for ch in obj.get("price_changes") or []:
            events.append(
                BookEvent(
                    venue=VENUE,
                    market_id=str(ch.get("asset_id") or asset),
                    recv_ts=recv_ts,
                    src_ts=_src_ts(obj),
                    sid=None,
                    seq=None,
                    kind="delta",
                    side="bid" if ch.get("side", "").upper() == "BUY" else "ask",
                    price=float(ch["price"]),
                    qty=float(ch["size"]),  # new absolute size at level
                )
            )
        return (events, []) if events else _void(obj, recv_ts)

    if typ == "last_trade_price":
        return [], [
            StreamTrade(
                venue=VENUE,
                market_id=asset,
                recv_ts=recv_ts,
                src_ts=_src_ts(obj),
                price=float(obj["price"]),
                qty=float(obj.get("size") or 0.0),
                taker_side=(obj.get("side") or "").lower() or None,
                seq=None,
            )
        ]

    return _void(obj, recv_ts)  # tick_size_change / unknown


def parse_message(
    raw: str | dict[str, Any] | list, recv_ts: datetime
) -> tuple[list[BookEvent], list[StreamTrade]]:
    """One WS frame (object OR array of objects) → (book_events, trades)."""
    if isinstance(raw, str) and raw.strip().upper() in KEEPALIVE_REPLIES:
        return [], []
    m = json.loads(raw) if isinstance(raw, str) else raw
    objs = m if isinstance(m, list) else [m]
    events: list[BookEvent] = []
    trades: list[StreamTrade] = []
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        e, t = _parse_one(obj, recv_ts)
        events.extend(e)
        trades.extend(t)
    return events, trades
