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


def test_to_market_info_carries_open_time():
    m = {"ticker": "KXHIGHNY-26AUG02-B88.5", "event_ticker": "KXHIGHNY-26AUG02",
         "close_time": "2026-08-03T00:00:00Z", "open_time": "2026-06-30T14:00:00Z"}
    info = to_market_info(m)
    assert info.open_time is not None and info.open_time.hour == 14


def test_get_markets_page_loop_survives_a_429(monkeypatch):
    """The KXNASDAQ100U hole (sweep audit 2026-08-02): a mid-pagination 429
    must be retried after Retry-After, not escape to the caller."""
    from collector.venues import kalshi as k

    class _Resp:
        def __init__(self, status, body=None, retry_after=None):
            self.status_code = status
            self._body = body or {}
            self.headers = {"Retry-After": retry_after} if retry_after else {}

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError(response=self)

        def json(self):
            return self._body

    responses = [
        _Resp(429, retry_after="0"),
        _Resp(200, {"markets": [{"ticker": "A"}], "cursor": ""}),
    ]

    class _Sess:
        def get(self, url, params=None, timeout=None):
            return responses.pop(0)

    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    out = k.get_markets(series_ticker="KXNASDAQ100U", session=_Sess())
    assert [m["ticker"] for m in out] == ["A"]
    assert sleeps == [0.0]


# ---------------------------------------------------------------------------
# get_markets_ascending — EXP-931 (silent newest-only truncation)
# ---------------------------------------------------------------------------


class _WindowSess:
    """Serves settled markets DESCENDING within a close-time window, the
    way Kalshi actually does (probed live 2026-08-02) — which is precisely
    why a page-budget truncation kept only the newest rows."""

    def __init__(self, close_tss, page=3):
        self.universe = sorted(close_tss)
        self.page = page
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        lo, hi = params["min_close_ts"], params["max_close_ts"]
        rows = [t for t in self.universe if lo <= t <= hi]
        rows.sort(reverse=True)
        start = int(params.get("cursor") or 0)
        chunk = rows[start : start + self.page]
        nxt = start + self.page
        body = {
            "markets": [{"ticker": f"M-{t}", "close_ts": t} for t in chunk],
            "cursor": str(nxt) if nxt < len(rows) else "",
        }

        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return body

        return _R()


def test_get_markets_ascending_covers_the_whole_range():
    from collector.venues import kalshi as k

    tss = [1000 + 100 * i for i in range(20)]
    sess = _WindowSess(tss)
    out, truncated = k.get_markets_ascending(
        "KXX", min_close_ts=1000, max_close_ts=2900, window_s=500, session=sess
    )
    assert truncated is False
    assert sorted(m["close_ts"] for m in out) == tss


def test_windows_are_inclusive_and_never_overlap():
    """Both bounds are inclusive on Kalshi, so a window must end at hi-1 and
    the next must start at hi — otherwise every boundary market is fetched
    twice and its candles paid for twice."""
    from collector.venues import kalshi as k

    sess = _WindowSess([1000 + 100 * i for i in range(20)])
    out, _ = k.get_markets_ascending(
        "KXX", min_close_ts=1000, max_close_ts=2900, window_s=500, session=sess
    )
    assert len(out) == len({m["ticker"] for m in out}), "boundary double-fetch"
    starts = [c["min_close_ts"] for c in sess.calls]
    ends = [c["max_close_ts"] for c in sess.calls]
    for lo, hi in zip(starts, ends, strict=True):
        assert lo <= hi
    for prev_hi, nxt_lo in zip(sorted(set(ends))[:-1], sorted(set(starts))[1:], strict=True):
        assert nxt_lo == prev_hi + 1


