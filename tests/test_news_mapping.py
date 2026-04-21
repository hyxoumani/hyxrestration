"""Alpaca News article -> NewsRow mapping tests. No network, no credentials."""

from __future__ import annotations

from alpaca.data.models.news import News

from hyx.news import NewsRow


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
    row = NewsRow.from_article(_article(headline=""))
    assert row.headline == ""
