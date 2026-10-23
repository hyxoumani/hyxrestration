"""Shadow reads only its own universe after the stream coverage expansion.

`book_events` gains books for `kalshi_book_series` (~290M rows/day at
staging). Shadow's universe is the watchlist's `kalshi_series` -- before the
expansion, the whole table. Its seed and poll push that universe into SQL, so
planting rows of other series must leave everything it does unchanged."""

from __future__ import annotations

import contextlib
import sys
from datetime import UTC, datetime, timedelta

import duckdb

from collector.venues.kalshi_ws import parse_message
from hyxlab.models import MarketInfo, Order
from hyxlab.store import Store
from hyxlab.streamstore import StreamStore
from simulator import shadow as shadow_mod
from simulator.shadow import ShadowLedger, ShadowRunner, shadow_universe
from simulator.strategy import Strategy

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
U1 = "KXLOWTNYC-26OCT23-B60.5"
U2 = "KXHIGHNY-26OCT23-B70.5"
X1 = "KXBTC15M-26OCT231215-15"  # books-only series, outside the universe
X2 = "KXHIGHTNYCX-26OCT23-B70.5"  # shares a prefix with KXHIGHNY's family


class BuyEach(Strategy):
    name = "buy_each"

    def __init__(self):
        self.seen: set[str] = set()

    def on_snapshot(self, s, ctx):
        if s.market_id in self.seen:
            return []
        self.seen.add(s.market_id)
        return [Order(s.venue, s.market_id, "yes", 3, tif="IOC")]


def _snap(mid, seq, bid, ask_no, ts, sid=1):
    return parse_message(
        {
            "type": "orderbook_snapshot",
            "sid": sid,
            "seq": seq,
            "msg": {
                "market_ticker": mid,
                "yes_dollars_fp": [[f"{bid / 100:.4f}", "100.00"]],
                "no_dollars_fp": [[f"{ask_no / 100:.4f}", "100.00"]],
            },
        },
        ts,
    )[0]


def _delta(mid, seq, price, qty, ts, sid=1):
    return parse_message(
        {
            "type": "orderbook_delta",
            "sid": sid,
            "seq": seq,
            "msg": {
                "market_ticker": mid,
                "price_dollars": f"{price:.4f}",
                "delta_fp": f"{qty:.2f}",
                "side": "yes",
                "ts_ms": int(ts.timestamp() * 1000),
            },
        },
        ts,
    )[0]


def _run(tmp_path, *, extras: bool, series):
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = Store(tmp_path / "archive.duckdb")
    archive.upsert_markets([MarketInfo(venue="kalshi", market_id=m) for m in (U1, U2, X1, X2)])
    archive.close()
    st = StreamStore(tmp_path / "stream.duckdb")

    def universe(i, ts):
        st.append_events(_snap(U1 if i % 2 else U2, i, 40 + i % 5, 58 - i % 5, ts))

    def other(i, ts):
        if extras:
            st.append_events(_snap(X1 if i % 2 else X2, i, 30, 60, ts, sid=1_000_001))
            st.append_events(_delta(X1, i, 0.3, 5.0, ts, sid=1_000_001))

    # History, with a book gap partway: the seed starts at its end.
    for i in range(1, 9):
        ts = T0 + timedelta(seconds=i)
        universe(i, ts)
        other(i, ts + timedelta(milliseconds=500))
    st.append_gap("kalshi", "books", T0 + timedelta(seconds=3), T0 + timedelta(seconds=4), "x")
    other(99, T0 + timedelta(seconds=20))  # the NEWEST row is outside the universe
    st.flush()

    sql: list[str] = []
    real = shadow_mod.stream_conn

    class Log:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, q, *a, **k):
            sql.append(q)
            return self._inner.execute(q, *a, **k)

        def __getattr__(self, n):
            return getattr(self._inner, n)

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return self._inner.__exit__(*e)

    shadow_mod.stream_conn = lambda p: Log(real(p))
    try:
        r = ShadowRunner(
            [BuyEach()],
            latency=0.0,
            stream_db=str(tmp_path / "stream.duckdb"),
            archive_db=str(tmp_path / "archive.duckdb"),
            ledger=ShadowLedger(tmp_path / "shadow.duckdb"),
            series=series,
        )
        r.poll_once()  # anchor + seed
        for k in range(3):  # three live polls, universe and extras interleaved
            for j in range(4):
                ts = T0 + timedelta(seconds=30 + 10 * k + j)
                st.append_events(_delta(U1, 100 + 10 * k + j, 0.41, 2.0, ts))
                other(200 + 10 * k + j, ts + timedelta(milliseconds=1))
                st.append_events(
                    _snap(U2, 300 + 10 * k + j, 45, 54, ts + timedelta(milliseconds=2))
                )
            st.flush()
            r.poll_once()
    finally:
        shadow_mod.stream_conn = real
    with duckdb.connect(str(tmp_path / "shadow.duckdb"), read_only=True) as conn:
        fills = conn.execute(
            "SELECT strategy, market_id, side, price, qty FROM shadow_fills ORDER BY ALL"
        ).fetchall()
    out = {
        "fills": [(f.market_id, f.side, f.price, f.qty, f.ts) for f in r.sim.result.fills],
        "ledger": fills,
        "stats": dict(r.stats),
        "depth": {m: r.replayer.depth(m) for m in (U1, U2)},
        "cursor_rows": r.stats["events"],
    }
    return out, sql, r


def test_planted_extra_series_rows_leave_shadow_output_unchanged(tmp_path):
    before, _, _ = _run(tmp_path / "a", extras=False, series=["KXHIGHNY", "KXLOWTNYC"])
    after, sql, _ = _run(tmp_path / "b", extras=True, series=["KXHIGHNY", "KXLOWTNYC"])
    assert before["fills"] and before["depth"][U1] is not None  # non-vacuous
    assert after == before
    # pushed into SQL, not filtered after fetchall: every book_events read
    # that returns rows carries the series predicate
    reads = [q for q in sql if "FROM book_events" in q and "ORDER BY" in q]
    assert len(reads) >= 2  # the seed walk and the polls
    assert all("list_contains" in q for q in reads)


def test_control_without_the_universe_the_extras_do_reach_shadow(tmp_path):
    """Discrimination: the planted rows are not inert by construction."""
    scoped, _, _ = _run(tmp_path / "a", extras=True, series=["KXHIGHNY", "KXLOWTNYC"])
    unscoped, _, _ = _run(tmp_path / "b", extras=True, series=None)
    assert unscoped["stats"]["events"] > scoped["stats"]["events"]
    assert {f[0] for f in unscoped["fills"]} > {f[0] for f in scoped["fills"]}


def test_the_daemon_runs_on_the_watchlist_kalshi_series(monkeypatch):
    import json
    from pathlib import Path

    wl = json.loads((Path("hyxlab/watchlist.json")).read_text())
    assert shadow_universe() == wl["kalshi_series"]
    assert not set(shadow_universe()) & set(wl["kalshi_book_series"])

    seen = {}

    class Stop(Exception):
        pass

    def fake_runner(strategies, **kw):
        seen.update(kw)
        raise Stop

    monkeypatch.setattr(shadow_mod, "ShadowRunner", fake_runner)
    monkeypatch.setattr(shadow_mod, "db_owner_lock_or_reason", lambda p: (object(), ""))
    monkeypatch.setattr(sys, "argv", ["shadow"])
    with contextlib.suppress(Stop):
        shadow_mod.main()
    assert seen["series"] == wl["kalshi_series"]