def test_budget_truncation_keeps_the_OLDEST_prefix_not_the_newest():
    """THE regression. The old path (`get_markets(max_pages=50)`) exhausted
    its budget against a DESCENDING feed, so it kept the newest 10,000 rows
    and dropped every older one — while the caller still advanced its
    watermark to the newest close. Anything stopped early here must instead
    be a contiguous prefix from min_close_ts, so max(close) stays honest."""
    from collector.venues import kalshi as k

    tss = [1000 + 100 * i for i in range(20)]
    sess = _WindowSess(tss)
    out, truncated = k.get_markets_ascending(
        "KXX",
        min_close_ts=1000,
        max_close_ts=2900,
        window_s=500,
        max_markets=6,
        session=sess,
    )
    assert truncated is True
    got = sorted(m["close_ts"] for m in out)
    assert got == tss[: len(got)], "kept a non-prefix slice — watermark would lie"
    assert max(got) < max(tss)


def test_budget_reached_exactly_at_the_end_is_not_truncated():
    """Discrimination control: `truncated` must mean 'coverage is
    incomplete', not merely 'the budget number was touched'."""
    from collector.venues import kalshi as k

    tss = [1000 + 100 * i for i in range(20)]
    out, truncated = k.get_markets_ascending(
        "KXX",
        min_close_ts=1000,
        max_close_ts=2900,
        window_s=500,
        max_markets=20,
        session=_WindowSess(tss),
    )
    assert len(out) == 20
    assert truncated is False


def test_a_dense_window_narrows_instead_of_truncating():
    """Density on this exchange spans 0/day to ~6,900/day. A window too dense
    to drain must halve and be RE-fetched whole — never half-accepted, or the
    contiguous-prefix guarantee (and the watermark that rests on it) breaks."""
    from collector.venues import kalshi as k

    tss = list(range(1000, 1000 + 400))  # 400 markets packed into 400s
    sess = _WindowSess(tss, page=50)  # 5 pages x 50 = 250 < 400
    out, truncated = k.get_markets_ascending(
        "KXX", min_close_ts=1000, max_close_ts=1399, window_s=400, session=sess
    )
    assert truncated is False
    assert sorted(m["close_ts"] for m in out) == tss, "narrowing lost or duped rows"
    widths = {c["max_close_ts"] - c["min_close_ts"] + 1 for c in sess.calls}
    assert min(widths) < 400, "never narrowed — it would have had to truncate"


def test_a_sparse_series_costs_one_request_for_the_whole_range():
    """The fixed-step alternative cost 240 requests per DORMANT series (231 of
    281 crypto/exotics series are dormant); this client shares Kalshi's rate
    budget with a live trading loop, so that price is not payable."""
    from collector.venues import kalshi as k

    sess = _WindowSess([], page=1000)
    out, truncated = k.get_markets_ascending(
        "KXX", min_close_ts=0, max_close_ts=60 * 86400, session=sess
    )
    assert (out, truncated) == ([], False)
    assert len(sess.calls) <= 9, f"{len(sess.calls)} requests to clear a dormant series"


def test_candlesticks_retries_a_429_instead_of_losing_the_series(monkeypatch):
    """Measured 2026-08-03: a candlesticks 429 escaped sweep_series' single
    inline retry and aborted KXSHIBA entirely — logged status='error',
    0 markets kept, watermark unmoved. This endpoint must back off like the
    others rather than cost a whole series."""
    from collector.venues import kalshi as k

    class _Resp:
        def __init__(self, status, body=None):
            self.status_code = status
            self._body = body or {}
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests

                raise requests.HTTPError(response=self)

        def json(self):
            return self._body

    responses = [_Resp(429), _Resp(200, {"candlesticks": [{"end_period_ts": 1}]})]

    class _Sess:
        def get(self, url, params=None, timeout=None):
            assert "candlesticks" in url
            return responses.pop(0)

    monkeypatch.setattr("time.sleep", lambda s: None)
    out = k.get_candlesticks("KXSHIBA", "KXSHIBA-1", 0, 1, session=_Sess())
    assert out == [{"end_period_ts": 1}]
