"""Report writer tests + end-to-end synthetic build against in-memory DuckDB."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from hyx.alpaca_client import NewsRow, OhlcvRow
from hyx.db.migrate import migrate
from hyx.report import TickerReport, write_report
from hyx.sentiment import MODEL_TAG
from hyx.slice1 import _build_ticker_report, _persist_news, _persist_ohlcv


def test_write_report_produces_md_and_csv(tmp_path: Path):
    tickers = [
        TickerReport(
            ticker="DE",
            ohlcv_rows_ingested=12,
            news_rows_ingested=5,
            latest_close=412.34,
            latest_bar_date=date(2026, 4, 20),
            sentiment_pos=3,
            sentiment_neg=1,
            sentiment_neu=1,
            top_headlines=[
                ("positive", "Deere beats earnings"),
                ("negative", "Deere warns on guidance"),
            ],
        ),
    ]
    md, csv_path = write_report(tmp_path, 1, date(2026, 4, 21), tickers, notes=["hello"])
    assert md.exists() and csv_path.exists()
    md_text = md.read_text()
    assert "# Slice 1 — 2026-04-21" in md_text
    assert "DE" in md_text
    assert "Deere beats earnings" in md_text

    csv_text = csv_path.read_text()
    lines = [ln for ln in csv_text.strip().split("\n") if ln]
    assert lines[0].startswith("ticker,")
    assert "DE,12,5" in lines[1]


def test_build_ticker_report_aggregates_sentiment():
    conn = duckdb.connect(":memory:")
    migrate(conn)

    # Seed OHLCV
    _persist_ohlcv(
        conn,
        [
            OhlcvRow("DE", datetime(2026, 4, 20, tzinfo=UTC), 100.0, 101.0, 99.0, 100.5, 500_000),
        ],
    )

    # Seed two articles tagged DE
    t = datetime(2026, 4, 20, 14, 0, tzinfo=UTC)
    articles = [
        NewsRow("n1", t, "good news for Deere", None, None, "test", ("DE",)),
        NewsRow("n2", t, "bad news for Deere", None, None, "test", ("DE",)),
    ]
    _persist_news(conn, articles)

    # Hand-inject sentiment rows so we don't load FinBERT in this test
    conn.execute(
        "INSERT INTO news_sentiment (news_id, model, label, score, score_pos, score_neg, score_neu) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ["n1", MODEL_TAG, "positive", 0.95, 0.95, 0.02, 0.03],
    )
    conn.execute(
        "INSERT INTO news_sentiment (news_id, model, label, score, score_pos, score_neg, score_neu) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ["n2", MODEL_TAG, "negative", 0.88, 0.05, 0.88, 0.07],
    )

    report = _build_ticker_report(conn, "DE", ohlcv_new=1, news_new=2)
    assert report.ticker == "DE"
    assert report.latest_close == 100.5
    assert report.sentiment_pos == 1
    assert report.sentiment_neg == 1
    assert report.sentiment_neu == 0
    assert len(report.top_headlines) == 2
    labels = {label for label, _ in report.top_headlines}
    assert labels == {"positive", "negative"}
