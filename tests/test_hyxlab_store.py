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


def test_poly_stats_stores_naive_utc_not_box_local(tmp_path):
    """DuckDB converts an aware datetime to the BOX's local time on insert.
    poly_market_stats postdated migration_1 and shipped without _naive_utc,
    so its whole ts column sat LEGACY_TZ behind the rest of the archive
    (found 2026-08-23). Every writer that takes an aware ts needs this."""
    store = Store(tmp_path / "t.duckdb")
    store.insert_poly_stats([("pm1", TS, 1.0, 2.0)])
    assert store.conn.execute("SELECT ts FROM poly_market_stats").fetchone()[0] == (
        TS.replace(tzinfo=None)
    )
    store.close()


def test_migration_2_shifts_poly_stats_local_to_utc(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    store.set_schema_version(1)
    legacy_local = datetime(2026, 7, 4, 10, 0)  # CDT, what the old path stored
    store.conn.execute("INSERT INTO poly_market_stats VALUES ('pm1',?,1.0,2.0)", [legacy_local])
    migrate(store)
    assert store.schema_version() == SCHEMA_VERSION
    assert store.conn.execute("SELECT ts FROM poly_market_stats").fetchone()[0] == (
        datetime(2026, 7, 4, 15, 0)
    )
    store.close()


def test_migration_2_is_safe_on_an_empty_table(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    store.set_schema_version(1)
    migrate(store)
    assert store.schema_version() == SCHEMA_VERSION
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


def test_insert_trades_dedups_on_trade_id(tmp_path):
    from collector.venues.kalshi import trade_row

    # Live REST shape, probed 2026-07-07.
    api_trade = {
        "trade_id": "d763a421-6682-5bce-7e71-0ef65e5756f8",
        "ticker": "KXHIGHTLV-26JUL06-T111",
        "created_time": "2026-07-06T17:21:56.956835Z",
        "yes_price_dollars": "0.0100",
        "no_price_dollars": "0.9900",
        "count_fp": "9.35",
        "taker_side": "yes",
        "taker_outcome_side": "yes",
        "taker_book_side": "bid",
        "is_block_trade": False,
    }
    row = trade_row(api_trade)
    assert row[4] == 0.01  # yes_price in dollars
    assert row[5] == 9.35  # fractional qty preserved
    store = Store(tmp_path / "t.duckdb")
    assert store.insert_trades([row]) == 1
    assert store.insert_trades([row]) == 0  # retro-pass re-run is safe
    assert store.counts()["trades"] == 1
    # tz-aware input must land as naive UTC, never box-local (the 5h-shift
    # corruption this exact path produced on 2026-07-07).
    stored = store.conn.execute("SELECT ts FROM trades").fetchone()[0]
    assert stored == datetime(2026, 7, 6, 17, 21, 56, 956835)
    store.close()


def test_trades_swept_tracks_progress(tmp_path):
    store = Store(tmp_path / "t.duckdb")
    assert store.trades_swept_ids() == set()
    store.mark_trades_swept("M1", 0, "empty")
    store.mark_trades_swept("M2", 12, "ok")
    assert store.trades_swept_ids() == {"M1", "M2"}
    store.mark_trades_swept("M1", 3, "ok")  # re-mark replaces, no dup
    assert store.conn.execute("SELECT count(*) FROM trades_swept").fetchone()[0] == 2
    store.close()


def test_open_retry_waits_out_transient_lock(tmp_path, monkeypatch):
    """Writers that must not die (poly sweep flush) wait out readers
    holding the file lock instead of crashing mid-run."""
    import duckdb

    from hyxlab import store as store_mod

    real_connect = duckdb.connect
    attempts = {"n": 0}

    def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise duckdb.IOException("Could not set lock on file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store_mod.duckdb, "connect", flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    store = store_mod.open_retry(tmp_path / "t.duckdb", retries=5, delay=0)
    assert attempts["n"] == 3
    store.close()


def test_open_retry_raises_after_exhaustion(tmp_path, monkeypatch):
    import duckdb
    import pytest

    from hyxlab import store as store_mod

    def always_locked(*args, **kwargs):
        raise duckdb.IOException("Could not set lock on file")

    monkeypatch.setattr(store_mod.duckdb, "connect", always_locked)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(duckdb.IOException):
        store_mod.open_retry(tmp_path / "t.duckdb", retries=3, delay=0)


def test_sweep_lock_excludes_second_holder_and_releases(tmp_path):
    from collector.sweep import acquire_sweep_lock

    path = str(tmp_path / "sweep.lock")
    first = acquire_sweep_lock(path)
    assert first is not None
    assert acquire_sweep_lock(path) is None  # held -> refused
    first.close()  # release (also happens on process death)
    third = acquire_sweep_lock(path)
    assert third is not None
    third.close()


def test_vintages_dedup_and_naive_utc(tmp_path):
    from datetime import UTC, date

    from hyxlab.models import EconVintage

    store = Store(tmp_path / "t.duckdb")
    v = EconVintage(
        series_id="CPIAUCSL",
        obs_date=date(2026, 6, 1),
        value=321.5,
        knowable_at=datetime(2026, 7, 11, 12, 30, tzinfo=UTC),
    )
    assert store.insert_vintages([v]) == 1
    assert store.insert_vintages([v]) == 0  # re-fetch is safe
    # a REVISION of the same period is a new row, not a dup
    v2 = EconVintage(
        series_id="CPIAUCSL",
        obs_date=date(2026, 6, 1),
        value=321.7,
        knowable_at=datetime(2026, 8, 11, 12, 30, tzinfo=UTC),
    )
    assert store.insert_vintages([v2]) == 1
    # stored-timestamp assertion (mistakes #10): tz-aware in, naive UTC out
    stored = store.conn.execute(
        "SELECT knowable_at FROM econ_vintages ORDER BY knowable_at LIMIT 1"
    ).fetchone()[0]
    assert stored == datetime(2026, 7, 11, 12, 30)
    store.close()


def test_news_dedup_on_url_hash_and_naive_utc(tmp_path):
    from datetime import UTC

    from hyxlab.models import NewsItem

    store = Store(tmp_path / "t.duckdb")
    n = NewsItem(
        source="gdelt",
        url_hash="abc123",
        published_at=None,
        knowable_at=datetime(2026, 7, 11, 9, 15, tzinfo=UTC),
        title="CPI surprises",
        tone=-2.5,
        topics="cpi,inflation",
    )
    assert store.insert_news([n]) == 1
    assert store.insert_news([n]) == 0  # same article re-seen
    stored = store.conn.execute("SELECT knowable_at, tone FROM news_items").fetchone()
    assert stored == (datetime(2026, 7, 11, 9, 15), -2.5)
    store.close()


def test_backup_rotation_and_consistency(tmp_path):
    import duckdb

    from collector.backup import backup_one

    src = tmp_path / "hyxtest.duckdb"
    store = Store(src)
    store.log_sweep("S1", datetime.now(UTC), datetime.now(UTC), 1, 1, "ok")
    store.close()
    dest = tmp_path / "backups"
    dest.mkdir()
    out = backup_one(src, dest)
    assert out is not None and out.name.startswith("hyxtest.") and out.suffix == ".duckdb"
    # same weekday slot overwrites (rotation), and the copy opens clean
    assert backup_one(src, dest) == out
    with duckdb.connect(str(out), read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM sweep_log").fetchone()[0] == 1
    assert backup_one(tmp_path / "missing.duckdb", dest) is None


def test_open_time_roundtrips_and_migrates(tmp_path):
    """2026-08-02 lifecycle telemetry: open_time is stored, and a pre-change
    DB (markets table without the column) gains it via the idempotent ALTER
    on next open instead of crashing the positional upsert."""
    from hyxlab.models import MarketInfo

    store = Store(tmp_path / "t.duckdb")
    store.upsert_markets([MarketInfo(
        venue="kalshi", market_id="KXHIGHNY-26AUG02-B88.5", series="KXHIGHNY",
        close_time=TS, open_time=datetime(2026, 6, 30, 14, tzinfo=UTC))])
    row = store.conn.execute(
        "SELECT open_time FROM markets WHERE market_id='KXHIGHNY-26AUG02-B88.5'"
    ).fetchone()
    assert row[0] == datetime(2026, 6, 30, 14)  # stored naive-UTC
    store.conn.close()

    # simulate a pre-change DB: drop the column, then reopen
    import duckdb as _duckdb
    conn = _duckdb.connect(str(tmp_path / "t.duckdb"))
    conn.execute("ALTER TABLE markets DROP COLUMN open_time")
    conn.close()
    store2 = Store(tmp_path / "t.duckdb")
    store2.upsert_markets([MarketInfo(
        venue="kalshi", market_id="KXHIGHNY-26AUG03-B89.5", series="KXHIGHNY",
        close_time=TS, open_time=TS)])
    n = store2.conn.execute(
        "SELECT count(*) FROM markets WHERE open_time IS NOT NULL").fetchone()[0]
    assert n == 1  # migrated column, upsert works; pre-change row stays NULL


def test_upsert_markets_last_wins_on_duplicate_keys_in_one_batch(tmp_path):
    """EXP-963 rewrote upsert_markets set-based (staging + one OR REPLACE:
    5,913 rows fell 11.0s -> 1.0s at production scale, and that was most
    of the collect cycle's lock hold). OR REPLACE over a SELECT keeps an
    ARBITRARY source row on duplicate keys, where the old executemany kept
    the LAST — a cycle can carry the same market twice (open + settled
    page overlap), and which row survives must not be luck."""
    from hyxlab.models import MarketInfo

    store = Store(tmp_path / "t.duckdb")
    store.upsert_markets([])  # empty cycle must be a no-op, not an error
    dups = [
        MarketInfo(venue="kalshi", market_id="M1", title=f"v{i}", series="S", close_time=TS)
        for i in range(40)
    ]
    store.upsert_markets(dups)
    title, updated_at = store.conn.execute(
        "SELECT title, updated_at FROM markets WHERE market_id='M1'"
    ).fetchone()
    assert title == "v39", f"duplicate-key upsert kept {title!r}, not the last row"
    assert store.counts()["markets"] == 1
    # mistakes #10: every store writer asserts its stored timestamp
    assert updated_at is not None and updated_at.tzinfo is None  # naive-UTC
    store.close()


def test_markets_filters_by_venue_and_liveness_and_pins_included_keys(tmp_path):
    """Regression (2026-08-07): the unfiltered markets dict reached ~430MB
    (486k rows, +13k/day since the breadth widening) and walked the shadow
    daemon to within ~35MB of its 1G cgroup cap. Long-lived holders filter
    with venue/alive_days; `include` pins keys past the filter so a held
    position that settles after the recency window still surfaces its
    result."""
    from datetime import timedelta

    from hyxlab.models import MarketInfo

    store = Store(tmp_path / "t.duckdb")
    now = datetime.now(UTC).replace(tzinfo=None)
    store.upsert_markets(
        [
            MarketInfo(venue="kalshi", market_id="LIVE", close_time=now + timedelta(days=1)),
            MarketInfo(venue="kalshi", market_id="OLD_OPEN", close_time=now - timedelta(days=30)),
            MarketInfo(
                venue="kalshi",
                market_id="OLD_DONE",
                close_time=now - timedelta(days=30),
                result="yes",
            ),
            MarketInfo(
                venue="kalshi",
                market_id="FRESH_DONE",
                close_time=now - timedelta(days=1),
                result="no",
            ),
            MarketInfo(venue="kalshi", market_id="NO_CLOSE_DONE", result="yes"),
            MarketInfo(venue="polymarket", market_id="P_LIVE", close_time=now + timedelta(days=1)),
        ]
    )
    assert len(store.markets()) == 6  # default stays unfiltered
    filtered = store.markets(venue="kalshi", alive_days=3)
    assert {k[1] for k in filtered} == {"LIVE", "OLD_OPEN", "FRESH_DONE"}
    pinned = store.markets(
        venue="kalshi", alive_days=3, include=[("kalshi", "OLD_DONE"), ("kalshi", "NO_CLOSE_DONE")]
    )
    assert {k[1] for k in pinned} == {"LIVE", "OLD_OPEN", "FRESH_DONE", "OLD_DONE", "NO_CLOSE_DONE"}
    store.close()
