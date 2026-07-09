"""Polymarket CLOB WebSocket protocol: subscribe payload + parsing.

Verified live 2026-07-06 (docs/sessions handoff):
- URL wss://ws-subscriptions-clob.polymarket.com/ws/market, no auth;
  send {"type": "market", "assets_ids": [...]} once after connect.
- Server replies with one full `book` per token, then `price_change`
  deltas (each carrying the NEW ABSOLUTE size at a price level, not a
  signed change) and occasional `last_trade_price` prints. Frames may be
  a single object or a JSON array of objects. ~5 connections/IP.
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


def subscribe_market(asset_ids: list[str]) -> str:
    return json.dumps({"type": "market", "assets_ids": sorted(asset_ids)})


def _src_ts(obj: dict[str, Any]) -> datetime | None:
    v = obj.get("timestamp")
    if not v:
        return None
    return datetime.fromtimestamp(int(v) / 1000.0, tz=UTC)  # epoch millis


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
        return events, []

    if typ == "price_change":
        events = []
        for ch in obj.get("changes") or []:
            events.append(
                BookEvent(
                    venue=VENUE,
                    market_id=asset,
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
        return events, []

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

    return [], []  # tick_size_change / PONG / unknown


def parse_message(
    raw: str | dict[str, Any] | list, recv_ts: datetime
) -> tuple[list[BookEvent], list[StreamTrade]]:
    """One WS frame (object OR array of objects) → (book_events, trades)."""
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
