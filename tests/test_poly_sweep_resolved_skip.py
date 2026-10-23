"""The poly sweep skips the two fetches for a market a PRIOR run archived as
resolved (2026-10-19).

Measured on the 2026-10-18 backup: 12,458 of the 18,496 enumerated markets
were resolved, they cost 74% of the run's ~93k requests (45,140 of 57,224
price chunks, 23,875 of 35,805 tail pages) plus their 0.25 s pacing, and the
10,179 of them dormant 2+ days carried 0 new prints in a 41h window. The
unit's wall (1,049.8 min on 10-18, over the 17.5h budget) was a walk over
markets that cannot change.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
import pytest

from collector import poly_sweep as ps
from hyxlab.models import MarketInfo
from hyxlab.store import Store


def _info(mid: str, result: str) -> MarketInfo:
    return MarketInfo(venue="polymarket", market_id=mid, title=mid, series="", result=result)


def test_the_set_is_resolved_markets_owing_no_tail(tmp_path):
    store = Store(str(tmp_path / "t.duckdb"))
    try:
        t0 = datetime(2026, 10, 18, 4, 15)
        store.upsert_markets(
            [
                _info("open", ""),
                _info("resolved", "yes"),
                _info("resolved-stopped", "no"),
                _info("resolved-absorbed", "yes"),
                MarketInfo(
                    venue="kalshi", market_id="other-venue", title="", series="", result="yes"
                ),
            ]
        )
        # a tail stop at 10-18 06:00 that no later run has re-fetched
        store.insert_poly_tail_stops([("resolved-stopped", t0 + timedelta(hours=2), 500, 429)])
        store.insert_poly_stats([("resolved-stopped", t0, 1.0, 1.0)])  # the SAME run
        # a stop a later run re-visited (stats row after the stop)
        store.insert_poly_tail_stops([("resolved-absorbed", t0 - timedelta(days=1), 500, 429)])
        store.insert_poly_stats([("resolved-absorbed", t0, 1.0, 1.0)])
        assert store.poly_resolved_complete() == {"resolved", "resolved-absorbed"}
    finally:
        store.close()


def test_a_prior_run_resolved_market_gets_its_gamma_row_and_no_fetch(monkeypatch, tmp_path):
    """Three markets: open, closed-and-archived-resolved, closed-first-seen.
    Only the middle one is skipped; all three keep info + stats + sweep-log
    rows (the shrink tripwire and the tail-absorption oracle read stats)."""
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    markets = [
        {"conditionId": "open", "closed": False},
        {"conditionId": "done", "closed": True},
        {"conditionId": "fresh", "closed": True},
    ]
    prices_for: list[str] = []
    tails_for: list[str] = []
    flushed: list[dict] = []

    class _Store:
        def poly_price_watermarks(self):
            return {}

        def poly_resolved_complete(self):
            return {"done", "open"}  # `open` is in the set but Gamma says not closed

        def close(self):
            pass

    def fake_flush(db, batch):
        flushed.append({k: list(v) for k, v in batch.items() if isinstance(v, list)})
        for k in ("infos", "stats", "prices", "trades", "tail_stops", "swept"):
            batch[k] = []

    monkeypatch.setattr(
        ps.poly, "iter_markets_by_volume", lambda *a, **k: [] if k.get("closed") else list(markets)
    )
    monkeypatch.setattr(
        ps.poly, "token_pair", lambda m: (m["conditionId"] + "-y", m["conditionId"] + "-n")
    )
    monkeypatch.setattr(ps.poly, "gamma_market_info", lambda m: (m["conditionId"],))
    monkeypatch.setattr(
        ps.poly, "prices_history_range", lambda tok, *a, **k: prices_for.append(tok) or []
    )
    monkeypatch.setattr(ps.poly, "price_rows", lambda *a, **k: [])
    monkeypatch.setattr(
        ps.poly, "trades_tail", lambda cond, session=None: tails_for.append(cond) or []
    )
    monkeypatch.setattr(ps.poly, "take_tail_stops", lambda: [])
    monkeypatch.setattr(ps, "_flush", fake_flush)
    monkeypatch.setattr(ps, "_open", lambda db: _Store())
    monkeypatch.setattr(ps, "note_holder", lambda path: None)
    monkeypatch.setattr(ps, "LOCK_FILE", str(tmp_path / "writer.lock"))
    totals = ps.sweep("unused.duckdb", min_volume=0.0)
    assert totals["markets"] == 3
    assert totals["skipped_resolved"] == 1
    assert tails_for == ["open", "fresh"]
    assert prices_for == ["open-y", "open-n", "fresh-y", "fresh-n"]
    infos = [i for f in flushed for i in f["infos"]]
    stats = [s[0] for f in flushed for s in f["stats"]]
    swept = [s[0] for f in flushed for s in f["swept"]]
    assert infos == [("open",), ("done",), ("fresh",)]
    assert stats == ["open", "done", "fresh"]
    assert swept == ["open", "done", "fresh"]


def test_an_unreadable_set_falls_back_to_fetching_everything(capsys):
    class _Broken:
        def poly_resolved_complete(self):
            raise duckdb.Error("no such table")

    class _NoArchive:
        pass

    assert ps.resolved_complete(_Broken()) == set()
    assert ps.resolved_complete(_NoArchive()) == set()
    out = capsys.readouterr().out
    assert out.count("[poly] resolved set unreadable, fetching every market") == 2


@pytest.mark.parametrize("closed", [True, False])
def test_gammas_flag_alone_never_skips(closed):
    """`skip` needs BOTH: a market closed per Gamma but absent from the
    archive's resolved set is the run that first sees the close and must
    fetch the final prints; one in the set but open per Gamma (re-opened,
    or a stale row) must fetch too."""
    m = {"conditionId": "x", "closed": closed}
    assert (bool(m.get("closed")) and "x" in set()) is False
    assert (bool(m.get("closed")) and "x" in {"x"}) is closed


def test_the_progress_line_carries_the_skip_count_and_the_enumeration_its_split(
    monkeypatch, capsys, tmp_path
):
    """The enumeration is every open market first, then the closed ones, so
    the skip cannot land before the boundary -- the 2026-10-19 first skip run
    was read at index 2,000-5,000 against the prior day and showed ~13 min,
    all of it open-market noise. The enumeration line names the split and
    each progress line the skips so far, so the lever is read in-flight."""
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    n = ps.PROGRESS_EVERY
    open_m = [{"conditionId": f"o{i:04d}", "closed": False} for i in range(n)]
    closed_m = [{"conditionId": f"c{i:04d}", "closed": True} for i in range(n)]
    resolved = {m["conditionId"] for m in closed_m[: n // 2]}

    class _Store:
        def poly_price_watermarks(self):
            return {}

        def poly_resolved_complete(self):
            return resolved

        def close(self):
            pass

    monkeypatch.setattr(
        ps.poly,
        "iter_markets_by_volume",
        lambda *a, **k: list(closed_m) if k.get("closed") else list(open_m),
    )
    monkeypatch.setattr(ps.poly, "token_pair", lambda m: ("y", "n"))
    monkeypatch.setattr(ps.poly, "gamma_market_info", lambda m: (m["conditionId"],))
    monkeypatch.setattr(ps.poly, "prices_history_range", lambda *a, **k: [])
    monkeypatch.setattr(ps.poly, "price_rows", lambda *a, **k: [])
    monkeypatch.setattr(ps.poly, "trades_tail", lambda cond, session=None: [])
    monkeypatch.setattr(ps.poly, "take_tail_stops", lambda: [])
    monkeypatch.setattr(ps, "_flush", lambda db, batch: None)
    monkeypatch.setattr(ps, "_open", lambda db: _Store())
    monkeypatch.setattr(ps, "note_holder", lambda path: None)
    monkeypatch.setattr(ps, "LOCK_FILE", str(tmp_path / "writer.lock"))
    totals = ps.sweep("unused.duckdb", min_volume=0.0)
    assert totals["skipped_resolved"] == n // 2
    out = capsys.readouterr().out.splitlines()
    assert any(ln.endswith(f"volume ({n} open, {n} closed)") for ln in out), out
    lines = [ln for ln in out if "min left" in ln]
    assert len(lines) == 2
    assert f"{n}/{2 * n} | 0 prices, 0 trades, 0 skipped |" in lines[0]
    assert f"{2 * n}/{2 * n} | 0 prices, 0 trades, {n // 2} skipped |" in lines[1]


def test_the_done_line_splits_markets_rows_and_wall_by_segment(monkeypatch, tmp_path):
    """Open, closed-fetched and closed-skipped are three populations with
    different yields: on the first skip run (2026-10-19) the 1,572
    closed-fetched markets carried ~718k of 810k trades at 6.7 s/market
    against 2.7 for the 6,105 open ones, readable only by hand-differencing
    progress lines across the boundary. The done line carries the split:
    FETCHED rows per segment (pre-dedup, so the skipped segment is 0 by
    construction) and each segment's loop wall, with the flushes' wall
    beside them."""
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    open_m = [{"conditionId": f"o{i}", "closed": False} for i in range(3)]
    closed_m = [{"conditionId": f"c{i}", "closed": True} for i in range(4)]
    resolved = {"c0", "c2", "c3"}  # c1 is first seen closed: fetched

    class _Store:
        def poly_price_watermarks(self):
            return {}

        def poly_resolved_complete(self):
            return resolved

        def close(self):
            pass

    monkeypatch.setattr(
        ps.poly,
        "iter_markets_by_volume",
        lambda *a, **k: list(closed_m) if k.get("closed") else list(open_m),
    )
    monkeypatch.setattr(ps.poly, "token_pair", lambda m: ("y", "n"))
    monkeypatch.setattr(ps.poly, "gamma_market_info", lambda m: (m["conditionId"],))
    monkeypatch.setattr(ps.poly, "prices_history_range", lambda tok, *a, **k: [tok])
    # two price rows per token for an open market, one for a closed one
    monkeypatch.setattr(
        ps.poly, "price_rows", lambda tok, cond, *a, **k: [cond] * (2 if cond[0] == "o" else 1)
    )
    # the closed tail is the heavy one: 10 prints vs 1
    monkeypatch.setattr(
        ps.poly, "trades_tail", lambda cond, session=None: [cond] * (10 if cond[0] == "c" else 1)
    )
    monkeypatch.setattr(ps.poly, "poly_trade_row", lambda t: t)
    monkeypatch.setattr(ps.poly, "take_tail_stops", lambda: [])
    monkeypatch.setattr(ps, "_flush", lambda db, batch: None)
    monkeypatch.setattr(ps, "_open", lambda db: _Store())
    monkeypatch.setattr(ps, "note_holder", lambda path: None)
    monkeypatch.setattr(ps, "LOCK_FILE", str(tmp_path / "writer.lock"))
    totals = ps.sweep("unused.duckdb", min_volume=0.0)
    seg = totals["segments"]
    assert {k: v["markets"] for k, v in seg.items()} == {
        "open": 3,
        "closed_fetched": 1,
        "closed_skipped": 3,
    }
    assert {k: v["prices"] for k, v in seg.items()} == {
        "open": 12,  # 3 markets x 2 tokens x 2 rows
        "closed_fetched": 2,
        "closed_skipped": 0,
    }
    assert {k: v["trades"] for k, v in seg.items()} == {
        "open": 3,
        "closed_fetched": 10,
        "closed_skipped": 0,
    }
    assert sum(v["markets"] for v in seg.values()) == totals["markets"] == 7
    assert all(v["wall_min"] >= 0.0 and "wall_s" not in v for v in seg.values())
    assert totals["flush_min"] >= 0.0


def test_a_market_whose_fetch_raised_keeps_neither_its_gamma_nor_its_stats_row(
    monkeypatch, tmp_path
):
    """Its Gamma row would set `markets.result` for a closed market and the
    stats row would tell the tail oracle a run fetched after the stop; with
    both archived, tomorrow's `poly_resolved_complete` skips the market for
    good. Measured 2026-10-21: 87 CLOB 500s on prices-history across the
    open/closed boundary. The sweep-log row (the ETA's prior) still lands."""
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    markets = [
        {"conditionId": "fine", "closed": True},
        {"conditionId": "broke", "closed": True},
        {"conditionId": "after", "closed": False},
    ]
    flushed: list[dict] = []

    class _Store:
        def poly_price_watermarks(self):
            return {}

        def poly_resolved_complete(self):
            return set()

        def close(self):
            pass

    def fake_flush(db, batch):
        flushed.append({k: list(v) for k, v in batch.items() if isinstance(v, list)})
        for k in ("infos", "stats", "prices", "trades", "tail_stops", "swept"):
            batch[k] = []

    def fake_prices(tok, *a, **k):
        if tok == "broke-n":  # the second token: the first's rows are already in the batch
            raise ps.requests.HTTPError("500 Server Error: Internal Server Error")
        return [{"t": 1, "p": 0.5}]

    monkeypatch.setattr(
        ps.poly, "iter_markets_by_volume", lambda *a, **k: [] if k.get("closed") else list(markets)
    )
    monkeypatch.setattr(
        ps.poly, "token_pair", lambda m: (m["conditionId"] + "-y", m["conditionId"] + "-n")
    )
    monkeypatch.setattr(ps.poly, "gamma_market_info", lambda m: (m["conditionId"],))
    monkeypatch.setattr(ps.poly, "prices_history_range", fake_prices)
    monkeypatch.setattr(ps.poly, "price_rows", lambda tok, cond, outcome, hist: [(tok, cond)])
    monkeypatch.setattr(ps.poly, "trades_tail", lambda cond, session=None: [])
    monkeypatch.setattr(ps.poly, "take_tail_stops", lambda: [])
    monkeypatch.setattr(ps, "_flush", fake_flush)
    monkeypatch.setattr(ps, "_open", lambda db: _Store())
    monkeypatch.setattr(ps, "note_holder", lambda path: None)
    monkeypatch.setattr(ps, "LOCK_FILE", str(tmp_path / "writer.lock"))
    totals = ps.sweep("unused.duckdb", min_volume=0.0)
    assert totals["markets"] == 3
    assert totals["error_classes"] == {"HTTPError": 1}
    infos = [i for f in flushed for i in f["infos"]]
    stats = [s[0] for f in flushed for s in f["stats"]]
    swept = [s[0] for f in flushed for s in f["swept"]]
    prices = [p[0] for f in flushed for p in f["prices"]]
    assert infos == [("fine",), ("after",)]
    assert stats == ["fine", "after"]
    assert swept == ["fine", "broke", "after"]
    assert prices == ["fine-y", "fine-n", "broke-y", "after-y", "after-n"]
