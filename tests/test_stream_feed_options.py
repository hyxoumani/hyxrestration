"""EDG-153 feed options: every one OFF by default, each fixture-tested.

(1) the venue's dedicated market-data host with the general host behind it,
(2) a timed websocket ping per connection (`ws_rtt=`), (3) a stamp at frame
arrival beside `recv_ts` (`rx_hold=`), (4) the stall instruments (`gc=`, the
stall sampler) and the `rx_lag` quantiles taken off a full on-loop sort.
No test here opens a socket to anything but 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import random
import time
from array import array
from datetime import UTC, datetime, timedelta

import pytest
import websockets

from collector import health, streamd
from collector.venues import kalshi_ws
from hyxlab.streamstore import StreamStore

DEDICATED = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
USER = "wss://user-ws.kalshi.com/trade-api/ws/v2"
ALL_KEYS = (
    "HYXLAB_KALSHI_WS_MD_URL",
    "HYXLAB_KALSHI_WS_MD_CHANNELS",
    "HYXLAB_KALSHI_WS_MD_SIGN_PATH",
    "HYXLAB_KALSHI_WS_USER_URL",
    "HYXLAB_KALSHI_WS_PRIMARY_RETRY_S",
    "HYXLAB_KALSHI_WS_PING_S",
    "HYXLAB_STREAM_ARRIVAL_STAMP",
    "HYXLAB_STREAM_GC_WATCH",
    "HYXLAB_STREAM_STALL_STACK_S",
    "HYXLAB_STREAM_RXLAG_PARTITION",
)


# -- defaults: a merge alone changes nothing ---------------------------------


def test_no_key_is_the_pre_edg153_daemon():
    cfg, bad = streamd.FeedConfig.from_env({})
    assert cfg == streamd.FeedConfig() and bad == []
    assert cfg.describe() is None
    assert not (cfg.md_url or cfg.user_url or cfg.ping_s or cfg.arrival_stamp)
    assert not (cfg.gc_watch or cfg.stall_stack_s or cfg.rxlag_partition)
    # Every key the code reads is in this test's list, so a new key cannot
    # ship without a default-off assertion here.
    src = inspect.getsource(streamd.FeedConfig.from_env)
    import re

    assert set(re.findall(r'"(HYXLAB_[A-Z_]+)"', src)) == set(ALL_KEYS)


def test_default_connect_call_is_byte_identical(monkeypatch, tmp_path):
    """The upgrade request a default daemon sends: the general URL, the
    WS_PATH signature, and no option websockets did not get before."""
    seen: list[tuple[tuple, dict]] = []
    signed: list[tuple] = []

    class FakeWS:
        async def send(self, msg):
            pass

        async def recv(self):
            raise asyncio.CancelledError

    class FakeConnect:
        def __init__(self, *a, **kw):
            seen.append((a, kw))

        async def __aenter__(self):
            return FakeWS()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(streamd.websockets, "connect", FakeConnect)
    monkeypatch.setattr(
        streamd.kalshi_ws, "auth_headers", lambda *a, **kw: signed.append((a, kw)) or {}
    )
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)
    d = streamd.Daemon(_store(tmp_path), watchlist={})
    d.key_id, d.pem = "k", b"p"
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(d._kalshi_loop("trades", lambda: "{}", None))
    assert seen == [((kalshi_ws.WS_URL,), {"additional_headers": {}, "max_size": 2**23})]
    assert signed == [(("k", b"p"), {})]


def _store(tmp_path):
    return StreamStore(tmp_path / "s.duckdb")


# -- (1) hosts ----------------------------------------------------------------


def test_channels_route_to_their_documented_hosts():
    assert kalshi_ws.url_for_channel("books") is None  # nothing configured
    for ch in ("trades", "ticker", "books", "lifecycle"):
        assert kalshi_ws.url_for_channel(ch, DEDICATED, USER) == DEDICATED
    for ch in ("fill", "user_orders"):
        assert kalshi_ws.url_for_channel(ch, DEDICATED, USER) == USER
        assert kalshi_ws.url_for_channel(ch, DEDICATED, None) is None
    # A canary moves one connection and leaves the rest on the general host.
    only = frozenset({"trades"})
    assert kalshi_ws.url_for_channel("trades", DEDICATED, md_channels=only) == DEDICATED
    assert kalshi_ws.url_for_channel("books", DEDICATED, md_channels=only) is None
    assert kalshi_ws.url_for_channel("poly", DEDICATED, USER) is None


def test_the_signed_path_is_the_hosts_own_unless_overridden():
    assert kalshi_ws.sign_path_for(kalshi_ws.WS_URL) == kalshi_ws.WS_PATH
    assert kalshi_ws.sign_path_for(DEDICATED) == "/trade-api/ws/v2"
    assert kalshi_ws.sign_path_for("wss://external-api-ws.kalshi.com/") == "/"
    assert kalshi_ws.sign_path_for("wss://external-api-ws.kalshi.com") == "/"
    assert kalshi_ws.sign_path_for("wss://x.example/", "/trade-api/ws/v2") == "/trade-api/ws/v2"
    # The general host is never re-signed, whatever the override says.
    assert kalshi_ws.sign_path_for(kalshi_ws.WS_URL, "/") == kalshi_ws.WS_PATH


def test_auth_headers_default_path_is_unchanged():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def verifies(headers, path):
        try:
            key.public_key().verify(
                base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
                f"{headers['KALSHI-ACCESS-TIMESTAMP']}GET{path}".encode(),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256(),
            )
            return True
        except Exception:
            return False

    h = kalshi_ws.auth_headers("kid", pem, ts_ms=1)
    assert verifies(h, kalshi_ws.WS_PATH) and not verifies(h, "/")
    h2 = kalshi_ws.auth_headers("kid", pem, ts_ms=1, path="/")
    assert verifies(h2, "/") and not verifies(h2, kalshi_ws.WS_PATH)


def test_chooser_falls_back_on_a_failed_open_and_retries_later():
    c = kalshi_ws.HostChooser(DEDICATED, retry_s=900.0)
    assert c.configured and c.pick(0.0) == DEDICATED
    assert c.failed_to_open(DEDICATED, 10.0) is True
    assert c.pick(11.0) == kalshi_ws.WS_URL  # the next attempt: the general host
    assert c.failed_to_open(kalshi_ws.WS_URL, 12.0) is False  # the fallback failing moves nothing
    assert c.pick(909.9) == kalshi_ws.WS_URL
    assert c.pick(910.0) == DEDICATED  # retry_s later the configured host again
    c.opened(DEDICATED)
    assert c.fallback_since is None


def test_chooser_with_nothing_configured_is_the_general_host_forever():
    for primary in (None, "", kalshi_ws.WS_URL):
        c = kalshi_ws.HostChooser(primary)
        assert not c.configured
        assert c.pick(0.0) == kalshi_ws.WS_URL
        assert c.failed_to_open(kalshi_ws.WS_URL, 1.0) is False
        assert c.pick(2.0) == kalshi_ws.WS_URL


def test_config_parses_and_never_raises_on_a_bad_value():
    env = {
        "HYXLAB_KALSHI_WS_MD_URL": DEDICATED,
        "HYXLAB_KALSHI_WS_MD_CHANNELS": "trades, ticker,nonsense",
        "HYXLAB_KALSHI_WS_MD_SIGN_PATH": "trade-api",  # no leading slash
        "HYXLAB_KALSHI_WS_USER_URL": "https://user-ws.kalshi.com/",  # not wss
        "HYXLAB_KALSHI_WS_PRIMARY_RETRY_S": "soon",
        "HYXLAB_KALSHI_WS_PING_S": "10",
        "HYXLAB_STREAM_ARRIVAL_STAMP": "1",
        "HYXLAB_STREAM_GC_WATCH": "maybe",
        "HYXLAB_STREAM_STALL_STACK_S": "-1",
        "HYXLAB_STREAM_RXLAG_PARTITION": "on",
    }
    cfg, bad = streamd.FeedConfig.from_env(env)
    assert cfg.md_url == DEDICATED
    assert cfg.md_channels == frozenset({"trades", "ticker"})
    assert cfg.md_sign_path is None and cfg.user_url is None
    assert cfg.primary_retry_s == 900.0 and cfg.ping_s == 10.0
    assert cfg.arrival_stamp and cfg.rxlag_partition
    assert not cfg.gc_watch and cfg.stall_stack_s == 0.0
    assert len(bad) == 6, bad
    line = cfg.describe()
    assert line.startswith(
        "feed options (EDG-153): md_host=external-api-ws.kalshi.com for ticker,trades"
    )


def _flaky_connect(monkeypatch, fail_hosts: set[str]):
    """websockets.connect that refuses the named hosts and hands every other
    one a socket that closes after one subscribe."""
    urls: list[str] = []
    signed: list[str | None] = []

    class FakeWS:
        async def send(self, msg):
            pass

        async def recv(self):
            raise ConnectionError("scripted drop after open")

    class FakeConnect:
        def __init__(self, url, *a, **kw):
            self.url = url
            urls.append(url)

        async def __aenter__(self):
            if kalshi_ws.host_of(self.url) in fail_hosts:
                raise OSError("scripted refusal")
            return FakeWS()

        async def __aexit__(self, *exc):
            return False

    def headers(kid, pem, ts_ms=None, path=None):
        signed.append(path)
        return {}

    monkeypatch.setattr(streamd.websockets, "connect", FakeConnect)
    monkeypatch.setattr(streamd.kalshi_ws, "auth_headers", headers)
    return urls, signed


def _run_attempts(monkeypatch, d, channel, n):
    sleeps = 0

    async def counting_sleep(secs):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= n:
            raise asyncio.CancelledError

    monkeypatch.setattr(streamd.asyncio, "sleep", counting_sleep)
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(d._kalshi_loop(channel, lambda: "{}", None))


def _daemon(monkeypatch, tmp_path, env):
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    d = streamd.Daemon(_store(tmp_path), watchlist={})
    d.key_id, d.pem = "k", b"p"
    return d


def test_a_refused_dedicated_host_falls_back_and_says_so(monkeypatch, tmp_path, capsys):
    urls, signed = _flaky_connect(monkeypatch, {"external-api-ws.kalshi.com"})
    d = _daemon(monkeypatch, tmp_path, {"HYXLAB_KALSHI_WS_MD_URL": DEDICATED})
    _run_attempts(monkeypatch, d, "trades", 3)
    assert urls[:3] == [DEDICATED, kalshi_ws.WS_URL, kalshi_ws.WS_URL]
    assert signed[:3] == ["/trade-api/ws/v2", None, None]  # the general host signs as before
    out = capsys.readouterr().out
    assert out.count("could not open external-api-ws.kalshi.com") == 1
    assert "falling back to api.elections.kalshi.com" in out
    assert "kalshi-trades: host api.elections.kalshi.com (general, fallback)" in out


def test_an_open_dedicated_host_is_named_and_kept(monkeypatch, tmp_path, capsys):
    urls, signed = _flaky_connect(monkeypatch, set())
    d = _daemon(
        monkeypatch,
        tmp_path,
        {
            "HYXLAB_KALSHI_WS_MD_URL": "wss://external-api-ws.kalshi.com/",
            "HYXLAB_KALSHI_WS_MD_SIGN_PATH": "/trade-api/ws/v2",
        },
    )
    _run_attempts(monkeypatch, d, "ticker", 3)
    assert set(urls) == {"wss://external-api-ws.kalshi.com/"}
    assert set(signed) == {"/trade-api/ws/v2"}
    assert "kalshi-ticker: host external-api-ws.kalshi.com (configured)" in capsys.readouterr().out


def test_a_channel_outside_the_canary_stays_on_the_general_host(monkeypatch, tmp_path, capsys):
    urls, _ = _flaky_connect(monkeypatch, set())
    d = _daemon(
        monkeypatch,
        tmp_path,
        {"HYXLAB_KALSHI_WS_MD_URL": DEDICATED, "HYXLAB_KALSHI_WS_MD_CHANNELS": "trades"},
    )
    _run_attempts(monkeypatch, d, "ticker", 2)
    assert set(urls) == {kalshi_ws.WS_URL}
    assert "host " not in capsys.readouterr().out  # an unmoved connection logs as before


# -- (2) ping round trip --------------------------------------------------------


def test_rtt_watch_quantiles_timeouts_and_reset():
    w = streamd.WsRttWatch()
    assert w.take() is None
    for ms in (31, 30, 29, 80, 32):
        w.note("books", ms / 1000)
    w.timeout("books")
    w.timeout("trades")
    got = w.take()
    assert got["books"] == (pytest.approx(0.029), pytest.approx(0.031), pytest.approx(0.080), 5, 1)
    assert got["trades"][3:] == (0, 1)
    clause = streamd.ws_rtt_clause(got)
    assert clause == "ws_rtt=books:29.0/31.0/80.0/5/1,trades:nan/nan/nan/0/1"
    assert w.take() is None


async def _echo_server():
    """A local websocket server: answers pings (websockets does that itself)
    and sends `n` text messages on request."""

    async def handler(conn):
        async for msg in conn:
            n = int(msg)
            for i in range(n):
                await conn.send(json.dumps({"type": "trade", "i": i, "pad": "x" * (i * 50)}))

    return await websockets.serve(handler, "127.0.0.1", 0)


def test_pinger_times_a_real_round_trip_on_localhost(monkeypatch):
    d = streamd.Daemon.__new__(streamd.Daemon)
    monkeypatch.setattr(streamd, "PING_WAIT_S", 2.0)

    async def main():
        server = await _echo_server()
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            task = asyncio.create_task(d._ws_pinger(ws, "books", 0.02))
            await asyncio.sleep(0.25)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        server.close()
        await server.wait_closed()

    asyncio.run(main())
    got = d.ws_rtt.take()
    lo, p50, hi, n, timeouts = got["books"]
    assert n >= 3 and timeouts == 0
    assert 0 < lo <= p50 <= hi < 0.5


def test_pinger_counts_an_unanswered_ping_and_never_closes(monkeypatch):
    monkeypatch.setattr(streamd, "PING_WAIT_S", 0.05)
    d = streamd.Daemon.__new__(streamd.Daemon)

    class Mute:
        closed = False

        async def ping(self):
            return asyncio.get_running_loop().create_future()  # a pong that never comes

        async def close(self):  # pragma: no cover - must not be called
            Mute.closed = True

    async def main():
        task = asyncio.create_task(d._ws_pinger(Mute(), "trades", 0.01))
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())
    got = d.ws_rtt.take()["trades"]
    assert got[3] == 0 and got[4] >= 1 and not Mute.closed


def test_pinger_ends_quietly_when_the_connection_closes():
    d = streamd.Daemon.__new__(streamd.Daemon)

    class Closed:
        async def ping(self):
            raise websockets.ConnectionClosedOK(None, None)

    async def main():
        await asyncio.wait_for(d._ws_pinger(Closed(), "ticker", 0.0), 1.0)

    asyncio.run(main())  # returns, does not raise
    assert d.ws_rtt.take() is None


# -- (3) arrival stamp --------------------------------------------------------------


def test_rx_hold_histogram_quantiles_are_bin_floors():
    w = streamd.RxHoldWatch()
    assert w.take() is None
    for i in range(1000):
        w.note(i * 0.00001)  # 0 .. 9.99 ms in 10 us steps
    w.note(5.0)  # one frame past the last bin
    p50, p99, worst, n = w.take()
    assert n == 1001 and worst == 5.0
    assert p50 == pytest.approx(0.0050, abs=1e-4)
    assert p99 == pytest.approx(0.0099, abs=1e-4)
    assert streamd.rx_hold_clause((p50, p99, worst, n)).startswith("rx_hold=5.0/9.9/5000.0/1001")
    assert w.take() is None
    w.note(-0.001)  # a negative hold cannot happen; it lands in bin 0, not an IndexError
    assert w.take()[0] == 0.0


def test_stamped_connection_pairs_one_stamp_with_each_message_in_order():
    """Against a real local server: N messages, fragmented and not, each
    popped with a stamp taken BEFORE `recv()` returned it."""

    async def main():
        server = await _echo_server()
        port = server.sockets[0].getsockname()[1]
        holds = []
        async with websockets.connect(
            f"ws://127.0.0.1:{port}", create_connection=streamd.StampedClientConnection
        ) as ws:
            assert isinstance(ws, streamd.StampedClientConnection)
            await ws.send("40")
            for i in range(40):
                raw = await ws.recv()
                now = time.monotonic_ns()
                assert json.loads(raw)["i"] == i
                assert ws.arrivals, "a message with no stamp"
                stamp = ws.arrivals.popleft()
                assert stamp <= now
                holds.append(now - stamp)
            assert not ws.arrivals  # exactly one stamp per message
        server.close()
        await server.wait_closed()
        return holds

    holds = asyncio.run(main())
    assert len(holds) == 40 and all(h >= 0 for h in holds)


def test_fragmented_message_gets_one_stamp():
    from websockets.frames import Frame, Opcode

    c = streamd.StampedClientConnection.__new__(streamd.StampedClientConnection)
    import collections

    c.arrivals, c._chunk_ns = collections.deque(), 7
    c.response = object()  # past the handshake
    c.recv_messages = type("Q", (), {"put": lambda self, f: None})()
    c.process_event(Frame(Opcode.TEXT, b"ab", fin=False))
    c.process_event(Frame(Opcode.CONT, b"cd", fin=True))
    c.process_event(Frame(Opcode.PING, b"", fin=True))
    assert list(c.arrivals) == [7]


# -- (4) the stall ---------------------------------------------------------------------


def test_rxlag_partition_reads_exactly_what_the_sort_reads():
    rng = random.Random(153)
    a, b = streamd.RecvLagWatch(), streamd.RecvLagWatch(partition=True)
    sent = 1_795_370_400_000
    t0 = datetime.fromtimestamp(sent / 1000, tz=UTC)
    for n, ch in ((50_001, "ticker"), (7, "trades")):
        for _ in range(n):
            ts = t0 + timedelta(milliseconds=rng.choice((rng.gauss(20, 5), rng.expovariate(0.01))))
            a.note(ch, ts, sent)
            b.note(ch, ts, sent)
    assert a.take() == b.take()
    assert b.take() is None


def test_rxlag_partition_is_faster_on_a_production_sized_period():
    """The defect: ~0.6-1.4M samples sorted ON THE LOOP every stats line
    (`rx_lag=ticker:...:633371` on 2026-11-24 20:59Z). Measured 2026-11-24:
    92 ms at 700k, 262 ms at 1.4M by sort; 7 and 12 ms by partition."""
    n = 400_000
    rng = random.Random(1)
    vals = array("d", (rng.gauss(0.02, 0.01) for _ in range(n)))
    slow, fast = streamd.RecvLagWatch(), streamd.RecvLagWatch(partition=True)
    slow._lags["ticker"] = array("d", vals)
    fast._lags["ticker"] = array("d", vals)
    t = time.perf_counter()
    r1 = slow.take()
    t_sort = time.perf_counter() - t
    t = time.perf_counter()
    r2 = fast.take()
    t_part = time.perf_counter() - t
    assert r1 == r2
    assert t_part < t_sort / 3, (t_part, t_sort)


def test_gc_watch_times_a_forced_gen2_collection(capsys):
    import gc

    w = streamd.GcPauseWatch()
    assert w.take() is None  # not installed: unmeasured, not zero
    w.install()
    try:
        junk = [[i] for i in range(200_000)]
        gc.collect(2)
        del junk
        n2, worst, total = w.take()
    finally:
        w.uninstall()
    assert n2 >= 1 and 0 < worst <= total
    assert streamd.gc_clause((n2, worst, total)).startswith(f"gc={n2}/")
    assert w.take() is None  # uninstalled again


def test_gc_watch_buffers_a_long_pause_line(monkeypatch):
    import gc

    monkeypatch.setattr(streamd, "GC_PAUSE_LOG_S", 0.0)
    w = streamd.GcPauseWatch()
    w.install()
    try:
        gc.collect(2)
    finally:
        w.uninstall()
    lines = w.drain_lines()
    assert lines and lines[0].startswith("gc pause: a gen-2 collection held the interpreter")
    assert w.drain_lines() == []


def test_stall_sampler_names_the_blocking_python_call():
    lines: list[str] = []
    beat = [time.monotonic()]

    def busy_loop_body():  # the defect under measurement, by name
        t = time.monotonic()
        while time.monotonic() - t < 0.4:
            sum(range(1000))

    s = streamd.StallSampler(0.1, lambda: beat[0], lines.append)
    s.start()
    try:
        beat[0] = time.monotonic()
        busy_loop_body()
        time.sleep(0.1)
    finally:
        s.stop()
    assert len(lines) == 1, lines
    assert lines[0].startswith("stall sample: loop silent 0.")
    assert "busy_loop_body" in lines[0] and "Python on the loop" in lines[0]


def test_stall_sampler_is_silent_while_the_loop_beats():
    lines: list[str] = []
    s = streamd.StallSampler(0.1, time.monotonic, lines.append)
    s.start()
    time.sleep(0.3)
    s.stop()
    assert lines == []


def test_stats_line_puts_the_new_clauses_before_the_holds(monkeypatch):
    monkeypatch.setattr(streamd, "rss_anon_mb", lambda: 1.0)
    monkeypatch.setattr(streamd, "glibc_arena_heaps", lambda: None)
    args = ("", {"a": 1}, 5, "sock_throttled=0 (+0)", (19, 17.34, 2.06), (0.1, 0.2, 0.3, 0.4))
    base = streamd.stats_line(*args, (0.03, 0), (1.25, 0), {"trades": (0.01, 0.05, 9)})
    extra = {
        "rtt": {"books": (0.029, 0.031, 0.08, 5, 0)},
        "hold": (0.0001, 0.002, 0.3, 1000),
        "gc": (2, 0.123, 0.4),
    }
    line = streamd.stats_line(*args, (0.03, 0), (1.25, 0), {"trades": (0.01, 0.05, 9)}, extra)
    assert (
        " rx_lag=trades:0.01/0.05/9 ws_rtt=books:29.0/31.0/80.0/5/0 rx_hold=0.1/2.0/300.0/1000"
        " gc=2/0.123/0.40 flush_hold=19/17.3/2.1 flush_steps=" in line
    )
    # The digest's two anchored patterns read the same thing with and without.
    for rx in (health._STREAMHOLD_RE, health._STREAMRX_RE):
        assert rx.search(line).groups() == rx.search(base).groups()
    # No extras is the old line exactly.
    assert (
        streamd.stats_line(*args, (0.03, 0), (1.25, 0), {"trades": (0.01, 0.05, 9)}, None) == base
    )


# -- end to end: the real receive loop against a local websocket server ----------


def _trade(seq: int) -> str:
    return json.dumps(
        {
            "type": "trade",
            "sid": 2,
            "seq": seq,
            "sending_ts_ms": 1751889600000,
            "msg": {
                "market_ticker": "M1",
                "yes_price_dollars": "0.4000",
                "count_fp": "5.00",
                "taker_side": "yes",
                "ts": 1751889600,
                "ts_ms": 1751889600000,
            },
        }
    )


def test_receive_loop_with_every_option_on_stores_the_same_rows(monkeypatch, tmp_path):
    """`_kalshi_loop` on a 127.0.0.1 server with the stamp, the pinger, GC
    timing and the partition on: the rows land exactly as with them off, and
    `rx_hold` / `ws_rtt` measure every frame and some pongs."""
    n = 50

    async def handler(conn):
        await conn.recv()  # the subscribe
        for i in range(1, n + 1):
            await conn.send(_trade(i))
        await conn.wait_closed()  # hold the connection open until either side closes

    def run(env) -> tuple[streamd.Daemon, list]:
        for k in ALL_KEYS:
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        store = StreamStore(tmp_path / f"s{len(env)}.duckdb")
        d = streamd.Daemon(store, watchlist={})
        d.key_id, d.pem = "k", b"p"

        async def main():
            server = await websockets.serve(handler, "127.0.0.1", 0)
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/trade-api/ws/v2"
            monkeypatch.setattr(kalshi_ws, "WS_URL", url)
            monkeypatch.setattr(kalshi_ws, "GENERAL_WS_URL", url)
            monkeypatch.setattr(kalshi_ws, "auth_headers", lambda *a, **kw: {})
            task = asyncio.create_task(d._kalshi_loop("trades", lambda: "{}", None))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(store._trades) >= n and (not env or d.ws_rtt._rtt):
                    break
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            server.close()
            await server.wait_closed()

        asyncio.run(main())
        rows = [(t.market_id, t.seq, t.price, t.qty, t.src_ts) for t in store._trades]
        return d, rows

    _, plain = run({})
    on = {
        "HYXLAB_STREAM_ARRIVAL_STAMP": "1",
        "HYXLAB_KALSHI_WS_PING_S": "0.02",
        "HYXLAB_STREAM_GC_WATCH": "1",
        "HYXLAB_STREAM_RXLAG_PARTITION": "1",
    }
    d, stamped = run(on)
    assert len(plain) == n and stamped == plain
    p50, p99, worst, frames = d.rx_hold.take()
    assert frames == n and 0 <= p50 <= p99 <= worst < 1.0
    assert d.ws_rtt.take()["trades"][3] >= 1
    assert d.recv_lag.partition is True
    extras = streamd.Daemon.feed_extras(d)
    assert set(extras) == {"rtt", "hold", "gc"}


def test_default_daemon_publishes_no_new_clause(monkeypatch, tmp_path):
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)
    d = streamd.Daemon(_store(tmp_path), watchlist={})
    assert d.feed_extras() is None
    assert d.recv_lag.partition is False
    # Both publishers pass the same extras to the one renderer.
    src = inspect.getsource(streamd)
    assert src.count("extras = self.feed_extras()") == 2
    assert src.count("*tail, extras))") == 2
