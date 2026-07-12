"""Persistence idempotency tests for slice 1.

Uses synthetic OhlcvRow/NewsRow fixtures so the tests don't depend on the
network or on Alpaca credentials. FinBERT scoring is exercised separately.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb

from phase0.hyx.db.migrate import migrate
from phase0.hyx.news import NewsRow
from phase0.hyx.prices import OhlcvRow
from phase0.hyx.slice1 import (
    _persist_news,
    _persist_ohlcv,
    _source_cursor,
    _update_cursor,
)


def _fresh_db() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    migrate(conn)
    return conn


def _ohlcv(ticker: str, year: int, month: int, day: int) -> OhlcvRow:
    return OhlcvRow(
        ticker=ticker,
        date=date(year, month, day),
        open=100.0,
        high=105.0,
        low=99.0,
        close=102.0,
        adj_close=101.5,
        volume=1_000_000,
    )


def _article(news_id: str, tickers: tuple[str, ...], when: datetime) -> NewsRow:
    return NewsRow(
        news_id=news_id,
        published_at=when,
        headline=f"sample headline {news_id}",
        summary=None,
        url=None,
        source="test",
        symbols=tickers,
    )


def test_persist_ohlcv_insert_or_ignore():
    conn = _fresh_db()
    rows = [_ohlcv("DE", 2024, 1, 2), _ohlcv("DE", 2024, 1, 3)]
    assert _persist_ohlcv(conn, rows) == 2
    # Replay — all rows collide on PK
    assert _persist_ohlcv(conn, rows) == 0
    # Add one more
    assert _persist_ohlcv(conn, [_ohlcv("DE", 2024, 1, 4)]) == 1


def test_persist_ohlcv_stores_adj_close():
    conn = _fresh_db()
    _persist_ohlcv(conn, [_ohlcv("DE", 2024, 1, 2)])
    row = conn.execute("SELECT close, adj_close FROM ohlcv_daily WHERE ticker = 'DE'").fetchone()
    assert row == (102.0, 101.5)


def test_persist_news_dedupes_articles_and_tags():
    conn = _fresh_db()
    t = datetime(2024, 2, 1, tzinfo=UTC)
    a = _article("abc", ("DE", "AGCO"), t)
    articles_new, tags_new = _persist_news(conn, [a])
    assert (articles_new, tags_new) == (1, 2)

    # Replay: same article, same tags — all collide
    articles_new, tags_new = _persist_news(conn, [a])
    assert (articles_new, tags_new) == (0, 0)

    # Different article, one new tag shared with 'abc' — only the new article + its tags
    b = _article("xyz", ("DE",), t)
    articles_new, tags_new = _persist_news(conn, [b])
    assert (articles_new, tags_new) == (1, 1)


def test_fetch_cursor_roundtrip():
    conn = _fresh_db()
    assert _source_cursor(conn, "yfinance_ohlcv", "DE") is None
    ts = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    _update_cursor(conn, "yfinance_ohlcv", "DE", ts)
    got = _source_cursor(conn, "yfinance_ohlcv", "DE")
    assert got is not None
    assert got.year == 2024 and got.month == 3 and got.day == 1


def test_audit_log_autoincrement():
    from phase0.hyx.audit import audit

    conn = _fresh_db()
    audit(conn, slice="test", level="info", event="one", payload={"k": 1}, echo=False)
    audit(conn, slice="test", level="info", event="two", payload=None, echo=False)
    rows = conn.execute("SELECT id, event, payload FROM audit_log ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert rows[0][1] == "one"
    assert rows[1][2] is None
