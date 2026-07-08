"""simui server: replay clock + WS control channel + static UI.

One WS connection == one replay session. The server owns the clock: a
per-connection task advances the session cursor by wall-tick × speed and
pushes frames; the client sends control/order messages. Everything binds
to localhost — this is a single-user lab tool, not a service.

Session mutations (advance/seek/order) run under a per-connection lock;
advance() runs in a worker thread so a high-speed tick over a dense
window doesn't stall the event loop.
"""

from __future__ import annotations

import asyncio
import http
import json
from datetime import datetime, timedelta
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from hyxlab.simui import session as sess
from hyxlab.strategies.probe import TightSpreadProbe

STATIC = Path(__file__).parent / "static" / "index.html"
TICK_S = 0.12  # wall seconds per clock tick
MAX_SPEED = 3600.0

# Strategies offerable in the UI. Factories, not instances: seek() needs
# fresh state (a portfolio cannot be carried backwards in time).
STRATEGY_REGISTRY = {
    "probe": TightSpreadProbe,
}


class Connection:
    """Per-WS state: the session plus its transport controls."""

    def __init__(self, stream_db: str, archive_db: str) -> None:
        self.stream_db = stream_db
        self.archive_db = archive_db
        self.session: sess.ReplaySession | None = None
        self.playing = False
        self.speed = 10.0
        self.lock = asyncio.Lock()


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def _send(ws, payload: dict) -> None:
    await ws.send(json.dumps(payload))


async def _send_frame(ws, conn: Connection, frame: dict) -> None:
    frame["type"] = "frame"
    frame["playing"] = conn.playing
    frame["speed"] = conn.speed
    await _send(ws, frame)


async def _handle_message(ws, conn: Connection, msg: dict) -> None:
    kind = msg.get("type")

    if kind == "catalog":
        events = await asyncio.to_thread(sess.list_events, conn.stream_db, conn.archive_db)
        await _send(ws, {"type": "catalog", "events": events})
        return

    if kind == "create":
        names = [n for n in msg.get("strategies", []) if n in STRATEGY_REGISTRY]

        def factory():
            return [STRATEGY_REGISTRY[n]() for n in names]

        async with conn.lock:
            conn.session = await asyncio.to_thread(
                sess.load_session,
                msg["event"],
                conn.stream_db,
                conn.archive_db,
                factory,
                float(msg.get("latency", 2.0)),
                float(msg.get("start_cash", 1000.0)),
            )
            if msg.get("start_ts"):
                await asyncio.to_thread(conn.session.seek, _parse_ts(msg["start_ts"]))
            conn.playing = True
            conn.speed = min(float(msg.get("speed", conn.speed)), MAX_SPEED)
            desc = conn.session.describe()
        desc["type"] = "session"
        await _send(ws, desc)
        return

    if conn.session is None:
        await _send(ws, {"type": "error", "message": "no session — send create first"})
        return

    if kind == "play":
        conn.playing = True
    elif kind == "pause":
        conn.playing = False
    elif kind == "speed":
        conn.speed = max(0.1, min(float(msg["speed"]), MAX_SPEED))
    elif kind == "seek":
        async with conn.lock:
            await asyncio.to_thread(conn.session.seek, _parse_ts(msg["ts"]))
            await _send_frame(ws, conn, conn.session.frame())
    elif kind == "order":
        async with conn.lock:
            try:
                conn.session.place_order(
                    msg["market_id"],
                    msg["side"],
                    float(msg["qty"]),
                    limit_price=(
                        float(msg["limit_price"]) if msg.get("limit_price") is not None else None
                    ),
                    action=msg.get("action", "open"),
                    tif=msg.get("tif", "GTC"),
                )
            except ValueError as e:
                await _send(ws, {"type": "error", "message": str(e)})
                return
            await _send_frame(ws, conn, conn.session.frame())
    elif kind == "cancel":
        async with conn.lock:
            conn.session.cancel_order(int(msg["order_id"]))
    elif kind == "history":
        # Trade prints for one market up to the CURSOR only (chart
        # prefill on focus switch — the future stays unknown).
        async with conn.lock:
            s = conn.session
            pts = [
                [t.recv_ts.isoformat(), t.price]
                for t in s.trades[: s._ti]
                if t.market_id == msg["market_id"]
            ]
        await _send(ws, {"type": "history", "market_id": msg["market_id"], "points": pts})
    else:
        await _send(ws, {"type": "error", "message": f"unknown message {kind!r}"})


async def _clock(ws, conn: Connection) -> None:
    ticks = 0
    while True:
        await asyncio.sleep(TICK_S)
        ticks += 1
        s = conn.session
        if s is None:
            continue
        if not s.meta_loaded and ticks % 100 == 0:
            # Archive was writer-locked at load (sweep/backfill); retry
            # and push titles/strikes/close-times when it frees up.
            async with conn.lock:
                landed = await asyncio.to_thread(s.ensure_metadata)
            if landed:
                await _send(ws, {"type": "meta", "markets": s.describe()["markets"]})
        if not conn.playing or s.cursor is None or s.t_max is None:
            continue
        target = s.cursor + timedelta(seconds=TICK_S * conn.speed)
        async with conn.lock:
            frame = await asyncio.to_thread(s.advance, target)
            ended = s.cursor >= s.t_max
        if ended:
            conn.playing = False
        await _send_frame(ws, conn, frame)
        if ended:
            await _send(ws, {"type": "ended"})


def _make_handler(stream_db: str, archive_db: str):
    async def handler(ws) -> None:
        conn = Connection(stream_db, archive_db)
        clock = asyncio.create_task(_clock(ws, conn))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _send(ws, {"type": "error", "message": "bad JSON"})
                    continue
                try:
                    await _handle_message(ws, conn, msg)
                except Exception as e:  # surface, don't kill the socket
                    await _send(ws, {"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            clock.cancel()

    return handler


def _process_request(connection, request):
    """Serve the single-page UI over plain HTTP; let /ws upgrade."""
    path = request.path.split("?", 1)[0]
    if path == "/ws":
        return None  # proceed with the WebSocket handshake
    if path == "/":
        body = STATIC.read_bytes()
        return Response(
            http.HTTPStatus.OK,
            "OK",
            Headers([("Content-Type", "text/html; charset=utf-8")]),
            body,
        )
    return Response(http.HTTPStatus.NOT_FOUND, "Not Found", Headers(), b"not found")


async def run(
    host: str = "127.0.0.1",
    port: int = 8877,
    stream_db: str = sess.STREAM_DB,
    archive_db: str = sess.ARCHIVE_DB,
) -> None:
    async with serve(
        _make_handler(stream_db, archive_db),
        host,
        port,
        process_request=_process_request,
        max_size=2**22,
    ):
        print(f"[simui] http://{host}:{port}  (stream={stream_db})", flush=True)
        await asyncio.get_running_loop().create_future()  # run forever
