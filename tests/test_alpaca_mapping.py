"""Mapping tests: Alpaca SDK models -> our persistence dataclasses.

These exercise code we actually wrote (`OhlcvRow.from_bar`, `NewsRow.from_article`)
rather than re-validating DuckDB constraints. No network, no credentials —
fixtures construct Bar / News instances directly from raw dicts.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alpaca.data.models.bars import Bar
from alpaca.data.models.news import News

from hyx.alpaca_client import NewsRow, OhlcvRow


def _bar(symbol: str = "DE", **overrides) -> Bar:
    raw = {
        "t": "2024-01-02T05:00:00Z",
        "o": 100.5,
        "h": 105.25,
        "l": 99.75,
        "c": 102.0,
        "v": 1_000_000,
        "n": 1234,
        "vw": 101.5,
    }
    raw.update(overrides)
    return Bar(symbol=symbol, raw_data=raw)


def _article(**overrides) -> News:
    raw = {
        "id": 12345,
        "headline": "Deere beats Q1 earnings estimates",
        "summary": "Record farm equipment demand",
        "url": "https://example.com/news/12345",
        "source": "benzinga",
        "author": "Jane Reporter",
        "created_at": "2024-01-02T05:00:00Z",
        "updated_at": "2024-01-02T05:00:00Z",
        "symbols": ["DE", "AGCO"],
        "content": "",
        "images": [],
    }
    raw.update(overrides)
    return News(raw_data=raw)


def test_ohlcv_row_preserves_bar_fields():
    row = OhlcvRow.from_bar(_bar())
    assert row.ticker == "DE"
    assert row.open == 100.5
    assert row.high == 105.25
    assert row.low == 99.75
    assert row.close == 102.0
    assert row.volume == 1_000_000


def test_ohlcv_row_timestamp_is_tz_aware_utc():
    row = OhlcvRow.from_bar(_bar())
    assert row.date.tzinfo is not None
    # Must be representable as UTC — slice 1 normalizes to a DATE column so we
    # care that `.date()` returns the correct calendar day.
    assert row.date.astimezone(UTC) == datetime(2024, 1, 2, 5, 0, tzinfo=UTC)
    assert row.date.date().isoformat() == "2024-01-02"


def test_ohlcv_row_coerces_float_volume_to_int():
    # Alpaca returns volume as a float (e.g. 1000000.0) per the SDK model.
    # Our schema expects BIGINT; from_bar() must coerce.
    row = OhlcvRow.from_bar(_bar(v=1_234_567.0))
    assert isinstance(row.volume, int)
    assert row.volume == 1_234_567


def test_news_row_preserves_article_fields():
    row = NewsRow.from_article(_article())
    assert row.news_id == "12345"  # int id stringified
    assert row.headline == "Deere beats Q1 earnings estimates"
    assert row.summary == "Record farm equipment demand"
    assert row.url == "https://example.com/news/12345"
    assert row.source == "benzinga"
    assert row.symbols == ("DE", "AGCO")  # list -> tuple (hashable / immutable)
    assert row.published_at.tzinfo is not None


def test_news_row_handles_empty_symbols():
    row = NewsRow.from_article(_article(symbols=[]))
    assert row.symbols == ()


def test_news_row_empty_headline_becomes_empty_string():
    # Alpaca occasionally returns null/absent headlines — we coerce to "" to
    # keep the NOT NULL headline column happy.
    row = NewsRow.from_article(_article(headline=""))
    assert row.headline == ""
