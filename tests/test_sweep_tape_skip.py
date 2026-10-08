"""The sweep skips a trade tape its candles prove empty (2026-10-08).

From 10-06 Kalshi's read limit fell below the sweep's unchanged rate: the
same ~95 markets/min that drew ~45 tape 429s per run drew ~16-25k, each one
a silent hole left to the retro-pass, whose backlog went 259 -> 38,931.
82% of fetched tapes were empty, and the hourly candles fetched just before
already prove it. Skipping those removes the tape request, not the data.

The coverage clause is the one with a measured counterexample: 186 markets
had zero-volume candles AND trades, every print after the last candle,
which ended before the close (10-07 backup, markets swept since 09-20).
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from collector import sweep as sweep_mod

CLOSE = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
CLOSE_TS = int(CLOSE.timestamp())


def _candle(end: datetime, volume="0.00") -> dict:
    return {"end_period_ts": int(end.timestamp()), "volume_fp": volume}


def _covering(volume="0.00") -> list[dict]:
    return [_candle(CLOSE - timedelta(hours=2), "0.00"), _candle(CLOSE, volume)]


def test_zero_volume_candles_reaching_the_close_prove_an_empty_tape():
    assert sweep_mod.tape_provably_empty(_covering(), CLOSE_TS)


def test_candles_ending_before_the_close_prove_nothing():
    # The measured counterexample: last candle 03:00, close 04:00, prints at
    # 03:2x-03:4x. That final partial hour is in no candle.
    candles = [_candle(CLOSE - timedelta(hours=2)), _candle(CLOSE - timedelta(hours=1))]
    assert not sweep_mod.tape_provably_empty(candles, CLOSE_TS)


def test_any_volume_means_fetch():
    assert not sweep_mod.tape_provably_empty(_covering("3.00"), CLOSE_TS)


def test_a_missing_volume_field_is_not_a_zero():
    # candle_row reads an absent volume_fp as 0.0; if the skip did too, an
    # API rename would silently skip every tape in the archive.
    candles = _covering()
    del candles[0]["volume_fp"]
    assert not sweep_mod.tape_provably_empty(candles, CLOSE_TS)
    candles = _covering()
    candles[1]["volume_fp"] = ""
    assert not sweep_mod.tape_provably_empty(candles, CLOSE_TS)


def test_no_candles_prove_nothing():
    assert not sweep_mod.tape_provably_empty([], CLOSE_TS)


class _Store:
    def __init__(self):
        self.marks = []
        self.watermark_writes = []

    def watermark(self, series):
        return None

    def set_watermark(self, series, ts):
        self.watermark_writes.append(ts)

    def log_sweep(self, *a):
        pass

    def upsert_markets(self, infos):
        pass

    def insert_candles(self, rows):
        return len(rows)

    def insert_trades(self, rows):
        pass

    def mark_trades_swept(self, ticker, n, status):
        self.marks.append((ticker, n, status))


def _market(ticker: str, close: datetime) -> dict:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "ticker": ticker,
        "open_time": (close - timedelta(hours=3)).strftime(fmt),
        "close_time": close.strftime(fmt),
    }


def test_sweep_skips_only_the_proven_tape_and_still_advances(monkeypatch):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    proven_close = now - timedelta(hours=6)
    short_close = now - timedelta(hours=3)  # the later close: drives the watermark
    candles = {
        "KXT-PROVEN": [_candle(proven_close - timedelta(hours=1)), _candle(proven_close)],
        "KXT-SHORT": [_candle(short_close - timedelta(hours=1))],
    }
    fetched = []
    store = _Store()

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield store

    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(
        sweep_mod.kalshi,
        "get_markets_ascending",
        lambda *a, **k: (
            [_market("KXT-PROVEN", proven_close), _market("KXT-SHORT", short_close)],
            False,
        ),
    )
    monkeypatch.setattr(
        sweep_mod.kalshi, "get_candlesticks", lambda s, ticker, *a, **k: candles[ticker]
    )
    monkeypatch.setattr(sweep_mod.kalshi, "candle_row", lambda *a: ("row",))
    monkeypatch.setattr(
        sweep_mod.kalshi, "get_trades", lambda t, **k: (fetched.append(t), ([], False))[1]
    )
    monkeypatch.setattr(sweep_mod.kalshi, "to_market_info", lambda m: m)
    monkeypatch.setattr(sweep_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(sweep_mod, "_TAPES_SKIPPED", 0)

    sweep_mod.sweep_series("unused.duckdb", "KXT", 2, session=None)

    assert fetched == ["KXT-SHORT"], "a proven-empty tape was requested, or a short one skipped"
    assert ("KXT-PROVEN", 0, "candle_empty") in store.marks
    assert ("KXT-SHORT", 0, "empty") in store.marks
    assert sweep_mod._TAPES_SKIPPED == 1
    assert store.watermark_writes == [short_close]


def test_a_skipped_market_still_moves_the_watermark(monkeypatch):
    # The skip must not bypass the max_close update: a series whose LAST
    # market is skipped would otherwise re-walk it every night.
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    close = now - timedelta(hours=6)
    store = _Store()

    @contextmanager
    def fake_burst(db, lock_file=None):
        yield store

    monkeypatch.setattr(sweep_mod, "writer_burst", fake_burst)
    monkeypatch.setattr(
        sweep_mod.kalshi, "get_markets_ascending", lambda *a, **k: ([_market("KXT-P", close)], False)
    )
    monkeypatch.setattr(
        sweep_mod.kalshi, "get_candlesticks", lambda *a, **k: [_candle(close)]
    )
    monkeypatch.setattr(sweep_mod.kalshi, "candle_row", lambda *a: ("row",))
    monkeypatch.setattr(
        sweep_mod.kalshi, "get_trades", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    monkeypatch.setattr(sweep_mod.kalshi, "to_market_info", lambda m: m)
    monkeypatch.setattr(sweep_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(sweep_mod, "_TAPES_SKIPPED", 0)

    sweep_mod.sweep_series("unused.duckdb", "KXT", 2, session=None)

    assert store.watermark_writes == [close]
