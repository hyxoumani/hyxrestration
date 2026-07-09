"""Kalshi WebSocket protocol: auth, subscribe payloads, message parsing.

Verified live 2026-07-06 (docs/sessions handoff):
- URL wss://api.elections.kalshi.com/trade-api/ws/v2; auth is RSA-PSS
  (SHA256, salt = digest length) over f"{ts_ms}GET{path}", sent as
  KALSHI-ACCESS-{KEY,TIMESTAMP,SIGNATURE} headers on the upgrade request.
- The `trade` channel with no market filter firehoses the WHOLE exchange
  (~105 ev/s observed); `orderbook_delta` requires market_tickers.
- Every data message carries (sid, seq); seq increments by 1 per sid, so
  a jump means missed messages → the book is unknown until re-seeded by
  a fresh orderbook_snapshot (Kalshi re-sends one on resubscribe).

Pure functions + a small SeqTracker; no sockets here (the daemon owns
I/O). Live frame shapes re-probed 2026-07-07: prices/quantities arrive as
STRING DOLLARS (`yes_price_dollars`, `count_fp`, `price_dollars`,
`delta_fp`, `yes_dollars_fp`/`no_dollars_fp` level pairs), timestamps as
`ts_ms` epoch millis (trades also carry `ts` epoch seconds, deltas an ISO
`ts`).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from hyxlab.streamstore import BookEvent, StreamTrade

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
VENUE = "kalshi"


# -- auth -----------------------------------------------------------------


def sign_pss(private_key_pem: bytes, message: str) -> str:
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    sig = key.sign(
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def auth_headers(key_id: str, private_key_pem: bytes, ts_ms: int | None = None) -> dict[str, str]:
    if ts_ms is None:
        ts_ms = int(datetime.now(UTC).timestamp() * 1000)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
        "KALSHI-ACCESS-SIGNATURE": sign_pss(private_key_pem, f"{ts_ms}GET{WS_PATH}"),
    }


# -- subscribe payloads -----------------------------------------------------


def subscribe_trades(cmd_id: int = 1) -> str:
    """Exchange-wide trade firehose (no market filter)."""
    return json.dumps({"id": cmd_id, "cmd": "subscribe", "params": {"channels": ["trade"]}})


def subscribe_books(tickers: list[str], cmd_id: int = 1) -> str:
    return json.dumps(
        {
            "id": cmd_id,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": sorted(tickers)},
        }
    )


# -- message parsing --------------------------------------------------------


def _src_ts(msg: dict[str, Any]) -> datetime | None:
    # ts_ms (epoch millis) is on trades and deltas; trades also carry
    # ts as epoch seconds, deltas carry ts as an ISO string.
    v = msg.get("ts_ms")
    if v is not None:
        return datetime.fromtimestamp(int(v) / 1000.0, tz=UTC)
    v = msg.get("ts")
    if v is None:
        return None
    if isinstance(v, str):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    return datetime.fromtimestamp(int(v), tz=UTC)


def parse_message(
    raw: str | dict[str, Any], recv_ts: datetime
) -> tuple[list[BookEvent], list[StreamTrade]]:
    """One WS frame → (book_events, trades). Unknown/control types → ([], [])."""
    m = json.loads(raw) if isinstance(raw, str) else raw
    typ = m.get("type")
    msg = m.get("msg") or {}
    sid, seq = m.get("sid"), m.get("seq")

    if typ == "trade":
        return [], [
            StreamTrade(
                venue=VENUE,
                market_id=msg["market_ticker"],
                recv_ts=recv_ts,
                src_ts=_src_ts(msg),
                price=float(msg["yes_price_dollars"]),
                qty=float(msg.get("count_fp") or 0.0),
                taker_side=msg.get("taker_side") or None,
                seq=seq,
            )
        ]

    if typ == "orderbook_snapshot":
        events = []
        for side, key in (("yes", "yes_dollars_fp"), ("no", "no_dollars_fp")):
            for price, qty in msg.get(key) or []:
                events.append(
                    BookEvent(
                        venue=VENUE,
                        market_id=msg["market_ticker"],
                        recv_ts=recv_ts,
                        src_ts=_src_ts(msg),
                        sid=sid,
                        seq=seq,
                        kind="snap",
                        side=side,
                        price=float(price),
                        qty=float(qty),
                    )
                )
        return events, []

    if typ == "orderbook_delta":
        return [
            BookEvent(
                venue=VENUE,
                market_id=msg["market_ticker"],
                recv_ts=recv_ts,
                src_ts=_src_ts(msg),
                sid=sid,
                seq=seq,
                kind="delta",
                side=msg["side"],
                price=float(msg["price_dollars"]),
                qty=float(msg["delta_fp"]),  # signed change
            )
        ], []

    return [], []  # subscribed/ok/error/heartbeat frames


class SeqTracker:
    """Per-sid sequence continuity. observe() returns True when a gap is
    detected (caller must resubscribe/re-seed and log a gap)."""

    def __init__(self) -> None:
        self._last: dict[int, int] = {}

    def observe(self, sid: int | None, seq: int | None) -> bool:
        if sid is None or seq is None:
            return False
        last = self._last.get(sid)
        self._last[sid] = seq
        return last is not None and seq != last + 1

    def reset(self, sid: int | None = None) -> None:
        if sid is None:
            self._last.clear()
        else:
            self._last.pop(sid, None)
