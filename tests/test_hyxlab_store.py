"""Store guarantees: idempotent inserts, schema versioning, tz migration."""

from datetime import UTC, date, datetime

from hyxlab.migrate import migrate
from hyxlab.models import Forecast, Snapshot
from hyxlab.store import SCHEMA_VERSION, Store

TS = datetime(2026, 7, 1, 12, tzinfo=UTC)
CANDLE = ("kalshi", "M1", TS, 3600, None, None, None, 0.30, 0.29, 0.31, None, None, 10.0, 5.0)


def test_insert_candles_is_idempotent(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    assert store.insert_candles([CANDLE]) == 1
    assert store.insert_candles([CANDLE]) == 0  # rerun of a backfill is safe
    assert store.counts()["candles"] == 1
    store.close()


def test_insert_forecasts_dedups_on_natural_key(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    fc = Forecast(station="NYC", fetched_at=TS, target_date=date(2026, 7, 2), high_f=90)
    store.insert_forecasts([fc])
    store.insert_forecasts([fc])
    assert store.counts()["nws_forecasts"] == 1
    store.close()


def test_fresh_db_is_born_at_current_version(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    assert store.schema_version() == SCHEMA_VERSION
    store.close()


def test_migration_1_shifts_legacy_local_to_utc(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    # Simulate a legacy DB: version 0 with a box-local (CDT, UTC-5) candle.
    store.set_schema_version(0)
    legacy_local = datetime(2026, 7, 4, 10, 0)  # what old code stored
    store.conn.execute(
        "INSERT INTO candles VALUES ('kalshi','M1',?,3600,NULL,NULL,NULL,0.3,0.29,0.31,NULL,NULL,1,1)",
        [legacy_local],
    )
    migrate(store)
    assert store.schema_version() == SCHEMA_VERSION
    row = store.conn.execute("SELECT end_ts FROM candles").fetchone()
    assert row[0] == datetime(2026, 7, 4, 15, 0)  # CDT + 5h = UTC
    store.close()


def _snap(venue, yes_bid, yes_ask, no_bid, no_ask):
    return Snapshot(
        venue=venue,
        market_id="M1",
        ts=TS,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_bid_size=1.0,
        yes_ask_size=1.0,
        no_bid_size=1.0,
        no_ask_size=1.0,
    )


def test_mirror_tripwire_passes_on_mirrored_kalshi_quotes(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    # Kalshi's single mirrored book: no_ask = 1 - yes_bid, no_bid = 1 - yes_ask.
    store.insert_snapshots([_snap("kalshi", 0.44, 0.46, 0.54, 0.56)])
    assert store.mirror_violations() == 0
    store.close()


def test_mirror_tripwire_flags_corrupted_kalshi_quotes(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    # no_ask 0.50 vs 1 - yes_bid = 0.56: impossible on Kalshi -> corruption.
    store.insert_snapshots([_snap("kalshi", 0.44, 0.46, 0.54, 0.50)])
    assert store.mirror_violations() == 1
    store.close()


def test_mirror_tripwire_ignores_independent_polymarket_books(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    # Polymarket YES/NO are independent token books; no mirror to enforce.
    store.insert_snapshots([_snap("polymarket", 0.44, 0.46, 0.44, 0.50)])
    assert store.mirror_violations() == 0
    store.close()


def test_watermarks_roundtrip(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    assert store.watermark("KXCPI") is None
    store.set_watermark("KXCPI", TS)
    assert store.watermark("KXCPI") == TS.replace(tzinfo=None)
    store.close()
