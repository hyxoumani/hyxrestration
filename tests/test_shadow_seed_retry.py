"""A seed that raised has not seeded (mistakes #238).

Measured 2026-11-23 17:41:14Z: the shadow's first poll died on a DuckDB
INTERNAL error inside the seed walk. The anchor was already set, so the next
poll took the "not the first poll" branch and the run traded on with no seed
and no seed line. The runner must forget the anchor and seed again."""

from __future__ import annotations

from datetime import timedelta

import duckdb

from hyxlab.models import MarketInfo
from hyxlab.store import Store
from hyxlab.streamstore import StreamStore
from simulator import shadow as shadow_mod
from simulator.shadow import ShadowLedger, ShadowRunner
from tests.test_shadow_universe import T0, U1, U2, BuyEach, _delta, _snap


def _runner(tmp_path):
    archive = Store(tmp_path / "archive.duckdb")
    archive.upsert_markets([MarketInfo(venue="kalshi", market_id=m) for m in (U1, U2)])
    archive.close()
    st = StreamStore(tmp_path / "stream.duckdb")
    st.append_gap("kalshi", "books", T0, T0 + timedelta(seconds=1), "x")
    st.append_events(_snap(U1, 1, 40, 58, T0 + timedelta(seconds=2)))
    st.append_events(_snap(U2, 2, 45, 54, T0 + timedelta(seconds=3)))
    st.flush()
    runner = ShadowRunner(
        [BuyEach()],
        latency=0.0,
        stream_db=str(tmp_path / "stream.duckdb"),
        archive_db=str(tmp_path / "archive.duckdb"),
        ledger=ShadowLedger(tmp_path / "shadow.duckdb"),
        series=None,
    )
    return st, runner


def _fail_once(monkeypatch):
    """The seed walk yields one event, then raises -- once."""
    real = shadow_mod.stream_events
    calls = {"n": 0}

    def walk(*a, **k):
        calls["n"] += 1
        if calls["n"] > 1:
            yield from real(*a, **k)
            return
        for e in real(*a, **k):
            yield e
            raise duckdb.InternalException("Attempted to access index 0 within vector of size 0")

    monkeypatch.setattr(shadow_mod, "stream_events", walk)
    return calls


def test_a_seed_that_raises_drops_the_anchor_and_says_so(tmp_path, monkeypatch, capsys) -> None:
    _, r = _runner(tmp_path)
    _fail_once(monkeypatch)
    r.poll_once()
    out = capsys.readouterr().out
    assert (r.cursor, r.gap_cursor) == (None, None)
    assert "seed FAILED" in out and "seeded books" not in out


def test_the_poll_after_a_failed_seed_seeds_whole_and_the_run_trades_both_books(
    tmp_path, monkeypatch, capsys
) -> None:
    st, r = _runner(tmp_path)
    calls = _fail_once(monkeypatch)
    r.poll_once()  # seed raises partway
    r.poll_once()  # re-anchors, seeds again
    assert calls["n"] == 2
    assert "(2 top states" in capsys.readouterr().out
    # Deltas only: a book the seed never imaged yields no snapshot to trade.
    st.append_events(_delta(U1, 3, 0.41, 2.0, T0 + timedelta(seconds=10)))
    st.append_events(_delta(U2, 4, 0.46, 2.0, T0 + timedelta(seconds=11)))
    st.flush()
    assert r.poll_once() == 2
    assert r.sim.strategies[0].seen == {U1, U2}
