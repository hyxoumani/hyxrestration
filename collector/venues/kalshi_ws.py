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

2026-10-20 coverage expansion, shapes read from docs.kalshi.com (websockets
pages, re-read 2026-10-23) and NOT yet probed live -- the #9 lesson is that a
doc shape is a premise until a frame confirms it, so every parser below treats
a missing field as NULL rather than raising:
- `update_subscription` edits an existing subscription in place:
  {"cmd": "update_subscription", "params": {"sid": N, "market_tickers": [...],
  "action": "add_markets" | "delete_markets" | "get_snapshot"}}; the reply is
  `ok` carrying sid, seq and the full post-update market list (or `error`,
  code 26 = "subscription market limit exceeded", 27 = command rate limit).
  The per-subscription market limit EXISTS (code 26) but its value is not
  published. `send_initial_snapshot` is documented for the TICKER channel
  only, and the orderbook page does not say whether `add_markets` sends the
  added books' snapshots -- so the daemon waits for one and asks with
  `get_snapshot` (which leaves the subscription unchanged) if none comes.
- `market_lifecycle_v2` is exchange-wide and takes NO market filter; frames
  carry sid AND seq, and the same subscription delivers `event_lifecycle`
  and `event_fee_update`. Event types: created, activated, deactivated,
  close_date_updated, determined, settled, price_level_structure_updated,
  metadata_updated. There is NO "closed" event: close is a time (`close_ts`).
