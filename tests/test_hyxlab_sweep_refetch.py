"""Below-watermark targeted repair window (EXP-1287, 2026-08-12).

The 294a5ae resume fix stopped NEW holes, but the clamp era (pre-fix
daily `--days 2` runs) burned permanent holes BEHIND each dense series'
watermark — measured from sweep_log 08-02..08-12: ~29h each on
KXBTC/KXBTCD and ~117-120h each on KXETH/KXETHD/KXSOLD/KXSOLE (~534h,
~152k markets total), all inside the venue purge horizon. The normal
floor `max(wm + 1s, now - PURGE_HORIZON_DAYS)` can never reach them by
design, and a bare watermark reset is not a viable repair for the dense
series (it re-walks interleaved covered range and drags the frontier
stale for months at ~7.6k/day density vs the 8k/day budget).

`--refetch-from` / `--refetch-to` fetch an explicit closed range:
- the watermark is neither consulted nor advanced (frontier untouched);
- the ceiling bounds the request so covered range above the hole is not
  re-walked;
- the floor is still bounded by the purge horizon;
- category-wide refetch is refused (requires --series).
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from collector import sweep as sweep_mod


class _StubStore:
    def __init__(self, wm: datetime | None = None):
        self._wm = wm
        self.logged = []
        self.watermark_reads = 0
        self.watermark_writes = []

    def watermark(self, series):
        self.watermark_reads += 1
        return self._wm

    def set_watermark(self, series, last_close_ts):
        self.watermark_writes.append((series, last_close_ts))

    def log_sweep(self, *args):
        self.logged.append(args)

    def upsert_markets(self, infos):
        pass

    def insert_candles(self, rows):
        return len(rows)

    def insert_trades(self, rows):
        pass

    def mark_trades_swept(self, ticker, n, status):
        pass


def _run(monkeypatch, store, markets=(), **kw):
    """Run sweep_series with stubbed venue; capture the markets request."""
    seen = {}

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield store

    def fake_ascending(series_ticker, status, *, min_close_ts, max_close_ts, **k):
        seen["min_close_ts"] = min_close_ts
        seen["max_close_ts"] = max_close_ts
        return list(markets), False

    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(sweep_mod.kalshi, "get_markets_ascending", fake_ascending)
    monkeypatch.setattr(sweep_mod.kalshi, "get_candlesticks", lambda *a, **k: [])
    monkeypatch.setattr(sweep_mod.kalshi, "get_trades", lambda *a, **k: ([], False))
    monkeypatch.setattr(sweep_mod.kalshi, "to_market_info", lambda m: m)
    monkeypatch.setattr(sweep_mod.time, "sleep", lambda s: None)
    sweep_mod.sweep_series("unused.duckdb", "KXTEST", 2, session=None, **kw)
    return seen


def _market(close: datetime) -> dict:
    iso = close.strftime("%Y-%m-%dT%H:%M:%SZ")
    open_iso = (close - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ticker": "KXTEST-X", "open_time": open_iso, "close_time": iso}


def test_refetch_floor_reaches_below_the_watermark(monkeypatch):
    # The whole point: a clamp-era hole sits BEHIND the watermark. The
    # normal floor (wm+1s) can never see it; --refetch-from must.
    wm = datetime.now(UTC) - timedelta(days=1)
    hole_lo = datetime.now(UTC) - timedelta(days=8)
    seen = _run(monkeypatch, _StubStore(wm), refetch_from=hole_lo)
    assert abs(seen["min_close_ts"] - int(hole_lo.timestamp())) <= 1, (
        "repair floor is not --refetch-from — a hole behind the watermark "
        "stays unreachable (the EXP-1271 clamp-era loss is unrepairable)"
    )


def test_refetch_ceiling_bounds_the_request(monkeypatch):
    # Without a ceiling the walk continues past the hole into range the
    # archive already holds — pure wasted request budget on a ~300/h series.
    hole_lo = datetime.now(UTC) - timedelta(days=8)
    hole_hi = datetime.now(UTC) - timedelta(days=7, hours=11)
    seen = _run(monkeypatch, _StubStore(), refetch_from=hole_lo, refetch_to=hole_hi)
    assert abs(seen["max_close_ts"] - int(hole_hi.timestamp())) <= 1


def test_refetch_never_touches_the_watermark(monkeypatch):
    # Forward would skip the un-repaired remainder; backward would make
    # the nightly sweep re-walk covered range. A repair pass must leave
    # the frontier exactly where the nightly incremental owns it.
    wm = datetime.now(UTC) - timedelta(days=1)
    store = _StubStore(wm)
    hole_lo = datetime.now(UTC) - timedelta(days=8)
    _run(
        monkeypatch,
        store,
        markets=[_market(hole_lo + timedelta(hours=2))],
        refetch_from=hole_lo,
        refetch_to=hole_lo + timedelta(hours=12),
    )
    assert store.watermark_writes == [], (
        f"repair pass wrote the watermark {store.watermark_writes} — the "
        f"nightly frontier is now corrupted"
    )
    assert store.logged, "repair coverage must still land in sweep_log"


def test_normal_sweep_still_advances_the_watermark(monkeypatch):
    # Control: the non-repair path is unchanged.
    store = _StubStore(None)
    close = datetime.now(UTC) - timedelta(hours=5)
    _run(monkeypatch, store, markets=[_market(close)])
    assert len(store.watermark_writes) == 1


def test_refetch_floor_is_bounded_by_the_purge_horizon(monkeypatch):
    # Below the purge horizon the venue no longer has the data.
    ancient = datetime.now(UTC) - timedelta(days=400)
    seen = _run(monkeypatch, _StubStore(), refetch_from=ancient)
    horizon = datetime.now(UTC) - timedelta(days=sweep_mod.PURGE_HORIZON_DAYS)
    assert abs(seen["min_close_ts"] - int(horizon.timestamp())) <= 5


def test_run_sweep_refuses_category_wide_refetch():
    # A below-watermark refetch re-requests range the watermarks say is
    # done; across whole categories that is a full archive re-walk.
    with pytest.raises(ValueError, match="--series"):
        sweep_mod.run_sweep(
            "unused.duckdb",
            2,
            sweep_mod.DEFAULT_CATEGORIES,
            session=object(),
            refetch_from=datetime.now(UTC) - timedelta(days=8),
        )
