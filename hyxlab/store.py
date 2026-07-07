"""DuckDB persistence for collected market data and forecasts.

One file (default `data/hyxlab.duckdb` — gitignored via the rooted /data/
rule) holds three tables:

- markets:       latest known metadata + settlement result per market
- snapshots:     append-only top-of-book observations
- nws_forecasts: append-only forecast pulls (fetched_at kept so the sim
                 can enforce no-lookahead when serving forecasts)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from hyxlab.models import Forecast, MarketInfo, Snapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    venue        VARCHAR NOT NULL,
    market_id    VARCHAR NOT NULL,
    title        VARCHAR,
    series       VARCHAR,
    close_time   TIMESTAMP,
    strike_type  VARCHAR,
    floor_strike DOUBLE,
    cap_strike   DOUBLE,
    result       VARCHAR,
    target_date  DATE,
    updated_at   TIMESTAMP,
    PRIMARY KEY (venue, market_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    venue         VARCHAR NOT NULL,
    market_id     VARCHAR NOT NULL,
    ts            TIMESTAMP NOT NULL,
    yes_bid       DOUBLE, yes_ask DOUBLE, no_bid DOUBLE, no_ask DOUBLE,
    yes_bid_size  DOUBLE, yes_ask_size DOUBLE,
    no_bid_size   DOUBLE, no_ask_size DOUBLE,
    last_price    DOUBLE,
    volume        DOUBLE,
    open_interest DOUBLE
);
CREATE TABLE IF NOT EXISTS nws_forecasts (
    station     VARCHAR NOT NULL,
    fetched_at  TIMESTAMP NOT NULL,
    target_date DATE NOT NULL,
    high_f      INTEGER NOT NULL,
    short       VARCHAR
);
CREATE TABLE IF NOT EXISTS candles (
    venue         VARCHAR NOT NULL,
    market_id     VARCHAR NOT NULL,
    end_ts        TIMESTAMP NOT NULL,
    period_s      INTEGER NOT NULL,
    price_open    DOUBLE, price_high DOUBLE, price_low DOUBLE, price_close DOUBLE,
    yes_bid_close DOUBLE, yes_ask_close DOUBLE,
    yes_bid_high  DOUBLE, yes_ask_low DOUBLE,
    volume        DOUBLE,
    open_interest DOUBLE
);
CREATE TABLE IF NOT EXISTS observations (
    station     VARCHAR NOT NULL,
    obs_date    DATE NOT NULL,
    high_f      INTEGER,
    PRIMARY KEY (station, obs_date)
);
CREATE TABLE IF NOT EXISTS series (
    venue          VARCHAR NOT NULL,
    ticker         VARCHAR NOT NULL,
    title          VARCHAR,
    category       VARCHAR,
    fee_type       VARCHAR,
    fee_multiplier DOUBLE,
    frequency      VARCHAR,
    updated_at     TIMESTAMP,
    PRIMARY KEY (venue, ticker)
);
CREATE TABLE IF NOT EXISTS sweep_log (
    series     VARCHAR NOT NULL,
    swept_at   TIMESTAMP NOT NULL,
    min_close  TIMESTAMP,
    max_close  TIMESTAMP,
    n_markets  INTEGER,
    n_candles  INTEGER,
    status     VARCHAR,
    note       VARCHAR
);
CREATE TABLE IF NOT EXISTS watermarks (
    series        VARCHAR NOT NULL PRIMARY KEY,
    last_close_ts TIMESTAMP
);
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);
"""

# Bump when adding a migration in hyxlab/migrate.py.
SCHEMA_VERSION = 1