- `ticker` is exchange-wide when no market is named ("omit to receive all
  markets"); "updates sent whenever any ticker field changes"; its frames
  carry sid, and the page documents NO seq.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from hyxlab.streamstore import BookEvent, StreamLifecycle, StreamTicker, StreamTrade

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


def subscribe_ticker(cmd_id: int = 1) -> str:
    """Exchange-wide `ticker` (no market filter = every market)."""
    return json.dumps({"id": cmd_id, "cmd": "subscribe", "params": {"channels": ["ticker"]}})


def subscribe_lifecycle(cmd_id: int = 1) -> str:
    """Exchange-wide `market_lifecycle_v2` (the channel takes no filter)."""
    return json.dumps(
        {"id": cmd_id, "cmd": "subscribe", "params": {"channels": ["market_lifecycle_v2"]}}
    )


def update_subscription(sid: int, tickers: list[str], action: str, cmd_id: int) -> str:
    """Add or delete markets on an EXISTING subscription -- the in-place edit
    that replaces the reconnect a ticker-set change used to cost -- or ask
    for fresh snapshots of markets already on it (`get_snapshot`).

    No `send_initial_snapshot`: the docs scope that flag to the ticker
    channel, and the books path does not rely on it (see the module doc)."""
    if action not in ("add_markets", "delete_markets", "get_snapshot"):
        raise ValueError(f"unsupported update_subscription action {action!r}")
    params: dict[str, Any] = {"sid": sid, "market_tickers": sorted(tickers), "action": action}
    return json.dumps({"id": cmd_id, "cmd": "update_subscription", "params": params})


#: Kalshi's error code for "adding markets would exceed the per-subscription
#: market limit" (docs.kalshi.com, websocket-connection).
ERR_MARKET_LIMIT = 26


def control_info(frame: dict[str, Any]) -> tuple[str | None, int | None, int | None, int | None]:
    """(type, client cmd id, sid, error code) of a protocol frame.

    `subscribed` names the new sid in `msg.sid`; `ok` carries it at the top
    level; `error` carries `msg.code`. Any of the four may be None."""
    msg = frame.get("msg") if isinstance(frame.get("msg"), dict) else {}
    sid = frame.get("sid")
    if sid is None:
        sid = msg.get("sid")
    code = msg.get("code") if frame.get("type") == "error" else None
    return frame.get("type"), _i(frame.get("id")), _i(sid), _i(code)


def series_of(ticker: str) -> str:
    """`KXBTC15M-26OCT201215-15` -> `KXBTC15M`. Kalshi's series ticker is the
    market ticker's first dash-separated field."""
    return ticker.split("-", 1)[0]


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

    def _void() -> tuple[list[BookEvent], list[StreamTrade]]:
        """A frame that carried sid/seq but archives no book row still
        CONSUMED a wire sequence number. Record it, or the archive shows a
        hole indistinguishable from real data loss: measured 2026-07-30, 70
        such seq in 26h while SeqTracker (which sees every frame on the wire)
        logged not one seq_gap, so the frames arrived and simply parsed to
        nothing. A void row also makes a Kalshi schema change loud — a new
        frame type silently swallowed here would otherwise just thin the
        book capture. Ignored by replay, which filters on snap/delta.

        `side` carries the frame's `type`. Without it the void row is a
        BLIND sentinel and the "loud" claim above is false: an empty
        snapshot, a control ack and an unrecognised new frame type all
        write the same row, so the sentinel absorbs a schema change into
        a plausible-looking count instead of surfacing it. Worse, before
        void rows existed such a frame left a seq hole and turned the QA
        seq check RED; recording it green-lights that check, so without a
        frame-type reader the fix TRADES a detectable failure for an
        invisible one. `side` is meaningless for a row that archives no
        book level, so it is the free column. Legacy void rows (written
        2026-07-30 08:26 → this commit) carry '' and are unattributable."""
        if sid is None or seq is None:
            return [], []
        return [
            BookEvent(
                venue=VENUE,
                market_id=msg.get("market_ticker") or "",
                recv_ts=recv_ts,
                src_ts=_src_ts(msg),
                sid=sid,
                seq=seq,
                kind="void",
                side=str(typ or "?"),
                price=0.0,
                qty=0.0,
            )
        ], []

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
        return (events, []) if events else _void()

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

    return _void()  # subscribed/ok/error/heartbeat frames


def _f(v: Any) -> float | None:
    """A doc-shaped numeric field (string dollars / fixed-point counts) to
    float; absent or unparseable is NULL, never an exception."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _epoch_s(v: Any) -> datetime | None:
    n = _i(v)
    return None if n is None else datetime.fromtimestamp(n, tz=UTC)


def parse_ticker(frame: dict[str, Any], recv_ts: datetime) -> StreamTicker | None:
    """One `ticker` frame -> a row; None for anything else."""
    if frame.get("type") != "ticker":
        return None
    msg = frame.get("msg") or {}
    ticker = msg.get("market_ticker")
    if not ticker:
        return None
    return StreamTicker(
        venue=VENUE,
        market_id=ticker,
        recv_ts=recv_ts,
        src_ts=_src_ts(msg),
        sid=frame.get("sid"),
        seq=frame.get("seq"),
        price=_f(msg.get("price_dollars")),
        yes_bid=_f(msg.get("yes_bid_dollars")),
        yes_ask=_f(msg.get("yes_ask_dollars")),
        yes_bid_size=_f(msg.get("yes_bid_size_fp")),
        yes_ask_size=_f(msg.get("yes_ask_size_fp")),
        last_trade_size=_f(msg.get("last_trade_size_fp")),
        volume=_f(msg.get("volume_fp")),
        open_interest=_f(msg.get("open_interest_fp")),
        dollar_volume=_i(msg.get("dollar_volume")),
        dollar_open_interest=_i(msg.get("dollar_open_interest")),
    )


#: Frame types on the lifecycle connection that are protocol, not data.
CONTROL_TYPES = frozenset({"subscribed", "unsubscribed", "ok", "error", "heartbeat", "pong"})


def parse_lifecycle(frame: dict[str, Any], recv_ts: datetime, raw: str) -> StreamLifecycle | None:
    """One lifecycle-channel frame -> a row, for EVERY sequenced data frame.

    Market-level types (`market_lifecycle_v2`, incl. `metadata_updated`)
    key on the market ticker; event-level types (`event_lifecycle`,
    `event_fee_update`) on the event ticker. A sequenced frame of a type
    this function has never heard of is still a row (`msg_type` = its type,
    raw JSON kept): it consumed a seq, and dropping it would leave a hole
    indistinguishable from loss -- the void-row rule, one table over.
    Control acks (unsequenced, or in CONTROL_TYPES) are None."""
    typ = frame.get("type")
    if not typ or typ in CONTROL_TYPES:
        return None
    msg = frame.get("msg") or {}
    meta = msg.get("additional_metadata") or {}
    event_ticker = msg.get("event_ticker") or meta.get("event_ticker")
    market = msg.get("market_ticker") or event_ticker or ""
    sending = frame.get("sending_ts_ms")
    deact = msg.get("is_deactivated")
    return StreamLifecycle(
        venue=VENUE,
        market_id=market,
        recv_ts=recv_ts,
        src_ts=(
            datetime.fromtimestamp(int(sending) / 1000.0, tz=UTC)
            if _i(sending) is not None
            else None
        ),
        sid=frame.get("sid"),
        seq=frame.get("seq"),
        msg_type=str(typ),
        event_type=msg.get("event_type"),
        event_ticker=event_ticker,
        series_ticker=msg.get("series_ticker") or (series_of(market) if market else None),
        open_ts=_epoch_s(msg.get("open_ts")),
        close_ts=_epoch_s(msg.get("close_ts")),
        determination_ts=_epoch_s(msg.get("determination_ts")),
        settled_ts=_epoch_s(msg.get("settled_ts")),
        result=msg.get("result") or None,
        settlement_value=_f(msg.get("settlement_value")),
        is_deactivated=deact if isinstance(deact, bool) else None,
        raw=raw,
    )


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

    def last(self, sid: int | None) -> int | None:
        """The last seq seen on `sid` (None if none yet)."""
        return None if sid is None else self._last.get(sid)

    def reset(self, sid: int | None = None) -> None:
        if sid is None:
            self._last.clear()
        else:
            self._last.pop(sid, None)
