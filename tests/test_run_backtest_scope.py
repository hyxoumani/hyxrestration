"""`simulator.run_backtest` can be bound to ONE forecast source and a
target-date window (2026-11-27, for `prereg_weather_v2_backtest.md`).

`nws_forecasts` holds backfilled MEX MOS and then the live gridpoint pull;
a run registered on the second must not be served the first, and a run on
a window must not load -- or fill -- a market outside it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from hyxlab.models import Forecast, MarketInfo
from hyxlab.store import Store

LIVE_FROM = datetime(2026, 7, 6, 19, 36, tzinfo=UTC)


def _market(mid: str, target: date) -> MarketInfo:
    return MarketInfo(
        venue="kalshi",
        market_id=mid,
        title=mid,
        series="KXHIGHNY",
        close_time=datetime(target.year, target.month, target.day, 6, tzinfo=UTC)
        + timedelta(days=1),
        strike_type="greater",
        floor_strike=82.0,
        cap_strike=None,
        result="yes",
        target_date=target,
    )


def _candle(mid: str, target: date, bid: float = 0.40, ask: float = 0.42) -> tuple:
    ts = datetime(target.year, target.month, target.day, 15, tzinfo=UTC) - timedelta(days=1)
    return ("kalshi", mid, ts, 3600, None, None, None, ask, bid, ask, None, None, 10.0, 5.0)


def _day_ahead(target: date, high: int) -> Forecast:
    at = datetime(target.year, target.month, target.day, 12, tzinfo=UTC) - timedelta(days=1)
    return Forecast("NYC", at, target, high)


# OLD has only a pre-live (other source) forecast; AUG and SEP sit in the two
# halves of the window; OUT is past it.
OLD, AUG, SEP, OUT = date(2026, 8, 10), date(2026, 8, 20), date(2026, 9, 20), date(2026, 12, 1)


def _archive(tmp_path):
    db = tmp_path / "lab.duckdb"
    store = Store(db)
    store.upsert_markets(
        [_market("M-OLD", OLD), _market("M-AUG", AUG), _market("M-SEP", SEP), _market("M-OUT", OUT)]
    )
    store.insert_candles(
        [_candle(m, d) for m, d in (("M-OLD", OLD), ("M-AUG", AUG), ("M-SEP", SEP), ("M-OUT", OUT))]
    )
    store.insert_forecasts(
        [
            Forecast("NYC", datetime(2026, 7, 1, 12, tzinfo=UTC), OLD, 70),
            _day_ahead(AUG, 90),
            _day_ahead(SEP, 90),
            _day_ahead(OUT, 90),
        ]
    )
    store.upsert_observations([("NYC", AUG, 87), ("NYC", SEP, 88), ("NYC", OUT, 88)])
    store.close()
    return db


def _run(db, capsys, *extra: str) -> str:
    from simulator.run_backtest import main

    main(["--db", str(db), *extra])
    return capsys.readouterr().out


WINDOW = ("--target-from", "2026-08-01", "--target-to", "2026-09-30")
SINCE = ("--forecast-since", LIVE_FROM.isoformat())


def _fills(out: str, name: str) -> int | None:
    for ln in out.splitlines():
        if ln.strip().startswith(name + " ") and "fills=" in ln:
            return int(ln.split("fills=")[1].split()[0])
    return None


def test_forecasts_since_drops_the_earlier_source(tmp_path):
    store = Store(_archive(tmp_path), read_only=True)
    assert len(store.forecasts()) == 4
    assert {f.target_date for f in store.forecasts(since=LIVE_FROM)} == {AUG, SEP, OUT}
    store.close()


def test_forecasts_target_bounds_are_inclusive(tmp_path):
    store = Store(_archive(tmp_path), read_only=True)
    got = store.forecasts(target_from=AUG, target_to=SEP)
    assert {f.target_date for f in got} == {AUG, SEP}
    store.close()


def test_candles_as_snapshots_restricts_to_the_given_ids(tmp_path):
    store = Store(_archive(tmp_path), read_only=True)
    assert {s.market_id for s in store.candles_as_snapshots(market_ids=["M-AUG"])} == {"M-AUG"}
    assert store.candles_as_snapshots(market_ids=[]) == []
    assert len(store.candles_as_snapshots()) == 4
    store.close()


def test_the_crossed_quote_gate_still_applies_under_an_id_filter(tmp_path):
    db = tmp_path / "x.duckdb"
    store = Store(db)
    store.insert_candles([_candle("M-X", AUG, bid=0.60, ask=0.40), _candle("M-Y", AUG)])
    assert store.candles_as_snapshots(market_ids=["M-X"]) == []
    # A NULL quote passes the gate but must not escape the id filter.
    store.insert_candles(
        [("kalshi", "M-N", datetime(2026, 8, 19, 16, tzinfo=UTC), 3600, *[None] * 10)]
    )
    assert {s.market_id for s in store.candles_as_snapshots(market_ids=["M-Y"])} == {"M-Y"}
    store.close()


def test_a_windowed_run_fills_only_markets_inside_the_window(tmp_path, capsys):
    out = _run(_archive(tmp_path), capsys, *WINDOW, *SINCE)
    assert "ABORT" not in out, out
    assert _fills(out, "KXHIGHNY") == 2, out  # AUG and SEP; not OUT, not OLD
    assert "3 markets, 3 settled, 3 with usable candles, 2 forecast rows" in out, out


def test_without_the_source_bound_the_other_source_is_traded(tmp_path, capsys):
    # The vacuity guard for the bound: M-OLD fills on its pre-live forecast.
    out = _run(_archive(tmp_path), capsys, *WINDOW)
    assert _fills(out, "KXHIGHNY") == 3, out


def test_a_windowed_run_prints_both_halves_by_target_date(tmp_path, capsys):
    out = _run(_archive(tmp_path), capsys, *WINDOW, *SINCE)
    # 08-01..09-30 is 61 days: the first half takes the odd day, through 08-31.
    assert _fills(out, "..2026-08-31") == 1, out
    assert _fills(out, "2026-09-01..") == 1, out


def test_the_validity_gate_reads_only_the_bounded_source_and_window(tmp_path, capsys):
    out = _run(_archive(tmp_path), capsys, *WINDOW, *SINCE)
    assert '"n": 2' in out, out  # AUG and SEP; OUT's day-ahead pair is outside


def test_one_target_bound_alone_is_refused(tmp_path):
    import pytest

    from simulator.run_backtest import main

    with pytest.raises(SystemExit):
        main(["--db", str(_archive(tmp_path)), "--target-from", "2026-08-01"])