def _naive_utc(dt: datetime | None) -> datetime | None:
    """Normalize to naive-UTC before insert.

    DuckDB's TIMESTAMP is naive and converts tz-aware values to the BOX's
    local time on insert — a silent, machine-dependent convention. All
    hyxlab timestamps are therefore stored as naive UTC explicitly; reads
    return naive UTC and all in-DB comparisons stay consistent.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class Store:
    def __init__(self, path: str | Path = "data/hyxlab.duckdb", read_only: bool = False) -> None:
        p = Path(path)
        fresh = not p.exists()
        if p.parent != Path(".") and not read_only:
            p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(p), read_only=read_only)
        if not read_only:
            self.conn.execute(_SCHEMA)
            if fresh:
                # Fresh DBs are born current; only pre-existing data migrates.
                self.conn.execute("INSERT INTO schema_meta VALUES (?)", [SCHEMA_VERSION])

    def schema_version(self) -> int:
        row = self.conn.execute("SELECT max(version) FROM schema_meta").fetchone()
        return row[0] if row and row[0] is not None else 0

    def set_schema_version(self, v: int) -> None:
        self.conn.execute("DELETE FROM schema_meta")
        self.conn.execute("INSERT INTO schema_meta VALUES (?)", [v])

    def insert_new(self, table: str, rows: list[tuple], key_cols: list[str]) -> int:
        """Anti-join insert: only rows whose natural key is absent. Idempotent
        re-runs of any backfill/sweep are safe (P5)."""
        if not rows:
            return 0
        self.conn.execute(f"CREATE OR REPLACE TEMP TABLE _staging AS SELECT * FROM {table} LIMIT 0")
        placeholders = ",".join("?" * len(rows[0]))
        self.conn.executemany(f"INSERT INTO _staging VALUES ({placeholders})", rows)
        on = " AND ".join(f"t.{k} IS NOT DISTINCT FROM s.{k}" for k in key_cols)
        before = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        self.conn.execute(
            f"INSERT INTO {table} SELECT s.* FROM _staging s"
            f" WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {on})"
        )
        after = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        self.conn.execute("DROP TABLE _staging")
        return after - before

    def close(self) -> None:
        self.conn.close()

    # -- writes ---------------------------------------------------------

    def upsert_markets(self, infos: list[MarketInfo]) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [
            (
                i.venue,
                i.market_id,
                i.title,
                i.series,
                _naive_utc(i.close_time),
                i.strike_type,
                i.floor_strike,
                i.cap_strike,
                i.result,
                i.target_date,
                now,
            )
            for i in infos
        ]
        self.conn.executemany("INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    def insert_snapshots(self, snaps: list[Snapshot]) -> None:
        rows = [
            (
                s.venue,
                s.market_id,
                _naive_utc(s.ts),
                s.yes_bid,
                s.yes_ask,
                s.no_bid,
                s.no_ask,
                s.yes_bid_size,
                s.yes_ask_size,
                s.no_bid_size,
                s.no_ask_size,
                s.last_price,
                s.volume,
                s.open_interest,
            )
            for s in snaps
        ]
        self.insert_new("snapshots", rows, ["venue", "market_id", "ts"])

    def upsert_series(self, rows: list[tuple]) -> None:
        """(venue, ticker, title, category, fee_type, fee_multiplier, frequency)."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.conn.executemany(
            "INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?)",
            [(*r, now) for r in rows],
        )

    def series_meta(self, venue: str = "kalshi") -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT ticker, title, category, fee_type, fee_multiplier, frequency"
            " FROM series WHERE venue = ?",
            [venue],
        ).fetchall()
        return {
            r[0]: {
                "title": r[1],
                "category": r[2],
                "fee_type": r[3],
                "fee_multiplier": r[4],
                "frequency": r[5],
            }
            for r in rows
        }

    def watermark(self, series: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT last_close_ts FROM watermarks WHERE series = ?", [series]
        ).fetchone()
        return row[0] if row else None

    def set_watermark(self, series: str, last_close_ts: datetime) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO watermarks VALUES (?, ?)",
            [series, _naive_utc(last_close_ts)],
        )

    def log_sweep(
        self,
        series: str,
        min_close,
        max_close,
        n_markets: int,
        n_candles: int,
        status: str,
        note: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO sweep_log VALUES (?,?,?,?,?,?,?,?)",
            [
                series,
                datetime.now(timezone.utc).replace(tzinfo=None),
                _naive_utc(min_close),
                _naive_utc(max_close),
                n_markets,
                n_candles,
                status,
                note,
            ],
        )

    def insert_forecasts(self, fcs: list[Forecast]) -> None:
        rows = [
            (f.station, _naive_utc(f.fetched_at), f.target_date, f.high_f, f.short) for f in fcs
        ]
        self.insert_new("nws_forecasts", rows, ["station", "fetched_at", "target_date"])

    def insert_candles(self, rows: list[tuple]) -> int:
        """Rows in candles-table column order (see _SCHEMA)."""
        rows = [(r[0], r[1], _naive_utc(r[2]), *r[3:]) for r in rows]
        return self.insert_new("candles", rows, ["venue", "market_id", "end_ts", "period_s"])

    def upsert_observations(self, rows: list[tuple[str, date, int | None]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO observations VALUES (?,?,?)", rows)

    # -- reads ----------------------------------------------------------

    def markets(self) -> dict[tuple[str, str], MarketInfo]:
        rows = self.conn.execute(
            "SELECT venue, market_id, title, series, close_time, strike_type,"
            " floor_strike, cap_strike, result, target_date FROM markets"
        ).fetchall()
        out: dict[tuple[str, str], MarketInfo] = {}
        for r in rows:
            info = MarketInfo(
                venue=r[0],
                market_id=r[1],
                title=r[2] or "",
                series=r[3] or "",
                close_time=r[4],
                strike_type=r[5] or "",
                floor_strike=r[6],
                cap_strike=r[7],
                result=r[8] or "",
                target_date=r[9],
            )
            out[(info.venue, info.market_id)] = info
        return out

    def iter_snapshots(self) -> list[Snapshot]:
        """All snapshots in replay order (ts, then venue/market for stability)."""
        rows = self.conn.execute(
            "SELECT venue, market_id, ts, yes_bid, yes_ask, no_bid, no_ask,"
            " yes_bid_size, yes_ask_size, no_bid_size, no_ask_size,"
            " last_price, volume, open_interest"
            " FROM snapshots ORDER BY ts, venue, market_id"
        ).fetchall()
        return [
            Snapshot(
                venue=r[0],
                market_id=r[1],
                ts=r[2],
                yes_bid=r[3],
                yes_ask=r[4],
                no_bid=r[5],
                no_ask=r[6],
                yes_bid_size=r[7] or 0.0,
                yes_ask_size=r[8] or 0.0,
                no_bid_size=r[9] or 0.0,
                no_ask_size=r[10] or 0.0,
                last_price=r[11],
                volume=r[12] or 0.0,
                open_interest=r[13] or 0.0,
            )
            for r in rows
        ]

    def forecasts(self) -> list[Forecast]:
        rows = self.conn.execute(
            "SELECT station, fetched_at, target_date, high_f, short"
            " FROM nws_forecasts ORDER BY fetched_at"
        ).fetchall()
        return [
            Forecast(station=r[0], fetched_at=r[1], target_date=r[2], high_f=r[3], short=r[4] or "")
            for r in rows
        ]

    def candles_as_snapshots(self) -> list[Snapshot]:
        """Synthesize Tier-1 snapshots from historical candle closes.

        NO-side quotes are the binary complement of the YES book
        (no_ask = 1 − yes_bid). Sizes are unknown at candle granularity, so
        they're set to +inf: the sim then caps fills at order qty and
        strategies must self-limit via max_qty — an optimistic fill model,
        which is why Tier 1 can kill strategies but not green-light them.
        """
        # Correctness gate (2026-07-07): candle bid/ask closes are sampled
        # at different sub-hour moments and can be crossed (bid > ask) or
        # carry empty-book sentinels (ask=1 with bid=0) — verified sums
        # ranged 0.11–2.00. Filling at such phantom quotes is fiction, so
        # they are excluded from replay.
        rows = self.conn.execute(
            "SELECT venue, market_id, end_ts, yes_bid_close, yes_ask_close,"
            " price_close, volume, open_interest FROM candles"
            " WHERE yes_bid_close IS NULL OR yes_ask_close IS NULL"
            "    OR (yes_bid_close <= yes_ask_close"
            "        AND NOT (yes_ask_close >= 0.999 AND yes_bid_close <= 0.001))"
            " ORDER BY end_ts, venue, market_id"
        ).fetchall()
        inf = float("inf")
        out: list[Snapshot] = []
        for r in rows:
            yes_bid, yes_ask = r[3], r[4]
            out.append(
                Snapshot(
                    venue=r[0],
                    market_id=r[1],
                    ts=r[2],
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    no_bid=None if yes_ask is None else 1.0 - yes_ask,
                    no_ask=None if yes_bid is None else 1.0 - yes_bid,
                    yes_bid_size=inf,
                    yes_ask_size=inf,
                    no_bid_size=inf,
                    no_ask_size=inf,
                    last_price=r[5],
                    volume=r[6] or 0.0,
                    open_interest=r[7] or 0.0,
                )
            )
        return out

    def observations(self) -> dict[tuple[str, date], int]:
        rows = self.conn.execute(
            "SELECT station, obs_date, high_f FROM observations WHERE high_f IS NOT NULL"
        ).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def counts(self) -> dict[str, int]:
        return {
            t: self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("markets", "snapshots", "nws_forecasts", "candles", "observations")
        }


def target_date_key(d: date) -> str:
    return d.isoformat()
