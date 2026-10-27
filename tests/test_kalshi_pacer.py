"""The Kalshi request pacer (2026-10-23): a per-process floor between request
starts, applied at every `sess.get` in `collector.venues.kalshi`, off by
default and switched on by the collector at 8 req/s.

Measured before it: the collector's ~64-request walk at ~18 req/s drew 3-5
x 429 per cycle and spent ~85% of a 13.6-34.1s fetch in the ladder's 5s
back-off (status 2026-10-23 03:15Z/03:45Z)."""

from __future__ import annotations

import ast
import re

import pytest

from collector.venues import kalshi as k


@pytest.fixture(autouse=True)
def _pace_off():
    k.set_pace(0.0)
    k.reset_pace_stats()
    k._PACE_LAST_MONO = None
    yield
    k.set_pace(0.0)
    k.reset_pace_stats()
    k._PACE_LAST_MONO = None


class _Clock:
    """A monotonic clock that advances only when something sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(round(s, 6))
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr("time.monotonic", c.monotonic)
    monkeypatch.setattr("time.sleep", c.sleep)
    return c


class _Sess:
    def __init__(self, body=None):
        self.calls = 0
        self._body = body if body is not None else {"markets": [], "cursor": ""}

    def get(self, url, params=None, timeout=None):
        self.calls += 1

        class R:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            def json(inner):
                return self._body

        return R()


def test_pacing_is_off_by_default_and_sleeps_nothing(clock):
    """Every existing caller is byte-identical until it names a rate, the
    way `transport_budget=` makes a call site name its retry unit."""
    sess = _Sess()
    for _ in range(5):
        k.get_markets(series_ticker="KXHIGHNY", session=sess)
    assert sess.calls == 5
    assert clock.sleeps == []
    assert k.pace_stats() == {"interval_s": 0.0, "requests": 5, "slept_s": 0.0}


def test_back_to_back_requests_are_spaced_to_the_floor(clock):
    k.set_pace(0.125)
    sess = _Sess()
    for _ in range(4):
        k.get_markets(series_ticker="KXHIGHNY", session=sess)
    # The first request pays nothing; each later one waits out the floor.
    assert clock.sleeps == [0.125, 0.125, 0.125]
    assert k.pace_stats() == {"interval_s": 0.125, "requests": 4, "slept_s": 0.375}


def test_a_request_that_arrives_after_the_floor_does_not_wait(clock):
    k.set_pace(0.125)
    sess = _Sess()
    k.get_markets(series_ticker="KXHIGHNY", session=sess)
    clock.now += 1.0  # the caller did a second of its own work
    k.get_markets(series_ticker="KXHIGHNY", session=sess)
    assert clock.sleeps == []


def test_the_floor_reaches_every_get_in_the_module(clock):
    """The tape and /series call `sess.get` directly, bypassing the 429
    ladder (the `_RATE_LIMIT_UNRETRIED` story); the pacer must not share
    that blind spot, or the collector's floor is undone by the first tape
    page a sibling pulls in the same process."""
    k.set_pace(0.125)
    k.get_markets(series_ticker="KXHIGHNY", session=_Sess())
    k.get_trades("M1", session=_Sess({"trades": [], "cursor": ""}))
    k.get_series_list(session=_Sess({"series": []}))
    assert k.pace_stats()["requests"] == 3
    assert clock.sleeps == [0.125, 0.125]


def test_a_429_retry_is_paced_too(clock):
    """The ladder's own sleep counts toward the floor (the clock advanced),
    but a zero Retry-After still cannot fire the retry inside the floor."""
    k.set_pace(0.125)

    class R:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"Retry-After": "0"} if status == 429 else {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"markets": [], "cursor": ""}

    responses = [R(429), R(200)]

    class S:
        def get(self, url, params=None, timeout=None):
            return responses.pop(0)

    k.get_markets(series_ticker="KXHIGHNY", session=S())
    assert clock.sleeps == [0.0, 0.125]  # Retry-After 0, then the floor


def test_negative_interval_is_refused():
    with pytest.raises(ValueError):
        k.set_pace(-1.0)


def test_reset_clears_the_counters_but_not_the_interval(clock):
    k.set_pace(0.125)
    sess = _Sess()
    k.get_markets(series_ticker="KXHIGHNY", session=sess)
    k.get_markets(series_ticker="KXHIGHNY", session=sess)
    k.reset_pace_stats()
    assert k.pace_stats() == {"interval_s": 0.125, "requests": 0, "slept_s": 0.0}


def test_collector_switches_the_pacer_on_at_8_per_second_and_publishes_pace_s(
    tmp_path, monkeypatch, capsys
):
    """`collect --once` is the caller that names its rate; the cycle line
    carries what the floor cost beside `fetch_s`."""
    from tests.test_hyxlab_writer_lock import _LockProbe, _run_collect

    assert k.COLLECT_PACE_INTERVAL_S == 0.125
    probe = _LockProbe(str(tmp_path / "writer.lock"))
    _run_collect(tmp_path, monkeypatch, probe, fetch_delay=0.0)
    assert k.pace_stats()["interval_s"] == 0.125
    out = capsys.readouterr().out
    m = re.search(r"timings=(\{.*\})", out)
    assert m, out
    t = ast.literal_eval(m.group(1))
    assert "pace_s" in t and t["pace_s"] <= t["fetch_s"]


def test_collector_publishes_requests_and_429s_per_cycle_from_a_fresh_count(
    tmp_path, monkeypatch, capsys
):
    """The floor is set from the SINK's rate, which is 429s per request --
    and a 429 carries no rate-limit header (195k organic samples), so the
    cycle line must carry both counts, reset PER CYCLE: a counter that ran
    since process start would publish the process's lifetime under the
    cycle's name (#72)."""
    import collector.collect as collect
    from tests.test_hyxlab_writer_lock import _LockProbe, _run_collect

    # Stale state from before the cycle: must NOT leak into the line.
    k._RATE_LIMIT_RETRIES = 7
    k._PACE_REQUESTS = 11
    real_main = collect.main

    def main_with_paced_walk():
        fixture_get_markets = collect.kalshi.get_markets

        def paced_get_markets(**kw):
            k._pace()  # one paced request per call, as the real walk does
            if kw.get("status") == "open":
                k._RATE_LIMIT_RETRIES += 1  # one 429 retried on the open page
            return fixture_get_markets(**kw)

        monkeypatch.setattr(collect.kalshi, "get_markets", paced_get_markets)
        real_main()

    monkeypatch.setattr(collect, "main", main_with_paced_walk)
    probe = _LockProbe(str(tmp_path / "writer.lock"))
    _run_collect(tmp_path, monkeypatch, probe, fetch_delay=0.0)
    out = capsys.readouterr().out
    m = re.search(r"timings=(\{.*\})", out)
    assert m, out
    t = ast.literal_eval(m.group(1))
    # One series x (open + settled) = 2 paced requests; one 429; neither
    # carries the 11 / 7 that were on the counters before the cycle.
    assert t["kalshi_req"] == 2, t
    assert t["kalshi_429"] == 1, t
