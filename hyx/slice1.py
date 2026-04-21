"""Slice 1 orchestrator — DE OHLCV + news + FinBERT sentiment.

Pulls daily bars and news for DE via Alpaca, scores headlines with FinBERT,
persists everything to DuckDB, and writes a daily MD/CSV report.

Incremental + idempotent: rerunning the same day is a no-op. First run backfills
5 years (OHLCV) / Alpaca's news history window (~2021+). CLI override:
    python -m hyx.slice1 --backfill-since 2024-01-01
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from hyx.alpaca_client import AlpacaDataClient, NewsRow, OhlcvRow
from hyx.audit import audit
from hyx.config import Config
from hyx.db import connection
from hyx.db.migrate import migrate
from hyx.report import TickerReport, write_report
from hyx.sentiment import MODEL_TAG, score_headlines

SLICE_NAME = "slice1"
TICKERS: tuple[str, ...] = ("DE",)
BACKFILL_YEARS = 5


# ---------------------------------------------------------------- fetch cursors


def _source_cursor(conn: duckdb.DuckDBPyConnection, source: str, ticker: str) -> datetime | None:
    row = conn.execute(
        "SELECT last_fetched_at FROM fetch_state WHERE source = ? AND ticker = ?",
        [source, ticker],
    ).fetchone()
    return row[0] if row else None


def _update_cursor(conn: duckdb.DuckDBPyConnection, source: str, ticker: str, ts: datetime) -> None:
    conn.execute(
        """
        INSERT INTO fetch_state (source, ticker, last_fetched_at) VALUES (?, ?, ?)
        ON CONFLICT (source, ticker) DO UPDATE SET last_fetched_at = excluded.last_fetched_at
        """,
        [source, ticker, ts],
    )


def _resolve_start(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    ticker: str,
    backfill_since: datetime | None,
    default_backfill: datetime,
) -> datetime:
    """Determine start timestamp for an incremental pull."""
    if backfill_since is not None:
        return backfill_since
    cursor = _source_cursor(conn, source, ticker)
    if cursor is None:
        return default_backfill
    return cursor


# --------------------------------------------------------------- persistence


def _persist_ohlcv(conn: duckdb.DuckDBPyConnection, rows: Iterable[OhlcvRow]) -> int:
    before = conn.execute("SELECT COUNT(*) FROM ohlcv_daily").fetchone()[0]
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO ohlcv_daily
                (ticker, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [r.ticker, r.date.date(), r.open, r.high, r.low, r.close, r.volume],
        )
    after = conn.execute("SELECT COUNT(*) FROM ohlcv_daily").fetchone()[0]
    return after - before


def _persist_news(conn: duckdb.DuckDBPyConnection, rows: Iterable[NewsRow]) -> tuple[int, int]:
    """Returns (articles_ingested, tag_rows_ingested)."""
    articles_before = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    tags_before = conn.execute("SELECT COUNT(*) FROM news_tickers").fetchone()[0]
    for r in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO news (news_id, published_at, headline, summary, url, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [r.news_id, r.published_at, r.headline, r.summary, r.url, r.source],
        )
        for tkr in r.symbols:
            conn.execute(
                "INSERT OR IGNORE INTO news_tickers (news_id, ticker) VALUES (?, ?)",
                [r.news_id, tkr],
            )
    articles_after = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    tags_after = conn.execute("SELECT COUNT(*) FROM news_tickers").fetchone()[0]
    return articles_after - articles_before, tags_after - tags_before


def _score_unscored(
    conn: duckdb.DuckDBPyConnection,
    tickers: tuple[str, ...],
    batch_size: int = 64,
) -> int:
    """Score headlines that don't yet have a row in news_sentiment for MODEL_TAG."""
    rows = conn.execute(
        """
        SELECT DISTINCT n.news_id, n.headline
        FROM news n
        JOIN news_tickers nt USING (news_id)
        WHERE nt.ticker = ANY(?)
          AND NOT EXISTS (
              SELECT 1 FROM news_sentiment s
              WHERE s.news_id = n.news_id AND s.model = ?
          )
        ORDER BY n.published_at
        """,
        [list(tickers), MODEL_TAG],
    ).fetchall()

    if not rows:
        return 0

    news_ids = [r[0] for r in rows]
    headlines = [r[1] for r in rows]
    scores = score_headlines(headlines, batch_size=batch_size)
    for news_id, s in zip(news_ids, scores, strict=True):
        conn.execute(
            """
            INSERT OR IGNORE INTO news_sentiment
                (news_id, model, label, score, score_pos, score_neg, score_neu)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [news_id, MODEL_TAG, s.label, s.score, s.pos, s.neg, s.neu],
        )
    return len(rows)


# ------------------------------------------------------------------- report


def _build_ticker_report(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    *,
    ohlcv_new: int,
    news_new: int,
) -> TickerReport:
    latest = conn.execute(
        "SELECT date, close FROM ohlcv_daily WHERE ticker = ? ORDER BY date DESC LIMIT 1",
        [ticker],
    ).fetchone()
    latest_date, latest_close = latest or (None, None)

    counts = conn.execute(
        """
        SELECT s.label, COUNT(*)
        FROM news_sentiment s
        JOIN news_tickers nt USING (news_id)
        WHERE nt.ticker = ? AND s.model = ?
        GROUP BY s.label
        """,
        [ticker, MODEL_TAG],
    ).fetchall()
    c = {label: n for label, n in counts}

    top = conn.execute(
        """
        SELECT s.label, n.headline
        FROM news n
        JOIN news_tickers nt USING (news_id)
        JOIN news_sentiment s USING (news_id)
        WHERE nt.ticker = ? AND s.model = ?
          AND s.label IN ('positive', 'negative')
        ORDER BY n.published_at DESC
        LIMIT 5
        """,
        [ticker, MODEL_TAG],
    ).fetchall()

    return TickerReport(
        ticker=ticker,
        ohlcv_rows_ingested=ohlcv_new,
        news_rows_ingested=news_new,
        latest_close=float(latest_close) if latest_close is not None else None,
        latest_bar_date=latest_date,
        sentiment_pos=c.get("positive", 0),
        sentiment_neg=c.get("negative", 0),
        sentiment_neu=c.get("neutral", 0),
        top_headlines=[(label, headline) for label, headline in top],
    )


# ------------------------------------------------------------------- entry


def run(backfill_since: datetime | None = None, db_path: Path | None = None) -> int:
    """Run slice 1 end-to-end. Returns process exit code."""
    cfg = Config.load(require_alpaca=True)
    db = db_path or cfg.db_path
    now = datetime.now(tz=UTC)
    default_backfill = now - timedelta(days=365 * BACKFILL_YEARS)

    with connection(db) as conn:
        applied = migrate(conn)
        audit(conn, slice=SLICE_NAME, level="info", event="migrate", payload={"applied": applied})

        client = AlpacaDataClient(cfg.alpaca_key, cfg.alpaca_secret)

        # --- OHLCV ---
        ohlcv_new = 0
        for tkr in TICKERS:
            start = _resolve_start(conn, "alpaca_ohlcv", tkr, backfill_since, default_backfill)
            audit(
                conn,
                slice=SLICE_NAME,
                level="info",
                event="ohlcv.fetch_start",
                payload={"ticker": tkr, "start": start.isoformat()},
            )
            rows = client.fetch_ohlcv_daily([tkr], start=start, end=now)
            ingested = _persist_ohlcv(conn, rows)
            ohlcv_new += ingested
            _update_cursor(conn, "alpaca_ohlcv", tkr, now)
            audit(
                conn,
                slice=SLICE_NAME,
                level="info",
                event="ohlcv.fetched",
                payload={"ticker": tkr, "rows_returned": len(rows), "rows_new": ingested},
            )

        # --- News ---
        news_new = 0
        for tkr in TICKERS:
            start = _resolve_start(conn, "alpaca_news", tkr, backfill_since, default_backfill)
            audit(
                conn,
                slice=SLICE_NAME,
                level="info",
                event="news.fetch_start",
                payload={"ticker": tkr, "start": start.isoformat()},
            )
            fetched = list(client.fetch_news([tkr], start=start, end=now))
            articles_new, tags_new = _persist_news(conn, fetched)
            news_new += articles_new
            _update_cursor(conn, "alpaca_news", tkr, now)
            audit(
                conn,
                slice=SLICE_NAME,
                level="info",
                event="news.fetched",
                payload={
                    "ticker": tkr,
                    "rows_returned": len(fetched),
                    "articles_new": articles_new,
                    "tag_rows_new": tags_new,
                },
            )

        # --- Sentiment ---
        scored = _score_unscored(conn, TICKERS)
        audit(
            conn,
            slice=SLICE_NAME,
            level="info",
            event="sentiment.scored",
            payload={"model": MODEL_TAG, "headlines_scored": scored},
        )

        # --- Report ---
        per_ticker = [
            _build_ticker_report(
                conn,
                tkr,
                ohlcv_new=ohlcv_new if tkr == TICKERS[0] else 0,
                news_new=news_new if tkr == TICKERS[0] else 0,
            )
            for tkr in TICKERS
        ]
        notes = [
            f"Backfill window start: {(backfill_since or default_backfill).date().isoformat()}",
            f"Sentiment model: {MODEL_TAG}",
            f"Headlines scored this run: {scored}",
        ]
        md_path, csv_path = write_report(cfg.reports_dir, 1, now.date(), per_ticker, notes=notes)
        audit(
            conn,
            slice=SLICE_NAME,
            level="info",
            event="report.written",
            payload={"md": str(md_path), "csv": str(csv_path)},
        )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="hyx.slice1")
    p.add_argument(
        "--backfill-since",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=None,
        help="ISO date (YYYY-MM-DD) to force a backfill start. Default: fetch_state or today-5y.",
    )
    args = p.parse_args()
    return run(backfill_since=args.backfill_since)


if __name__ == "__main__":
    raise SystemExit(main())
