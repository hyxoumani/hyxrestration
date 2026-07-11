"""Connector parsing against live-captured fixtures (tests/fixtures/).

These pin the data contracts: if a venue changes its response shape, the
fixture-based test localizes the break to one connector module.
"""

import json
from datetime import date
from pathlib import Path

from collector.venues.alfred import parse_vintage_csv, pessimistic_knowable_at
from collector.venues.alpaca_news import parse_news_payload
from collector.venues.kalshi import to_market_info, to_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


def test_alfred_vintage_csv_parses_point_in_time():
    text = (FIXTURES / "alfred_cpiaucsl_20240115.csv").read_text()
    rows = parse_vintage_csv(text, "CPIAUCSL", date(2024, 1, 15))
    assert len(rows) == 4
    last = rows[-1]
    # The 2024-01-15 vintage ends at December 2023 (released 2024-01-11):
    # the point-in-time property this whole source exists for.
    assert last.obs_date == date(2023, 12, 1)
    assert last.value == 308.850
    # knowable_at is pessimistic end-of-day ET on the vintage date.
    assert last.knowable_at == pessimistic_knowable_at(date(2024, 1, 15))
    assert last.knowable_at.date() >= date(2024, 1, 15)


def test_alfred_rejects_wrong_series_header():
    text = (FIXTURES / "alfred_cpiaucsl_20240115.csv").read_text()
    try:
        parse_vintage_csv(text, "ICSA", date(2024, 1, 15))
        raise AssertionError("expected ValueError on series mismatch")
    except ValueError:
        pass


def test_alpaca_news_payload_maps_to_news_items():
    payload = json.loads((FIXTURES / "alpaca_news.json").read_text())
    items = parse_news_payload(payload)
    assert len(items) == 2
    n = items[0]
    assert n.source == "alpaca"
    assert n.knowable_at == n.published_at  # wire timestamp is the honest one
    assert n.knowable_at.year == 2025
    assert n.title
    assert len(n.url_hash) == 16


def test_kalshi_series_fixture_has_sweep_fields():
    d = json.loads((FIXTURES / "kalshi_series.json").read_text())
    for s in d["series"]:
        # Fields the C8 sweep enumeration and fee model depend on.
        assert s["ticker"]
        assert s["category"] in ("Economics", "Climate and Weather")
        assert s["fee_type"] in ("quadratic", "quadratic_with_maker_fees")
        assert "fee_multiplier" in s


def test_kalshi_settled_market_fixture_parses():
    d = json.loads((FIXTURES / "kalshi_market_settled.json").read_text())
    m = d["markets"][0]
    info = to_market_info(m)
    assert info.result in ("yes", "no")  # settled fixture must carry a result
    assert info.series == "KXHIGHNY"
    assert info.target_date is not None
    snap = to_snapshot(m)
    assert snap.market_id == info.market_id


def test_kalshi_get_trades_reports_truncation():
    """A page-capped tape must surface truncated=True so callers never
    mark it 'ok' — retention gives no second chance at those prints."""
    from collector.venues import kalshi

    class _Sess:
        def get(self, url, params=None, timeout=None):
            class R:
                @staticmethod
                def raise_for_status():
                    pass

                @staticmethod
                def json():
                    return {"trades": [{"trade_id": "t"}], "cursor": "MORE"}

            return R()

    rows, truncated = kalshi.get_trades("M1", max_pages=2, session=_Sess())
    assert len(rows) == 2 and truncated

    class _End(_Sess):
        def get(self, url, params=None, timeout=None):
            class R:
                @staticmethod
                def raise_for_status():
                    pass

                @staticmethod
                def json():
                    return {"trades": [{"trade_id": "t"}], "cursor": ""}

            return R()

    rows, truncated = kalshi.get_trades("M1", max_pages=2, session=_End())
    assert len(rows) == 1 and not truncated
