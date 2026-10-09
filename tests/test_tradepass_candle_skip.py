"""The retro-pass marks tapes the ARCHIVED candles prove empty (2026-10-09).

The sweep's 612de86 skip cut its own tape 429s 2,681 -> 380 over the same
06:10-08:15Z window, but the retro-pass inherited a 51,755-market backlog
and fetched every tape: 83% of it (43,213) was provably empty from candles
already in the archive. The archive adds one hazard the live API lacks:
`candle_row` stores an absent `volume_fp` as 0.0, so a stored zero needs a
same-day market with volume before it proves anything.
"""

from datetime import UTC, datetime, timedelta

import collector.trades_backfill as tb
from hyxlab.store import Store

CLOSE = datetime(2026, 10, 3, 4, 0)


def _seed(db: str, markets: dict[str, list[tuple[datetime, float]]], swept=()) -> None:
    store = Store(db)
    try:
        for ticker, candles in markets.items():
            store.conn.execute(
                "INSERT INTO markets (venue, market_id, series, close_time, result)"
                " VALUES ('kalshi', ?, 'KXT', ?, 'no')",
                [ticker, CLOSE],
            )
            for end, vol in candles:
                store.conn.execute(
                    "INSERT INTO candles (venue, market_id, end_ts, period_s, volume)"
                    " VALUES ('kalshi', ?, ?, 3600, ?)",
                    [ticker, end, vol],
                )
        for ticker, status in swept:
            store.mark_trades_swept(ticker, 3, status)
    finally:
        store.close()


def _covering(vol=0.0):
    return [(CLOSE - timedelta(hours=1), 0.0), (CLOSE, vol)]


# A same-day market that traded: proves `volume` was being recorded that day.
LIVE = {"KXT-LIVE": _covering(vol=12.0)}


def _swept(db: str) -> dict[str, tuple]:
    store = Store(db)
    try:
        rows = store.conn.execute(
            "SELECT market_id, status, n_trades, swept_at FROM trades_swept"
        ).fetchall()
    finally:
        store.close()
    return {r[0]: r[1:] for r in rows}


def _run(tmp_path, monkeypatch, markets, swept=()):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tb, "LOCK_FILE", str(tmp_path / "writer.lock"))
    db = str(tmp_path / "t.duckdb")
    _seed(db, markets, swept)
    return db, tb.mark_proven_empty(db)


def test_zero_volume_candles_reaching_the_close_are_marked(tmp_path, monkeypatch):
    db, n = _run(tmp_path, monkeypatch, {"KXT-E": _covering(), **LIVE})
    assert n == 1
    status, n_trades, swept_at = _swept(db)["KXT-E"]
    assert (status, n_trades) == ("candle_empty", 0)
    # naive UTC, like every other swept_at -- not the host's CDT
    now = datetime.now(UTC).replace(tzinfo=None)
    assert abs((now - swept_at).total_seconds()) < 60
    assert "KXT-LIVE" not in _swept(db)


def test_candles_stopping_short_of_the_close_prove_nothing(tmp_path, monkeypatch):
    short = [(CLOSE - timedelta(hours=2), 0.0), (CLOSE - timedelta(hours=1), 0.0)]
    db, n = _run(tmp_path, monkeypatch, {"KXT-S": short, **LIVE})
    assert n == 0 and "KXT-S" not in _swept(db)


def test_any_volume_or_no_candles_prove_nothing(tmp_path, monkeypatch):
    db, n = _run(tmp_path, monkeypatch, {"KXT-V": _covering(vol=1.0), "KXT-N": [], **LIVE})
    assert n == 0 and _swept(db) == {}


def test_a_day_with_no_volume_anywhere_is_a_possible_rename(tmp_path, monkeypatch):
    # Every candle that day stored 0.0 -- indistinguishable from an absent
    # field, so nothing is proven and every tape fetches.
    db, n = _run(tmp_path, monkeypatch, {"KXT-A": _covering(), "KXT-B": _covering()})
    assert n == 0 and _swept(db) == {}


def test_an_already_swept_tape_is_left_alone(tmp_path, monkeypatch):
    db, n = _run(tmp_path, monkeypatch, {"KXT-OK": _covering(), **LIVE}, swept=[("KXT-OK", "ok")])
    assert n == 0
    assert _swept(db)["KXT-OK"][:2] == ("ok", 3)


def test_main_never_fetches_a_proven_tape(tmp_path, monkeypatch):
    db, _ = str(tmp_path / "t.duckdb"), None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tb, "LOCK_FILE", str(tmp_path / "writer.lock"))
    _seed(db, {"KXT-E": _covering(), **LIVE})
    fetched = []

    def get_trades(ticker, session=None):
        fetched.append(ticker)
        return [], False

    monkeypatch.setattr(tb.kalshi, "get_trades", get_trades)
    monkeypatch.setattr(tb.time, "sleep", lambda s: None)
    monkeypatch.setattr("sys.argv", ["tradepass", "--db", db])
    tb.main()
    assert fetched == ["KXT-LIVE"]
    swept = _swept(db)
    assert swept["KXT-E"][0] == "candle_empty" and swept["KXT-LIVE"][0] == "empty"
