"""Named ladder series skip the candle of a market the listing says never
traded (mistakes #179, 2026-11-11).

KXHYPE and KXHYPED list ~6,770 markets a day each and 1-3% of them trade.
At the flat 2,000 budget both frontiers receded 14 h/day for 52 days, to 704
and 730 hours behind. The `/markets` listing already carries each settled
market's final `volume_fp`, so for those two series an explicit zero skips
the candlesticks request, and the per-series budget counts FETCHED markets.

The properties that keep the watermark honest are the ones tested here: the
recorded set is a close-time prefix that never splits an instant, a field
that is missing is not a zero, and a listing with no positive volume in it
proves nothing about the field.
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from collector import sweep as sweep_mod
from hyxlab.store import Store

# Inside the sweep's default look-back: with no watermark the floor is
# `now - days`, and a close older than that cannot move `max_close`.
T0 = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=30)
FMT = "%Y-%m-%dT%H:%M:%SZ"


def _m(ticker: str, hours: int, volume="0.00") -> dict:
    close = T0 + timedelta(hours=hours)
    m = {
        "ticker": ticker,
        "open_time": (close - timedelta(hours=1)).strftime(FMT),
        "close_time": close.strftime(FMT),
    }
    if volume is not None:
        m["volume_fp"] = volume
    return m


def test_only_an_explicit_zero_volume_is_untraded():
    assert sweep_mod.listing_untraded(_m("A", 1, "0.00"))
    assert sweep_mod.listing_untraded(_m("A", 1, 0))
    assert not sweep_mod.listing_untraded(_m("A", 1, "3.00"))
    assert not sweep_mod.listing_untraded(_m("A", 1, None))
    assert not sweep_mod.listing_untraded(_m("A", 1, ""))
    assert not sweep_mod.listing_untraded(_m("A", 1, "n/a"))


def test_the_plan_orders_by_close_and_skips_the_untraded():
    # the venue returns each window newest-first
    listing = [_m("C", 3, "5.00"), _m("B", 2), _m("A", 1)]
    markets, untraded, cut = sweep_mod.plan_untraded_skip(listing, 2000)
    assert [m["ticker"] for m in markets] == ["A", "B", "C"]
    assert untraded == {"A", "B"}
    assert not cut


def test_the_budget_counts_fetched_markets_not_listed_ones():
    listing = [_m(f"U{i}", i) for i in range(1, 50)]
    listing += [_m("T1", 50, "1.00"), _m("T2", 60, "1.00"), _m("T3", 70, "1.00")]
    markets, untraded, cut = sweep_mod.plan_untraded_skip(listing, 2)
    # 49 untraded markets cost nothing; the run stops after its 2nd fetch
    assert markets[-1]["ticker"] == "T2" and len(markets) == 51
    assert len(untraded) == 49
    assert cut


def test_a_cut_never_splits_one_close_instant():
    # Hourly strikes close together: resuming at `wm + 1s` would lose every
    # market of the cut hour that was left behind.
    listing = [_m("T1", 1, "1.00"), _m("T2", 2, "1.00"), _m("U2", 2), _m("T2b", 2, "1.00")]
    listing.append(_m("T3", 3, "1.00"))
    markets, untraded, cut = sweep_mod.plan_untraded_skip(listing, 2)
    assert {m["ticker"] for m in markets} == {"T1", "T2", "U2", "T2b"}
    assert untraded == {"U2"}
    assert cut


def test_reaching_the_budget_on_the_last_instant_is_not_a_cut():
    listing = [_m("T1", 1, "1.00"), _m("T2", 2, "1.00"), _m("U2", 2)]
    markets, _, cut = sweep_mod.plan_untraded_skip(listing, 2)
    assert len(markets) == 3 and not cut


def test_a_listing_with_no_positive_volume_skips_nothing():
    # A venue that starts sending 0.00 for every market must not turn the
    # whole series into unfetched rows.
    listing = [_m("A", 1), _m("B", 2)]
    markets, untraded, cut = sweep_mod.plan_untraded_skip(listing, 2000)
    assert untraded == set() and len(markets) == 2 and not cut


class _FakeStore:
    def __init__(self):
        self.marks = []
        self.watermark_writes = []
        self.upserted = []
        self.logs = []

    def watermark(self, series):
        return None

    def set_watermark(self, series, ts):
        self.watermark_writes.append(ts)

    def log_sweep(self, *a):
        self.logs.append(a)

    def upsert_markets(self, infos):
        self.upserted.extend(infos)

    def insert_candles(self, rows):
        return len(rows)

    def insert_trades(self, rows):
        pass

    def mark_trades_swept_many(self, marks):
        self.marks.extend(marks)


def _wire(monkeypatch, listing, truncated=False):
    store = _FakeStore()
    seen = {"candles": [], "list_kw": None}

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield store

    def fake_list(*a, **k):
        seen["list_kw"] = k
        return list(listing), truncated

    def fake_candles(series, ticker, open_ts, close_ts, *a, **k):
        seen["candles"].append(ticker)
        return [{"end_period_ts": close_ts, "volume_fp": "1.00"}]

    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod.kalshi, "get_markets_ascending", fake_list)
    monkeypatch.setattr(sweep_mod.kalshi, "get_candlesticks", fake_candles)
    monkeypatch.setattr(sweep_mod.kalshi, "candle_row", lambda *a: ("row",))
    monkeypatch.setattr(sweep_mod.kalshi, "get_trades", lambda *a, **k: ([], False))
    monkeypatch.setattr(sweep_mod.kalshi, "to_market_info", lambda m: m["ticker"])
    monkeypatch.setattr(sweep_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(sweep_mod, "_UNTRADED_SKIPPED", 0)
    return store, seen


def test_an_untraded_market_is_recorded_and_marked_without_a_request(monkeypatch):
    # the untraded market closes LAST: the watermark must still reach it
    listing = [_m("KXH-U", 5), _m("KXH-T", 2, "4.00")]
    store, seen = _wire(monkeypatch, listing)

    n_m, n_c, truncated = sweep_mod.sweep_series(
        "unused.duckdb", "KXH", 2, session=None, skip_untraded=True
    )

    assert seen["candles"] == ["KXH-T"]
    assert ("KXH-U", 0, "listing_untraded") in store.marks
    assert sorted(store.upserted) == ["KXH-T", "KXH-U"]
    assert store.watermark_writes == [T0 + timedelta(hours=5)]
    # the count returned is a cost: markets a request was spent on
    assert (n_m, n_c, truncated) == (1, 1, False)
    assert store.logs[-1][3] == 2, "sweep_log keeps the markets RECORDED"
    assert sweep_mod._UNTRADED_SKIPPED == 1
    assert "1 of 2 untraded by listing" in store.logs[-1][-1]


def test_the_listing_is_paged_past_the_fetch_budget(monkeypatch):
    _, seen = _wire(monkeypatch, [_m("KXH-T", 2, "4.00")])
    sweep_mod.sweep_series("unused.duckdb", "KXH", 2, session=None, skip_untraded=True)
    assert seen["list_kw"]["max_markets"] == sweep_mod.UNTRADED_LIST_MAX


def test_without_the_flag_every_listed_market_is_fetched(monkeypatch):
    listing = [_m("KXE-U", 5), _m("KXE-T", 2, "4.00")]
    store, seen = _wire(monkeypatch, listing)
    sweep_mod.sweep_series("unused.duckdb", "KXE", 2, session=None)
    assert sorted(seen["candles"]) == ["KXE-T", "KXE-U"]
    assert seen["list_kw"]["max_markets"] == sweep_mod.MAX_MARKETS_PER_SERIES
    assert not [m for m in store.marks if m[2] == "listing_untraded"]


def test_a_fetch_budget_cut_is_logged_truncated_at_an_honest_watermark(monkeypatch):
    listing = [_m("KXH-T1", 1, "1.00"), _m("KXH-U", 2), _m("KXH-T2", 3, "1.00")]
    listing.append(_m("KXH-T3", 4, "1.00"))
    store, seen = _wire(monkeypatch, listing)
    n_m, _, truncated = sweep_mod.sweep_series(
        "unused.duckdb", "KXH", 2, session=None, max_markets=2, skip_untraded=True
    )
    assert truncated and n_m == 2
    assert seen["candles"] == ["KXH-T1", "KXH-T2"]
    assert store.watermark_writes == [T0 + timedelta(hours=3)]
    assert store.logs[-1][-2] == "truncated"


def _dispatch(monkeypatch, **kw):
    calls = {}

    class _S:
        def log_sweep(self, *a):
            pass

        def counts(self):
            return {}

        def close(self):
            pass

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield _S()

    def fake_series(db, ticker, days, sess, budget, **k):
        calls[ticker] = (budget, k.get("skip_untraded", False))
        return 0, 0, False

    targets = [{"ticker": t, "category": "Test"} for t in ("KXETH", "KXHYPE", "KXHYPED")]
    monkeypatch.setattr(sweep_mod.kalshi, "get_series_list", lambda sess: [])
    monkeypatch.setattr(sweep_mod, "refresh_series", lambda store, sess, series=None: targets)
    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod, "sweep_series", fake_series)
    totals = sweep_mod.run_sweep("unused.duckdb", 2, ["Test"], **kw)
    return calls, totals


def test_the_nightly_run_skips_for_the_named_series_only(monkeypatch):
    calls, totals = _dispatch(monkeypatch)
    assert calls == {
        "KXETH": (8000, False),
        "KXHYPE": (sweep_mod.MAX_MARKETS_PER_SERIES, True),
        "KXHYPED": (sweep_mod.MAX_MARKETS_PER_SERIES, True),
    }
    assert totals["untraded_skipped"] == 0


def test_an_explicit_budget_or_a_repair_pass_fetches_everything(monkeypatch):
    calls, _ = _dispatch(monkeypatch, max_markets=5)
    assert calls["KXHYPE"] == (5, False)
    calls, _ = _dispatch(
        monkeypatch, only_series=["KXHYPE"], refetch_from=T0, refetch_to=T0 + timedelta(days=1)
    )
    assert calls["KXHYPE"][1] is False


def test_the_batch_mark_matches_marking_row_by_row(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    store.mark_trades_swept("M0", 9, "ok")
    store.mark_trades_swept_many(
        [("M0", 0, "listing_untraded"), ("M1", 0, "listing_untraded"), ("M1", 4, "ok")]
    )
    store.mark_trades_swept_many([])
    rows = store.conn.execute(
        "SELECT market_id, n_trades, status, swept_at IS NOT NULL FROM trades_swept ORDER BY 1"
    ).fetchall()
    store.close()
    assert rows == [("M0", 0, "listing_untraded", True), ("M1", 4, "ok", True)]
