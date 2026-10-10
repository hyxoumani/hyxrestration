"""A 429 mid-tape resumes at the refused page instead of discarding the tape.

Measured 2026-10-10: Kalshi's public bucket passes 9 back-to-back requests
and refuses the 10th, and `get_trades` paged a tape back-to-back, so EVERY
tape of 10+ pages (>=~9k prints) 429'd at the same page on every attempt and
every retry restarted it from page 0. Archived tapes of >=9k prints fell from
88-115/day (09-25..10-04) to 0-2/day (10-05..10-08) -- the most-traded
markets, lost for good once retention passes them.
"""

import pytest
import requests

from collector.venues import kalshi


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self.headers = {}
        self._body = body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self._body


class _Bucket:
    """A tape of `pages` pages behind the measured bucket: 9 back-to-back
    requests pass, the 10th is refused; any sleep refills it."""

    def __init__(self, pages):
        self.pages = pages
        self.burst = 0
        self.cursors = []

    def slept(self, _s):
        self.burst = 0

    def get(self, url, params=None, timeout=None):
        self.burst += 1
        if self.burst >= 10:
            return _Resp(429)
        page = int(params.get("cursor") or 0)
        self.cursors.append(page)
        nxt = str(page + 1) if page + 1 < self.pages else ""
        return _Resp(200, {"trades": [{"trade_id": f"t{page}"}], "cursor": nxt})


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(kalshi, "_log_429_headers", lambda *a: None)
    kalshi.reset_retry_counts()
    yield
    kalshi.reset_retry_counts()


def test_a_27_page_tape_completes_behind_the_measured_bucket(monkeypatch):
    sess = _Bucket(27)
    monkeypatch.setattr(kalshi.time, "sleep", sess.slept)
    rows, truncated = kalshi.get_trades("KXBTC15M-26OCT040345-45", session=sess)
    assert len(rows) == 27 and not truncated
    # resumed, never restarted: every page fetched exactly once
    assert sess.cursors == list(range(27))
    assert kalshi.retry_counts()["rate_limit"] == 2
    assert kalshi.rate_limit_unretried() == 0


def test_a_page_refused_through_the_whole_ladder_still_raises(monkeypatch):
    sleeps = []
    monkeypatch.setattr(kalshi.time, "sleep", sleeps.append)

    class _Always:
        def get(self, url, params=None, timeout=None):
            return _Resp(429)

    with pytest.raises(requests.HTTPError):
        kalshi.get_trades("M1", session=_Always())
    assert sleeps == list(kalshi.TAPE_PAGE_429_WAITS)
    assert kalshi.retry_counts()["rate_limit"] == len(kalshi.TAPE_PAGE_429_WAITS)
    assert kalshi.rate_limit_unretried() == 1


def test_the_ladder_resets_per_page(monkeypatch):
    """Each page gets the whole ladder: a long tape under a steady trickle of
    refusals must not exhaust one allowance shared by all its pages."""
    monkeypatch.setattr(kalshi.time, "sleep", lambda s: None)
    n = len(kalshi.TAPE_PAGE_429_WAITS)

    class _EveryPageOnce:
        def __init__(self):
            self.refused = set()

        def get(self, url, params=None, timeout=None):
            page = int(params.get("cursor") or 0)
            if page not in self.refused:
                self.refused.add(page)
                return _Resp(429)
            nxt = str(page + 1) if page + 1 < n + 3 else ""
            return _Resp(200, {"trades": [{"trade_id": f"t{page}"}], "cursor": nxt})

    rows, _ = kalshi.get_trades("M1", session=_EveryPageOnce())
    assert len(rows) == n + 3


def test_the_callers_pacing_reaches_every_page_but_the_first(monkeypatch):
    """An unpaced 27-page tape drains the bucket the 5-min collector shares:
    10-10, collect's fetch went 25-50s -> 80-170s while tradepass paged
    back-to-back. The pause goes BETWEEN pages, never before the first."""
    sleeps = []
    monkeypatch.setattr(kalshi.time, "sleep", sleeps.append)
    sess = _Bucket(5)
    sess.burst = -100  # no refusals: isolate the pacing
    rows, _ = kalshi.get_trades("M1", session=sess, page_pause_s=0.5)
    assert len(rows) == 5
    assert sleeps == [0.5] * 4
