"""Tests for the 3x exponential-backoff retry helper.

time.sleep is monkeypatched to record the delay sequence without actually
sleeping — the tests run in milliseconds.
"""

from __future__ import annotations

import pytest

from hyx.alpaca_client import _RETRY_DELAYS, _with_retry


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """Replace time.sleep with a recorder. Returns the list sleeps land in."""
    calls: list[float] = []
    monkeypatch.setattr("hyx.alpaca_client.time.sleep", lambda s: calls.append(s))
    return calls


def test_succeeds_on_first_try_no_delay(recorded_sleeps):
    result = _with_retry(lambda: "ok", what="dummy")
    assert result == "ok"
    assert recorded_sleeps == []


def test_retries_after_transient_failure_then_succeeds(recorded_sleeps):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient 503")
        return "ok"

    result = _with_retry(flaky, what="dummy")
    assert result == "ok"
    assert calls["n"] == 3
    # Two retries ⇒ two delays: 1s, 2s
    assert recorded_sleeps == [_RETRY_DELAYS[0], _RETRY_DELAYS[1]]


def test_exhausts_all_retries_and_wraps_in_runtime_error(recorded_sleeps):
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise RuntimeError("server exploded")

    with pytest.raises(RuntimeError, match="dummy failed after 4 attempts"):
        _with_retry(always_fails, what="dummy")

    # 1 initial + 3 retries = 4 calls; delays are 1s, 2s, 4s in order.
    assert calls["n"] == 4
    assert recorded_sleeps == list(_RETRY_DELAYS)


def test_auth_error_raises_immediately_without_retry(recorded_sleeps):
    calls = {"n": 0}

    def unauthorized():
        calls["n"] += 1
        raise RuntimeError("401 Unauthorized — invalid api key")

    with pytest.raises(RuntimeError, match="Unauthorized"):
        _with_retry(unauthorized, what="dummy")

    assert calls["n"] == 1
    assert recorded_sleeps == []


def test_400_bad_request_skips_retry(recorded_sleeps):
    def bad_request():
        raise RuntimeError("400 Bad Request: malformed symbol")

    with pytest.raises(RuntimeError, match="Bad Request"):
        _with_retry(bad_request, what="dummy")

    assert recorded_sleeps == []


def test_forbidden_skips_retry(recorded_sleeps):
    def forbidden():
        raise RuntimeError("403 Forbidden")

    with pytest.raises(RuntimeError, match="Forbidden"):
        _with_retry(forbidden, what="dummy")

    assert recorded_sleeps == []
